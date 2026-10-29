#!/bin/sh
# run_all.sh - 集群侧测量跑套 (真 MPI)
#
# 只做一件事: 跑算例 -> 生成每个算例的测量报告。不做任何开/关对照。
#
# 用法:
#   NP=64 ITERS=20 sh tools/run_all.sh
#
# 集群上的三条硬约束:
#  1. 事件目录必须放**节点本地盘**(/dev/shm 或 $TMPDIR)。一行一事件的写盘
#     若落在 Lustre/GPFS/NFS 家目录上会拖慢并行文件系统, 反过来干扰测量。
#     探针在 finalize 时会一次性汇集到 LB_COLLECT_DIR 供离线分析。
#  2. 每个算例都用 timeout 包一层。即使预期不会卡住, 也必须有这层兜底,
#     否则一旦异常, 作业会占着机时直到墙钟上限被调度器杀掉。
#  3. 正式测量前先做一次 LB_OFF=1 的空跑, 标定探针自身开销(目标 < 1%)。
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

NP=${NP:-8}
ITERS=${ITERS:-8}
TMO=${TMO:-900}
PY=${PY:-python3}
MPIRUN=${MPIRUN:-mpirun}

command -v "$PY" >/dev/null 2>&1 || { echo "缺少 python3"; exit 2; }
[ -x "$ROOT/build/lb_scenarios" ] || { echo "先 make MPI=1"; exit 2; }

COLLECT_ROOT=${LB_COLLECT_DIR:-$ROOT/runs}
mkdir -p "$COLLECT_ROOT"

if [ -d /dev/shm ] && [ -w /dev/shm ]; then
  LOCAL_BASE=/dev/shm/lbprobe.$$
else
  LOCAL_BASE=${TMPDIR:-/tmp}/lbprobe.$$
fi
mkdir -p "$LOCAL_BASE"
echo "[measure] 本地事件目录: $LOCAL_BASE  ->  汇集目录: $COLLECT_ROOT"
trap 'rm -rf "$LOCAL_BASE"' EXIT

CASES="sim_realistic sim_uniform sim_gpu_sync sim_empty_rank"

for name in $CASES; do
  run_dir="$LOCAL_BASE/${name}"
  coll_dir="$COLLECT_ROOT/${name}_$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$run_dir" "$coll_dir"

  echo ""
  echo "== measure: $name (np=$NP iters=$ITERS) =="
  env LB_RUN_DIR="$run_dir" LB_HB_MS=0 \
      LB_COLLECT_DIR="$coll_dir" \
      timeout -k 5 "$TMO" "$MPIRUN" -np "$NP" ./build/lb_scenarios "$name" "$ITERS"
  rc=$?
  [ $rc -eq 124 ] && echo "[measure] 注意: 被 timeout 强杀, 报告的数据可靠性一节会标注"

  # 兜底: 有些 MPI/文件系统组合下汇集可能没生效, 这里再拷一次
  cp -f "$run_dir"/events.rank*.jsonl "$coll_dir"/ 2>/dev/null || true
  echo "{\"scenario\":\"$name\",\"nranks\":$NP,\"iters\":$ITERS,\"exit\":$rc}" \
      > "$coll_dir/run.json"

  $PY tools/lb_analyze.py "$coll_dir" --quiet
  echo "[$name] 报告: $coll_dir/report.md"
done

echo ""
echo "================ 测量汇总 ================"
printf '%-18s %6s %14s %16s %14s %10s\n' 算例 进程数 相互等待rank·ms 同步等待rank·ms 平均在等rank 判定
for f in "$COLLECT_ROOT"/*/analysis.json; do
  [ -f "$f" ] || continue
  $PY -c "
import json,sys
a=json.load(open(sys.argv[1],encoding='utf-8')); m=a['metrics']
print('%-18s %6d %14.1f %16.1f %14.3f %10s' % (a['scenario'], m['nranks'],
      m['mutual_wait_ms_total'], m['wait_ms_total'],
      m['mean_waiting_ranks'], a['verdict'][:10]))
" "$f"
done
echo ""
echo "报告: $COLLECT_ROOT/<算例>_*/report.md"
