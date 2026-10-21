#!/bin/bash
# usage: run_all.sh  (ONLY=<case-prefix> MODES="run|hook" to filter)
cd "$(dirname "$0")"; rm -rf work; mkdir work
bash parity.sh > parity.out 2>&1; bash argtypos.sh > argtypos.out 2>&1
echo "done: $PWD/parity.out $PWD/argtypos.out"
