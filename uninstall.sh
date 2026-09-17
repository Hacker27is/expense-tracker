#!/bin/bash
# Stops Expense Tracker and removes it from login items. Your data folder is left untouched.
cd "$(dirname "$0")"
./autostart_off.sh >/dev/null
echo "Expense Tracker is stopped and will no longer start at login."
echo "Your receipts and records are still in: $(pwd)/data"
echo "To remove everything, drag the folder $(pwd) to the Trash."
