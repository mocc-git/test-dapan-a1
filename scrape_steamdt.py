#!/usr/bin/env python3
"""SteamDT 大盘+热门板块 K线采集（含 VOL 真实成交量 + TUR 真实成交额）

由 scrape_home.py 在 csqaq 采集完成后调用，复用同一 Playwright page/context。
输出格式与 csqaq sub_kline 兼容，便于前端统一处理。
"""
import json
import time
import urllib.parse

STEAMDT_URL = "https://www.steamdt.com"

KLINE_TYPE_MAP = {"1hour": 1, "1day": 2, "7day": 3}
PERIOD_BTNS = {"1day": "日K", "1hour": "时K", "7day": "周K"}

STEAMDT_BROAD_NAME = "大盘指数"
STEAMDT_BLOCKS_NAME = "热门板块"


def scrape_steamdt(page):
    """采集SteamDT大盘+热门板块K线数据（复用外部传入的page）

    Args:
        page: Playwright page对象（由scrape_home.py创建）

    Returns:
        dict: {
            "scrape_ok": bool,
            "scrape_fail": str,
            "broad": {"name": str, "periods": {period: [{t,o,c,h,l,v,tur}, ...]}},
            "blocks": {block_id: {"id": str, "name": str, "periods": {period: [...]}}},
            "indices": {combined index dict for frontend compatibility}
        }
    """
    print(f"\n{'='*60}", flush=True)
    print(f"  抓取 SteamDT 大盘+热门板块数据", flush=True)
    print(f"{'='*60}", flush=True)

    result = {
        "scrape_ok": False,
        "scrape_fail": "",
        "broad": None,
        "blocks": {},
        "indices": {},
    }

    kline_responses = []
    block_list = []

    def handle_response(response):
        url = response.url
        try:
            if "steamdt.com" not in url:
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct and "text" not in ct:
                return
            if response.status != 200:
                return

            if "/api/user/item/block/v1/relation" in url:
                body = response.text()
                parsed = json.loads(body)
                if parsed.get("success") and parsed.get("data"):
                    nonlocal block_list
                    if not block_list:
                        data = parsed["data"]
                        if isinstance(data, dict):
                            relation_list = data.get("relation", []) or []
                            next_level_list = data.get("next_level", []) or []
                            for b in relation_list:
                                bid = str(b.get("typeVal") or b.get("id") or b.get("blockId") or "")
                                bname = b.get("name") or b.get("nameZh") or b.get("blockName") or ""
                                btype = b.get("type") or "HOT"
                                if bid and bname:
                                    block_list.append({
                                        "id": bid, "name": bname, "blockType": btype,
                                        "trendList": b.get("trendList") or [],
                                        "index": b.get("index"),
                                        "riseFallRate": b.get("riseFallRate"),
                                        "riseFallDiff": b.get("riseFallDiff"),
                                    })
                            for b in next_level_list:
                                bid = str(b.get("typeVal") or b.get("id") or b.get("blockId") or "")
                                bname = b.get("name") or b.get("nameZh") or b.get("blockName") or ""
                                btype = b.get("type") or "ITEM_TYPE"
                                if bid and bname:
                                    block_list.append({
                                        "id": bid, "name": bname, "blockType": btype,
                                        "trendList": b.get("trendList") or [],
                                        "index": b.get("index"),
                                        "riseFallRate": b.get("riseFallRate"),
                                        "riseFallDiff": b.get("riseFallDiff"),
                                    })
                        elif isinstance(data, list):
                            for b in data:
                                bid = str(b.get("typeVal") or b.get("id") or b.get("blockId") or "")
                                bname = b.get("name") or b.get("nameZh") or b.get("blockName") or ""
                                btype = b.get("type") or "HOT"
                                if bid and bname:
                                    block_list.append({
                                        "id": bid, "name": bname, "blockType": btype,
                                        "trendList": b.get("trendList") or [],
                                        "index": b.get("index"),
                                        "riseFallRate": b.get("riseFallRate"),
                                        "riseFallDiff": b.get("riseFallDiff"),
                                    })
                        print(f"  [拦截] block/relation: {len(block_list)} 个板块", flush=True)

            if "/api/user/statistics/v1/kline" in url or "/api/user/item/block/v1/kline" in url:
                body = response.text()
                parsed = json.loads(body)
                if parsed.get("success") and parsed.get("data") and isinstance(parsed["data"], list):
                    ktype = None
                    if "/api/user/item/block/v1/kline" in url:
                        try:
                            pd = json.loads(response.request.post_data or "{}")
                            ktype = pd.get("klineType")
                        except Exception:
                            pass
                    else:
                        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                        ktype = int(params.get("type", [0])[0])
                    kline_responses.append({"ktype": ktype, "data": parsed["data"]})
                    print(f"  [拦截] kline ktype={ktype}: {len(parsed['data'])} 条", flush=True)
        except Exception as e:
            pass

    page.on("response", handle_response)

    def _dismiss_cookie():
        try:
            page.evaluate("""() => {
                const btns = document.querySelectorAll('button, div');
                for (const btn of btns) {
                    if (btn.textContent.trim() === '同意' && btn.offsetParent !== null) { btn.click(); return true; }
                }
                return false;
            }""")
        except Exception:
            pass

    def _click_kline_tab():
        return page.evaluate("""() => {
            const tabs = document.querySelectorAll('.el-tabs__item');
            for (const tab of tabs) {
                if (tab.textContent.trim() === 'K线图' && tab.offsetParent !== null) { tab.click(); return true; }
            }
            return false;
        }""")

    def _click_period(btn_text):
        return page.evaluate(f"""() => {{
            const items = document.querySelectorAll('.item.period');
            for (const el of items) {{
                if (el.textContent.trim() === '{btn_text}' && el.offsetParent !== null) {{ el.click(); return true; }}
            }}
            return false;
        }}""")

    def _wait_for_ktype(ktype, timeout=10000):
        end = time.time() + timeout / 1000
        while time.time() < end:
            for kr in kline_responses:
                if kr["ktype"] == ktype and len(kr["data"]) > 5:
                    return kr["data"]
            page.wait_for_timeout(500)
        return None

    def _convert(data_list):
        out = []
        for item in data_list:
            if not isinstance(item, (list, tuple)) or len(item) < 7:
                continue
            try:
                ts = int(item[0])
                if ts < 1e12:
                    ts *= 1000
                out.append({
                    "t": ts,
                    "o": float(item[1]),
                    "c": float(item[2]),
                    "h": float(item[3]),
                    "l": float(item[4]),
                    "v": float(item[5]) if item[5] else 0,
                    "tur": float(item[6]) if item[6] else 0,
                })
            except Exception:
                continue
        return out

    def _get_chart_expr():
        return """(window.__klineChart || (
            (() => {
                const charts = document.querySelectorAll('div');
                for (const d of charts) {
                    if (d.__klinecharts__ || d._chart) return d.__klinecharts__ || d._chart;
                }
                return null;
            })()
        ))"""

    def _merge_kline_list(existing, new_data):
        merged = {}
        for item in existing:
            t = item.get("t")
            if t is not None:
                merged[str(t)] = item
        for item in new_data:
            t = item.get("t")
            if t is not None:
                merged[str(t)] = item
        result = list(merged.values())
        result.sort(key=lambda x: int(x.get("t", 0)))
        return result

    def _load_steamdt_full_kline(ktype, max_slides=8, init_wait=10000):
        print(f"  [dataZoom] SteamDT 加载 ktype={ktype} 完整历史...", flush=True)
        end_wait = time.time() + init_wait / 1000
        existing = []
        while time.time() < end_wait:
            for kr in kline_responses:
                if kr["ktype"] == ktype and len(kr["data"]) > 5:
                    existing = _convert(kr["data"])
                    break
            if existing:
                break
            page.wait_for_timeout(500)
        prev_count = len(existing)
        if prev_count == 0:
            print(f"  [dataZoom] ⚠ 初始数据为空，跳过滑动", flush=True)
            return existing

        chart_expr = _get_chart_expr()
        stable_rounds = 0
        for slide_idx in range(max_slides):
            try:
                page.evaluate(f"""{chart_expr} && {chart_expr}.scrollToDataIndex && {chart_expr}.scrollToDataIndex(0)""")
                page.wait_for_timeout(1800)

                new_data = None
                for kr in reversed(kline_responses):
                    if kr["ktype"] == ktype and len(kr["data"]) > 5:
                        new_data = _convert(kr["data"])
                        break

                if new_data and len(new_data) > prev_count:
                    print(f"  [dataZoom] 第{slide_idx+1}次滑动: {prev_count} -> {len(new_data)} 条", flush=True)
                    existing = _merge_kline_list(existing, new_data)
                    prev_count = len(existing)
                    stable_rounds = 0
                else:
                    stable_rounds += 1
                    if stable_rounds >= 1:
                        print(f"  [dataZoom] 数据不再增长，停止滑动（最终 {prev_count} 条）", flush=True)
                        break
            except Exception as e:
                print(f"  [dataZoom] 滑动异常: {e}", flush=True)
                break

        page.wait_for_timeout(1000)
        return existing

    try:
        print(f"\n[1] 访问大盘BROAD页面...", flush=True)
        page.goto(f"{STEAMDT_URL}/section?type=BROAD", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        _dismiss_cookie()
        page.wait_for_timeout(500)

        if not _click_kline_tab():
            print(f"  ⚠ 未找到K线图标签，尝试继续", flush=True)
        page.wait_for_timeout(3000)

        broad_periods = {}
        for pk in ["1day", "1hour", "7day"]:
            ktype = KLINE_TYPE_MAP[pk]
            if pk != "1day":
                kline_responses.clear()
                if not _click_period(PERIOD_BTNS[pk]):
                    print(f"  ⚠ 未找到 {PERIOD_BTNS[pk]} 按钮", flush=True)
                    continue
                page.wait_for_timeout(2000)
            else:
                page.wait_for_timeout(2000)
                has_1day = any(kr["ktype"] == ktype and len(kr["data"]) > 5 for kr in kline_responses)
                if not has_1day:
                    kline_responses.clear()
                    if _click_period(PERIOD_BTNS[pk]):
                        page.wait_for_timeout(3000)
                    else:
                        print(f"  ⚠ 未找到 {PERIOD_BTNS[pk]} 按钮，等待初始数据", flush=True)
            data = _wait_for_ktype(ktype, 10000)
            if data:
                converted = _convert(data)
                broad_periods[pk] = converted
                nz_v = sum(1 for d in converted if d["v"] > 0)
                nz_tur = sum(1 for d in converted if d["tur"] > 0)
                print(f"  ✓ 大盘 {pk}: {len(converted)} 条 vol非零{nz_v} tur非零{nz_tur}", flush=True)
            else:
                print(f"  ✗ 大盘 {pk}: 未获取到数据", flush=True)

        for pk in ["1day", "1hour", "7day"]:
            if pk in broad_periods:
                max_slides = 6 if pk == "1day" else 4
                print(f"\n  [1.5] dataZoom滑动加载大盘 {pk} 完整数据...", flush=True)
                kline_responses.clear()
                if _click_period(PERIOD_BTNS[pk]):
                    page.wait_for_timeout(3000)
                else:
                    page.wait_for_timeout(2000)
                full_data = _load_steamdt_full_kline(KLINE_TYPE_MAP[pk], max_slides=max_slides)
                if len(full_data) > len(broad_periods.get(pk, [])):
                    broad_periods[pk] = full_data
                    print(f"  ✓ 大盘 {pk} 最终: {len(full_data)} 条", flush=True)

        result["broad"] = {"name": STEAMDT_BROAD_NAME, "id": "broad", "periods": broad_periods}

        print(f"\n[2] 采集 {len(block_list)} 个板块...", flush=True)
        for i, blk in enumerate(block_list):
            bid, bname, btype = blk["id"], blk["name"], blk.get("blockType", "HOT")
            print(f"  [{i+1}/{len(block_list)}] {bname}({bid}, type={btype})...", flush=True)
            try:
                kline_responses.clear()
                page.goto(f"{STEAMDT_URL}/section?type={btype}&typeVal={bid}", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)
                _dismiss_cookie()
                page.wait_for_timeout(500)

                if not _click_kline_tab():
                    print(f"    ⚠ 未找到K线图标签", flush=True)
                page.wait_for_timeout(3000)

                block_periods = {}
                for pk in ["1day", "1hour", "7day"]:
                    ktype = KLINE_TYPE_MAP[pk]
                    if pk != "1day":
                        kline_responses.clear()
                        if not _click_period(PERIOD_BTNS[pk]):
                            continue
                        page.wait_for_timeout(2000)
                    else:
                        page.wait_for_timeout(2000)
                        has_1day = any(kr["ktype"] == ktype and len(kr["data"]) > 5 for kr in kline_responses)
                        if not has_1day:
                            kline_responses.clear()
                            if _click_period(PERIOD_BTNS[pk]):
                                page.wait_for_timeout(3000)
                    data = _wait_for_ktype(ktype, 15000)
                    if data:
                        converted = _convert(data)
                        block_periods[pk] = converted
                        nz_v = sum(1 for d in converted if d["v"] > 0)
                        print(f"    ✓ {pk}: {len(converted)} 条 vol非零{nz_v}", flush=True)

                if "1day" in block_periods:
                    print(f"    [dataZoom] 滑动加载 {bname} 1day 完整数据...", flush=True)
                    kline_responses.clear()
                    if _click_period(PERIOD_BTNS["1day"]):
                        page.wait_for_timeout(3000)
                    else:
                        page.wait_for_timeout(2000)
                    full_1day = _load_steamdt_full_kline(KLINE_TYPE_MAP["1day"], max_slides=4)
                    if len(full_1day) > len(block_periods.get("1day", [])):
                        block_periods["1day"] = full_1day
                        print(f"    ✓ {bname} 1day 最终: {len(full_1day)} 条", flush=True)

                block_summary = {
                    "trendList": blk.get("trendList", []),
                    "index": blk.get("index"),
                    "chgRate": blk.get("riseFallRate"),
                    "chgDiff": blk.get("riseFallDiff"),
                }
                result["blocks"][bid] = {
                    "id": bid,
                    "name": bname,
                    "blockType": btype,
                    "summary": block_summary,
                    "periods": block_periods,
                }
            except Exception as e:
                print(f"    ✗ 失败: {type(e).__name__}: {e}", flush=True)

        print(f"\n[3] 整理indices格式（兼容前端）...", flush=True)
        indices = {}
        if result["broad"]:
            b = result["broad"]
            indices["broad"] = {"id": "broad", "name": b["name"], "periods": b["periods"]}
        for bid, bd in result["blocks"].items():
            indices[bid] = {
                "id": bid,
                "name": bd["name"],
                "blockType": bd.get("blockType", "HOT"),
                "summary": bd.get("summary", {}),
                "trendList": bd.get("summary", {}).get("trendList", []),
                "index": bd.get("summary", {}).get("index"),
                "chgRate": bd.get("summary", {}).get("chgRate"),
                "chgDiff": bd.get("summary", {}).get("chgDiff"),
                "periods": bd["periods"],
            }
        result["indices"] = indices

        result["scrape_ok"] = bool(result["broad"] and result["broad"]["periods"])
        if not result["scrape_ok"]:
            result["scrape_fail"] = "SteamDT大盘数据采集失败"

        print(f"\n  SteamDT采集完成:", flush=True)
        print(f"    大盘周期: {list(broad_periods.keys())}", flush=True)
        print(f"    板块数量: {len(result['blocks'])}", flush=True)
        print(f"    状态: {'✓ 成功' if result['scrape_ok'] else '✗ 失败'}", flush=True)

    except Exception as e:
        result["scrape_fail"] = f"{type(e).__name__}: {e}"
        print(f"  [ERROR] SteamDT采集异常: {type(e).__name__}: {e}", flush=True)
    finally:
        page.remove_listener("response", handle_response)

    return result


if __name__ == "__main__":
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="zh-CN",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()
        result = scrape_steamdt(page)
        browser.close()
        with open("steamdt_result.json", "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n结果已保存到 steamdt_result.json")
