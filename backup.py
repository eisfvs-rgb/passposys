"""
backup.py
---------
Scheduled + manual database/image backup logic.

Owns:
  - Locating mysqldump.exe (portable MySQL first, then common installs, then PATH)
  - _run_scheduled_backup(): dump DB -> copy images into group folders -> zip ->
    keep last 2 local copies -> upload to CloudStore
  - APScheduler wiring (start_scheduler()) that runs the above once 10s after
    startup and then every 10 minutes, guarded so only one worker process
    runs it (via an flock-based lock file; always "the worker" on Windows).

app.py calls start_scheduler(app) once, after the Flask app object exists,
to avoid a circular import (this module doesn't import app.py at all).
The manual "/backup_database" route in app.py builds its own zip using the
same LOCAL_BACKUP_DIR/BACKUP_TEMP_DIR constants imported from here.
"""

import os
import sys
import shutil
import threading
from datetime import timedelta

IS_WINDOWS = sys.platform.startswith('win')
# subprocess.CREATE_NO_WINDOW (0x08000000) is Windows-only; there is no
# equivalent or need for it on macOS/Linux since there's no console window
# to suppress there.
_POPEN_EXTRA_KWARGS = {'creationflags': 0x08000000} if IS_WINDOWS else {}

from config import BASE_DIR, DB_CONFIG, UPLOAD_FOLDER, FACE_FOLDER
from db import get_filename_group_map
from cloudstore_backup import upload_zip_to_cloudstore
from time_utils import ist_now

# =====================================================
# SCHEDULED AUTO-BACKUP (every 10 minutes)
# =====================================================

def _find_mysqldump():
    """Locate mysqldump[.exe] — prefers portable MySQL bundled with the app."""
    import shutil as _shutil
    import glob as _glob

    exe_suffix = '.exe' if IS_WINDOWS else ''

    # 1. Portable MySQL bundled with the app (highest priority)
    portable = os.path.join(BASE_DIR, 'mysql', 'bin', f'mysqldump{exe_suffix}')
    if os.path.exists(portable):
        return portable

    # 2. Common system MySQL installs (fallback)
    if IS_WINDOWS:
        candidates = [
            r"C:\Program Files\MySQL\MySQL Server 9.7\bin\mysqldump.exe",
            r"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqldump.exe",
        ] + _glob.glob(r"C:\Program Files\MySQL\MySQL Server *\bin\mysqldump.exe")
    else:
        # Common Homebrew / macOS package layouts, in rough priority order.
        candidates = [
            "/opt/homebrew/bin/mysqldump",        # Homebrew on Apple Silicon
            "/usr/local/bin/mysqldump",            # Homebrew on Intel Mac
            "/usr/local/mysql/bin/mysqldump",      # Official MySQL .pkg installer
        ] + _glob.glob("/opt/homebrew/opt/mysql*/bin/mysqldump")
    for cand in candidates:
        if os.path.exists(cand):
            return cand

    # 3. PATH fallback
    return _shutil.which("mysqldump") or "/usr/bin/mysqldump"


# Local folder where the last 2 backups are permanently kept
# Windows-compatible: stored next to the app (or exe) in a 'backups' folder
LOCAL_BACKUP_DIR = os.path.join(BASE_DIR, "passport_backups")
# Temp folder used only during rclone upload (deleted immediately after)
BACKUP_TEMP_DIR  = os.path.join(BASE_DIR, "passport_backups_tmp")

os.makedirs(LOCAL_BACKUP_DIR, exist_ok=True)
os.makedirs(BACKUP_TEMP_DIR,  exist_ok=True)

import threading
import shutil



def _run_scheduled_backup():
    """
    Runs every 10 minutes via APScheduler.
    1. mysqldump        →  BACKUP_TEMP_DIR  (temp SQL file, named db.sql in the zip)
    2. copy images      →  BACKUP_TEMP_DIR/folders/<GROUP_NAME>/face/   and /passport/
                            (organized by group_name, looked up from the DB)
    3. zip everything   →  BACKUP_TEMP_DIR  (temp zip: db.sql + folders/...)
    4. move zip         →  LOCAL_BACKUP_DIR (permanent copy, keep last 2 locally)
    5. delete temp files/folders from BACKUP_TEMP_DIR
    """
    import shutil as _shutil
    import subprocess
    import zipfile
    
    MYSQLDUMP_PATH = _find_mysqldump()

    timestamp    = ist_now().strftime("%Y%m%d_%H%M%S")
    prefix       = "db_backup_scheduled"
    sql_filename = "db.sql"
    zip_filename = f"{prefix}_{timestamp}.zip"

    sql_path     = os.path.join(BACKUP_TEMP_DIR, sql_filename)        # temp — deleted after zipping
    zip_tmp      = os.path.join(BACKUP_TEMP_DIR, zip_filename)        # temp — deleted after moving
    zip_local    = os.path.join(LOCAL_BACKUP_DIR, zip_filename)       # permanent local copy
    folders_root = os.path.join(BACKUP_TEMP_DIR, "folders")           # temp — deleted after zipping

    try:
        if not os.path.exists(MYSQLDUMP_PATH):
            print("[Backup] mysqldump not found — skipping.")
            return

        db_host = DB_CONFIG.get('host', 'localhost')
        db_user = DB_CONFIG.get('user', 'passport_user')
        db_pass = DB_CONFIG.get('password', 'test123')
        db_name = DB_CONFIG.get('database', 'passport_db')

        env = os.environ.copy()
        env['MYSQL_PWD'] = db_pass

        # ── Step 1: mysqldump to temp folder ──────────────────────
        dump_cmd = [
            MYSQLDUMP_PATH,
            f"--host={db_host}",
            f"--port={DB_CONFIG.get('port', 3307)}",
            f"--user={db_user}",
            "--skip-add-drop-table",
            "--no-tablespaces",
            "--lock-tables=false",        # ← replaces --single-transaction + --skip-lock-tables
            "--column-statistics=0",      # ← ADD THIS
            "--hex-blob",
            "--default-character-set=utf8mb4",
            db_name
        ]
        with open(sql_path, 'wb') as f:
            result = subprocess.run(dump_cmd, stdout=f, stderr=subprocess.PIPE, env=env, **_POPEN_EXTRA_KWARGS)
        if result.returncode != 0:
            raise Exception(f"mysqldump failed: {result.stderr.decode(errors='replace')}")

        # ── Step 2: copy passport + face images into folders/<GROUP_NAME>/{passport,face}/ ──
        if os.path.exists(folders_root):
            _shutil.rmtree(folders_root, ignore_errors=True)
        os.makedirs(folders_root, exist_ok=True)

        try:
            filename_group_map = get_filename_group_map()
        except Exception as e:
            print(f"[Backup] Could not load group mapping from DB: {e}")
            filename_group_map = {}

        def _sanitize(name, fallback):
            safe = "".join(
                c for c in (name or fallback) if c.isalnum() or c in (" ", "_", "-")
            ).strip()
            return safe or fallback

        def _group_dir(group_name, visa_type, kind):
            safe_visa  = _sanitize(visa_type, "nusuk")
            safe_group = _sanitize(group_name, "GROUP 1")
            d = os.path.join(folders_root, safe_visa, safe_group, kind)
            os.makedirs(d, exist_ok=True)
            return d

        def _iter_files(root):
            """Recursively yield every file under root — handles flat layout,
            group-subfoldered layout (uploads/<GROUP>/file), and any other
            nesting depth, so nothing gets skipped."""
            if not os.path.isdir(root):
                return
            for dirpath, _dirs, files_ in os.walk(root):
                for f_ in files_:
                    yield os.path.join(dirpath, f_), f_

        def _backup_name(orig_fname, passport_number, dest_dir, prefix=""):
            """Build the in-zip filename from the passport number, keeping the
            original extension. Falls back to the original filename if the
            passport number is blank/unknown. Appends a numeric suffix on
            collision so same-passport-number files never overwrite each other."""
            ext = os.path.splitext(orig_fname)[1]
            pn = _sanitize((passport_number or "").strip(), "")
            base = f"{prefix}{pn}" if pn else os.path.splitext(orig_fname)[0]
            candidate = f"{base}{ext}"
            n = 1
            while os.path.exists(os.path.join(dest_dir, candidate)):
                n += 1
                candidate = f"{base}_{n}{ext}"
            return candidate

        images_copied = 0

        # Passport (original) images live under UPLOAD_FOLDER (flat or grouped)
        for src, fname in _iter_files(UPLOAD_FOLDER):
            # skip intermediate OCR-resized copies (not originals)
            if fname.lower().endswith("_ocr.jpg"):
                continue
            group_name, visa_type, passport_number = filename_group_map.get(fname, ("GROUP 1", "nusuk", ""))
            dest_dir = _group_dir(group_name, visa_type, "passport")
            try:
                dest_name = _backup_name(fname, passport_number, dest_dir)
                _shutil.copy2(src, os.path.join(dest_dir, dest_name))
                images_copied += 1
            except Exception as e:
                print(f"[Backup] Could not copy passport image {fname}: {e}")

        # Face images live under FACE_FOLDER (flat or grouped), named face_<original_filename>
        for src, fname in _iter_files(FACE_FOLDER):
            orig_fname = fname[len("face_"):] if fname.startswith("face_") else fname
            group_name, visa_type, passport_number = filename_group_map.get(orig_fname, ("GROUP 1", "nusuk", ""))
            dest_dir = _group_dir(group_name, visa_type, "faces")
            try:
                dest_name = _backup_name(orig_fname, passport_number, dest_dir)
                _shutil.copy2(src, os.path.join(dest_dir, dest_name))
                images_copied += 1
            except Exception as e:
                print(f"[Backup] Could not copy face image {fname}: {e}")

        print(f"[Backup] Copied {images_copied} image(s) into group folders for backup.")

        # ── Step 3: zip db.sql + folders/ into temp zip ──────────────
        with zipfile.ZipFile(zip_tmp, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.write(sql_path, arcname=sql_filename)
            for root, _dirs, files_ in os.walk(folders_root):
                for f_ in files_:
                    full = os.path.join(root, f_)
                    arc = os.path.join("folders", os.path.relpath(full, folders_root))
                    zf.write(full, arcname=arc)

        # SQL and folders no longer needed
        if os.path.exists(sql_path):
            os.remove(sql_path)
        _shutil.rmtree(folders_root, ignore_errors=True)

        # ── Step 4: move zip to permanent local backup folder ──────
        _shutil.copy2(zip_tmp, zip_local)

        # Keep only last 2 local backups
        local_zips = sorted(
            [f for f in os.listdir(LOCAL_BACKUP_DIR) if f.endswith('.zip')],
            reverse=True  # newest first (timestamp in filename)
        )
        for old in local_zips[2:]:
            try:
                os.remove(os.path.join(LOCAL_BACKUP_DIR, old))
                print(f"[Backup] Purged old local backup: {old}")
            except Exception as e:
                print(f"[Backup] Could not purge local {old}: {e}")

        print(f"[Backup] Scheduled local backup completed: {zip_filename}")
        
        # ---> ADD THESE TWO LINES <---
        # Uploads the newly created zip file to CloudStore
        upload_zip_to_cloudstore(zip_local)

    except Exception as e:
        print(f"[Backup] ERROR: {e}")

    
    finally:
        # Step 5: Always clean up temp files
        if 'sql_path' in locals() and os.path.exists(sql_path):
            os.remove(sql_path)
        if 'zip_tmp' in locals() and os.path.exists(zip_tmp):
            os.remove(zip_tmp)
        if 'folders_root' in locals():
            _shutil.rmtree(folders_root, ignore_errors=True)


# ── Start APScheduler on app startup (only in one gunicorn worker) ────────────────────
from apscheduler.schedulers.background import BackgroundScheduler
try:
    import fcntl
    _FCNTL_AVAILABLE = True
except ImportError:
    fcntl = None  # Windows — fcntl not available
    _FCNTL_AVAILABLE = False

_SCHEDULER_LOCK = "/tmp/passport_backup_scheduler.lock"


def _is_scheduler_worker(app):
    """Returns True only for the first worker that acquires the lock file.
    On Windows (no fcntl), always returns True since there is only one process.
    """
    if not _FCNTL_AVAILABLE:
        # Windows: single-process launcher, no multi-worker competition
        return True
    try:
        _lock_fh = open(_SCHEDULER_LOCK, 'w')
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        app._scheduler_lock_fh = _lock_fh  # hold open so lock persists
        return True
    except (IOError, OSError):
        return False


def start_scheduler(app):
    """
    Called once from app.py right after the Flask app is created. Takes the
    app object as a parameter (instead of importing it) so this module has
    no dependency on app.py and can't create a circular import.
    """
    if _is_scheduler_worker(app):
        _backup_scheduler = BackgroundScheduler(daemon=True)

        # First backup: 10 seconds after service starts
        _backup_scheduler.add_job(
            func=_run_scheduled_backup,
            trigger="date",
            run_date=ist_now() + timedelta(seconds=10),
            id="startup_backup",
        )

        # Recurring backup: every 10 minutes
        _backup_scheduler.add_job(
            func=_run_scheduled_backup,
            trigger="interval",
            minutes=10,
            id="scheduled_db_backup",
            max_instances=1,
            misfire_grace_time=60
        )
        _backup_scheduler.start()
        print(f"[Backup] Scheduler started in worker PID {os.getpid()} — initial backup in 10s, then every 10 minutes.")
    else:
        print(f"[Backup] Worker PID {os.getpid()} — scheduler already running in another worker, skipping.")

