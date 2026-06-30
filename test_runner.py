#!/usr/bin/env python3
"""SteamDT 采集测试运行器

独立的测试脚本，创建Playwright环境并调用scrape_steamdt(page)。
记录耗时和数据完整性，输出测试结果。
"""
import json
import time
import sys
import os
import importlib.util

VARIANT_FILE = os.environ.get("VARIANT_FILE", "scrape_steamdt.py")


def load_variant(filepath):
    """动态加载变体模块"""
    spec = importlib.util.spec_from_file_location("scrape_steamdt_variant", filepath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    from playwright.sync_api import sync_playwright

    print(f"=" * 60, flush=True)
    print(f"  SteamDT 采集测试", flush=True)
    print(f"  变体: {VARIANT_FILE}", flush=True)
    print(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"=" * 60, flush=True)

    # 加载变体
    variant_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), VARIANT_FILE)
    if not os.path.exists(variant_path):
        print(f"  ✗ 变体文件不存在: {variant_path}", flush=True)
        sys.exit(1)

    print(f"\n[加载] {VARIANT_FILE}...", flush=True)
    mod = load_variant(variant_path)

    # 计时开始
    t_start = time.time()

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
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()

        result = mod.scrape_steamdt(page)
        browser.close()

    # 计时结束
    t_end = time.time()
    elapsed = t_end - t_start

    # 统计数据
    broad_periods = {}
    block_count = 0
    total_klines = 0
    blocks_with_data = 0

    if result.get("broad"):
        for pk, data in result["broad"].get("periods", {}).items():
            broad_periods[pk] = len(data)
            total_klines += len(data)

    for bid, bd in result.get("blocks", {}).items():
        block_count += 1
        has_data = False
        for pk, data in bd.get("periods", {}).items():
            total_klines += len(data)
            if len(data) > 5:
                has_data = True
        if has_data:
            blocks_with_data += 1

    # 输出测试结果
    test_result = {
        "variant": VARIANT_FILE,
        "elapsed_seconds": round(elapsed, 1),
        "elapsed_str": f"{int(elapsed // 60)}m{elapsed % 60:.0f}s",
        "scrape_ok": result.get("scrape_ok", False),
        "scrape_fail": result.get("scrape_fail", ""),
        "broad_periods": broad_periods,
        "block_count": block_count,
        "blocks_with_data": blocks_with_data,
        "total_klines": total_klines,
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
    }

    print(f"\n{'=' * 60}", flush=True)
    print(f"  测试结果", flush=True)
    print(f"{'=' * 60}", flush=True)
    print(f"  变体: {VARIANT_FILE}", flush=True)
    print(f"  耗时: {test_result['elapsed_str']} ({elapsed:.1f}s)", flush=True)
    print(f"  采集状态: {'✓ 成功' if test_result['scrape_ok'] else '✗ 失败'}", flush=True)
    print(f"  大盘周期: {broad_periods}", flush=True)
    print(f"  板块数: {block_count} (有数据: {blocks_with_data})", flush=True)
    print(f"  总K线条数: {total_klines}", flush=True)

    # 保存结果
    result_file = "steamdt_test_result.json"
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump(test_result, f, ensure_ascii=False, indent=2)
    print(f"\n  结果已保存: {result_file}", flush=True)

    # 同时保存原始采集数据
    with open("steamdt_raw_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    return test_result


if __name__ == "__main__":
    main()
