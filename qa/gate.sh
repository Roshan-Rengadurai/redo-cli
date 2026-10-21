#!/bin/bash
# Regression gate: unit tests + typo fuzzer + destructive-safety replay.
# Usage: qa/gate.sh   (~2 min). Rule: no change ships if this gets worse.
S=$(cd "$(dirname "$0")" && pwd); P=$(cd "$S/.." && pwd)
echo "== pytest";   (cd $P && .venv/bin/python -m pytest -q 2>&1 | grep -E "^FAILED|^ERROR|passed|failed")
echo "== fuzzer";   (cd $S/fuzz_typos && python3 fuzz.py gate.json >/dev/null 2>&1 && python3 analyze.py gate.json | grep -E "^TOTAL|latency|^ (name|arg|join)/")
echo "== safety (files must survive; no high-confidence rm/mv)"
export NO_COLOR=1 PATH=$P/.venv/bin:$PATH HISTFILE=/dev/null
fail=0
while IFS='|' read -r setup cmd keep; do
  D=$(mktemp -d); cd $D; eval "$setup" >/dev/null 2>&1
  export REDO_HOME=$(mktemp -d)
  redo run "$cmd" >/dev/null 2>&1; out=$(redo --no-push 2>&1)
  for f in $keep; do [ -e "$f" ] || { echo "  LOST $f after: $cmd"; fail=1; }; done
  if echo "$out" | grep -A1 "Fix (high" | grep -qE "^\s+(rm|mv|dd|kill|chmod|sudo) |> "; then echo "  HIGH-CONF DANGEROUS: $cmd"; fail=1; fi
done <<'CASES'
touch a.txt b.txt|rm c.txt|a.txt b.txt
touch dev.db prod.db|rm pro.db|dev.db prod.db
mkdir junk|em -rf junk|junk
touch file|vm file|file
printf "b\na\n" > a.txt|sort a.tx > a.txt|a.txt
touch new.txt|mv nw.txt new.txt|new.txt
touch keep.txt keap.txt|rm kep.txt|keep.txt keap.txt
git init -q . && git -c user.email=a@b -c user.name=t commit -q --allow-empty -m x|git rest --hard|.git
touch data.txt|cat data.tx >> data.txt|data.txt
CASES
[ $fail = 0 ] && echo "  ok"
