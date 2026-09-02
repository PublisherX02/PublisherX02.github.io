#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

dashboard_command='python dashboard/app.py'

if command -v x-terminal-emulator >/dev/null 2>&1; then
    x-terminal-emulator -e bash -lc "cd \"$SCRIPT_DIR\" && $dashboard_command; exec bash" &
elif command -v gnome-terminal >/dev/null 2>&1; then
    gnome-terminal --working-directory="$SCRIPT_DIR" -- bash -lc "$dashboard_command; exec bash" &
elif command -v konsole >/dev/null 2>&1; then
    konsole --workdir "$SCRIPT_DIR" -e bash -lc "$dashboard_command; exec bash" &
else
    nohup python dashboard/app.py >dashboard-demo.log 2>&1 &
    dashboard_pid=$!
    echo "Dashboard started in background (PID $dashboard_pid); output: $SCRIPT_DIR/dashboard-demo.log"
fi

python -m run_agent "$@"
