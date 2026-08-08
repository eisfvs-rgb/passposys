@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  PassPoSys - OneFile Rebuild Script
echo  (Dynamic MEI Extraction Mode)
echo ============================================

REM --- Make sure venv exists ---
if not exist venv (
    echo [ERROR] venv folder not found.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

REM --- Force Playwright's browser to install INSIDE the playwright package
REM     folder (not the OS-default %LOCALAPPDATA%\ms-playwright\ cache), so
REM     that "--collect-all playwright" below actually bundles the browser
REM     binary into the exe. Without this, the exe ships the Python driver
REM     but not chrome-headless-shell.exe, and it fails at runtime with:
REM       "BrowserType.launch: Executable doesn't exist at ...\_MEIxxxx\
REM        playwright\driver\package\.local-browsers\..."
set "PLAYWRIGHT_BROWSERS_PATH=0"

REM --- Make sure the chromium headless shell is actually present before
REM     building -- if it's missing/corrupt, fail the build now instead of
REM     shipping a broken exe. This also (re)installs it if a Playwright
REM     version bump changed the required browser build number.
echo Verifying Playwright browser (chromium headless shell)...
python -m playwright install chromium
if errorlevel 1 (
    echo [ERROR] "playwright install chromium" failed. Fix that before building.
    pause
    exit /b 1
)

REM --- Quick sanity check: warn if both opencv variants are present ---
pip list 2>nul | findstr /i "opencv-python " >nul
if not errorlevel 1 (
    echo  [WARNING] opencv-python non-headless is installed alongside headless.
    echo  Fix with: pip uninstall opencv-python -y
)

REM --- Clean up the corrupted static temp folder from previous crashes ---
if exist "%TEMP%\357c57df-316a-46e8-8621-3f774af27563" (
    echo Cleaning corrupted static temp directory...
    rmdir /s /q "%TEMP%\357c57df-316a-46e8-8621-3f774af27563"
)

REM --- Encrypt environment files ---
if exist .env ( python encrypt_env.py )
if exist token.json ( python encrypt_token.py )
if not exist static mkdir static

REM --- Clean previous build ---
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist app.spec del app.spec
echo Cleaning __pycache__ / .pyc files...
for /d /r %%d in (__pycache__) do ( if exist "%%d" rmdir /s /q "%%d" )
del /s /q *.pyc >nul 2>&1

REM --- Locate the installed browser folder so we can explicitly bundle it.
REM     Its name (e.g. chromium_headless_shell-1228) changes whenever the
REM     Playwright version bumps, so this must be discovered, not hardcoded.
for /f "delims=" %%p in ('python -c "import playwright, os; print(os.path.join(os.path.dirname(playwright.__file__), 'driver', 'package', '.local-browsers'))"') do set "PW_BROWSERS_DIR=%%p"

if not exist "%PW_BROWSERS_DIR%" (
    echo [ERROR] Playwright browsers folder not found at:
    echo   %PW_BROWSERS_DIR%
    echo Run "playwright install chromium" ^(with PLAYWRIGHT_BROWSERS_PATH=0^) first.
    pause
    exit /b 1
)

echo Building exe with PyInstaller...
pyinstaller --onedir --noconsole --name "PasspoSys v2.1" --icon=app_icon.ico ^
    --splash "splash.png" ^
    --collect-all cv2 ^
    --collect-all playwright ^
    --add-data "%PW_BROWSERS_DIR%;playwright\driver\package\.local-browsers" ^
    --collect-all cryptography ^
    --add-data "templates;templates" ^
    --add-data "static;static" ^
    --add-data "private;private" ^
    --add-data "arial.ttf;." ^
    --add-data "arialbd.ttf;." ^
    --add-data "haarcascade_frontalface_default.xml;cv2/data" ^
    --add-data "token.enc;." ^
    --add-data "env.enc;." ^
    --hidden-import mysql.connector.locales.eng.client_error ^
    --collect-all google_auth_oauthlib ^
    --collect-all googleapiclient ^
    --hidden-import google.oauth2.credentials ^
    --hidden-import google.auth.transport.requests ^
    --collect-all apscheduler ^
    --collect-all openpyxl ^
    --hidden-import pypdf ^
    --hidden-import ntplib ^
    launch.py

if errorlevel 1 (
    echo Build failed.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Build successful! Check the 'dist' folder.
echo.
echo  REMINDER: Copy passposys.key next to the
echo  exe on every machine before running it.
echo ============================================
pause