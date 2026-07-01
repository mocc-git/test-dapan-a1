#!/usr/bin/env python3
"""GitHub Actions Playwright 首页饰品指数抓取脚本

在 GA runner 内运行，用 Playwright 抓取 CSQAQ 首页饰品指数 K 线和各类指标数据。
输出 home_result.json。

数据来源：
1. GET /proxies/api/v1/current_data — 所有指数当前值 + 武器类型涨跌 + 其他指标
2. GET /proxies/api/v1/sub_data?id={id}&type={type} — 指数 K 线历史（daily/hours，main_data 合成）
3. GET /proxies/api/v1/sub/kline?id={id}&type={type} — 真实 OHLCV 历史（1hour/4hour/1day/7day）
4. GET /api/v1/info/get_rank_list — 涨跌排行（36 条）
5. GET /api/v1/monitor/rank — 库存监控排行（196 条）

用法：
  python scrape_home.py
  python scrape_home.py --indices 1,2,7,8  # 只抓指定指数
  python scrape_home.py --periods 1hour,4hour,1day,7day  # 只抓指定周期
"""

import argparse
import json
import datetime
import urllib.parse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright

HOME_URL = "https://csqaq.com/home"
RESULT_FILE = "home_result.json"

# 默认抓取的指数 ID（从 current_data 的 sub_index_data 获取）
# id -> (name, name_key)
DEFAULT_INDEX_IDS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]

# 默认抓取的周期（sub_data 旧接口，main_data 合成）
DEFAULT_PERIODS = ["daily"]

# K 线真实 OHLCV 周期（sub/kline 新接口）
# 已移除 4hour，与 SteamDT 保持一致（SteamDT 无 4hour 周期）
DEFAULT_KLINE_PERIODS = ["1hour", "1day", "7day"]

# 指数名称映射（用于点击切换）
INDEX_NAME_MAP = {
    1: "饰品指数", 2: "租赁指数", 3: "百元主战", 4: "探员指数", 5: "原皮指数",
    6: "贴纸指数", 7: "匕首指数", 8: "手套指数", 9: "挂件指数", 10: "音乐盒",
    11: "2023巴黎", 12: "2024哥本", 14: "2024上海", 15: "武库指数", 16: "千战指数",
    17: "2025奥斯汀", 18: "一代手套", 19: "二代手套", 20: "三代手套",
    21: "收藏品", 22: "多普勒", 23: "伽玛多普勒", 24: "红皮指数",
}

# 周期切换按钮文本映射（sub_data 旧接口）
PERIOD_BUTTON_MAP = {
    "daily": "日线",
    "hours": "时线",
}

# K 线周期按钮文本映射（sub/kline 新接口，第二套按钮 .item.period）
KLINE_PERIOD_MAP = {
    "1hour": "1小时",
    "1day": "日线",
    "7day": "周线",
}


def _click_index_by_name(page, idx_name):
    """点击指数名称切换（匹配文本内容且可见的叶子元素）"""
    return page.evaluate(f"""() => {{
        const allElements = document.querySelectorAll('*');
        for (const el of allElements) {{
            if (el.textContent.trim() === '{idx_name}' && el.offsetParent !== null && el.children.length === 0) {{
                el.click();
                return true;
            }}
        }}
        return false;
    }}""")


def _click_kline_period(page, btn_text):
    """点击 K 线周期按钮（第二套按钮 .item.period）"""
    return page.evaluate(f"""() => {{
        // 优先匹配 .item.period
        let els = document.querySelectorAll('.item.period');
        for (const el of els) {{
            if (el.textContent.trim() === '{btn_text}' && el.offsetParent !== null) {{
                el.click();
                return true;
            }}
        }}
        // 回退：匹配任意可见元素
        const all = document.querySelectorAll('span, div, button');
        for (const el of all) {{
            if (el.textContent.trim() === '{btn_text}' && el.offsetParent !== null && el.children.length === 0) {{
                el.click();
                return true;
            }}
        }}
        return false;
    }}""")


def _click_kline_mode_button(page):
    """点击 .ant-segmented-item 的"K线"按钮，切换到 K 线模式"""
    return page.evaluate("""() => {
        const els = document.querySelectorAll('.ant-segmented-item');
        for (const el of els) {
            if (el.textContent.trim() === 'K线' && el.offsetParent !== null) {
                el.click();
                return true;
            }
        }
        return false;
    }""")


def _ensure_vol_indicator(page):
    """确保 VOL(成交量) 指标已打开

    点击"指标"按钮打开菜单，检查 VOL(成交量) 是否已选中，
    未选中则点击选中，确保 K 线图显示成交量数据。
    打开后切换其他指标时 VOL 状态会保持，只需在 K 线模式开始时调用一次。

    Returns:
        dict: {success: bool, action: str, was_checked: bool}
    """
    print(f"  [VOL] 确保 VOL(成交量) 指标已打开...", flush=True)

    # 1. 点击"指标"按钮打开菜单
    menu_opened = page.evaluate("""() => {
        const els = document.querySelectorAll('.item.tools');
        for (const el of els) {
            if (el.textContent.trim() === '指标') { el.click(); return true; }
        }
        return false;
    }""")
    if not menu_opened:
        print(f"  [VOL] ✗ 未找到'指标'按钮", flush=True)
        return {"success": False, "action": "menu_not_found", "was_checked": False}
    page.wait_for_timeout(1500)

    # 2. 检查并点击 VOL(成交量)
    result = page.evaluate("""() => {
        const els = document.querySelectorAll('.klinecharts-pro-checkbox');
        for (const el of els) {
            const label = el.querySelector('.label');
            if (label && label.textContent.includes('VOL')) {
                const isChecked = el.classList.contains('checked');
                if (!isChecked) {
                    el.click();
                    return {action: 'opened', wasChecked: false, label: label.textContent};
                }
                return {action: 'already_open', wasChecked: true, label: label.textContent};
            }
        }
        return {action: 'not_found', wasChecked: false};
    }""")
    print(f"  [VOL] 结果: {result}", flush=True)

    # 3. 关闭菜单
    page.evaluate("document.body.click()")
    page.wait_for_timeout(500)

    success = result.get("action") in ("opened", "already_open")
    return {"success": success, **result}


def load_last_result(filepath="last_home_result.json"):
    """加载上次采集结果，用于增量模式判断是否跳过dataZoom

    读取上次采集的 home_result.json，统计有完整历史（>=200条1day）的指数数量。
    如果存在完整历史的指数，返回完整数据供合并使用；否则返回None。
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        sub_kline = data.get("sub_kline", {})
        full_count = 0
        for idx_id_str, info in sub_kline.items():
            periods = info.get("periods", {})
            if len(periods.get("1day", [])) >= 200:
                full_count += 1
        if full_count > 0:
            print(f"  [增量] 加载上次结果: {full_count} 个指数有完整历史", flush=True)
            return data
        else:
            print(f"  [增量] 上次结果无完整历史，将执行全量dataZoom", flush=True)
            return None
    except Exception:
        print(f"  [增量] 无上次结果，将执行全量dataZoom", flush=True)
        return None


def _load_csqaq_full_kline(page, idx_id, period, api_data_ref, max_slides=6):
    """通过 dataZoom 向左滑动加载 csqaq 完整 K 线历史数据

    csqaq 使用 klinecharts-pro 库，通过调用 chart.getDataList() 获取数据。
    滑动策略：多次点击左侧/触发 dataZoom 向左滚动，触发懒加载更多历史数据，
    直到连续两次滑动后数据条数不再增长。

    Args:
        page: Playwright page
        idx_id: 指数ID
        period: 周期字符串
        api_data_ref: api_data dict引用（用于更新sub_kline）
        max_slides: 最大滑动次数
    """
    print(f"  [dataZoom] 加载 {idx_id} {period} 完整历史...", flush=True)
    key = (idx_id, period)

    prev_count = 0
    try:
        initial = api_data_ref.get("sub_kline", {}).get(key, [])
        prev_count = len(initial) if isinstance(initial, list) else 0
    except Exception:
        pass

    if prev_count == 0:
        print(f"  [dataZoom] ⚠ 初始数据为空，跳过滑动", flush=True)
        return prev_count

    chart_expr = """(window.__klineChart || (
        (() => {
            const charts = document.querySelectorAll('div');
            for (const d of charts) {
                if (d.__klinecharts__ || d._chart) return d.__klinecharts__ || d._chart;
            }
            return null;
        })()
    ))"""

    stable_rounds = 0
    for slide_idx in range(max_slides):
        try:
            page.evaluate(f"""{chart_expr} && {chart_expr}.scrollToDataIndex && {chart_expr}.scrollToDataIndex(0)""")
            page.wait_for_timeout(2000)

            current = api_data_ref.get("sub_kline", {}).get(key, [])
            current_count = len(current) if isinstance(current, list) else 0

            if current_count > prev_count:
                print(f"  [dataZoom] 第{slide_idx+1}次滑动: {prev_count} -> {current_count} 条", flush=True)
                prev_count = current_count
                stable_rounds = 0
            else:
                stable_rounds += 1
                if stable_rounds >= 2:
                    print(f"  [dataZoom] 数据不再增长，停止滑动（最终 {current_count} 条）", flush=True)
                    break
        except Exception as e:
            print(f"  [dataZoom] 滑动异常: {e}", flush=True)
            break

    page.wait_for_timeout(1000)
    return prev_count


def scrape_home(page, index_ids, periods, kline_periods=None, skip_steamdt=False):
    """抓取首页数据

    Args:
        page: Playwright page
        index_ids: 指数 ID 列表
        periods: sub_data 旧接口周期列表（如 ['daily', 'hours']）
        kline_periods: sub/kline 新接口周期列表（如 ['1hour', '4hour', '1day', '7day']），
                       None 表示不抓取真实 OHLCV
        skip_steamdt: D3模式专用，跳过SteamDT采集（由独立线程并行采集）
    """
    if kline_periods is None:
        kline_periods = []
    print(f"\n{'='*60}", flush=True)
    print(f"  抓取 CSQAQ 首页饰品指数数据", flush=True)
    print(f"  指数: {index_ids}", flush=True)
    print(f"  周期: {periods}", flush=True)
    print(f"{'='*60}", flush=True)

    result = {
        "scrape_time": datetime.datetime.now().isoformat(),
        "home_url": HOME_URL,
        "current_data": None,
        "sub_data": {},  # {index_id: {period: data}}
        "sub_kline": {},  # {index_id: {period: [{t,o,c,h,l,v}, ...]}}
        "steamdt_kline": {},  # SteamDT双源数据 {broad/block_id: {name, periods: {period: [{t,o,c,h,l,v,tur}]}}}
        "rank_list": None,  # 涨跌排行 36 条
        "monitor_rank": None,  # 库存监控排行 196 条
        "scrape_ok": False,
        "scrape_fail": "",
    }

    # 拦截 API 响应
    api_data = {
        "current_data": None,
        "sub_data": {},  # {(id, type): data}
        "sub_kline": {},  # {(id, type): [{t,o,c,h,l,v}, ...]}
        "rank_list": None,
        "monitor_rank": None,
    }

    def handle_response(response):
        url = response.url
        if "csqaq.com" not in url:
            return
        try:
            body = response.text()
            if not body or len(body) > 5000000:
                return

            # current_data 接口
            if "/proxies/api/v1/current_data" in url:
                parsed = json.loads(body)
                if parsed.get("code") == 200 and parsed.get("data"):
                    api_data["current_data"] = parsed["data"]
                    print(f"  [拦截] current_data: {len(body)} bytes", flush=True)
                return

            # sub_data 接口（旧，main_data 合成）
            if "/proxies/api/v1/sub_data" in url:
                parsed = json.loads(body)
                if parsed.get("code") == 200 and parsed.get("data"):
                    parsed_url = urllib.parse.urlparse(url)
                    params = urllib.parse.parse_qs(parsed_url.query)
                    idx_id = int(params.get("id", [0])[0])
                    idx_type = params.get("type", [""])[0]
                    if idx_id and idx_type:
                        api_data["sub_data"][(idx_id, idx_type)] = parsed["data"]
                        print(f"  [拦截] sub_data id={idx_id} type={idx_type}: {len(body)} bytes", flush=True)
                return

            # sub/kline 接口（新，真实 OHLCV）
            if "/proxies/api/v1/sub/kline" in url:
                parsed = json.loads(body)
                if parsed.get("code") == 200 and parsed.get("data"):
                    parsed_url = urllib.parse.urlparse(url)
                    params = urllib.parse.parse_qs(parsed_url.query)
                    idx_id = int(params.get("id", [0])[0])
                    idx_type = params.get("type", [""])[0]
                    if idx_id and idx_type:
                        api_data["sub_kline"][(idx_id, idx_type)] = parsed["data"]
                        cnt = len(parsed["data"])
                        print(f"  [拦截] sub/kline id={idx_id} type={idx_type}: {cnt} 条", flush=True)
                return

            # get_rank_list 接口（涨跌排行）
            if "/api/v1/info/get_rank_list" in url:
                parsed = json.loads(body)
                if parsed.get("code") == 200:
                    api_data["rank_list"] = parsed.get("data") or []
                    cnt = len(api_data["rank_list"]) if isinstance(api_data["rank_list"], list) else 0
                    print(f"  [拦截] get_rank_list: {cnt} 条", flush=True)
                return

            # monitor/rank 接口（库存监控排行）
            if "/api/v1/monitor/rank" in url:
                parsed = json.loads(body)
                if parsed.get("code") == 200:
                    api_data["monitor_rank"] = parsed.get("data") or []
                    cnt = len(api_data["monitor_rank"]) if isinstance(api_data["monitor_rank"], list) else 0
                    print(f"  [拦截] monitor/rank: {cnt} 条", flush=True)
                return
        except Exception as e:
            print(f"  [拦截异常] {type(e).__name__}: {e}", flush=True)

    page.on("response", handle_response)

    try:
        # 1. 访问首页
        print(f"\n[1] 访问首页...", flush=True)
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(8000)
        print(f"  首页加载完成", flush=True)

        # 2. 等待 current_data 加载
        print(f"\n[2] 等待 current_data 加载...", flush=True)
        if api_data["current_data"]:
            sub_count = len(api_data["current_data"].get("sub_index_data", []))
            chg_count = len(api_data["current_data"].get("chg_type_data", []))
            print(f"  ✓ current_data: {sub_count} 个指数, {chg_count} 个武器类型", flush=True)
        else:
            print(f"  ✗ current_data 未加载", flush=True)

        # 3. 抓取所有指数的 sub_data（旧接口，main_data 合成）
        print(f"\n[3] 抓取所有指数的 sub_data...", flush=True)
        for idx_id in index_ids:
            idx_name = INDEX_NAME_MAP.get(idx_id, f"id={idx_id}")
            for period in periods:
                # 检查是否已有该数据
                if (idx_id, period) in api_data["sub_data"]:
                    print(f"  ✓ {idx_name}({idx_id}) {period}: 已有数据", flush=True)
                    continue

                # 点击指数名称切换
                print(f"  点击 {idx_name}({idx_id})...", flush=True)
                if not _click_index_by_name(page, idx_name):
                    print(f"    ✗ 未找到 {idx_name} 按钮", flush=True)
                    continue
                page.wait_for_timeout(2000)

                # 切换周期（旧接口按钮）
                period_btn = PERIOD_BUTTON_MAP.get(period)
                if period_btn:
                    print(f"    切换周期到 {period_btn}...", flush=True)
                    page.evaluate(f"""() => {{
                        const els = document.querySelectorAll('.ant-segmented-item-label, span, div');
                        for (const el of els) {{
                            if (el.textContent.trim() === '{period_btn}' && el.offsetParent !== null) {{
                                el.click();
                                return true;
                            }}
                        }}
                        return false;
                    }}""")
                    page.wait_for_timeout(3000)

                # 检查是否获取到数据
                if (idx_id, period) in api_data["sub_data"]:
                    data = api_data["sub_data"][(idx_id, period)]
                    ts_count = len(data.get("timestamp", []))
                    print(f"    ✓ {idx_name} {period}: {ts_count} 条", flush=True)
                else:
                    print(f"    ✗ {idx_name} {period}: 未获取到数据", flush=True)

        # 4. 抓取真实 OHLCV（sub/kline 新接口）
        if kline_periods:
            print(f"\n[4] 抓取真实 OHLCV (sub/kline)...", flush=True)
            print(f"  K 线周期: {kline_periods}", flush=True)

            # 4.1 点击"K线"按钮切换到 K 线模式
            print(f"  [4.1] 点击 K线 模式按钮...", flush=True)
            if not _click_kline_mode_button(page):
                print(f"    ✗ 未找到 K线 模式按钮，跳过 sub/kline 抓取", flush=True)
            else:
                page.wait_for_timeout(5000)

                # 4.1.1 确保 VOL(成交量) 指标已打开
                # VOL 指标打开后切换指数时状态会保持，只需在 K 线模式开始时确认一次
                vol_result = _ensure_vol_indicator(page)
                if not vol_result["success"]:
                    print(f"    ⚠ VOL 指标确保失败: {vol_result['action']}，继续采集", flush=True)
                page.wait_for_timeout(2000)

                # 4.2 特殊处理：切换到百元主战，重置 K 线图选中状态
                # 根因：饰品指数是默认选中状态，主循环点击"饰品指数"名称时
                # 页面认为是当前选中，不重新加载K线图，导致后续周期按钮无效
                # 修复：只切换到百元主战（不切回饰品指数），让主循环从饰品指数开始时
                # 点击"饰品指数"名称会触发页面重新加载K线图（发送1hour请求）
                print(f"  [4.2] 特殊处理：切换到百元主战，重置 K 线图选中状态...", flush=True)
                if 3 in index_ids:
                    if not _click_index_by_name(page, "百元主战"):
                        print(f"    ⚠ 未找到 百元主战 按钮", flush=True)
                    # 轮询等待百元主战默认周期(1hour)API响应到达，最长10秒
                    for _w in range(20):
                        if (3, "1hour") in api_data["sub_kline"]:
                            print(f"  ✓ 百元主战 1hour 默认数据已到达（K线图已重置）", flush=True)
                            break
                        page.wait_for_timeout(500)
                    else:
                        print(f"  ⚠ 百元主战 1hour 默认数据等待超时(10s)", flush=True)
                else:
                    # 百元主战不在列表中，无法重置，直接继续
                    print(f"    ⚠ 百元主战不在指数列表中，跳过重置", flush=True)

                # 4.3 遍历所有指数 × 所有 K 线周期
                print(f"  [4.3] 遍历 {len(index_ids)} 指数 × {len(kline_periods)} 周期...", flush=True)
                for idx_id in index_ids:
                    idx_name = INDEX_NAME_MAP.get(idx_id, f"id={idx_id}")
                    # 切换指数名称：页面会自动重新加载K线图（发送1hour请求）
                    # 注意：必须从非选中状态切换才会触发，4.2已确保当前选中百元主战
                    print(f"    切换到 {idx_name}({idx_id})...", flush=True)
                    if not _click_index_by_name(page, idx_name):
                        print(f"      ✗ 未找到 {idx_name} 按钮", flush=True)
                        continue
                    # 固定等待3秒让页面切换完成（K线图重新加载需要时间）
                    page.wait_for_timeout(3000)

                    # 轮询等待1hour默认数据到达（最长8秒）
                    # 切换指数名称时页面会发送1hour请求，确认K线图已重新加载
                    if "1hour" in kline_periods and (idx_id, "1hour") not in api_data["sub_kline"]:
                        for _w in range(16):
                            if (idx_id, "1hour") in api_data["sub_kline"]:
                                cnt = len(api_data["sub_kline"][(idx_id, "1hour")])
                                print(f"      ✓ {idx_name} 1hour: {cnt} 条（K线图已加载）", flush=True)
                                break
                            page.wait_for_timeout(500)
                        else:
                            print(f"      ⚠ {idx_name} 1hour: 默认数据等待超时", flush=True)
                    elif "1hour" in kline_periods:
                        cnt = len(api_data["sub_kline"][(idx_id, "1hour")])
                        print(f"      ✓ {idx_name} 1hour: 已有数据 {cnt} 条", flush=True)

                    # 依次点击 4 个周期
                    for period in kline_periods:
                        btn_text = KLINE_PERIOD_MAP.get(period)
                        if not btn_text:
                            continue
                        # 检查是否已有该数据
                        if (idx_id, period) in api_data["sub_kline"]:
                            print(f"      ✓ {idx_name} {period}: 已有数据", flush=True)
                            continue

                        print(f"      点击周期 {btn_text}...", flush=True)
                        click_ok = _click_kline_period(page, btn_text)
                        if not click_ok:
                            print(f"      ✗ 未找到 {btn_text} 按钮", flush=True)
                            continue
                        # 轮询等待API响应，最长8秒
                        for _w in range(16):
                            if (idx_id, period) in api_data["sub_kline"]:
                                cnt = len(api_data["sub_kline"][(idx_id, period)])
                                print(f"      ✓ {idx_name} {period}: {cnt} 条", flush=True)
                                break
                            page.wait_for_timeout(500)
                        else:
                            print(f"      ✗ {idx_name} {period}: 等待超时未获取到数据", flush=True)

                # 4.4 dataZoom滑动加载日线完整数据（仅1day）
                # G1: 条件跳过dataZoom（读取上次结果判断）
                print(f"\n  [4.4] dataZoom滑动加载日线完整数据（G1增量模式）...", flush=True)
                target_indices_for_zoom = list(index_ids)
                last_result = load_last_result()
                last_sub_kline = (last_result or {}).get("sub_kline", {})
                g1_skipped = 0
                g1_executed = 0
                for idx_id in target_indices_for_zoom:
                    if idx_id not in index_ids:
                        continue
                    if "1day" not in kline_periods:
                        continue
                    idx_name = INDEX_NAME_MAP.get(idx_id, f"id={idx_id}")

                    # G1: 检查上次结果是否已有完整历史（>=200条）
                    last_idx_data = last_sub_kline.get(str(idx_id), {})
                    last_1day = (last_idx_data.get("periods", {})).get("1day", [])
                    if len(last_1day) >= 200:
                        # 合并上次历史数据与当前最新数据
                        current_1day = api_data["sub_kline"].get((idx_id, "1day"), [])
                        merged = {}
                        for item in last_1day:
                            t = item.get("t")
                            if t is not None:
                                merged[str(t)] = item
                        for item in current_1day:
                            t = item.get("t")
                            if t is not None:
                                merged[str(t)] = item
                        merged_list = sorted(merged.values(), key=lambda x: int(x.get("t", 0)))
                        api_data["sub_kline"][(idx_id, "1day")] = merged_list
                        print(f"    [增量] 跳过 {idx_name}({idx_id}) dataZoom（合并: 上次{len(last_1day)}条 + 当前{len(current_1day)}条 = {len(merged_list)}条）", flush=True)
                        g1_skipped += 1
                        continue

                    g1_executed += 1
                    if (idx_id, "1day") in api_data["sub_kline"]:
                        print(f"    切换到 {idx_name}({idx_id}) 日线...", flush=True)
                        if not _click_index_by_name(page, idx_name):
                            print(f"      ✗ 未找到 {idx_name}", flush=True)
                            continue
                        page.wait_for_timeout(2000)
                        _click_kline_period(page, KLINE_PERIOD_MAP["1day"])
                        page.wait_for_timeout(3000)
                        final_count = _load_csqaq_full_kline(page, idx_id, "1day", api_data)
                        print(f"      ✓ {idx_name} 1day 最终: {final_count} 条", flush=True)
                print(f"  [4.4] G1汇总: 跳过{g1_skipped}个, 执行{g1_executed}个", flush=True)

                # 4.5 汇总 sub/kline 抓取结果
                kline_success = sum(1 for k in api_data["sub_kline"].keys())
                kline_total = len(index_ids) * len(kline_periods)
                print(f"\n  [4.5] sub/kline 汇总: {kline_success}/{kline_total} 成功", flush=True)
        else:
            print(f"\n[4] 跳过 sub/kline 抓取（kline_periods 为空）", flush=True)

        # 5. 整理结果
        print(f"\n[5] 整理结果...", flush=True)
        result["current_data"] = api_data["current_data"]

        for idx_id in index_ids:
            idx_name = INDEX_NAME_MAP.get(idx_id, f"id={idx_id}")
            result["sub_data"][str(idx_id)] = {
                "name": idx_name,
                "id": idx_id,
                "periods": {},
            }
            for period in periods:
                if (idx_id, period) in api_data["sub_data"]:
                    result["sub_data"][str(idx_id)]["periods"][period] = api_data["sub_data"][(idx_id, period)]

        # 整理 sub_kline（真实 OHLCV）
        for idx_id in index_ids:
            idx_name = INDEX_NAME_MAP.get(idx_id, f"id={idx_id}")
            result["sub_kline"][str(idx_id)] = {
                "name": idx_name,
                "id": idx_id,
                "periods": {},
            }
            for period in kline_periods:
                if (idx_id, period) in api_data["sub_kline"]:
                    result["sub_kline"][str(idx_id)]["periods"][period] = api_data["sub_kline"][(idx_id, period)]

        # 整理 rank_list 和 monitor_rank
        result["rank_list"] = api_data["rank_list"]
        result["monitor_rank"] = api_data["monitor_rank"]

        # 6. 采集 SteamDT 双源数据（复用同一 page/context）
        # D3模式：skip_steamdt=True 时跳过，由独立线程并行采集
        if skip_steamdt:
            print(f"\n[6] 跳过 SteamDT 采集（D3模式：由独立线程并行采集）", flush=True)
            result["steamdt_kline"] = {}
        else:
            print(f"\n[6] 采集 SteamDT 大盘+热门板块数据...", flush=True)
            try:
                from scrape_steamdt import scrape_steamdt
                steamdt_result = scrape_steamdt(page)
                result["steamdt_kline"] = steamdt_result.get("indices", {})
                if steamdt_result.get("scrape_ok"):
                    broad = steamdt_result.get("broad", {})
                    blocks = steamdt_result.get("blocks", {})
                    bperiods = list(broad.get("periods", {}).keys()) if broad else []
                    print(f"  ✓ SteamDT 采集成功: 大盘周期{bperiods}, 板块{len(blocks)}个", flush=True)
                else:
                    print(f"  ⚠ SteamDT 采集失败: {steamdt_result.get('scrape_fail', 'unknown')}", flush=True)
            except Exception as e:
                print(f"  ⚠ SteamDT 采集异常: {type(e).__name__}: {e}", flush=True)
                result["steamdt_kline"] = {}

        # 标记成功
        if result["current_data"]:
            result["scrape_ok"] = True
        else:
            result["scrape_fail"] = "无 current_data"

    except Exception as e:
        result["scrape_fail"] = f"{type(e).__name__}: {e}"
        print(f"  [ERROR] {type(e).__name__}: {e}", flush=True)

    page.remove_listener("response", handle_response)
    return result


def run_csqaq_subset_thread(subset_index_ids, periods, kline_periods, group_id):
    """D1线程：独立Playwright实例运行CSQAQ子集采集（跳过SteamDT）

    Args:
        subset_index_ids: 该线程负责的指数ID子集
        periods: sub_data周期
        kline_periods: sub/kline周期
        group_id: 分组ID（用于日志标识）
    """
    print(f"\n[D1-G{group_id}] 启动子集线程, 指数={subset_index_ids}...", flush=True)
    thread_start = datetime.datetime.now()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            context = browser.new_context(
                viewport={"width": 1400, "height": 900},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="zh-CN",
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = context.new_page()

            max_retries = 1
            scrape_result = None
            for attempt in range(max_retries + 1):
                scrape_result = scrape_home(page, subset_index_ids, periods,
                                            kline_periods=kline_periods, skip_steamdt=True)
                if scrape_result.get("scrape_ok") or attempt == max_retries:
                    break
                print(f"\n[D1-G{group_id}] 第 {attempt+1} 次抓取失败，重试...", flush=True)
                page.goto("about:blank")
                page.wait_for_timeout(1000)

            browser.close()

        elapsed = (datetime.datetime.now() - thread_start).total_seconds()
        print(f"[D1-G{group_id}] 完成, 耗时 {elapsed:.0f}s", flush=True)
        return scrape_result
    except Exception as e:
        print(f"[D1-G{group_id}] FATAL: {type(e).__name__}: {e}", flush=True)
        return {"scrape_ok": False, "scrape_fail": f"FATAL: {type(e).__name__}: {e}"}


def run_steamdt_thread():
    """D1线程：独立Playwright实例运行SteamDT采集"""
    print(f"\n[D1-SteamDT] 启动独立线程...", flush=True)
    thread_start = datetime.datetime.now()
    try:
        from scrape_steamdt import scrape_steamdt
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            context = browser.new_context(
                viewport={"width": 1400, "height": 900},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="zh-CN",
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = context.new_page()

            steamdt_result = scrape_steamdt(page)

            browser.close()

        elapsed = (datetime.datetime.now() - thread_start).total_seconds()
        print(f"[D1-SteamDT] 完成, 耗时 {elapsed:.0f}s", flush=True)
        return steamdt_result
    except Exception as e:
        print(f"[D1-SteamDT] FATAL: {type(e).__name__}: {e}", flush=True)
        return {"scrape_ok": False, "scrape_fail": f"FATAL: {type(e).__name__}: {e}"}


def main():
    parser = argparse.ArgumentParser(description="CSQAQ 首页饰品指数抓取（D1 4context并行）")
    parser.add_argument("--indices", default="", help="逗号分隔的指数 ID（默认全部）")
    parser.add_argument("--periods", default="", help="逗号分隔的 sub_data 周期（默认 daily,hours）")
    parser.add_argument("--kline-periods", default="",
                        help="逗号分隔的 sub/kline 周期（默认 1hour,1day,7day；传 'none' 跳过）")
    args = parser.parse_args()

    print("=" * 60, flush=True)
    print("  CSQAQ 4context并行抓取（D1模式）", flush=True)
    print("=" * 60, flush=True)

    # 解析参数
    if args.indices:
        index_ids = [int(x.strip()) for x in args.indices.split(",") if x.strip()]
    else:
        index_ids = DEFAULT_INDEX_IDS

    if args.periods:
        periods = [x.strip() for x in args.periods.split(",") if x.strip()]
    else:
        periods = DEFAULT_PERIODS

    # K 线真实 OHLCV 周期
    if args.kline_periods == "none":
        kline_periods = []
    elif args.kline_periods:
        kline_periods = [x.strip() for x in args.kline_periods.split(",") if x.strip()]
    else:
        kline_periods = DEFAULT_KLINE_PERIODS

    # D1: 将指数分成4组（约6个/组）
    num_groups = 4
    chunk_size = (len(index_ids) + num_groups - 1) // num_groups
    groups = [index_ids[i*chunk_size:(i+1)*chunk_size] for i in range(num_groups)]
    for i, g in enumerate(groups):
        print(f"  G{i+1}: {g}", flush=True)

    print(f"  sub_data 周期: {periods}", flush=True)
    print(f"  sub/kline 周期: {kline_periods}", flush=True)
    print(f"  模式: D1 4context并行（{num_groups}组CSQAQ + 1个SteamDT = {num_groups+1}线程）", flush=True)

    start_time = datetime.datetime.now()

    result = {
        "version": "v2_d1_parallel",
        "start_time": start_time.isoformat(),
        "home_url": HOME_URL,
        "indices": index_ids,
        "periods": periods,
        "kline_periods": kline_periods,
        "data": None,
    }

    # D1: 5线程并行采集（4个CSQAQ子集 + 1个SteamDT）
    try:
        with ThreadPoolExecutor(max_workers=num_groups + 1) as executor:
            future_groups = {}
            for i, group in enumerate(groups):
                if group:
                    future_groups[i+1] = executor.submit(
                        run_csqaq_subset_thread, group, periods, kline_periods, i+1)
            future_steamdt = executor.submit(run_steamdt_thread)

            group_results = {}
            for gid, future in future_groups.items():
                group_results[gid] = future.result()
            steamdt_result = future_steamdt.result()

        # 合并4个CSQAQ子集结果
        merged = {
            "current_data": None,
            "sub_data": {},
            "sub_kline": {},
            "rank_list": None,
            "monitor_rank": None,
            "steamdt_kline": {},
            "scrape_ok": False,
            "scrape_fail": "",
        }
        ok_count = 0
        fail_msgs = []
        for gid, gres in group_results.items():
            if not gres:
                fail_msgs.append(f"G{gid}:无结果")
                continue
            if gres.get("scrape_ok"):
                ok_count += 1
                if not merged["current_data"]:
                    merged["current_data"] = gres.get("current_data")
                if not merged["rank_list"]:
                    merged["rank_list"] = gres.get("rank_list")
                if not merged["monitor_rank"]:
                    merged["monitor_rank"] = gres.get("monitor_rank")
                g_sub_data = gres.get("sub_data", {})
                g_sub_kline = gres.get("sub_kline", {})
                merged["sub_data"].update(g_sub_data)
                merged["sub_kline"].update(g_sub_kline)
            else:
                fail_msgs.append(f"G{gid}:{gres.get('scrape_fail', 'unknown')}")

        merged["scrape_ok"] = ok_count > 0
        merged["scrape_fail"] = "; ".join(fail_msgs) if fail_msgs else ""

        # 合并SteamDT结果
        if steamdt_result.get("scrape_ok"):
            merged["steamdt_kline"] = steamdt_result.get("indices", {})
            print(f"\n[D1] SteamDT 结果已合并", flush=True)
        else:
            print(f"\n[D1] ⚠ SteamDT 采集失败: {steamdt_result.get('scrape_fail', 'unknown')}", flush=True)

        print(f"[D1] CSQAQ子集成功: {ok_count}/{len(group_results)}", flush=True)

        result["data"] = merged

    except Exception as e:
        print(f"\n[FATAL] {type(e).__name__}: {e}", flush=True)
        result["data"] = {
            "scrape_ok": False,
            "scrape_fail": f"FATAL: {type(e).__name__}: {e}",
        }

    end_time = datetime.datetime.now()
    result["end_time"] = end_time.isoformat()
    result["total_duration_seconds"] = (end_time - start_time).total_seconds()

    # 保存结果
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)

    # 汇总
    print(f"\n{'='*60}", flush=True)
    print(f"  汇总", flush=True)
    print(f"{'='*60}", flush=True)
    if result["data"]:
        ok = "✓" if result["data"].get("scrape_ok") else "✗"
        print(f"  状态: {ok} {result['data'].get('scrape_fail', '')}", flush=True)
        if result["data"].get("current_data"):
            cd = result["data"]["current_data"]
            print(f"  current_data: {len(cd.get('sub_index_data', []))} 个指数, "
                  f"{len(cd.get('chg_type_data', []))} 个武器类型", flush=True)
        sub_data = result["data"].get("sub_data", {})
        total_kline = 0
        for idx_id, info in sub_data.items():
            for period, data in info.get("periods", {}).items():
                ts_count = len(data.get("timestamp", []))
                total_kline += ts_count
                print(f"  sub_data id={idx_id}({info['name']}) {period}: {ts_count} 条", flush=True)
        print(f"  sub_data K线总条数: {total_kline}", flush=True)

        # sub_kline 汇总
        sub_kline = result["data"].get("sub_kline", {})
        total_kline_ohlc = 0
        success_count = 0
        fail_count = 0
        for idx_id, info in sub_kline.items():
            for period, kline_data in info.get("periods", {}).items():
                cnt = len(kline_data) if isinstance(kline_data, list) else 0
                total_kline_ohlc += cnt
                success_count += 1
                print(f"  sub_kline id={idx_id}({info['name']}) {period}: {cnt} 条", flush=True)
        expected = len(index_ids) * len(kline_periods)
        fail_count = max(0, expected - success_count)
        print(f"  sub_kline 真实 OHLCV 总条数: {total_kline_ohlc}", flush=True)
        print(f"  sub_kline 成功率: {success_count}/{expected}（失败 {fail_count}）", flush=True)

        # rank_list 和 monitor_rank 汇总
        rank_list = result["data"].get("rank_list") or []
        monitor_rank = result["data"].get("monitor_rank") or []
        print(f"  rank_list: {len(rank_list)} 条", flush=True)
        print(f"  monitor_rank: {len(monitor_rank)} 条", flush=True)

        # steamdt_kline 汇总
        steamdt = result["data"].get("steamdt_kline", {})
        if steamdt:
            steamdt_total = 0
            for sid, sinfo in steamdt.items():
                for pk, pdata in sinfo.get("periods", {}).items():
                    cnt = len(pdata) if isinstance(pdata, list) else 0
                    steamdt_total += cnt
            broad = steamdt.get("broad", {})
            bperiods = list(broad.get("periods", {}).keys())
            print(f"  steamdt_kline: 大盘周期{bperiods}, 板块{len(steamdt)-1}个, 总{steamdt_total}条", flush=True)
        else:
            print(f"  steamdt_kline: 无数据", flush=True)
    print(f"  耗时: {result['total_duration_seconds']:.0f}s", flush=True)
    print(f"  结果: {RESULT_FILE}", flush=True)


if __name__ == "__main__":
    main()
