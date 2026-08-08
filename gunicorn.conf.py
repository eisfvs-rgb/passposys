# =============================================================================
# gunicorn.conf.py  —  Passport App  (multi-user optimised)
# =============================================================================
import os
import sys

# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------
bind        = "127.0.0.1:9000"
backlog     = 2048

# ---------------------------------------------------------------------------
# Workers  (right-sized for 20 users, 30-passport batches, 5-min gap)
# ---------------------------------------------------------------------------
worker_class   = "gthread"
workers        = 4
threads        = 4
worker_connections = 1000

# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------
timeout          = 120
graceful_timeout = 60
keepalive        = 5

# ---------------------------------------------------------------------------
# Worker lifecycle
# ---------------------------------------------------------------------------
max_requests        = 800
max_requests_jitter = 80

# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------
worker_tmp_dir = "/dev/shm"

# ---------------------------------------------------------------------------
# Logging — send to journald (stdout/stderr), NOT to files.
# Files require the directory to exist before the process starts.
# journald always exists. View logs with: journalctl -u passport_app -f
# ---------------------------------------------------------------------------
accesslog  = "-"    # "-" = stdout → journald
errorlog   = "-"    # "-" = stderr → journald
loglevel   = "debug"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s %(D)s'

# ---------------------------------------------------------------------------
# Process naming
# ---------------------------------------------------------------------------
proc_name = "passport_app"

# ---------------------------------------------------------------------------
# Server hooks
# ---------------------------------------------------------------------------

def when_ready(server):
    """
    Called once in the MASTER process after server is ready, before workers fork.
    Runs init_db() exactly once to avoid the concurrent DDL race when all
    workers call it simultaneously at boot.
    """
    try:
        _db = sys.modules.get("db")
        if _db is not None:
            _db.init_db()
            server.log.info("[master] init_db() completed successfully.")
    except Exception as e:
        server.log.warning(f"[master] init_db() warning (non-fatal): {e}")


def post_fork(server, worker):
    """
    Reset per-process singletons after fork.
    Uses sys.modules (not import) to avoid re-executing module-level code
    that requires env vars — avoids the 'Subscription key cannot be None' crash.
    """
    import random
    random.seed()

    _db = sys.modules.get("db")
    if _db is not None:
        _db._db_pool = None

    # NOTE: _gcloud_vision_client no longer lives in the local app process —
    # OCR (ProvA/ProvB Vision) now runs entirely on the VPS via
    # ocr_scan_service.py. Nothing to reset here anymore.


def worker_exit(server, worker):
    """Release DB pool connections on clean worker exit."""
    _db = sys.modules.get("db")
    if _db is not None:
        _db._db_pool = None


def on_exit(server):
    server.log.info("Passport app gunicorn master exiting.")
