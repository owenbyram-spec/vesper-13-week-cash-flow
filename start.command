#!/bin/sh
cd -- "$(dirname -- "$0")" || exit 1
if command -v python3.12 >/dev/null 2>&1; then
  python3.12 run.py "$@"
else
  python3 run.py "$@"
fi
result=$?
if [ "$result" -ne 0 ]; then
  printf '\nSetup stopped. Review the message above and docs/QUICK-START.md.\n'
  printf 'Press Return to close this window.'
  read -r ignored
fi
exit "$result"
