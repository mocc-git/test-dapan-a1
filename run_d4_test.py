#!/usr/bin/env python3
"""D4 并行采集测试运行器

调用 scrape_home.py 的 main() 函数（触发D4并行逻辑），
然后读取保存的 home_result.json 生成测试摘要。

与 test_home_runner.py 的区别：
  - test_home_runner.py 调用 scrape_home()（单线程，不触发并行）
  - run_d4_test.py 调用 main()（触发D4 6组并行+SteamDT=7线程）
"""
import json
import time
import sys
import os
import importlib.util


def load_module(filepath, module_name):
    spec = importlib.util.spec_from_file_location(module_name, filepath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    print(f"{'=' * 60}", flush=True)
    print(f"  CSQAQ D4 并行采集测试 (6组CSQAQ+SteamDT=7线程)", flush=True)
    print(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"{'=' * 60}", flush=True)

    home_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrape_home.py")
    if not os.path.exists(home_path):
        print(f"  X scrape_home.py not found: {home_path}", flush=True)
        sys.exit(1)

    print(f"\n[load] scrape_home.py...", flush=True)
    mod = load_module(home_path, "scrape_home")

    index_ids = mod.DEFAULT_INDEX_IDS
    periods = mod.DEFAULT_PERIODS
    kline_periods = mod.DEFAULT_KLINE_PERIODS

    print(f"  indices: {len(index_ids)}", flush=True)
    print(f"  sub_data periods: {periods}", flush=True)
    print(f"  sub_kline periods: {kline_periods}", flush=True)

    t_start = time.time()

    mod.main()

    t_end = time.time()
    elapsed = t_end - t_start

    result_file = getattr(mod, "RESULT_FILE", "home_result.json")
    if not os.path.exists(result_file):
        print(f"\n  X {result_file} not found after main()", flush=True)
        sys.exit(1)

    with open(result_file, "r", encoding="utf-8") as f:
        saved_result = json.load(f)

    data = saved_result.get("data", {})
    sub_kline = data.get("sub_kline", {})

    index_stats = {}
    total_klines = 0
    for idx_id, info in sub_kline.items():
        periods_data = info.get("periods", {})
        stats = {
            "name": info.get("name", ""),
            "1day": len(periods_data.get("1day", [])),
            "1hour": len(periods_data.get("1hour", [])),
            "7day": len(periods_data.get("7day", [])),
        }
        index_stats[idx_id] = stats
        for cnt in [stats["1day"], stats["1hour"], stats["7day"]]:
            total_klines += cnt

    indices_with_full_history = sum(1 for s in index_stats.values() if s["1day"] >= 200)
    indices_with_data = sum(1 for s in index_stats.values() if s["1day"] > 0)

    steamdt = data.get("steamdt_kline", {})
    steamdt_total = 0
    steamdt_blocks = 0
    for sid, sinfo in steamdt.items():
        if sid != "broad":
            steamdt_blocks += 1
        for pk, pdata in sinfo.get("periods", {}).items():
            steamdt_total += len(pdata)

    test_result = {
        "test_type": "home_datazoom_all_d4_parallel",
        "elapsed_seconds": round(elapsed, 1),
        "elapsed_str": f"{int(elapsed // 60)}m{elapsed % 60:.0f}s",
        "scrape_ok": data.get("scrape_ok", False),
        "scrape_fail": data.get("scrape_fail", ""),
        "index_count": len(sub_kline),
        "indices_with_data": indices_with_data,
        "indices_with_full_history": indices_with_full_history,
        "total_klines": total_klines,
        "steamdt_blocks": steamdt_blocks,
        "steamdt_total_klines": steamdt_total,
        "index_stats": index_stats,
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "d4_parallel": True,
        "d4_groups": 6,
        "d4_threads": 7,
    }

    print(f"\n{'=' * 60}", flush=True)
    print(f"  Test Result (D4 Parallel)", flush=True)
    print(f"{'=' * 60}", flush=True)
    print(f"  elapsed: {test_result['elapsed_str']} ({elapsed:.1f}s)", flush=True)
    print(f"  scrape_ok: {'OK' if test_result['scrape_ok'] else 'FAIL'}", flush=True)
    print(f"  indices: {test_result['index_count']}", flush=True)
    print(f"  indices_with_data: {indices_with_data}/{test_result['index_count']}", flush=True)
    print(f"  full_history(>=200 1day): {indices_with_full_history}/{test_result['index_count']}", flush=True)
    print(f"  total_klines: {total_klines}", flush=True)
    print(f"  steamdt_blocks: {steamdt_blocks}", flush=True)
    print(f"  steamdt_klines: {steamdt_total}", flush=True)

    print(f"\n  per-index 1day:", flush=True)
    for idx_id, stats in sorted(index_stats.items(), key=lambda x: int(x[0]) if str(x[0]).isdigit() else 999):
        marker = "OK" if stats["1day"] >= 200 else ("WARN" if stats["1day"] > 0 else "FAIL")
        print(f"    [{marker}] {stats['name']}({idx_id}): 1day={stats['1day']}, 1hour={stats['1hour']}, 7day={stats['7day']}", flush=True)

    out_file = "home_test_result.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(test_result, f, ensure_ascii=False, indent=2)
    print(f"\n  saved: {out_file}", flush=True)

    with open("home_raw_result.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  saved: home_raw_result.json", flush=True)

    return test_result


if __name__ == "__main__":
    main()
