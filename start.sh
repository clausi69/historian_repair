#!/bin/sh
# Start CCU-Historian repair tool (installs JPype1 on first run)
cd "$(dirname "$0")"
python3 -c "import jpype" 2>/dev/null || python3 -m pip install --user -r requirements.txt
exec python3 -m historian_repair "$@"
