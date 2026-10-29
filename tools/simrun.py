#!/usr/bin/env python3
"""simrun.py - 无 MPI 环境下的多进程仿真启动器 (等价于 mpirun -np N)

为什么需要它:
  1. 探针与场景代码可以在开发机/CI 上直接验证, 不必占用集群排队;
  2. 死锁场景必须有"硬超时兜底"——真 MPI 卡住时 mpirun 自己也不会退出,
     这个启动器会在 等待超时 + 余量 后强制收割, 保证 CI 不会挂住。

用法:
  python tools/simrun.py <scenario> [iters] [--nranks 4] [--timeout-ms 6000]

产物: runs/<scenario>_<时间戳>/ 下每个 rank 的 events.rankN.jsonl 与 hb.rankN.bin,
      外加 run.json (退出码/是否被强杀), 供 lb_analyze.py 消费。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def find_binary(explicit=None):
    cands = [explicit] if explicit else []
    cands += [
        os.path.join(ROOT, "build", "lb_scenarios.exe"),
        os.path.join(ROOT, "build", "lb_scenarios"),
        os.path.join(ROOT, "build", "lb_scenarios_sim.exe"),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    sys.exit("找不到场景可执行文件; 先运行 build.ps1 / make 编译")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario")
    ap.add_argument("iters", nargs="?", type=int, default=6)
    ap.add_argument("--nranks", type=int, default=4)
    ap.add_argument("--timeout-ms", type=int, default=6000,
                    help="每个 rank 的本地等待超时(超时后自己退出并落盘证据)")
    ap.add_argument("--stall-ms", type=int, default=1500,
                    help="看门狗判定'卡住'的阈值, 必须显著小于 timeout-ms")
    ap.add_argument("--binary", default=None)
    ap.add_argument("--outdir", default=None)
    args = ap.parse_args()

    binary = find_binary(args.binary)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = args.outdir or os.path.join(ROOT, "runs", f"{args.scenario}_{stamp}")
    os.makedirs(run_dir, exist_ok=True)

    procs, logs, t0 = [], [], time.time()
    for r in range(args.nranks):
        env = dict(os.environ)
        env.update({
            "LB_RANK": str(r),
            "LB_NRANKS": str(args.nranks),
            "LB_RUN_DIR": run_dir,
            "LB_WAIT_TIMEOUT_MS": str(args.timeout_ms),
            "LB_STALL_MS": str(args.stall_ms),
            "LB_HB_MS": "100",
            # 默认不主动 abort: 要让看门狗先观测到互等并留下证据, 再由本地超时收尾
            "LB_ABORT_ON_DEADLOCK": env.get("LB_ABORT_ON_DEADLOCK", "0"),
        })
        # 输出重定向到文件而不是管道: 沙箱下管道可能被拒, 且文件本身就是证据
        lf = open(os.path.join(run_dir, f"stdout.rank{r}.log"), "wb")
        logs.append(lf)
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        procs.append(subprocess.Popen([binary, args.scenario, str(args.iters)],
                                      env=env, stdout=lf, stderr=subprocess.STDOUT, **kw))

    # 硬超时: 本地等待超时 + 宽限
    hard = t0 + (args.timeout_ms / 1000.0) + 15.0
    killed = False
    while any(p.poll() is None for p in procs):
        if time.time() > hard:
            killed = True
            for p in procs:
                if p.poll() is None:
                    p.kill()
            break
        time.sleep(0.2)

    codes = [p.wait() for p in procs]
    for lf in logs:
        lf.close()

    meta = {
        "scenario": args.scenario,
        "nranks": args.nranks,
        "iters": args.iters,
        "run_dir": run_dir,
        "binary": binary,
        "exit_codes": codes,
        "hard_killed": killed,
        "wall_s": round(time.time() - t0, 3),
        "wait_timeout_ms": args.timeout_ms,
        "stall_ms": args.stall_ms,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }
    with open(os.path.join(run_dir, "run.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    bad = [i for i, c in enumerate(codes) if c != 0]
    print(f"[simrun] {args.scenario}: nranks={args.nranks} wall={meta['wall_s']}s")
    print(f"[simrun] 退出码: {codes}" + (f"  (非零 rank: {bad})" if bad else ""))
    if killed:
        print("[simrun] 警告: 有 rank 未在本地超时内退出, 已被强制收割")
    print(f"[simrun] 运行目录: {run_dir}")
    return 7 if (bad or killed) else 0


if __name__ == "__main__":
    sys.exit(main())
