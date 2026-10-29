#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_reports.py - 对一批 analysis.json 做管线自检

**自检对象是测量链路本身，不是被测代码。** 它检查的是"这份报告的数据可不可信"，
而不是"程序有没有在等"。适合放在 CI 里、或每次批量测量后跑一遍。

检查项：
  1. 拆分恒等式  到齐前等待 + 到齐后耗时 == 等待时长
  2. 两个分段量  到齐前等待 <= 等待合计
  3. 各 rank 求和 == 等待合计
  4. 日志完整性  非法行 0、无缺失 rank、无未收尾、无未配对 enter
  5. 进程数一致  声明进程数 vs 实际收集
  6. 采集截断    记录是否只覆盖采样窗口

用法：
  python3 tools/check_reports.py            # 默认扫 ./runs
  python3 tools/check_reports.py <dir>...   # 扫指定目录（含子目录里的 analysis.json）
  python3 tools/check_reports.py a.json b.json
退出码：0 全部通过；1 有失败项；2 找不到 analysis.json
"""
import glob
import json
import os
import sys

# Windows 控制台默认 GBK，报告含 U+2212 等字符时 print 会抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 浮点比较容差：绝对值 0.05 rank·ms，另加一个相对量兜底
ABS_TOL = 0.05
REL_TOL = 1e-6


def tol_for(magnitude):
    return max(ABS_TOL, abs(magnitude) * REL_TOL)


def find_analysis_json(args):
    paths = []
    for a in args:
        if os.path.isdir(a):
            paths.extend(glob.glob(os.path.join(a, "**", "analysis.json"), recursive=True))
        elif os.path.isfile(a):
            paths.append(a)
        else:
            print("跳过（不存在）: %s" % a)
    return sorted(set(paths))


def check_one(path):
    """返回 (scenario, [(ok, name, detail)])"""
    with open(path, encoding="utf-8") as f:
        a = json.load(f)
    m = a.get("metrics", {})
    results = []

    total = m.get("wait_ms_total", 0.0)
    mutual = m.get("mutual_wait_ms_total", 0.0)
    post = m.get("post_arrival_ms_total", 0.0)

    # 1. 拆分恒等式
    diff = abs(total - (mutual + post))
    results.append((diff <= tol_for(total), "拆分恒等式",
                    "total=%.4f vs mutual+post=%.4f (diff=%.6f, tol=%.4f)"
                    % (total, mutual + post, diff, tol_for(total))))

    # 2. 分段量不得超过合计
    results.append((mutual <= total + tol_for(total), "到齐前等待 <= 合计",
                    "mutual=%.4f, total=%.4f" % (mutual, total)))

    # 3. 各 rank 求和 == 等待合计（per_rank_wait_ms 是 {rank: ms} 的字典）
    per_rank = m.get("per_rank_wait_ms") or {}
    if isinstance(per_rank, dict):
        values = list(per_rank.values())
    else:
        values = list(per_rank)
    rank_sum = sum(v for v in values if isinstance(v, (int, float)))
    results.append((abs(rank_sum - total) <= tol_for(total), "各 rank 求和 == 合计",
                    "sum(per_rank)=%.4f vs total=%.4f (%d 个 rank)"
                    % (rank_sum, total, len(values))))

    # 4. 日志完整性
    parse_errors = m.get("parse_errors", a.get("parse_errors", 0))
    ok4 = (parse_errors == 0
           and not a.get("missing_ranks")
           and not a.get("missing_finalize")
           and not a.get("unclosed_enters"))
    results.append((ok4, "日志完整性",
                    "非法行=%s 缺失rank=%s 未收尾=%s 未配对enter=%s"
                    % (parse_errors, a.get("missing_ranks") or "无",
                       a.get("missing_finalize") or "无",
                       a.get("unclosed_enters") or "无")))

    # 5. 进程数一致
    declared = a.get("declared_nranks")
    actual = m.get("nranks")
    results.append((declared in (None, actual), "进程数一致",
                    "声明=%s 实收=%s" % (declared, actual)))

    # 6. 截断（不判失败，只如实报告）
    truncated = m.get("truncated_at_sync")
    results.append((True, "采集截断标记",
                    "已截断于 %s 个同步点" % truncated if truncated else "未截断（覆盖整个运行）"))

    return a.get("scenario") or os.path.basename(os.path.dirname(path)), results


def main():
    args = sys.argv[1:] or ["runs"]
    paths = find_analysis_json(args)
    if not paths:
        print("找不到 analysis.json（先跑 tools/run_all_sim.sh 或手工生成）")
        return 2

    failed = []
    for p in paths:
        scenario, results = check_one(p)
        print("== %s ==" % scenario)
        print("   %s" % p)
        for ok, name, detail in results:
            print("   [%s] %-18s %s" % ("OK  " if ok else "FAIL", name, detail))
            if not ok:
                failed.append("%s / %s" % (scenario, name))
        print("")

    print("=" * 60)
    if failed:
        print("管线自检: %d 项失败" % len(failed))
        for f in failed:
            print("  - %s" % f)
        return 1
    print("管线自检: 全部通过（%d 份报告）" % len(paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
