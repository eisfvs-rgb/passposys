#!/bin/bash
# ============================================
#  PassPoSys Portable Launcher (macOS)
#  Mirrors start.bat, but launch.py already
#  handles MySQL init/start/setup internally
#  -- this script just launches the app.
# ============================================
set -e
cd "$(dirname "$0")"

APP_NAME="PassPoSys.app"          # adjust if your PyInstaller --name differs
APP_BIN="PassPoSys.app/Contents/MacOS/PassPoSys"

echo "============================================"
echo " PassPoSys Portable Launcher (macOS)"
echo "============================================"

if [ ! -e "$APP_BIN" ] && [ ! -e "PassPoSys" ]; then
    echo "ERROR: Could not find the PassPoSys app binary next to this script."
    echo "Expected: $APP_BIN  (or a plain 'PassPoSys' onedir binary)"
    read -p "Press Enter to exit..."
    exit 1
fi

# Portable MySQL binaries need the execute bit set the first time they're
# extracted on macOS (launch.py also does this defensively at runtime,
# this is just a belt-and-suspenders check so `start.sh` alone still works).
if [ -d "mysql/bin" ]; then
    chmod +x mysql/bin/mysqld mysql/bin/mysqladmin mysql/bin/mysql mysql/bin/mysqldump 2>/dev/null || true
fi

echo "Starting PassPoSys..."
echo "Open browser to: http://127.0.0.1:9000"
echo

if [ -e "$APP_BIN" ]; then
    open "$APP_NAME"
else
    ./PassPoSys &
fi

echo "App launched! Run stop.sh to shut everything down."
