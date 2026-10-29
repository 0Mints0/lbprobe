#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lb_analyze.py - 同步点等待测量分析器

回答两个问题:
  1. 当前计算过程中进程之间**是否存在相互等待**?
  2. **等待时间是多少**?

方法(只需要探针的最小插桩: 每个集合通信前后的两行):
  一次同步里, 每个 rank 都有一段"待在集合通信里"的时间。把它按最晚到齐者拆成两段:
    到齐前等待 = 最晚进入者时刻 − 本 rank 进入时刻   —— 我算完了在等人 => 相互等待
    到齐后耗时 = 本 rank 退出时刻 − 最晚进入者时刻   —— 全组到齐后原语本身的开销
  两者之和严格等于该 rank 这次同步的等待时长, 所以拆分不重不漏。

  聚合量:
    相互等待时长 = Σ(各 rank 的到齐前等待)        单位 rank·ms
    平均同时在等待的 rank 数 = 等待区间并集的并发度时间加权平均
    等待占比 = 相互等待时长 / (墙钟 × 进程数)

输入: 运行目录(含 events.rank*.jsonl)
输出: analysis.json + report.md

用法:
  py -3 tools/lb_analyze.py <run_dir> [--json] [--wait-threshold 5] [--mutual-threshold 50]
"""
import argparse
import json
import os
import sys
from collections import defaultdict

# Windows 控制台默认是 GBK/CP936，报告里含 U+2212(−)、·、≥ 等字符时
# print(rep) 会直接抛 UnicodeEncodeError 中断收尾。文件本身始终按 UTF-8 写，
# 这里只把控制台输出的编码放宽，不影响 report.md 的内容。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

KIND_NAME = {0: "other", 1: "barrier", 2: "allreduce", 3: "allgather",
             4: "alltoallv", 5: "sendrecv", 6: "recv", 7: "bcast"}

# 点对点等待: 语义与集合通信不同, 不能套用"到齐前等待"模型(见 analyze() 里的说明)
P2P_KINDS = frozenset(["sendrecv", "recv", "other"])


def load_events(run_dir):
    """读所有 rank 的事件日志。坏行必须计数上报 —— 静默跳过会让结论失真。"""
    per_rank, bad = {}, []
    for fn in sorted(os.listdir(run_dir)):
        if not (fn.startswith("events.rank") and fn.endswith(".jsonl")):
            continue
        r = int(fn[len("events.rank"):-len(".jsonl")])
        evs = []
        with open(os.path.join(run_dir, fn), "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    evs.append(json.loads(line))
                except ValueError:
                    bad.append({"file": fn, "line": lineno, "raw": line[:200]})
        per_rank[r] = evs
    return per_rank, bad


def mean_concurrent(intervals, t0, t1):
    """[t0,t1] 内同时处于等待的区间数的加权平均 = 平均同时在等待的 rank 数"""
    if t1 <= t0:
        return 0.0
    edges = []
    for s, e in intervals:
        if e <= t0 or s >= t1:
            continue
        edges.append((max(s, t0), 1))
        edges.append((min(e, t1), -1))
    if not edges:
        return 0.0
    edges.sort()
    area, cur, prev = 0.0, 0, edges[0][0]
    for t, d in edges:
        area += cur * (t - prev)
        cur += d
        prev = t
    area += cur * (t1 - prev)
    return area / (t1 - t0)


def pair_intervals(events):
    """把 enter/exit 配成区间。按 (kind,tag) 各自排队, 支持同一 tag 多次调用。"""
    pending = defaultdict(list)
    out = []
    for e in events:
        t = e.get("e")
        if t == "enter":
            k = KIND_NAME.get(e.get("k"), "?")
            pending[(k, e.get("tag"))].append(e.get("te"))
        elif t == "exit":
            k = KIND_NAME.get(e.get("k"), "?")
            key = (k, e.get("tag"))
            if pending[key]:
                t0 = pending[key].pop(0)
                out.append({"kind": k, "tag": e.get("tag"), "in": t0,
                            "out": e.get("te"), "st": e.get("st", 0),
                            "dur": e.get("dur_ms", -1.0)})
    unclosed = [{"kind": k, "tag": tg, "in": v[0]}
                for (k, tg), v in pending.items() for _ in v]
    return sorted(out, key=lambda x: x["in"] if x["in"] is not None else 0), unclosed


def analyze(run_dir, wait_threshold=5.0, mutual_threshold=50.0,
            single_rank_threshold=60.0, skip_ms=0, iterations=0):
    meta = {}
    mp = os.path.join(run_dir, "run.json")
    if os.path.isfile(mp):
        with open(mp, "r", encoding="utf-8") as f:
            meta = json.load(f)

    per_rank, parse_errors = load_events(run_dir)
    if not per_rank:
        raise SystemExit("运行目录里没有 events.rank*.jsonl: %s" % run_dir)
    ranks = sorted(per_rank)
    nranks = len(ranks)

    # 声明进程数(取自 init 事件) vs 实际收集到的文件数:
    # 真实运行里若某个 rank 崩溃或汇集失败, 报告必须自己说出来,
    # 否则会拿不完整的数据得出"等待很小"的错误结论。
    declared_nranks = None
    for r in ranks:
        for e in per_rank[r]:
            if e.get("e") == "init" and e.get("nranks"):
                declared_nranks = e["nranks"]
                break
        if declared_nranks:
            break
    missing_ranks = (sorted(set(range(declared_nranks)) - set(ranks))
                     if declared_nranks and declared_nranks > nranks else [])

    # 采集是否被事件数上限截断(LB_MAX_SYNC)。截断本身不是错误(长跑算例必须限制
    # 日志量), 但报告必须说清楚: 结论只覆盖采样到的那一段。
    truncated_at = None
    for r in ranks:
        for e in per_rank[r]:
            if e.get("e") == "truncated":
                truncated_at = e.get("max")
                break
        if truncated_at:
            break

    # --- 每个 rank 的墙钟(init -> finalize) ---
    wall, t_start, t_end, has_finalize = {}, {}, {}, {}
    for r in ranks:
        t0 = t1 = None
        for e in per_rank[r]:
            if e.get("e") == "init" and t0 is None:
                t0 = e.get("te")
            if e.get("e") == "finalize":
                t1 = e.get("te")
                has_finalize[r] = True
        if t0 is None:
            t0 = per_rank[r][0].get("te") if per_rank[r] else None
        if t1 is None:                      # 异常退出: 用最后一条事件兜底
            t1 = per_rank[r][-1].get("te") if per_rank[r] else t0
        t_start[r], t_end[r] = t0, t1
        wall[r] = (t1 - t0) if (t0 is not None and t1 is not None) else 0.0

    iv_by_rank, unclosed_by_rank = {}, {}
    for r in ranks:
        iv_by_rank[r], unclosed_by_rank[r] = pair_intervals(per_rank[r])

    # --- 跨 rank 配对成"同步点": 同一 (kind,tag) 的第 n 次调用 ---
    # 注意: **点对点等待(sendrecv/recv/other)不能按这个模型算**。
    # 集合通信是"到齐"语义: 最晚进入者之后的等待可以归因给"大家在等它";
    # 而点对点 Wait 只是"等某个邻居的数据", 各进程的第 n 次调用根本不是同一个事件
    # (rank0 的第 n 次 Wait 可能比 rank2 的第 n 次早 40 秒), 套用"到齐"模型会得到
    # 荒谬的数(实测把 8492 rank·ms 算成 1801万)。所以点对点单独统计阻塞时长。
    groups = defaultdict(dict)              # (kind,tag) -> {rank: [iv,...]}
    for r in ranks:
        for iv in iv_by_rank[r]:
            groups[(iv["kind"], iv["tag"])].setdefault(r, []).append(iv)

    sync_points = []
    unmatched = []
    for (kind, tag), per in groups.items():
        if kind in P2P_KINDS:
            continue
        nmax = max(len(v) for v in per.values())
        for occ in range(nmax):
            members = {}
            for r, lst in per.items():
                if occ < len(lst):
                    members[r] = lst[occ]
                else:
                    unmatched.append({"rank": r, "kind": kind, "tag": tag, "occurrence": occ})
            if len(members) < 2:
                continue
            ins = {r: v["in"] for r, v in members.items() if v["in"] is not None}
            outs = {r: v["out"] for r, v in members.items() if v["out"] is not None}
            if not ins:
                continue
            t_last = max(ins.values())
            t_first = min(ins.values())
            mutual = sum(max(0.0, t_last - t) for t in ins.values())
            post = sum(max(0.0, outs[r] - t_last) for r in outs)
            total = sum(max(0.0, outs[r] - ins[r]) for r in outs if r in ins)
            last_ranks = sorted(r for r, t in ins.items() if t == t_last)
            sync_points.append({
                "kind": kind, "tag": tag, "occurrence": occ, "n_ranks": len(members),
                "arrival_skew_ms": round(t_last - t_first, 3),
                "mutual_wait_ms": round(mutual, 3),
                "post_arrival_ms": round(post, 3),
                "wait_ms": round(total, 3),
                "last_arriver": last_ranks,
                "max_rank_wait_ms": round(max((outs[r] - ins[r])
                                              for r in outs if r in ins), 3)
                if outs else 0.0,
                "t_first": t_first, "t_last": t_last,
            })
    sync_points.sort(key=lambda s: s["t_first"])

    # --- 点对点等待(界面/边界交换): 只统计"这个进程在 Wait 里阻塞了多久" ---
    # 这里不用跨 rank 配对: 每个 rank 自己阻塞多久就是多久, 跨 rank 只比"谁等得多"。
    p2p_tag = defaultdict(lambda: {"kinds": set(), "per_rank": {},
                                   "n": 0, "tot": 0.0, "max": 0.0})
    p2p_total, p2p_calls, p2p_max_block = 0.0, 0, 0.0
    p2p_per_rank = defaultdict(float)
    p2p_iv = []
    for r in ranks:
        for iv in iv_by_rank[r]:
            if iv["kind"] not in P2P_KINDS:
                continue
            if iv["in"] is None or iv["out"] is None:
                continue
            d = max(0.0, iv["out"] - iv["in"])
            g = p2p_tag[iv["tag"]]
            g["kinds"].add(iv["kind"])
            pr = g["per_rank"].setdefault(r, {"n": 0, "tot": 0.0, "max": 0.0})
            pr["n"] += 1
            pr["tot"] += d
            pr["max"] = max(pr["max"], d)
            g["n"] += 1
            g["tot"] += d
            g["max"] = max(g["max"], d)
            p2p_total += d
            p2p_calls += 1
            p2p_per_rank[r] += d
            p2p_max_block = max(p2p_max_block, d)
            p2p_iv.append((iv["in"], iv["out"]))

    p2p_rows = []
    for tg, g in p2p_tag.items():
        tots = [v["tot"] for v in g["per_rank"].values()]
        p2p_rows.append({
            "tag": tg,
            "kind": "/".join(sorted(g["kinds"])),
            "calls": g["n"],
            "ranks": len(g["per_rank"]),
            "block_ms": round(g["tot"], 2),
            "mean_ms": round(g["tot"] / g["n"], 4) if g["n"] else 0.0,
            "max_ms": round(g["max"], 2),
            "calls_by_rank": {str(r): v["n"] for r, v in sorted(g["per_rank"].items())},
            "block_by_rank": {str(r): round(v["tot"], 2)
                              for r, v in sorted(g["per_rank"].items())},
            "spread_ms": round(max(tots) - min(tots), 2) if tots else 0.0,
        })
    p2p_rows.sort(key=lambda x: -x["block_ms"])

    # --- 聚合 ---
    mutual_total = sum(s["mutual_wait_ms"] for s in sync_points)
    post_total = sum(s["post_arrival_ms"] for s in sync_points)
    wait_total = sum(s["wait_ms"] for s in sync_points)

    all_iv = []
    per_rank_wait = defaultdict(float)
    for r in ranks:
        for iv in iv_by_rank[r]:
            if iv["in"] is None or iv["out"] is None:
                continue
            all_iv.append((iv["in"], iv["out"]))
            per_rank_wait[r] += max(0.0, iv["out"] - iv["in"])

    g_t0 = min((v for v in t_start.values() if v is not None), default=0.0)
    g_t1 = max((v for v in t_end.values() if v is not None), default=0.0)
    wall_global = max(0.0, g_t1 - g_t0)
    mean_waiting = mean_concurrent(all_iv, g_t0, g_t1)
    mean_waiting_p2p = mean_concurrent(p2p_iv, g_t0, g_t1) if p2p_iv else 0.0
    coll_iv = []
    for r in ranks:
        for iv in iv_by_rank[r]:
            if iv["kind"] in P2P_KINDS:
                continue
            if iv["in"] is not None and iv["out"] is not None:
                coll_iv.append((iv["in"], iv["out"]))
    mean_waiting_coll = mean_concurrent(coll_iv, g_t0, g_t1)

    # --- 启动一次性 vs 稳态: 用 --skip-ms 指定的起点把前段切掉 ---
    steady_from = (g_t0 + skip_ms) if skip_ms and skip_ms > 0 else None

    def _in_steady(t):
        return (steady_from is None) or (t is not None and t >= steady_from)

    mutual_steady = sum(s["mutual_wait_ms"] for s in sync_points
                        if _in_steady(s["t_first"]))
    mutual_startup = mutual_total - mutual_steady
    p2p_steady, p2p_calls_steady = 0.0, 0
    for r in ranks:
        for iv in iv_by_rank[r]:
            if iv["kind"] not in P2P_KINDS:
                continue
            if iv["in"] is None or iv["out"] is None or not _in_steady(iv["in"]):
                continue
            p2p_steady += max(0.0, iv["out"] - iv["in"])
            p2p_calls_steady += 1

    rank_time = wall_global * nranks
    wait_share = (wait_total / rank_time * 100.0) if rank_time > 0 else 0.0
    mutual_share = (mutual_total / (mutual_total + post_total) * 100.0) \
        if (mutual_total + post_total) > 0 else 0.0
    waiting_ranks_share = (mean_waiting / nranks * 100.0) if nranks else 0.0
    p2p_share = (p2p_total / rank_time * 100.0) if rank_time > 0 else 0.0

    wait_by_kind = defaultdict(float)
    last_arriver_count = defaultdict(int)
    for s in sync_points:
        wait_by_kind[s["kind"]] += s["wait_ms"]
        for r in s["last_arriver"]:
            last_arriver_count[r] += 1

    # --- 等待的形态: 这份等待该归给谁? ---
    # 只用集合通信的进入/退出时刻就能判断, 不需要任何额外插桩:
    #   归给固定一个 rank -> 单向等待: 大家在等同一个落后进程(负载不均/设备同步)
    #   分散在多个 rank   -> 相互等待: 谁也不固定落后, 是整体进度不一致
    # 注意必须**按等待量加权**, 不能按次数: 少数几次大等待才是真正吃掉时间的那部分,
    # 按次数算会被大量"几乎不花时间"的同步点稀释掉(实测会被稀释到 42%)。
    weight_by_rank = defaultdict(float)
    for s in sync_points:
        share = s["mutual_wait_ms"] / float(max(1, len(s["last_arriver"])))
        for r in s["last_arriver"]:
            weight_by_rank[r] += share
    total_mutual_weight = sum(weight_by_rank.values())
    dominant_rank, dominant_weight, concentration = None, 0.0, 0.0
    if weight_by_rank and total_mutual_weight > 0:
        dominant_rank, dominant_weight = max(weight_by_rank.items(), key=lambda kv: kv[1])
        concentration = dominant_weight / total_mutual_weight
    count_concentration = 0.0
    if sync_points and last_arriver_count:
        count_concentration = max(last_arriver_count.values()) / float(len(sync_points))

    # --- 按调用点(tag)聚合: 真实算例有几千个同步点, 逐条明细看不出重点,
    #     必须按 tag 汇总才能回答"等待集中在哪个调用点" ---
    tag_agg = defaultdict(lambda: {"n": 0, "mutual": 0.0, "post": 0.0, "wait": 0.0,
                                   "skew": 0.0, "ranks": set(), "kinds": set()})
    for s in sync_points:
        g = tag_agg[s["tag"]]
        g["n"] += 1
        g["mutual"] += s["mutual_wait_ms"]
        g["post"] += s["post_arrival_ms"]
        g["wait"] += s["wait_ms"]
        g["skew"] = max(g["skew"], s["arrival_skew_ms"])
        g["ranks"].add(s["n_ranks"])
        g["kinds"].add(s["kind"])
    tag_rows = [{"tag": k, "kind": "/".join(sorted(v["kinds"])), "count": v["n"],
                 "n_ranks": sorted(v["ranks"]),
                 "mutual_wait_ms": round(v["mutual"], 2),
                 "post_arrival_ms": round(v["post"], 2),
                 "wait_ms": round(v["wait"], 2),
                 "max_skew_ms": round(v["skew"], 2)}
                for k, v in sorted(tag_agg.items(), key=lambda kv: -kv[1]["mutual"])]

    # --- 判定(阈值可配, 并在报告里写明, 便于复核) ---
    base = ("同步点等待合计 %.1f rank·ms，占 墙钟×进程数 的 %.2f%%；"
            "平均 %.2f 个 rank 同时在等待（占 %.1f%%）。"
            % (wait_total, wait_share, mean_waiting, waiting_ranks_share))

    if waiting_ranks_share < wait_threshold:
        verdict_code = "none"
        verdict = "未观测到有实际影响的等待"
        detail = (base + " 平均同时在等待的 rank 占比低于阈值 %.0f%%，"
                         "本次测量未发现会拖慢运行的进程间等待。" % wait_threshold)
    elif mutual_share >= mutual_threshold:
        mutual_bit = ("其中 %.0f%% 属于到齐前等待（我算完了在等其它进程），"
                      "合计 **%.1f rank·ms**（≈ %.3f 核·秒）。"
                      % (mutual_share, mutual_total, mutual_total / 1000.0))
        # 注意量纲: concentration 是小数, 阈值是百分数, 必须统一后再比
        if concentration * 100.0 >= single_rank_threshold and dominant_rank is not None:
            verdict_code = "wait_single_rank"
            verdict = "存在进程间等待，形态是单向等待（归因于单个进程）"
            detail = (base + mutual_bit +
                      " **这份相互等待的 %.0f%% 都归因于 rank %s**（它在 %.0f%% 的同步点上最后到达），"
                      "即其余进程在等它一个：这是单点进度落后（负载不均或设备/IO 阻塞），"
                      "不是双向的互相等待。"
                      % (concentration * 100.0, dominant_rank, count_concentration * 100.0))
        else:
            verdict_code = "wait_mutual"
            verdict = "存在相互等待"
            detail = (base + mutual_bit +
                      " 等待分散在多个进程之间（最集中的 rank %s 也只占 %.0f%%），"
                      "属于进程之间进度不一致造成的相互等待。"
                      % (dominant_rank, concentration * 100.0))
    else:
        verdict_code = "primitive_overhead"
        verdict = "存在等待，但主要不是进程间互相等待"
        detail = (base + " 到齐前等待只占全部等待的 %.0f%%（低于阈值 %.0f%%），"
                  "主要是集合通信原语自身的开销。"
                  % (mutual_share, mutual_threshold))

    metrics = {
        "nranks": nranks,
        "wall_ms_global": round(wall_global, 2),
        "wall_ms_per_rank": {str(r): round(wall[r], 2) for r in ranks},
        "sync_point_count": len(sync_points),
        "mutual_wait_ms_total": round(mutual_total, 2),
        "post_arrival_ms_total": round(post_total, 2),
        "wait_ms_total": round(wait_total, 2),
        "mutual_wait_core_seconds": round(mutual_total / 1000.0, 4),
        "wait_core_seconds": round(wait_total / 1000.0, 4),
        "wait_share_pct_of_rank_time": round(wait_share, 3),
        "mutual_share_pct_of_wait": round(mutual_share, 2),
        "mean_waiting_ranks": round(mean_waiting, 3),
        "mean_waiting_ranks_pct": round(waiting_ranks_share, 2),
        "max_arrival_skew_ms": round(max((s["arrival_skew_ms"] for s in sync_points),
                                         default=0.0), 2),
        "wait_by_kind_ms": {k: round(v, 2) for k, v in
                            sorted(wait_by_kind.items(), key=lambda kv: -kv[1])},
        "last_arriver_counts": {str(k): v for k, v in
                                sorted(last_arriver_count.items(), key=lambda kv: -kv[1])},
        "dominant_last_arriver": dominant_rank,
        "dominant_last_arriver_share_pct": round(concentration * 100.0, 2),
        "dominant_last_arriver_count_share_pct": round(count_concentration * 100.0, 2),
        "per_rank_wait_ms": {str(r): round(per_rank_wait[r], 2) for r in ranks},
        "per_rank_wait_share_pct": {
            str(r): round(per_rank_wait[r] / wall[r] * 100.0, 2) if wall[r] > 0 else 0.0
            for r in ranks},
        "thresholds": {"wait_share_pct": wait_threshold,
                       "mutual_share_pct": mutual_threshold,
                       "single_rank_share_pct": single_rank_threshold},
        "parse_errors": len(parse_errors),
        "truncated_at_sync": truncated_at,
        # --- 点对点(边界交换) ---
        "p2p_block_ms_total": round(p2p_total, 2),
        "p2p_calls_total": p2p_calls,
        "p2p_max_block_ms": round(p2p_max_block, 2),
        "p2p_share_pct_of_rank_time": round(p2p_share, 3),
        "p2p_per_rank_ms": {str(r): round(p2p_per_rank[r], 2) for r in ranks},
        "p2p_mean_waiting_ranks": round(mean_waiting_p2p, 3),
        "p2p_tag_count": len(p2p_rows),
        # --- 分段: 启动一次性 vs 稳态 ---
        "skip_ms": skip_ms,
        "mutual_wait_startup_ms": round(mutual_startup, 2),
        "mutual_wait_steady_ms": round(mutual_steady, 2),
        "p2p_block_steady_ms": round(p2p_steady, 2),
        "p2p_calls_steady": p2p_calls_steady,
        "collective_mean_waiting_ranks": round(mean_waiting_coll, 3),
        # --- 每迭代归一化(需要 --iterations) ---
        "iterations": iterations,
        "mutual_wait_per_iter": round(mutual_total / iterations, 4) if iterations else None,
        "p2p_block_per_iter": round(p2p_total / iterations, 4) if iterations else None,
        "p2p_steady_per_iter": round(p2p_steady / iterations, 4) if iterations else None,
        "mutual_steady_per_iter": round(mutual_steady / iterations, 4) if iterations else None,
    }

    return {
        "run_dir": run_dir,
        "scenario": meta.get("scenario") or os.path.basename(os.path.abspath(run_dir)),
        "declared_nranks": declared_nranks,
        "missing_ranks": missing_ranks,
        "verdict": verdict,
        "verdict_code": verdict_code,
        "verdict_detail": detail,
        "mutual_wait": verdict_code in ("wait_mutual", "wait_single_rank"),
        "metrics": metrics,
        "sync_points": sync_points,
        "p2p_summary": p2p_rows,
        "tag_summary": tag_rows,
        "unmatched_intervals": unmatched,
        "unclosed_enters": [{"rank": r, "n": len(v)}
                            for r, v in unclosed_by_rank.items() if v],
        "missing_finalize": [r for r in ranks if not has_finalize.get(r)],
        "parse_errors": parse_errors,
    }


def render_report(a):
    m = a["metrics"]
    L = []
    L.append("# 均衡化代码 进程互相等待 测量报告\n")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    L.append("| 算例 | `%s` |" % a["scenario"])
    L.append("| 进程数 | %d |" % m["nranks"])
    L.append("| 采样墙钟 | %.2f ms |" % m["wall_ms_global"])
    L.append("| 采集到的同步点 | %d 个 |" % m["sync_point_count"])
    L.append("")

    L.append("## 0. 结论\n")
    L.append("### %s\n" % a["verdict"])
    L.append(a["verdict_detail"])
    L.append("")
    # 数据不完整时必须显式告警: 否则会拿缺了 rank 的日志得出"等待很小"的错误结论
    if a.get("missing_ranks") or a["parse_errors"] or a["missing_finalize"]:
        L.append("> **⚠ 数据不完整**：缺失 rank 日志 %d 个、非法日志行 %d 行、"
                 "未正常收尾 rank %d 个。以下结论可能不成立，请先补齐数据再采用。"
                 % (len(a.get("missing_ranks") or []), len(a["parse_errors"]),
                    len(a["missing_finalize"])))
        L.append("")
    L.append("| 关键量 | 值 |")
    L.append("|---|---|")
    L.append("| **相互等待时长（到齐前等待合计）** | **%.2f rank·ms**（%.4f 核·秒） |"
             % (m["mutual_wait_ms_total"], m["mutual_wait_core_seconds"]))
    L.append("| 同步点等待合计（相互等待 + 原语开销） | %.2f rank·ms（%.4f 核·秒） |"
             % (m["wait_ms_total"], m["wait_core_seconds"]))
    L.append("| 其中：到齐前等待（相互等待） | %.2f rank·ms（占 %.0f%%） |"
             % (m["mutual_wait_ms_total"], m["mutual_share_pct_of_wait"]))
    L.append("| 其中：到齐后通信耗时（原语开销） | %.2f rank·ms（占 %.0f%%） |"
             % (m["post_arrival_ms_total"], 100.0 - m["mutual_share_pct_of_wait"]))
    L.append("| 平均同时在等待的 rank 数 | %.3f / %d（%.2f%%） |"
             % (m["mean_waiting_ranks"], m["nranks"], m["mean_waiting_ranks_pct"]))
    L.append("| 等待占 墙钟×进程数 的比例 | %.3f%% |" % m["wait_share_pct_of_rank_time"])
    L.append("| 最大到达离散度 | %.2f ms |" % m["max_arrival_skew_ms"])
    L.append("| 最集中的最后到达者 | rank %s（占同步点的 %.2f%%） |"
             % (m["dominant_last_arriver"], m["dominant_last_arriver_share_pct"]))
    if m.get("p2p_calls_total"):
        L.append("| **点对点等待合计（边界交换 Wait 阻塞）** | **%.2f rank·ms**（占 墙钟×进程数 %.3f%%） |"
                 % (m["p2p_block_ms_total"], m["p2p_share_pct_of_rank_time"]))
        L.append("| 点对点 Wait 调用次数 / 单次最长 | %d 次 / %.2f ms |"
                 % (m["p2p_calls_total"], m["p2p_max_block_ms"]))
    if m.get("iterations"):
        L.append("| **每迭代·集合通信相互等待** | **%s rank·ms/迭代**（迭代数 %d） |"
                 % (("%.4f" % m["mutual_wait_per_iter"]) if m["mutual_wait_per_iter"] is not None else "-",
                    m["iterations"]))
        L.append("| **每迭代·点对点等待** | **%s rank·ms/迭代** |"
                 % (("%.4f" % m["p2p_block_per_iter"]) if m["p2p_block_per_iter"] is not None else "-"))
    if m.get("skip_ms"):
        L.append("| 分段（跳过前 %d ms）· 集合通信相互等待 | 启动 %s / 稳态 %s rank·ms |"
                 % (m["skip_ms"], "%.2f" % m["mutual_wait_startup_ms"],
                    "%.2f" % m["mutual_wait_steady_ms"]))
        L.append("| 分段 · 点对点等待 | 稳态 %s rank·ms（%d 次，均 %s ms/次） |"
                 % ("%.2f" % m["p2p_block_steady_ms"], m["p2p_calls_steady"],
                    ("%.4f" % (m["p2p_block_steady_ms"] / m["p2p_calls_steady"]))
                    if m["p2p_calls_steady"] else "-"))
        if m.get("iterations"):
            L.append("| 分段·每迭代 | 集合通信 %s / 点对点 %s rank·ms |"
                     % (("%.4f" % m["mutual_steady_per_iter"]) if m["mutual_steady_per_iter"] is not None else "-",
                        ("%.4f" % m["p2p_steady_per_iter"]) if m["p2p_steady_per_iter"] is not None else "-"))
    L.append("")
    L.append("> 判定阈值（可在命令行调整）：平均同时在等待的 rank 占比 < %.0f%% 视为无实际影响；"
             "到齐前等待占全部等待 ≥ %.0f%% 且最后到达者分散在多个进程之间，判定为相互等待。"
             % (m["thresholds"]["wait_share_pct"], m["thresholds"]["mutual_share_pct"]))
    L.append("")

    L.append("## 1. 等待热点（按同步类型；**仅集合通信**）\n")
    if m["wait_by_kind_ms"]:
        tot = sum(m["wait_by_kind_ms"].values()) or 1.0
        L.append("| 同步类型 | 等待累计 (rank·ms) | 占等待总量 |")
        L.append("|---|---|---|")
        for k, v in m["wait_by_kind_ms"].items():
            L.append("| %s | %.2f | %.2f%% |" % (k, v, v / tot * 100.0))
    else:
        L.append("未采集到同步点。")
    L.append("")
    L.append("> 点对点等待（`recv`/`sendrecv`/`other`）**不参与**这一节：它没有\"到齐\"语义，"
             "按集合通信模型算会得出荒谬的数（实测把 8492 rank·ms 算成 1801 万）。"
             "点对点见 §1.2。\n")

    # 真实算例动辄几千个同步点, 逐条明细读不出重点 -> 必须按调用点汇总
    L.append("### 1.1 按调用点(tag)聚合（看等待集中在哪个调用点）\n")
    if a.get("tag_summary"):
        tot_m = m["mutual_wait_ms_total"] or 1.0
        L.append("| tag | 同步类型 | 出现次数 | 参与的 rank 数 | 相互等待 (rank·ms) | 原语开销 (rank·ms) | 占相互等待 | 最大到达离散 (ms) |")
        L.append("|---|---|---|---|---|---|---|---|")
        for t in a["tag_summary"][:40]:
            L.append("| %s | %s | %d | %s | %.2f | %.2f | %.2f%% | %.2f |" % (
                t["tag"], t["kind"], t["count"],
                ",".join(str(x) for x in t["n_ranks"]),
                t["mutual_wait_ms"], t["post_arrival_ms"],
                t["mutual_wait_ms"] / tot_m * 100.0, t["max_skew_ms"]))
        if len(a["tag_summary"]) > 40:
            L.append("| ... | | | | | | | 共 %d 个 tag |" % len(a["tag_summary"]))
        L.append("")
        L.append("> 「参与的 rank 数」只有 1 个时表示该同步点只有单进程参与（无等待可言）。"
                 "按 pcom 分组的算例，同一个调用点在不同组里会出现不同的 tag。\n")
    else:
        L.append("无数据。\n")

    # ---- 1.2 点对点等待(边界交换) ----
    L.append("### 1.2 点对点等待（界面 / 边界交换的 `MPI_Wait`）\n")
    p2p = a.get("p2p_summary") or []
    if p2p:
        L.append("| tag | 类型 | 调用次数 | 覆盖进程 | 阻塞合计 (rank·ms) | 平均每次 (ms) | 单次最长 (ms) | 跨进程差 (ms) |")
        L.append("|---|---|---|---|---|---|---|---|")
        for t in p2p[:30]:
            L.append("| %s | %s | %d | %d | %.2f | %.4f | %.2f | %.2f |"
                     % (t["tag"], t["kind"], t["calls"], t["ranks"],
                        t["block_ms"], t["mean_ms"], t["max_ms"], t["spread_ms"]))
        if len(p2p) > 30:
            L.append("| ... | | | | | | | 共 %d 个调用点 |" % len(p2p))
        L.append("")
        L.append("| rank | 点对点阻塞合计 (ms) | 占其墙钟 |")
        L.append("|---|---|---|")
        for rk, v in sorted(m["p2p_per_rank_ms"].items(), key=lambda kv: int(kv[0])):
            w = m["wall_ms_per_rank"].get(rk, 0) or 0
            L.append("| %s | %.2f | %.2f%% |" % (rk, v, (v / w * 100.0) if w else 0.0))
        L.append("")
        L.append("> 读法：**平均每次** 越接近 0，说明数据基本都提前到了、Wait 几乎不阻塞；"
                 "**跨进程差** 越大，说明有的进程在边界上等得明显更多（典型成因是各进程负载不均、"
                 "或某个进程负责的域更大）。这一类等待是均衡化最直接的作用面。\n")
    else:
        L.append("未采集到点对点 Wait（可能是没有插桩，或本次运行没走界面交换）。\n")

    # ---- 1.3 启动一次性 vs 稳态 ----
    if m.get("skip_ms"):
        L.append("### 1.3 分段：启动一次性 vs 稳态（跳过前 %d ms）\n" % m["skip_ms"])
        L.append("| 分段 | 集合通信相互等待 (rank·ms) | 点对点等待 (rank·ms) | 点对点调用次数 |")
        L.append("|---|---|---|---|")
        L.append("| 启动段（前 %d ms） | %.2f | %.2f | %d |"
                 % (m["skip_ms"], m["mutual_wait_startup_ms"],
                    m["p2p_block_ms_total"] - m["p2p_block_steady_ms"],
                    m["p2p_calls_total"] - m["p2p_calls_steady"]))
        L.append("| **稳态段** | **%.2f** | **%.2f** | %d |"
                 % (m["mutual_wait_steady_ms"], m["p2p_block_steady_ms"],
                    m["p2p_calls_steady"]))
        L.append("")
        L.append("> 启动段是一次性成本（读网格、划分、应用均衡化计划），**不应与稳态混在一起比较**。\n")

    L.append("## 2. 等待责任归属（同步点上最后到达的 rank）\n")
    if m["last_arriver_counts"]:
        L.append("| rank | 最后到达次数 | 该 rank 自身等待 (ms) | 占其墙钟 |")
        L.append("|---|---|---|---|")
        for k, v in m["last_arriver_counts"].items():
            L.append("| %s | %d | %s | %s%% |" % (
                k, v, m["per_rank_wait_ms"].get(k, "-"),
                m["per_rank_wait_share_pct"].get(k, "-")))
        L.append("")
        L.append("> 「最后到达」= 该次同步里最后一个进入集合通信的 rank，"
                 "其余 rank 的到齐前等待主要由它造成。\n")
    else:
        L.append("无数据。\n")

    L.append("## 3. 逐同步点明细\n")
    if a["sync_points"]:
        L.append("| # | 同步类型 | tag | 第几次 | 参与 rank | 到达离散 (ms) | 到齐前等待 (rank·ms) | 到齐后耗时 (rank·ms) | 最后到达 |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for i, s in enumerate(a["sync_points"][:80], 1):
            L.append("| %d | %s | %s | %d | %d | %.2f | %.2f | %.2f | %s |" % (
                i, s["kind"], s["tag"], s["occurrence"] + 1, s["n_ranks"],
                s["arrival_skew_ms"], s["mutual_wait_ms"], s["post_arrival_ms"],
                ",".join(str(x) for x in s["last_arriver"])))
        if len(a["sync_points"]) > 80:
            L.append("| ... | | | | | | | | 共 %d 条 |" % len(a["sync_points"]))
    else:
        L.append("未采集到同步点。")
    L.append("")

    L.append("## 4. 各 rank 等待分布\n")
    L.append("| rank | 墙钟 (ms) | 等待 (ms) | 等待占其墙钟 |")
    L.append("|---|---|---|---|")
    for r in sorted(m["per_rank_wait_ms"], key=lambda x: int(x)):
        L.append("| %s | %s | %s | %s%% |" % (
            r, m["wall_ms_per_rank"].get(r, "-"), m["per_rank_wait_ms"][r],
            m["per_rank_wait_share_pct"][r]))
    L.append("")

    L.append("## 5. 数据可靠性\n")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    L.append("| 事件日志非法行 | %d |" % len(a["parse_errors"]))
    L.append("| 采集是否被事件数上限截断 | %s |"
             % ("是，每个 rank 采集到 %s 个同步点后停止（LB_MAX_SYNC）" % m["truncated_at_sync"]
                if m.get("truncated_at_sync") else "否（采集覆盖整个运行）"))
    L.append("| 声明进程数 / 实收集到 | %s / %d |"
             % (a.get("declared_nranks") or "-", m["nranks"]))
    L.append("| 缺失的 rank 日志 | %s |"
             % (",".join(str(x) for x in a.get("missing_ranks") or []) or "无"))
    L.append("| 未正常收尾的 rank（缺 finalize） | %s |"
             % (",".join(str(x) for x in a["missing_finalize"]) or "无"))
    L.append("| 未配对的 enter（有进无出） | %s |"
             % (",".join("%s:%d" % (u["rank"], u["n"]) for u in a["unclosed_enters"]) or "无"))
    L.append("| 各 rank 调用次数不一致的同步点 | %d 处 |" % len(a["unmatched_intervals"]))
    L.append("")
    L.append("---")
    L.append("数据来源：各 rank 的 `events.rank*.jsonl`（一行一事件，实时落盘）。")
    L.append("等待时长定义：进入集合通信到退出之间的时长；"
             "「到齐前等待」= 最晚进入者时刻 − 本 rank 进入时刻；"
             "「到齐后耗时」= 本 rank 退出时刻 − 最晚进入者时刻。二者之和严格等于等待时长。")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--wait-threshold", type=float, default=5.0,
                    help="平均同时在等待的 rank 占比阈值(%%)，低于它视为无实际影响")
    ap.add_argument("--mutual-threshold", type=float, default=50.0,
                    help="到齐前等待占全部等待的比例阈值(%%)，达到即判定存在进程间等待")
    ap.add_argument("--single-rank-threshold", type=float, default=60.0,
                    help="单一 rank 占相互等待的比例阈值(%%)，超过它判为单向等待")
    ap.add_argument("--skip-ms", type=int, default=0,
                    help="把开头这么多毫秒当作'启动一次性成本'单列，其余算稳态(0=不分段)")
    ap.add_argument("--iterations", type=int, default=0,
                    help="本次运行完成的迭代数，用来把等待归一化成'每迭代'(从运行日志的 Iteration 行取)")
    args = ap.parse_args()

    a = analyze(args.run_dir, args.wait_threshold, args.mutual_threshold,
                args.single_rank_threshold, args.skip_ms, args.iterations)

    # newline="\n"：Windows 上默认会把 \n 翻成 \r\n，而 .gitattributes 又把入库内容归一化成 LF，
    # 结果是磁盘上的文件与仓库里的文件字节不一致、git diff 永远显示"改过"。统一按 LF 写。
    with open(os.path.join(args.run_dir, "analysis.json"), "w", encoding="utf-8",
              newline="\n") as f:
        json.dump(a, f, ensure_ascii=False, indent=2)
    rep = render_report(a)
    with open(os.path.join(args.run_dir, "report.md"), "w", encoding="utf-8",
              newline="\n") as f:
        f.write(rep)

    if args.json:
        print(json.dumps({"scenario": a["scenario"], "verdict": a["verdict"],
                          "verdict_code": a["verdict_code"],
                          "mutual_wait": a["mutual_wait"],
                          "metrics": a["metrics"]}, ensure_ascii=False))
    elif not args.quiet:
        print(rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
