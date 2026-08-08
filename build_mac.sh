#!/bin/bash
# ============================================
#  PassPoSys - macOS Build Script
#  (Apple Silicon / arm64 -- run this on the
#   GitHub Actions macos-14 runner, or on a
#   real Mac if you ever get one)
#
#  Mirrors build.bat, translated for macOS:
#    - PyInstaller path separator is ":" not ";"
#    - No .ico icon -- macOS wants .icns
#    - --noconsole -> --windowed
#    - "NUL" log device doesn't exist -> not applicable here,
#      that's a launch.py runtime concern, not a build concern
# ============================================
set -euo pipefail
cd "$(dirname "$0")"

echo "============================================"
echo " PassPoSys - macOS Build (Apple Silicon)"
echo "============================================"

if [ ! -d venv ]; then
    echo "[ERROR] venv folder not found. Create it first:"
    echo "  python3 -m venv venv"
    exit 1
fi

source venv/bin/activate

# --- Force Playwright's browser to install INSIDE the playwright package
#     folder, same reasoning as build.bat: so --collect-all playwright
#     actually bundles the browser binary into the app bundle.
export PLAYWRIGHT_BROWSERS_PATH=0

echo "Verifying Playwright browser (chromium headless shell)..."
python -m playwright install chromium

# --- Sanity check: warn if both opencv variants are present ---
if pip list 2>/dev/null | grep -qi "^opencv-python "; then
    echo "[WARNING] opencv-python (non-headless) is installed alongside headless."
    echo "Fix with: pip uninstall opencv-python -y"
fi

# --- Encrypt environment files (same encrypted .env/.token as Windows --
#     these are just Fernet-encrypted bytes, platform independent, so the
#     SAME env.enc / token.enc / passposys.key from Windows can be reused
#     unchanged. Re-run this step only if you've edited .env/token.json.)
# --- NOTE: encrypt_env.py / encrypt_token.py are intentionally NOT run
#     here. env.enc / token.enc are already committed to the repo,
#     produced once on your Windows machine, and are reused unchanged --
#     they're just Fernet-encrypted bytes, not platform-specific. There is
#     no .env or token.json on the CI runner (correctly -- they hold
#     plaintext secrets and are gitignored), so re-running these would do
#     nothing useful here. If you ever need to change a secret value,
#     update .env locally on your Windows machine, re-run encrypt_env.py
#     there, and commit the resulting env.enc.
mkdir -p static

# --- Clean previous build ---
rm -rf build dist app.spec
echo "Cleaning __pycache__ / .pyc files..."
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete

# --- Locate the installed Playwright browser folder (version-dependent
#     folder name, must be discovered, not hardcoded) ---
PW_BROWSERS_DIR=$(python -c "import playwright, os; print(os.path.join(os.path.dirname(playwright.__file__), 'driver', 'package', '.local-browsers'))")

if [ ! -d "$PW_BROWSERS_DIR" ]; then
    echo "[ERROR] Playwright browsers folder not found at:"
    echo "  $PW_BROWSERS_DIR"
    echo "Run 'playwright install chromium' (with PLAYWRIGHT_BROWSERS_PATH=0) first."
    exit 1
fi

echo "Building app with PyInstaller..."

ICON_ARGS=()
if [ -f "app_icon.icns" ]; then
    ICON_ARGS=(--icon=app_icon.icns)
else
    echo "[NOTE] app_icon.icns not found -- building without a custom icon."
    echo "       Convert app_icon.ico to .icns first if you want one (see README)."
fi

pyinstaller --onedir --windowed --name "PassPoSys" "${ICON_ARGS[@]}" \
    --collect-all cv2 \
    --collect-all playwright \
    --add-data "$PW_BROWSERS_DIR:playwright/driver/package/.local-browsers" \
    --collect-all cryptography \
    --add-data "templates:templates" \
    --add-data "static:static" \
    --add-data "private:private" \
    --add-data "arial.ttf:." \
    --add-data "arialbd.ttf:." \
    --add-data "haarcascade_frontalface_default.xml:cv2/data" \
    --add-data "token.enc:." \
    --add-data "env.enc:." \
    --hidden-import mysql.connector.locales.eng.client_error \
    --collect-all google_auth_oauthlib \
    --collect-all googleapiclient \
    --hidden-import google.oauth2.credentials \
    --hidden-import google.auth.transport.requests \
    --collect-all apscheduler \
    --collect-all openpyxl \
    --hidden-import pypdf \
    --hidden-import ntplib \
    launch.py

echo
echo "============================================"
echo " Build successful! Check the 'dist' folder."
echo
echo " REMINDER: Copy passposys.key next to the"
echo " app bundle on every machine before running it."
echo "============================================"
