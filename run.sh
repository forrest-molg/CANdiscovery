#!/bin/bash
# CANdiscovery Webapp Launcher
# Usage: ./run.sh        — start in background  (port 5001)
#        ./run.sh --fg   — start in foreground

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "[run.sh] Stopping any running CANdiscovery instance..."
pkill -f "python.*CANdiscovery.*app\.py" 2>/dev/null || true
sleep 0.3

echo "[run.sh] Starting CANdiscovery → http://$(hostname -I | awk '{print $1}'):5001"
echo "[run.sh] Logs will be saved to: $SCRIPT_DIR/CANlogs/"

mkdir -p CANlogs

if [[ "$1" == "--fg" ]]; then
    python webapp/app.py
else
    python webapp/app.py &
    echo "[run.sh] Started in background (PID $!)"
    echo "[run.sh] To stop: pkill -f 'python.*CANdiscovery.*app.py'"
fi
