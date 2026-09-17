#!/bin/bash
# Expense Tracker installer for macOS. Also updates an existing install (your data is kept).
#   curl -fsSL https://raw.githubusercontent.com/Hacker27is/expense-tracker/main/install.sh | bash
set -euo pipefail

REPO="Hacker27is/expense-tracker"
DIR="${TRACKER_DIR:-$HOME/ExpenseTracker}"
URL="http://127.0.0.1:${TRACKER_PORT:-8765}"

if [ "$(uname)" != "Darwin" ]; then
  echo "Sorry, Expense Tracker only runs on macOS."
  exit 1
fi

echo "==> Installing Expense Tracker into $DIR"

# uv provides its own Python, so no Xcode or Homebrew is needed.
UV="$(command -v uv || true)"
if [ -z "$UV" ]; then
  if [ ! -x "$HOME/.local/bin/uv" ]; then
    echo "==> Downloading uv (Python manager)"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
  fi
  UV="$HOME/.local/bin/uv"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if [ -n "${TRACKER_SRC:-}" ]; then
  rsync -a --exclude data --exclude .venv --exclude .git --exclude __pycache__ "$TRACKER_SRC/" "$TMP/src/"
else
  echo "==> Downloading the latest version"
  curl -fsSL "https://github.com/$REPO/archive/refs/heads/main.tar.gz" | tar xz -C "$TMP"
  mv "$TMP/expense-tracker-main" "$TMP/src"
fi

mkdir -p "$DIR"
rsync -a --delete --exclude data --exclude .venv "$TMP/src/" "$DIR/"
cd "$DIR"
chmod +x ./*.sh ./*.command

echo "==> Setting up Python"
[ -x .venv/bin/python ] || "$UV" venv --quiet --python 3.12 .venv
"$UV" pip install --quiet --python .venv/bin/python -r requirements.txt

if [ "${TRACKER_NO_AUTOSTART:-}" != "1" ]; then
  ./autostart_on.sh >/dev/null
  printf "==> Starting"
  for _ in $(seq 1 30); do
    if curl -s -o /dev/null --max-time 1 "$URL/api/settings"; then break; fi
    printf "."
    sleep 1
  done
  echo
  open "$URL"
  echo
  echo "Done! Expense Tracker is running and starts automatically when you log in."
  echo "Open it any time at $URL (bookmark it) or double-click start.command in $DIR"
else
  echo "Done (autostart skipped)."
fi
