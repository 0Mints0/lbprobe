#!/bin/sh
# run_all_sim.sh - 本机/CI 仿真跑套 (无 MPI, 用文件模拟集合通信)
#
# 目的: 在没有集群的机器上验证测量链路本身(探针 -> 事件日志 -> 分析器 -> 报告)
#       是否可用, 并对管线做几项自检。自检对象是管线, 不是被测代码。
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

NP=${NP:-8}
ITERS=${ITERS:-8}
PY=${PY:-python3}
SIMRUN="$PY tools/simrun.py"

command -v "$PY" >/dev/null 2>&1 || { echo "缺少 python3"; exit 2; }

[ -x "$ROOT/build/lb_scenarios" ] || {
  echo "先编译仿真版本: sh tools/build_sim.sh"; exit 2; }

for name in sim_realistic sim_uniform sim_gpu_sync sim_empty_rank; do
  echo ""
  echo "== measure: $name =="
  $SIMRUN "$name" "$ITERS" --nranks "$NP" --timeout-ms 15000
  d=$(ls -dt "$ROOT"/runs/${name}_* 2>/dev/null | head -1)
  $PY tools/lb_analyze.py "$d" --quiet
  echo "报告: $d/report.md"
done

echo ""
echo "================ 测量汇总 ================"
printf '%-18s %6s %16s %16s %12s %s\n' 算例 进程数 '相互等待rank·ms' '同步等待rank·ms' '平均在等' 判定
fail=0
for f in "$ROOT"/runs/*/analysis.json; do
  [ -f "$f" ] || continue
  line=$($PY -c "
import json,sys
a=json.load(open(sys.argv[1],encoding='utf-8')); m=a['metrics']
ok = (m['parse_errors']==0) and abs(m['wait_ms_total']-
      (m['mutual_wait_ms_total']+m['post_arrival_ms_total']))<0.05
print('%-18s %6d %16.1f %16.1f %12.3f %s%s' % (a['scenario'], m['nranks'],
      m['mutual_wait_ms_total'], m['wait_ms_total'], m['mean_waiting_ranks'],
      a['verdict'], '' if ok else '  [管线自检失败]'))
" "$f")
  echo "$line"
  case "$line" in *"管线自检失败"*) fail=1 ;; esac
done
echo ""
[ $fail -eq 0 ] && echo "管线自检: 通过" || echo "管线自检: 有失败项"
exit $fail
