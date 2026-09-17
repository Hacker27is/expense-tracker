#!/bin/bash
# Double-click to open Expense Tracker (starts it if it is not running).
cd "$(dirname "$0")"
if curl -s -o /dev/null --max-time 2 http://127.0.0.1:8765/api/settings; then
  open http://127.0.0.1:8765
  exit 0
fi
if [ ! -x .venv/bin/python ]; then
  echo "First run: installing dependencies…"
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt || exit 1
fi
echo "Expense Tracker is running. Keep this window open while you need the bot."
exec .venv/bin/python app.py
