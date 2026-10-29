#!/bin/sh
# build_sim.sh - 编译仿真版本 (无 MPI 机器上用, 仅用于验证测量链路)
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
mkdir -p build
CC=${CC:-gcc}
$CC -O2 -Wall -Wextra -Iinclude -std=c99 -D_POSIX_C_SOURCE=200809L \
    src/lbprobe.c src/lbprobe_sim.c tests/lb_scenarios.c \
    -o build/lb_scenarios -lm
echo "built: build/lb_scenarios (simulation backend)"
