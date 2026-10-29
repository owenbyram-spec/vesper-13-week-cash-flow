#!/bin/sh
set -eu
cd "$(dirname "$0")"
if command -v python3.12 >/dev/null 2>&1; then
    exec python3.12 start_platform.py "$@"
fi
exec python3 start_platform.py "$@"
