"""
mofa_downloader.py
-------------------
Local standalone script for DB + Playwright; CAPTCHA solving is sent to the
VPS server (only the CAPTCHA image is sent, nothing else).

  1. Query DB for passports that are "processed" (nusuk: is_processed=TRUE,
     visit_visa: is_visa_processed=TRUE) and not yet downloaded from MOFA
     (mofa_pdf_downloaded_at IS NULL), ordered by processed date ascending.
  2. For each record, use Playwright (local, headless) to log in / search /
     submit the MOFA form.
  3. If a CAPTCHA field appears, screenshot it and POST it to the VPS's
     /ocr/solve_captcha route (solving happens server-side, using the
     existing LlmB key rotation), get back the digits, fill locally, resubmit.
  4. On success: save PDF to visa_processed/<group_name>/<passport_number>_...pdf
     and set mofa_pdf_downloaded_at = NOW() in the DB.
  5. On any failure: skip this record, continue to the next, until the last one.
  6. Continuously watch (poll every 1 hour) for newly-processed passports and
     run a download batch immediately whenever any are found. Call
     start_background() to run this watch loop in a background thread
     (triggered on app login). start_background() also pre-warms a Chromium
     browser in the background so it is ready before the user clicks eVisa.

Browser lifecycle:
  - Every download run (single or batch) launches its own fresh Chromium
    instance on the thread doing the work, and closes it when done. There is
    no shared/pre-warmed browser kept across threads -- Playwright's sync API
    binds a Page to the OS thread that created it, so sharing one across
    threads crashes with `greenlet.error: cannot switch to a different
    thread` once the creating thread exits. CAPTCHA is solved per-run (once
    per batch, since records in a batch share one browser/context).
  - Progress for a batch/single run is tracked in-memory per job_id (see
    _new_job/_job_tick/_job_finish/get_job_progress) so callers can report
    live "X of N downloaded" counts.

Requires a new column on `passports`:
    ALTER TABLE passports ADD COLUMN mofa_pdf_downloaded_at TIMESTAMP NULL DEFAULT NULL;
(added automatically at startup by _ensure_column(), mirroring db.py's
migration style.)

Server-side: paste solve_captcha_route.py's route into ocr_scan_service.py
on the VPS (only CAPTCHA-solving moves server-side; nothing else does).

Dependencies: playwright, requests, mysql-connector-python.
DB connection uses DB_CONFIG below (edit directly, or set
DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD env vars).
This script does NOT import config.py/db.py -- no dependency on env.enc.
"""

import sys
import os
import time
import base64
import logging
import traceback
import threading

import requests
import mysql.connector
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
from logging.handlers import RotatingFileHandler

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mofa_downloader")


def _configure_bundled_browser_path():
    """When running as a frozen (PyInstaller onefile) exe, point Playwright
    at the chromium build shipped inside the exe instead of relying on the
    OS-default per-user browser cache, which won't exist on a machine that
    never ran `playwright install` outside this app.

    Must run before any sync_playwright() call. Playwright reads
    PLAYWRIGHT_BROWSERS_PATH at import/launch time, so setting the env var
    here is sufficient -- no other wiring required.
    """
    if not getattr(sys, 'frozen', False):
        return  # dev environment: use whatever `playwright install` set up locally
    bundle_root = sys._MEIPASS
    browsers_dir = os.path.join(bundle_root, "playwright", "driver", "package", ".local-browsers")
    if os.path.isdir(browsers_dir):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_dir
    else:
        logger.error(
            "Bundled Playwright browser folder not found at %s -- "
            "the exe was built without it. MOFA downloads will fail.",
            browsers_dir,
        )


_configure_bundled_browser_path()

# BASE_DIR must be the folder next to the .exe, NOT __file__ -- under
# PyInstaller, __file__ resolves inside the temp _MEI extraction folder,
# which launch.py actively deletes on every startup/exit. That silently
# broke both the log file and the visa_processed output folder.
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Separate local log file (next to the exe), same convention as
# ocr_debug.log: rotates at 10MB, keeps 5 backups.
_LOG_PATH = os.path.join(BASE_DIR, "mofa_downloader.log")
_file_handler = RotatingFileHandler(_LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_file_handler.setLevel(logging.INFO)
logger.addHandler(_file_handler)

VISA_PROCESSED_ROOT = os.path.join(BASE_DIR, "visa_processed")
POLL_INTERVAL_SECONDS = 3600       # how often the background loop checks DB (1 hour)
HEARTBEAT_EVERY_N_POLLS = 1        # log a quiet "still watching" line every poll
REQUEST_TIMEOUT = 60               # seconds, for the captcha-solve call

# VPS endpoint that solves CAPTCHAs (see solve_captcha_route.py, pasted into
# ocr_scan_service.py). Override with OCR_API_BASE env var if needed.
OCR_API_BASE = os.environ.get("OCR_API_BASE", "https://pms.passposys.com/ocr")
OCR_API_SECRET = os.environ.get("OCR_API_SECRET", "")

# Standalone DB connection -- does not import config.py/db.py, so this
# script has no dependency on env.enc / HOST_API_SECRET. Edit these values
# (or set the matching environment variables) to match your MySQL setup.
DB_CONFIG = {
    'host':     os.environ.get('DB_HOST', '127.0.0.1'),
    'port':     int(os.environ.get('DB_PORT', 3307)),
    'database': os.environ.get('DB_NAME', 'passport_db'),
    'user':     os.environ.get('DB_USER', 'passport_user'),
    'password': os.environ.get('DB_PASSWORD', 'passposys_local'),
}


def get_connection():
    return mysql.connector.connect(**DB_CONFIG)


# ---------------------------------------------------------------------------
# Remote CAPTCHA solving -- only the CAPTCHA image is sent to the server
# ---------------------------------------------------------------------------

# The background thread has no Flask request context, so it can't use
# ocr_client.py's per-request contextvars user_id. Instead, the logged-in
# user_id is captured once (from session['user_id']) when start_background()
# is first called, and reused for every captcha-solve call from this thread.
_current_user_id = None


class CaptchaServiceUnavailable(Exception):
    """Raised when our own captcha-solving service can't be reached or
    rejects the request -- distinct from MOFA rejecting the CAPTCHA answer
    itself, so the UI can show the correct one of the two."""
    pass


def _solve_captcha_remote(image_path):
    """POST the CAPTCHA screenshot to the VPS's /ocr/solve_captcha route and
    return the digits. Solving happens server-side; nothing else about this
    scan is sent to the server."""
    url = f"{OCR_API_BASE}/solve_captcha"
    with open(image_path, "rb") as f:
        captcha_b64 = base64.b64encode(f.read()).decode("ascii")

    body = {
        "secret":      OCR_API_SECRET,
        "captcha_b64": captcha_b64,
    }
    if _current_user_id is not None:
        body["user_id"] = _current_user_id
    try:
        from secrets_client import load_install_token
        install_token = load_install_token()
        if install_token:
            body["install_token"] = install_token
            # Send the device MAC alongside install_token -- required by
            # the server (_authorized() in ocr_scan_service.py rejects any
            # request missing this field, before even checking the token
            # itself). Mirrors what fetch_runtime_secrets() already sends
            # to secrets.php for the same install_token.
            try:
                from mac_lock import get_local_mac
                mac_address = get_local_mac()
                if mac_address:
                    body["mac_address"] = mac_address
                else:
                    logger.warning(
                        "get_local_mac() returned None -- captcha-solve request "
                        "will be sent without mac_address and the server will "
                        "reject it (generic 401 'token_mismatch', regardless of "
                        "the install_token itself being valid)."
                    )
            except Exception:
                logger.exception("Failed to determine local MAC address for captcha-solve request.")
    except Exception:
        pass

    try:
        resp = requests.post(url, json=body, timeout=REQUEST_TIMEOUT, verify=True)
    except requests.exceptions.RequestException as e:
        logger.warning(f"Could not reach captcha-solve service: {e}")
        raise CaptchaServiceUnavailable(
            "Could not reach the captcha-solving service. Please try again later."
        ) from e

    if resp.status_code == 401:
        logger.warning(
            f"captcha-solve service rejected install_token (401): {resp.text[:300]}"
        )
        raise CaptchaServiceUnavailable(
            "The captcha-solving service rejected this device's credentials."
        )

    if resp.status_code != 200:
        logger.warning(f"captcha-solve service returned status {resp.status_code}: {resp.text[:300]}")
        raise CaptchaServiceUnavailable(
            f"The captcha-solving service returned an unexpected error (status {resp.status_code})."
        )

    try:
        return resp.json().get("captcha_code", "")
    except Exception as e:
        logger.warning(f"Malformed captcha-solve response: {e}")
        raise CaptchaServiceUnavailable(
            "The captcha-solving service returned an unreadable response."
        ) from e


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def clear_all_passports():
    """
    Deletes ALL rows from general_data and passports (keeps table structure).
    Destructive and irreversible -- intended for test/reset use only.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM general_data")
    general_data_deleted = cursor.rowcount

    cursor.execute("DELETE FROM passports")
    passports_deleted = cursor.rowcount

    conn.commit()
    cursor.close()
    conn.close()

    logger.info(f"clear_all_passports: deleted {passports_deleted} passport row(s), {general_data_deleted} general_data row(s).")
    return passports_deleted, general_data_deleted


def _ensure_column():
    """Add mofa_pdf_downloaded_at to passports if it doesn't already exist."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SHOW COLUMNS FROM passports LIKE 'mofa_pdf_downloaded_at'")
    if not cursor.fetchone():
        cursor.execute(
            "ALTER TABLE passports ADD COLUMN mofa_pdf_downloaded_at TIMESTAMP NULL DEFAULT NULL"
        )
        conn.commit()
        logger.info("Added passports.mofa_pdf_downloaded_at column.")
    cursor.close()
    conn.close()


def mark_all_processed():
    """
    Marks EVERY passport as processed (matching whichever flag applies to
    its visa_type), and clears mofa_pdf_downloaded_at so all of them are
    picked up on the next run. Returns (nusuk_count, visit_visa_count).
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE passports p
        JOIN general_data g ON g.passport_id = p.id
        SET p.is_processed = TRUE,
            p.processed_at = NOW(),
            p.mofa_pdf_downloaded_at = NULL
        WHERE COALESCE(g.visa_type, 'nusuk') = 'nusuk'
    """)
    nusuk_count = cursor.rowcount

    cursor.execute("""
        UPDATE passports p
        JOIN general_data g ON g.passport_id = p.id
        SET p.is_visa_processed = TRUE,
            p.visa_processed_at = NOW(),
            p.mofa_pdf_downloaded_at = NULL
        WHERE g.visa_type = 'visit_visa'
    """)
    visit_visa_count = cursor.rowcount

    conn.commit()
    cursor.close()
    conn.close()

    logger.info(f"mark_all_processed: {nusuk_count} nusuk + {visit_visa_count} visit_visa row(s) marked processed.")
    return nusuk_count, visit_visa_count


def mark_processed(passport_number, visa_type):
    """
    Manually marks a passport as 'processed' for testing, matching the same
    condition _get_pending_records() checks for (is_processed for nusuk,
    is_visa_processed for visit_visa), and clears mofa_pdf_downloaded_at so
    it's picked up on the next run.
    """
    conn = get_connection()
    cursor = conn.cursor()
    if visa_type == 'nusuk':
        cursor.execute("""
            UPDATE passports p
            JOIN general_data g ON g.passport_id = p.id
            SET p.is_processed = TRUE,
                p.processed_at = NOW(),
                p.mofa_pdf_downloaded_at = NULL
            WHERE p.passport_number = %s
              AND g.visa_type = 'nusuk'
        """, (passport_number,))
    elif visa_type == 'visit_visa':
        cursor.execute("""
            UPDATE passports p
            JOIN general_data g ON g.passport_id = p.id
            SET p.is_visa_processed = TRUE,
                p.visa_processed_at = NOW(),
                p.mofa_pdf_downloaded_at = NULL
            WHERE p.passport_number = %s
              AND g.visa_type = 'visit_visa'
        """, (passport_number,))
    else:
        raise ValueError("visa_type must be 'nusuk' or 'visit_visa'")

    conn.commit()
    affected = cursor.rowcount
    cursor.close()
    conn.close()

    if affected == 0:
        logger.warning(f"mark_processed: no matching row for passport_number={passport_number}, visa_type={visa_type}")
    else:
        logger.info(f"mark_processed: marked passport_number={passport_number} ({visa_type}) as processed.")
    return affected


def _sanitize_group(group_name):
    g = ''.join(c for c in (group_name or "GROUP 1") if c.isalnum() or c in (" ", "_", "-")).strip()
    return g or "GROUP 1"


def _get_pending_records():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT
            p.id AS passport_id,
            p.passport_number,
            p.given_names,
            p.nationality,
            g.group_name,
            g.visa_type,
            p.processed_at,
            p.visa_processed_at
        FROM passports p
        JOIN general_data g ON g.passport_id = p.id
        WHERE p.mofa_pdf_downloaded_at IS NULL
          AND (
                (COALESCE(g.visa_type, 'nusuk') = 'nusuk' AND p.is_processed = TRUE)
             OR (g.visa_type = 'visit_visa' AND p.is_visa_processed = TRUE)
          )
        ORDER BY COALESCE(p.processed_at, p.visa_processed_at) ASC
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def _get_record_by_id(passport_id):
    """Fetch a single passport record in the same shape _get_pending_records()
    returns, regardless of its current is_processed/is_visa_processed state --
    used by trigger_single_download(), which is invoked explicitly on one
    record rather than discovered via the normal queue scan."""
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT
            p.id AS passport_id,
            p.passport_number,
            p.given_names,
            p.nationality,
            g.group_name,
            g.visa_type
        FROM passports p
        JOIN general_data g ON g.passport_id = p.id
        WHERE p.id = %s
    """, (passport_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def _mark_downloaded(passport_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE passports SET mofa_pdf_downloaded_at = NOW() WHERE id = %s", (passport_id,))
    conn.commit()
    cursor.close()
    conn.close()


# ---------------------------------------------------------------------------
# Batch/run coordination
# ---------------------------------------------------------------------------
# NOTE: there used to be a "pre-warmed browser" kept alive across threads and
# reused by trigger_batch_download(). That page object was created on one
# thread and later driven from a different thread, which crashes Playwright's
# sync API with `greenlet.error: cannot switch to a different thread` for
# every record in the batch. That mechanism has been removed entirely --
# _run_once() always launches its own browser on its own calling thread (see
# above), which is the only thread-safe pattern for Playwright's sync API.
# ---------------------------------------------------------------------------

# Ensures only one _run_once() executes at a time (prevents the hourly loop
# and a button-click batch from racing over the same DB records simultaneously).
_run_once_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Per-job progress tracking (in-memory) -- lets the group page show live
# "X of N downloaded" counts the same way the results page does, instead of
# only "group has >=1 pdf yes/no".
# ---------------------------------------------------------------------------
_progress_lock = threading.Lock()
_job_progress  = {}   # job_id (str) -> {"total": int, "done": int, "failed": int, "finished": bool, "last_error": str|None}
_job_counter   = 0


def _new_job(total):
    """Create a new progress-tracking entry and return its job_id."""
    global _job_counter
    with _progress_lock:
        _job_counter += 1
        job_id = str(_job_counter)
        _job_progress[job_id] = {"total": total, "done": 0, "failed": 0, "finished": False, "last_error": None}
    return job_id


def _job_tick(job_id, success, error_message=None):
    if job_id is None:
        return
    with _progress_lock:
        entry = _job_progress.get(job_id)
        if entry is None:
            return
        if success:
            entry["done"] += 1
        else:
            entry["failed"] += 1
            if error_message:
                # Keep the most recent failure reason so the UI can show
                # *something* concrete instead of just a failed count --
                # e.g. "MOFA site unreachable" vs "CAPTCHA rejected 3x".
                entry["last_error"] = error_message


def _job_finish(job_id):
    if job_id is None:
        return
    with _progress_lock:
        entry = _job_progress.get(job_id)
        if entry is not None:
            entry["finished"] = True


def get_job_progress(job_id):
    """Returns a copy of the progress dict for job_id, or None if unknown."""
    with _progress_lock:
        entry = _job_progress.get(job_id)
        return dict(entry) if entry is not None else None


# ---------------------------------------------------------------------------
# Playwright: local login/search/submit/PDF-save, with remote captcha solving
# ---------------------------------------------------------------------------

def _download_one(page, passport_no, first_name, nationality_code, output_filename, deadline=None):
    print(f"[*] Navigating to MOFA Visa Search for {passport_no}...")
    # Explicit, short timeout on the FIRST network call to MOFA. Without
    # this, an unreachable site doesn't fail in Playwright's usual ~30s --
    # a connection that's black-holed (packets dropped, no RST/refusal)
    # stalls at the OS TCP-connect level, which can run 60-120s on some
    # networks/OSes before the underlying socket gives up, and only THEN
    # does Playwright's navigation timeout even get a chance to apply.
    # A hard 15s cap here means "MOFA unreachable" is detected fast and
    # consistently, instead of sometimes taking 30s and sometimes 100s+.
    try:
        page.goto(
            "https://visa.mofa.gov.sa/visaservices/searchvisa",
            wait_until="domcontentloaded",
            timeout=15000,
        )
    except PWTimeoutError as e:
        raise PWTimeoutError(
            f"MOFA site did not respond within 15s (likely unreachable): {e}"
        ) from e
    time.sleep(1)

    
    page.locator("#ddlFirstValue").select_option("PassPortNo")
    page.locator("#tbFirstValue").fill(passport_no)
    page.locator("#ddlSecondValue").select_option("fName")
    page.locator("#tbSecondValue").fill("A")
    page.locator("#NationalityId").select_option(nationality_code)
    page.locator('input[name="ReaderType"][value="1"]').check()

    print("[*] Submitting the form...")
    page.locator("#btnSubmit").click()
    time.sleep(1)

    captcha_input = page.locator(
        "input[id*='captcha' i], input[name*='captcha' i], input#CaptchaCode, "
        "input#txtCaptcha, input#imgCaptcha, input.CaptchaCode"
    ).first
    captcha_img_locator = page.locator(
        "img[src*='captcha' i], img[id*='captcha' i], #imgCaptcha, .captcha-image"
    ).first
    has_captcha = captcha_input.count() > 0 and captcha_input.is_visible()

    # Wrong/expired CAPTCHA answers don't produce an error modal or a
    # field-validation-error on this site -- MOFA just re-renders the same
    # search form with a fresh CAPTCHA challenge. Previously the only wait
    # condition was success/error-modal/validation-error, so a rejected
    # CAPTCHA fell through none of those and just hung for the full 30s
    # before being skipped as a timeout (this is what the deployed logs
    # show: two consecutive 30s timeouts for the same passport). We now
    # retry the CAPTCHA solve/submit up to MAX_CAPTCHA_ATTEMPTS times,
    # treating "the captcha field is visible again" as its own recognized
    # end-state instead of silently waiting out the clock on it.
    MAX_CAPTCHA_ATTEMPTS = 3

    if has_captcha:
        for attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
            if deadline is not None and time.monotonic() >= deadline:
                raise MofaStartupTimeout(
                    f"MOFA download did not start within {STARTUP_TIMEOUT_SECONDS}s "
                    f"(stalled during CAPTCHA attempt {attempt})."
                )
            print(f"[!] CAPTCHA field detected (attempt {attempt}/{MAX_CAPTCHA_ATTEMPTS}). Sending to server for solving...")

            error_modal = page.locator("#dlgMessage").first
            if error_modal.is_visible():
                page.locator(
                    "#dlgMessage .close, #dlgMessage button[data-dismiss='modal'], #dlgMessage .btn"
                ).first.click()
                time.sleep(1.5)

            captcha_path = os.path.join(BASE_DIR, "captcha_retry.png")
            if captcha_img_locator.is_visible():
                captcha_img_locator.screenshot(path=captcha_path)
            else:
                viewport = page.viewport_size
                clip = {
                    "x": 0,
                    "y": viewport["height"] // 2,
                    "width": viewport["width"],
                    "height": viewport["height"] // 2,
                }
                page.screenshot(path=captcha_path, clip=clip)

            captcha_code = _solve_captcha_remote(captcha_path)

            current_nationality = page.locator("#NationalityId").input_value()
            if not current_nationality or current_nationality in ("", "0"):
                page.locator("#ddlFirstValue").select_option("PassPortNo")
                page.locator("#tbFirstValue").fill(passport_no)
                page.locator("#ddlSecondValue").select_option("fName")
                page.locator("#tbSecondValue").fill("A")
                page.locator("#NationalityId").select_option(nationality_code)
                page.locator('input[name="ReaderType"][value="1"]').check()

            if captcha_code and len(captcha_code) >= 4:
                captcha_input.fill("")
                captcha_input.press_sequentially(captcha_code, delay=150)
            else:
                print("[-] Failed to get CAPTCHA code from server. Proceeding without it (will likely fail).")

            print("[*] Submitting the form (with CAPTCHA)...")
            page.locator("#btnSubmit").click()

            print("[*] Waiting for result (30s timeout)...")
            try:
                page.wait_for_function("""
                    () => document.querySelector('.evisa-container') ||
                          (document.querySelector('#dlgMessage') && getComputedStyle(document.querySelector('#dlgMessage')).display !== 'none') ||
                          document.querySelector('.field-validation-error') ||
                          (() => {
                              const el = document.querySelector("input[id*='captcha' i], input[name*='captcha' i], input#CaptchaCode, input#txtCaptcha, input#imgCaptcha, input.CaptchaCode");
                              return !!(el && el.offsetParent !== null);
                          })()
                """, timeout=30000)
            except PWTimeoutError:
                if attempt >= MAX_CAPTCHA_ATTEMPTS:
                    raise
                print(f"[-] No recognized state after {attempt} attempt(s) (page truly stalled) -- retrying...")
                continue

            # If a fresh CAPTCHA challenge is showing again, the previous
            # code was rejected -- loop back and solve the new one, unless
            # we're out of attempts.
            still_has_captcha = captcha_input.count() > 0 and captcha_input.is_visible()
            if still_has_captcha:
                if attempt >= MAX_CAPTCHA_ATTEMPTS:
                    raise RuntimeError(
                        f"MOFA rejected the CAPTCHA {MAX_CAPTCHA_ATTEMPTS} time(s) in a row for passport {passport_no}."
                    )
                print(f"[-] CAPTCHA rejected (attempt {attempt}/{MAX_CAPTCHA_ATTEMPTS}) -- retrying with a new challenge...")
                time.sleep(1)
                continue

            # Any other recognized end-state (success/error modal/validation
            # error) -- fall through to the normal handling below.
            break
    else:
        print("[*] Waiting for result (30s timeout)...")
        page.wait_for_function("""
            () => document.querySelector('.evisa-container') ||
                  (document.querySelector('#dlgMessage') && getComputedStyle(document.querySelector('#dlgMessage')).display !== 'none') ||
                  document.querySelector('.field-validation-error')
        """, timeout=30000)

    final_error_modal = page.locator("#dlgMessage").first
    if final_error_modal.is_visible():
        error_text = page.locator("#dlgMessage .modal-body").first.inner_text().strip()
        raise RuntimeError(f"MOFA error popup: {error_text}")

    print("[*] Success! Waiting for layout/images to render...")
    page.wait_for_load_state("load")
    time.sleep(1)

    page.pdf(
        path=output_filename,
        format="A4",
        print_background=True,
        margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
    )
    print(f"[+] PDF saved: {output_filename}")



def _classify_download_error(exc) -> str:
    """
    Turn a raw exception from _download_one() into a short, user-facing
    reason. Playwright network failures (DNS, connection refused, TLS,
    or navigation timeout while the page was still loading MOFA's own
    site) all indicate the MOFA site itself is unreachable right now --
    that's a distinct, actionable message from "this one passport's
    CAPTCHA/search failed".
    """
    text = str(exc)
    lower = text.lower()
    unreachable_markers = (
        "net::err_", "err_connection", "err_name_not_resolved",
        "err_internet_disconnected", "err_timed_out", "err_connection_refused",
        "err_connection_reset", "ssl", "did not respond within 15s",
        "timeout 15000ms exceeded", "timeout 30000ms exceeded",
        "navigating to \"https://visa.mofa.gov.sa",
    )
    if isinstance(exc, PWTimeoutError) or any(m in lower for m in unreachable_markers):
        return "MOFA site is unreachable or timed out. Please try again later."
    if isinstance(exc, CaptchaServiceUnavailable):
        return str(exc)
    if "captcha" in lower:
        return "MOFA rejected the CAPTCHA repeatedly for this passport."
    if "mofa error popup" in lower:
        return text
    return f"Unexpected error: {text[:200]}"


class MofaStartupTimeout(Exception):
    """Raised when the very first record of a job hasn't finished (success
    or failure) within STARTUP_TIMEOUT_SECONDS of the job starting. This
    stops the whole job early instead of letting the CAPTCHA retry loop
    and per-navigation timeouts silently compound past a minute or more
    with the UI showing nothing."""
    pass


STARTUP_TIMEOUT_SECONDS = 40


def _process_records(page, records, job_id=None, deadline=None):
    """
    Inner loop: download all records using an already-open Playwright page.
    Called by _run_once() with a fresh page created on the same thread.
    Skips individual passports on error/timeout and continues with the rest.
    Ticks job_id's progress counter (done/failed) after each record so
    callers can report live "X of N" counts (see get_job_progress()), and
    records a classified, user-facing reason for the most recent failure
    so the UI isn't left with only a bare failed count.

    deadline: a time.monotonic() value by which the FIRST record must have
    finished (done or failed) -- if it hasn't, the whole job stops early
    with a MofaStartupTimeout instead of continuing to wait. Only checked
    before the first record starts and applies solely to that first
    record; once anything has completed, the job runs to normal
    completion for the rest of the batch.
    """
    for i, rec in enumerate(records):
        if i == 0 and deadline is not None and time.monotonic() >= deadline:
            # Startup already took too long before we even began (e.g. the
            # browser/context launch itself stalled) -- bail immediately.
            raise MofaStartupTimeout(
                f"MOFA download did not start within {STARTUP_TIMEOUT_SECONDS}s."
            )

        passport_number  = rec["passport_number"]
        first_name       = rec["given_names"]
        nationality_code = rec["nationality"]
        group_name       = _sanitize_group(rec["group_name"])

        group_dir = os.path.join(VISA_PROCESSED_ROOT, group_name)
        os.makedirs(group_dir, exist_ok=True)
        output_filename = os.path.join(group_dir, f"{passport_number}_visa.pdf")

        logger.info(f"Downloading visa PDF for passport {passport_number} (group={group_name})...")
        try:
            _download_one(
                page, passport_number, first_name, nationality_code, output_filename,
                deadline=(deadline if i == 0 else None),
            )
            _mark_downloaded(rec["passport_id"])
            _job_tick(job_id, success=True)
        except MofaStartupTimeout:
            # Only ever raised for the first record -- propagate up so the
            # whole job stops instead of being caught/skipped like a normal
            # per-record failure.
            raise
        except PWTimeoutError as e:
            logger.warning(f"Skipping passport {passport_number} (timeout): {e}")
            _job_tick(job_id, success=False, error_message=_classify_download_error(e))
            continue
        except Exception as e:
            logger.warning(
                f"Skipping passport {passport_number} due to error: {e}\n{traceback.format_exc()}"
            )
            _job_tick(job_id, success=False, error_message=_classify_download_error(e))
            continue


def _run_once(records, job_id=None):
    """
    Download all records in one Playwright browser session.

    Always launches a fresh browser on the calling thread and closes it when
    done. This is the ONLY safe pattern here: Playwright's sync API binds a
    Page to the OS thread (greenlet dispatcher) that created it, so a page
    created on one thread cannot be driven from another thread -- doing so
    raises `greenlet.error: cannot switch to a different thread`. Creating
    and using the browser/page on the same thread, within the same call,
    sidesteps that entirely. (This is also why the STARTUP_TIMEOUT_SECONDS
    cap below is enforced as a deadline checked from *within* this same
    thread at safe points, rather than by a separate watchdog thread
    reaching in to cancel Playwright calls.)

    If the first record hasn't finished (succeeded or failed) within
    STARTUP_TIMEOUT_SECONDS of this call starting, the whole job stops
    early with a single job-level failure and a clear reason, instead of
    silently continuing to wait past a minute or more with the UI showing
    nothing (see MofaStartupTimeout / STARTUP_TIMEOUT_SECONDS).
    """
    logger.info(f"_run_once: {len(records)} pending record(s) to download.")
    if not records:
        _job_finish(job_id)
        return

    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS

    with _run_once_lock:
        logger.info("_run_once: launching fresh browser.")
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                locale="ar-SA",
            )
            page = context.new_page()
            page.add_init_script("window.print = () => {};")
            try:
                _process_records(page, records, job_id=job_id, deadline=deadline)
            except MofaStartupTimeout as e:
                logger.warning(f"_run_once: startup timeout, stopping job early: {e}")
                # Mark every record in this batch as failed with the same
                # clear reason -- the job stops here rather than trying
                # the remaining records, since a startup stall this early
                # means MOFA (or our own network path to it) is down for
                # the whole session, not just one passport.
                if job_id:
                    progress = get_job_progress(job_id) or {"done": 0, "failed": 0}
                    remaining = len(records) - progress["done"] - progress["failed"]
                    for _ in range(max(0, remaining)):
                        _job_tick(job_id, success=False, error_message=str(e))
            finally:
                browser.close()

    _job_finish(job_id)
    logger.info("_run_once: finished processing all pending records for this run.")


# ---------------------------------------------------------------------------
# Background watch loop (hourly polling)
# ---------------------------------------------------------------------------

_background_thread = None
_background_lock   = threading.Lock()


def main():
    logger.info("mofa_downloader main() starting...")
    try:
        _ensure_column()
    except Exception as e:
        logger.error(f"FATAL: _ensure_column() failed, thread cannot continue: {e}\n{traceback.format_exc()}")
        return
    logger.info(f"Watching for processed-but-not-downloaded passports every {POLL_INTERVAL_SECONDS}s...")
    poll_count = 0
    while True:
        poll_count += 1
        try:
            records = _get_pending_records()
            if records:
                logger.info(f"Found {len(records)} pending record(s). Starting download run...")
                _run_once(records)
            elif poll_count % HEARTBEAT_EVERY_N_POLLS == 0:
                # Periodic proof-of-life so a quiet DB isn't mistaken for a dead/stuck thread.
                logger.info("Still watching, no pending records.")
        except Exception as e:
            logger.error(f"Unexpected error in watch loop: {e}\n{traceback.format_exc()}")
        time.sleep(POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Single-record trigger (used by the "Check Visa" badge on the results page)
# ---------------------------------------------------------------------------

_single_download_lock        = threading.Lock()
_single_download_in_progress = set()  # passport_ids currently being downloaded one-off


def _run_single(passport_id, user_id=None, job_id=None):
    global _current_user_id
    if user_id is not None:
        _current_user_id = user_id
    try:
        rec = _get_record_by_id(passport_id)
        if not rec:
            logger.warning(f"trigger_single_download: no passport found for id={passport_id}")
            _job_finish(job_id)
            return
        logger.info(
            f"trigger_single_download: starting one-off download for passport_id={passport_id} "
            f"(passport_number={rec['passport_number']})."
        )
        _run_once([rec], job_id=job_id)
    except Exception as e:
        logger.error(
            f"trigger_single_download: run failed for passport_id={passport_id}: "
            f"{e}\n{traceback.format_exc()}"
        )
        _job_finish(job_id)
    finally:
        with _single_download_lock:
            _single_download_in_progress.discard(passport_id)


def trigger_single_download(passport_id, user_id=None):
    """
    Kicks off a one-off MOFA download for a single passport_id in a
    background thread, independent of the main watch loop's polling. Used
    by the results-page "Check Visa" badge: the caller (a Flask route)
    should call this and return immediately -- the frontend then polls
    /api/mofa_visa/status until mofa_pdf_downloaded_at is set.

    Runs regardless of the record's is_processed/is_visa_processed state --
    the caller is responsible for deciding when a single-record trigger is
    appropriate.

    Safe to call repeatedly for the same passport_id while a run is already
    in progress -- subsequent calls are no-ops until the first finishes.

    Returns a dict {"started": bool, "job_id": str|None}.
    """
    with _single_download_lock:
        if passport_id in _single_download_in_progress:
            logger.info(
                f"trigger_single_download: passport_id={passport_id} already in progress; "
                "ignoring duplicate trigger."
            )
            return {"started": False, "job_id": None}
        _single_download_in_progress.add(passport_id)

    try:
        _ensure_column()
    except Exception as e:
        logger.error(f"trigger_single_download: _ensure_column() failed: {e}\n{traceback.format_exc()}")
        with _single_download_lock:
            _single_download_in_progress.discard(passport_id)
        return {"started": False, "job_id": None}

    job_id = _new_job(total=1)
    t = threading.Thread(
        target=_run_single,
        args=(passport_id,),
        kwargs={"user_id": user_id, "job_id": job_id},
        name=f"mofa_single_{passport_id}",
        daemon=True,
    )
    t.start()
    return {"started": True, "job_id": job_id}


# ---------------------------------------------------------------------------
# Batch trigger (used by the eVisa button on the results page for 2+ records)
# ---------------------------------------------------------------------------

def trigger_batch_download(passport_ids, user_id=None):
    """
    Runs all given passport_ids through ONE Playwright browser session in a
    single background thread. CAPTCHA is solved only if/when the MOFA site
    challenges -- typically only on the first record; remaining records
    reuse the same page/cookies so no repeat challenge appears.

    Returns a dict {"started": bool, "job_id": str|None}. job_id can be
    polled via get_job_progress(job_id) to get live {"total", "done",
    "failed", "finished"} counts for this batch -- this is what lets the
    group page show real per-passport progress instead of only a
    group-has-any-pdf-yet/no signal.
    """
    global _current_user_id
    if user_id is not None:
        _current_user_id = user_id

    try:
        _ensure_column()
    except Exception as e:
        logger.error(f"trigger_batch_download: _ensure_column() failed: {e}\n{traceback.format_exc()}")
        return {"started": False, "job_id": None}

    job_id = _new_job(total=len(passport_ids))

    def _run_batch():
        records = []
        for pid in passport_ids:
            rec = _get_record_by_id(pid)
            if rec:
                records.append(rec)
            else:
                logger.warning(f"trigger_batch_download: no record found for passport_id={pid}, skipping.")
        if not records:
            logger.warning("trigger_batch_download: no valid records to process.")
            _job_finish(job_id)
            return
        logger.info(
            f"trigger_batch_download: starting batch of {len(records)} passport(s)."
        )
        _run_once(records, job_id=job_id)

    t = threading.Thread(target=_run_batch, name="mofa_batch", daemon=True)
    t.start()
    logger.info(f"trigger_batch_download: batch thread started for {len(passport_ids)} passport(s), job_id={job_id}.")
    return {"started": True, "job_id": job_id}


# ---------------------------------------------------------------------------
# App startup: background watch loop
# ---------------------------------------------------------------------------

def start_background(user_id=None):
    """
    Starts the continuous watch loop (polls every hour, runs immediately when
    pending records are found) in a background thread, exactly once per app
    process. Safe to call on every login; subsequent calls after the first
    are no-ops for the watch loop.
    """
    global _background_thread, _current_user_id
    logger.info("start_background() called.")
    if user_id is not None:
        _current_user_id = user_id

    with _background_lock:
        if _background_thread is not None and _background_thread.is_alive():
            logger.info("MOFA downloader background thread already running; not starting another.")
        else:
            _background_thread = threading.Thread(target=main, name="mofa_downloader", daemon=True)
            _background_thread.start()
            logger.info("MOFA downloader background thread started.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--clear-all-passports":
        # Usage: python mofa_downloader.py --clear-all-passports
        clear_all_passports()
    elif len(sys.argv) >= 2 and sys.argv[1] == "--mark-all-processed":
        # Usage: python mofa_downloader.py --mark-all-processed
        mark_all_processed()
    elif len(sys.argv) >= 3 and sys.argv[1] == "--mark-processed":
        # Usage: python mofa_downloader.py --mark-processed <passport_number> <nusuk|visit_visa>
        passport_number = sys.argv[2]
        visa_type = sys.argv[3] if len(sys.argv) >= 4 else "nusuk"
        mark_processed(passport_number, visa_type)
    else:
        main()