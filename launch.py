import os
import sys
import threading
import webbrowser
import time
import subprocess
import socket
import shutil, glob, tempfile
import urllib.request
import urllib.error
import logging
import traceback
import datetime

# ==========================================
# LOGGING SETUP (must happen BEFORE stdout/stderr
# are redirected to devnull below, and must catch
# any exception that happens anywhere in this script,
# including ones that would otherwise only show up as
# a bare "Failed to execute script" popup with no detail).
# ==========================================
if getattr(sys, 'frozen', False):
    _LOG_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _LOG_DIR = os.path.dirname(os.path.abspath(__file__))

LOG_FILE = os.path.join(_LOG_DIR, 'passposys.log')

import logging.handlers

logger = logging.getLogger('passposys')
logger.setLevel(logging.DEBUG)

# File logging writes to passposys.log next to the app so unhandled
# errors (like the reparse 500) always leave a record to diagnose from.
# Set PASSPOSYS_LOG=0 to disable and go back to no file output.
if os.environ.get("PASSPOSYS_LOG", "1") != "0":
    _file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'
    )
    _file_handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
    ))
    logger.addHandler(_file_handler)
else:
    logger.addHandler(logging.NullHandler())

logger.info("=" * 60)
logger.info("PasspoSys starting up (PID %s)", os.getpid())
logger.info("Python: %s", sys.version.replace('\n', ' '))
logger.info("Frozen (exe build): %s", getattr(sys, 'frozen', False))
logger.info("Executable: %s", sys.executable)

def _log_uncaught_exception(exc_type, exc_value, exc_tb):
    """Catch ANY exception that would otherwise crash silently or only
    show a bare popup, and write the full traceback to passposys.log."""
    logger.critical(
        "UNCAUGHT EXCEPTION - app is about to crash:\n%s",
        ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
    )
    # Still let the default handler run so the normal popup/console
    # behavior is unchanged - we're only adding a log record, not
    # hiding the failure.
    sys.__excepthook__(exc_type, exc_value, exc_tb)

sys.excepthook = _log_uncaught_exception

# ==========================================
# THE GLOBAL FIX: MUTE ALL LOGS AND EMOJIS
# ==========================================
# NOTE: print() output is now silenced by design (stdout/stderr -> devnull),
# but nothing important is lost: every print() call below also has an
# equivalent logger.info()/logger.error() call that goes to passposys.log.
try:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8', errors='ignore')
    sys.stderr = open(os.devnull, 'w', encoding='utf-8', errors='ignore')
except Exception:
    pass

PORT = 9000
BIND_HOST = '0.0.0.0'   # what the server binds to (allows LAN access)
HOST = '127.0.0.1'      # what THIS process uses to talk to itself (self-check, browser-open)

def app_already_running():
    """
    Check if the app server is already responding.
    Returns True for 200 OK, or 401/302 Redirects (Login page).
    """
    try:
        req = urllib.request.Request(f"http://{HOST}:{PORT}/", method="HEAD")
        urllib.request.urlopen(req, timeout=1)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False

# ====================================================================
# BAIL OUT IMMEDIATELY IF ALREADY RUNNING (Prevents file deletion)
# ====================================================================
if app_already_running():
    webbrowser.open(f"http://{HOST}:{PORT}")
    sys.exit(0)

# ─────────────────────────────────────────────
#  Base directory
# ─────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    _BUNDLE  = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _BUNDLE  = BASE_DIR

# ─────────────────────────────────────────────
#  Clean up stale PyInstaller _MEI temp folders
# ─────────────────────────────────────────────
def _cleanup_old_mei_folders():
    if not getattr(sys, 'frozen', False):
        return
    current_mei = sys._MEIPASS
    temp_dir = tempfile.gettempdir()
    # Updated to find standard PyInstaller _MEI dynamic folders
    for folder in glob.glob(os.path.join(temp_dir, '_MEI*')):
        if folder == current_mei:
            continue
        try:
            shutil.rmtree(folder, ignore_errors=True)
        except Exception:
            pass

_cleanup_old_mei_folders()

# ─────────────────────────────────────────────
#  Decrypt env.enc and load into os.environ
#  (env.enc is bundled inside the exe)
#
#  The encryption key is NEVER stored inside the binary.
#  It is read from a separate file: passposys.key, placed
#  next to the exe by the installer/admin. If the key file
#  is missing the app refuses to start.
# ─────────────────────────────────────────────
def _load_encryption_key():
    """
    Load the master encryption key from passposys.key (next to the exe).
    This file must NOT be bundled inside the exe — it stays external so
    the key is never extractable from the binary alone.
    """
    key_path = os.path.join(BASE_DIR, 'passposys.key')
    if not os.path.exists(key_path):
        raise RuntimeError(
            "passposys.key not found next to the executable.\n"
            "This file must be created by the installer and kept secure.\n"
            "Generate it with:  python encrypt_env.py --genkey"
        )
    with open(key_path, 'rb') as f:
        raw = f.read().strip()
    if len(raw) < 16:
        raise RuntimeError("passposys.key is too short or corrupted.")
    return raw


def _load_encrypted_env():
    import base64, hashlib
    from cryptography.fernet import Fernet

    raw_key = _load_encryption_key()
    _key   = base64.urlsafe_b64encode(hashlib.sha256(raw_key).digest())
    fernet = Fernet(_key)

    # Expose the raw key bytes in the environment so config.py and
    # cloudstore_backup.py can use the same key without re-reading the file.
    os.environ['_PASSPOSYS_ENC_KEY'] = raw_key.decode('utf-8', errors='replace')

    enc_path = os.path.join(_BUNDLE, 'env.enc')
    if not os.path.exists(enc_path):
        raise RuntimeError(
            "env.enc not found in the application bundle. "
            "Re-run the build process to regenerate it."
        )

    with open(enc_path, 'rb') as f:
        decrypted = fernet.decrypt(f.read()).decode('utf-8')

    for line in decrypted.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key   = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and not os.environ.get(key):
            os.environ[key] = value
    print("Encrypted env loaded.")
    logger.info("Encrypted env loaded successfully.")

try:
    _load_encrypted_env()
except Exception:
    logger.critical("Failed to load encrypted env:\n%s", traceback.format_exc())
    raise

# ─────────────────────────────────────────────
#  Platform detection (used for MySQL binary names,
#  subprocess creation flags, and the NUL/dev-null log target)
# ─────────────────────────────────────────────
IS_WINDOWS = sys.platform.startswith('win')
IS_MAC     = sys.platform == 'darwin'

# subprocess.CREATE_NO_WINDOW (0x08000000) only exists on Windows.
# On macOS/Linux there is no console window to suppress, so this is
# simply omitted from the Popen kwargs entirely on those platforms.
_POPEN_EXTRA_KWARGS = {'creationflags': 0x08000000} if IS_WINDOWS else {}

# ─────────────────────────────────────────────
#  MySQL portable settings
# ─────────────────────────────────────────────
MYSQL_DIR      = os.path.join(BASE_DIR, 'mysql')
MYSQL_BIN      = os.path.join(MYSQL_DIR, 'bin')
MYSQL_DATA     = os.path.join(MYSQL_DIR, 'data')
MYSQL_PORT     = 3307
_EXE_SUFFIX    = '.exe' if IS_WINDOWS else ''
MYSQL_EXE      = os.path.join(MYSQL_BIN, f'mysqld{_EXE_SUFFIX}')
MYSQLADMIN_EXE = os.path.join(MYSQL_BIN, f'mysqladmin{_EXE_SUFFIX}')
MYSQL_CLI      = os.path.join(MYSQL_BIN, f'mysql{_EXE_SUFFIX}')
# Windows uses the special "NUL" device name; macOS/Linux use /dev/null.
_NULL_DEVICE   = 'NUL' if IS_WINDOWS else '/dev/null'
SEED_SQL       = os.path.join(BASE_DIR, 'passport_db_seed.sql')
DB_INIT_FLAG   = os.path.join(BASE_DIR, '.db_initialized')
DB_NAME        = 'passport_db'
DB_USER        = 'passport_user'
DB_PASS        = 'passposys_local'

# Set env variables so app/config.py can read them
os.environ['DB_PASSWORD'] = DB_PASS
os.environ['DB_PORT']     = str(MYSQL_PORT)

# FLASK_SECRET_KEY and LOCAL_API_TOKEN must be supplied via env.enc.
# A missing or weak secret key allows session cookie forgery.
if not os.environ.get('FLASK_SECRET_KEY'):
    raise RuntimeError(
        "FLASK_SECRET_KEY is not set in env.enc. "
        "Generate a strong random value and add it before distributing the app."
    )
if not os.environ.get('LOCAL_API_TOKEN'):
    raise RuntimeError(
        "LOCAL_API_TOKEN is not set in env.enc. "
        "Generate a strong random value and add it before distributing the app."
    )


def mysql_available():
    try:
        s = socket.create_connection(('127.0.0.1', MYSQL_PORT), timeout=2)
        s.close()
        return True
    except OSError:
        return False


def app_already_running():
    """
    Checks if the app server is responding. 
    If it returns ANY HTTP status (200, 302, 401, etc.), it means 
    the server is alive and we should NOT start a second instance.
    """
    import urllib.request
    import urllib.error
    import socket
    
    try:
        # We ask for the headers only (HEAD request) to make it lightning fast
        req = urllib.request.Request(f"http://{HOST}:{PORT}/", method="HEAD")
        urllib.request.urlopen(req, timeout=2)
        return True
    except urllib.error.HTTPError:
        # The server responded, but gave an HTTP error (e.g., 401 Unauthorized for the login page).
        # This proves the first instance is perfectly alive!
        return True
    except Exception:
        # Catch-all for ConnectionRefused, Timeout, URLError, OSError.
        # This means the server is completely offline.
        return False


def run_mysql_cmd(*args):
    cmd = [MYSQL_CLI, '--host=127.0.0.1', f'--port={MYSQL_PORT}',
           '-u', 'root'] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def _ensure_mysql_binaries_executable():
    """
    On macOS/Linux, files extracted from a zip/tarball don't retain their
    executable bit, and PyInstaller-bundled resources aren't marked
    executable either. Windows has no such concept, so this is a no-op
    there. Without this, mysqld/mysqladmin/mysql fail with
    "Permission denied" (errno 13) on first run on a fresh Mac.
    """
    if IS_WINDOWS:
        return
    for exe_path in (MYSQL_EXE, MYSQLADMIN_EXE, MYSQL_CLI):
        try:
            if os.path.exists(exe_path):
                st = os.stat(exe_path)
                os.chmod(exe_path, st.st_mode | 0o111)  # add execute for all
        except Exception:
            logger.warning("Could not chmod +x %s", exe_path, exc_info=True)


def start_mysql():
    if not os.path.exists(MYSQL_EXE):
        print("No portable MySQL found - assuming external MySQL is running.")
        logger.info("No portable MySQL found at %s - assuming external MySQL.", MYSQL_EXE)
        return

    _ensure_mysql_binaries_executable()

    print("=" * 50)
    print(" Portable MySQL Manager")
    print("=" * 50)

    if mysql_available():
        print(f"MySQL already running on port {MYSQL_PORT}.")
        setup_database()
        return

    if not os.path.exists(os.path.join(MYSQL_DATA, 'mysql')):
        print("First run: Initializing MySQL data directory...")
        print("   Please wait 30-60 seconds...")
        result = subprocess.run([
            MYSQL_EXE,
            '--initialize-insecure',
            f'--basedir={MYSQL_DIR}',
            f'--datadir={MYSQL_DATA}',
            f'--log-error={_NULL_DEVICE}'
        ], capture_output=True, text=True)

        if result.returncode != 0:
            print("MySQL initialization failed!")
            logger.critical(
                "MySQL --initialize-insecure failed (code %s).\nstdout:\n%s\nstderr:\n%s",
                result.returncode, result.stdout, result.stderr
            )
            input("Press Enter to exit...")
            sys.exit(1)
        print("MySQL initialized successfully.")
        logger.info("MySQL initialized successfully.")

    print(f"Starting MySQL on port {MYSQL_PORT}...")
    
    subprocess.Popen([
        MYSQL_EXE,
        f'--basedir={MYSQL_DIR}',
        f'--datadir={MYSQL_DATA}',
        f'--port={MYSQL_PORT}',
        '--bind-address=127.0.0.1',
        f'--log-error={_NULL_DEVICE}',
        # This is a portable, single-instance local DB with no replication
        # or point-in-time-recovery requirement, so binary logging just
        # accumulates binlog.NNNNNN files forever (observed growing to
        # several GB over time on disk -- see TreeSize screenshot). Turning
        # it off entirely stops that from happening on future startups.
        '--disable-log-bin',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_POPEN_EXTRA_KWARGS)

    print("Waiting for MySQL to be ready", end='', flush=True)
    for i in range(30):
        time.sleep(2)
        print('.', end='', flush=True)
        if mysql_available():
            print(" Ready!")
            break
    else:
        print("\nMySQL did not start!")
        logger.critical("MySQL did not become ready on port %s after 60s.", MYSQL_PORT)
        input("Press Enter to exit...")
        sys.exit(1)

    setup_database()


def setup_database():
    if os.path.exists(DB_INIT_FLAG):
        print("Database already set up, skipping.")
        return

    print("First run: Setting up database and user...")

    run_mysql_cmd('-e',
        f"CREATE DATABASE IF NOT EXISTS {DB_NAME} "
        f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")

    run_mysql_cmd('-e',
        f"CREATE USER IF NOT EXISTS '{DB_USER}'@'127.0.0.1' "
        f"IDENTIFIED BY '{DB_PASS}';")

    run_mysql_cmd('-e',
        f"GRANT ALL PRIVILEGES ON {DB_NAME}.* TO '{DB_USER}'@'127.0.0.1'; "
        f"FLUSH PRIVILEGES;")

    print("Database and user created.")

    if os.path.exists(SEED_SQL):
        print("Importing database tables...")
        with open(SEED_SQL, 'r', encoding='utf-8') as f:
            sql = f.read()
        result = subprocess.run(
            [MYSQL_CLI, '--host=127.0.0.1', f'--port={MYSQL_PORT}',
             '-u', 'root', DB_NAME],
            input=sql, capture_output=True, text=True
        )
        if result.returncode == 0:
            print("Tables imported successfully.")
        else:
            print(f"SQL import warning: {result.stderr[:200]}")
    else:
        print("No seed SQL found - app will create tables on first login.")

    with open(DB_INIT_FLAG, 'w') as f:
        f.write('initialized')
    print("Database setup complete.")


def stop_mysql():
    if not os.path.exists(MYSQLADMIN_EXE):
        return
    if mysql_available():
        print("\nStopping MySQL...")
        subprocess.run([
            MYSQLADMIN_EXE,
            '--host=127.0.0.1',
            f'--port={MYSQL_PORT}',
            '-u', 'root',
            'shutdown'
        ], capture_output=True)
        print("MySQL stopped.")


# ─────────────────────────────────────────────
#  Main entry point
# ─────────────────────────────────────────────


def open_browser():
    time.sleep(2)
    url = f"http://{HOST}:{PORT}"
    print(f"\nOpening browser to {url} ...")
    webbrowser.open(url)
    try:
        import pyi_splash
        pyi_splash.close()
    except ImportError:
        pass

if __name__ == '__main__':
    # 0. If the app server is already running (a previous instance of
    #    this exe is active), just open the browser to it and exit.
    #    This avoids duplicate MySQL/Flask startup attempts and the
    #    noisy "already in use" style errors that come with them.
    if app_already_running():
        print(f"App is already running on {HOST}:{PORT}. Opening browser...")
        webbrowser.open(f"http://{HOST}:{PORT}")
        sys.exit(0)

    # 1. Clean up stale _MEI folders from any previous forced closures immediately
    _cleanup_old_mei_folders()

    print("=" * 50)
    print(" PassPoSys Application")
    print("=" * 50)
    logger.info("PassPoSys application starting main sequence.")

    try:
        start_mysql()
    except SystemExit:
        raise
    except Exception:
        logger.critical("start_mysql() raised an unexpected error:\n%s", traceback.format_exc())
        raise

    try:
        from app import app
        logger.info("app import succeeded.")
    except Exception:
        logger.critical("Failed to import app (Flask app object):\n%s", traceback.format_exc())
        raise

    try:
        from waitress import serve
    except Exception:
        logger.critical("Failed to import waitress:\n%s", traceback.format_exc())
        raise

    threading.Thread(target=open_browser, daemon=True).start()

    try:
        print(f"\nServer listening on {BIND_HOST}:{PORT}")
        print("   Press Ctrl+C to stop.\n")
        logger.info("Server listening on %s:%s", BIND_HOST, PORT)

        # Start the waitress server
        serve(app, host=BIND_HOST, port=PORT)

    except KeyboardInterrupt:
        # 2. Catch Ctrl+C so the app doesn't crash abruptly
        print("\nShutting down server gracefully...")
        logger.info("Shutdown requested via KeyboardInterrupt.")

    except Exception:
        logger.critical("Server crashed while running:\n%s", traceback.format_exc())
        raise

    finally:
        # 3. Ensure MySQL stops and the script reaches the absolute end.
        # Once the script ends cleanly, PyInstaller's bootloader will 
        # automatically delete the CURRENT _MEI folder.
        stop_mysql()
        print("Shutdown complete. You may close this window.")
        logger.info("Shutdown complete.")