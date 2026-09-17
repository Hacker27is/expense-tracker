#!/bin/bash
# Stops the background tracker and disables autostart. Data is not touched.
LABEL="local.expense-tracker"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Autostart disabled."
