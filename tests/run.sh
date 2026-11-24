#!/usr/bin/env bash
# Run every test shipped in this directory: each python test file on its own
# (python3 -m pytest -q <file>), then each shell test (*.sh except this runner).
# Exit status is 0 only when every file passed. Files are run one at a time so
# a crash in one (a Qt test, say) cannot hide the rest.
set -uo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
failed=()
ran=0

shopt -s nullglob
for f in "$here"/test_*.py; do
  ran=$((ran + 1))
  echo "== python3 -m pytest -q $(basename "$f")"
  python3 -m pytest -q "$f" || failed+=("$(basename "$f")")
done
for f in "$here"/*.sh; do
  [[ $(basename "$f") == run.sh ]] && continue
  ran=$((ran + 1))
  echo "== bash $(basename "$f")"
  bash "$f" || failed+=("$(basename "$f")")
done

echo
if (( ${#failed[@]} )); then
  echo "FAILED (${#failed[@]} of $ran): ${failed[*]}"
  exit 1
fi
echo "all $ran test files passed"
