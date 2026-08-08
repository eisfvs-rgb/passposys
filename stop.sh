#!/bin/bash
# ============================================
#  PassPoSys Portable Stopper (macOS)
#  Mirrors stop.bat
# ============================================
cd "$(dirname "$0")"
MYSQL_BIN="./mysql/bin"

echo "Stopping PassPoSys app..."
pkill -f "PassPoSys" 2>/dev/null || true
echo "App stopped."

echo "Stopping MySQL..."
if [ -x "$MYSQL_BIN/mysqladmin" ]; then
    "$MYSQL_BIN/mysqladmin" --host=127.0.0.1 --port=3307 -u root shutdown 2>/dev/null || true
fi
echo "MySQL stopped."

echo
echo "Everything is shut down. Goodbye!"
