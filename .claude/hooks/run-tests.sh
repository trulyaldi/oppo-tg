#!/bin/bash
# Usage: run-tests.sh edit|stop
# edit: PostToolUse after Edit/Write; run tests only when a Python file changed, report failures to Claude.
# stop: Stop hook; block completion (exit 2) while tests fail. Gives up after 5 consecutive blocks to avoid loops.
mode="$1"
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
input=$(cat)

PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python

# Nothing to test yet.
ls tests/test_*.py >/dev/null 2>&1 || { rm -f .claude/.stop-block-count; exit 0; }

if [ "$mode" = "edit" ]; then
  file=$(printf '%s' "$input" | $PY -c 'import sys,json; print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))' 2>/dev/null)
  case "$file" in *.py) ;; *) exit 0 ;; esac
fi

out=$($PY -m pytest -q -x 2>&1)
rc=$?

if [ $rc -eq 0 ]; then
  rm -f .claude/.stop-block-count
  exit 0
fi

if [ "$mode" = "stop" ]; then
  n=$(cat .claude/.stop-block-count 2>/dev/null || echo 0)
  n=$((n + 1))
  if [ "$n" -gt 5 ]; then
    rm -f .claude/.stop-block-count
    exit 0
  fi
  echo "$n" > .claude/.stop-block-count
fi

printf 'Tests are failing. Fix them before continuing.\n%s\n' "$(printf '%s' "$out" | tail -n 30)" >&2
exit 2
