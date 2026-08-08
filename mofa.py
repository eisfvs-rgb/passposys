"""
mofa_downloader.py
-------------------
Fully standalone local script -- everything runs on this machine, including
CAPTCHA solving (via Groq directly, no server call).

  1. Query DB for passports that are "processed" (nusuk: is_processed=TRUE,
     visit_visa: is_visa_processed=TRUE) and not yet downloaded from MOFA
     (mofa_pdf_downloaded_at IS NULL), ordered by processed date ascending.
  2. For each record, use Playwright (local, headless) to log in / search /
     submit the MOFA form.
  3. If a CAPTCHA field appears, screenshot it and solve it locally via the
     Groq Vision API (GROQ_API_KEY must be set in the environment).
  4. On success: save PDF to visa_processed/<group_name>/<passport_number>_...pdf
     and set mofa_pdf_downloaded_at = NOW() in the DB.
  5. On any failure: skip this record, continue to the next, until the last one.
  6. Sleep, repeat every 6 hours (main()), or call start_background() to run
     the same loop in a background thread (e.g. triggered on app login).

Requires a new column on `passports`:
    ALTER TABLE passports ADD COLUMN mofa_pdf_downloaded_at TIMESTAMP NULL DEFAULT NULL;
(added automatically at startup by _ensure_column(), mirroring db.py's
migration style.)

Dependencies: playwright, groq, mysql-connector-python.
Environment: GROQ_API_KEY must be set. DB connection uses DB_CONFIG below
(edit directly, or set DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD env vars).
This script does NOT import config.py/db.py -- no dependency on env.enc.
"""

import os
import time
import base64
import re
import logging
import traceback

import mysql.connector
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
from groq import Groq

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mofa_downloader")

VISA_PROCESSED_ROOT = "visa_processed"
RUN_INTERVAL_SECONDS = 6 * 60 * 60  # 6 hours

# Standalone DB connection -- does not import config.py/db.py, so this
# script has no dependency on env.enc / HOST_API_SECRET. Edit these values
# (or set the matching environment variables) to match your MySQL setup.
DB_CONFIG = {
    'host': os.environ.get('DB_HOST', '127.0.0.1'),
    'port': int(os.environ.get('DB_PORT', 3307)),
    'database': os.environ.get('DB_NAME', 'passport_db'),
    'user': os.environ.get('DB_USER', 'passport_user'),
    'password': os.environ.get('DB_PASSWORD', 'passposys_local'),
}


def get_connection():
    return mysql.connector.connect(**DB_CONFIG)


# ---------------------------------------------------------------------------
# Local CAPTCHA solving via Groq
# ---------------------------------------------------------------------------

def _solve_captcha_local(image_path):
    """Sends the CAPTCHA screenshot to Groq Vision API and extracts the digits."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        logger.warning("GROQ_API_KEY not set in environment. Cannot solve CAPTCHA.")
        return ""

    client = Groq(api_key=api_key)
    try:
        with open(image_path, "rb") as f:
            base64_image = base64.b64encode(f.read()).decode("utf-8")

        chat_completion = client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract the numeric CAPTCHA digits shown in this image. Output ONLY the raw digits without explanation."},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}},
                    ],
                }
            ],
            model="qwen/qwen3.6-27b",
            temperature=0.0,
            max_tokens=300,
        )
        raw_response = chat_completion.choices[0].message.content.strip()
        cleaned_text = re.sub(r'<think>.*?</think>', '', raw_response, flags=re.DOTALL).strip()
        captcha_code = re.sub(r'\D', '', cleaned_text)
        return captcha_code
    except Exception as e:
        logger.warning(f"Groq CAPTCHA solve failed: {e}")
        return ""


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
    Marks ONLY unprocessed passports as processed (matching whichever flag applies to
    its visa_type), and does NOT reset mofa_pdf_downloaded_at.
    Returns (nusuk_count, visit_visa_count).
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Update Nusuk visas: Only where is_processed is False/Null
    cursor.execute("""
        UPDATE passports p
        JOIN general_data g ON g.passport_id = p.id
        SET p.is_processed = TRUE,
            p.processed_at = NOW()
        WHERE COALESCE(g.visa_type, 'nusuk') = 'nusuk'
          AND (p.is_processed = FALSE OR p.is_processed IS NULL)
    """)
    nusuk_count = cursor.rowcount

    # Update Visit visas: Only where is_visa_processed is False/Null
    cursor.execute("""
        UPDATE passports p
        JOIN general_data g ON g.passport_id = p.id
        SET p.is_visa_processed = TRUE,
            p.visa_processed_at = NOW()
        WHERE g.visa_type = 'visit_visa'
          AND (p.is_visa_processed = FALSE OR p.is_visa_processed IS NULL)
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


def _mark_downloaded(passport_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE passports SET mofa_pdf_downloaded_at = NOW() WHERE id = %s", (passport_id,))
    conn.commit()
    cursor.close()
    conn.close()


# ---------------------------------------------------------------------------
# Playwright: local login/search/submit/PDF-save, with remote captcha solving
# ---------------------------------------------------------------------------

def _download_one(page, passport_no, first_name, nationality_code, output_filename):
    print(f"[*] Navigating to MOFA Visa Search for {passport_no}...")
    page.goto("https://visa.mofa.gov.sa/visaservices/searchvisa", wait_until="networkidle")
    time.sleep(2)

    page.locator("#ddlFirstValue").select_option("PassPortNo")
    page.locator("#tbFirstValue").fill(passport_no)
    page.locator("#ddlSecondValue").select_option("fName")
    page.locator("#tbSecondValue").fill(first_name)
    page.locator("#NationalityId").select_option(nationality_code)
    page.locator('input[name="ReaderType"][value="1"]').check()

    print("[*] Submitting the form...")
    page.locator("#btnSubmit").click()
    time.sleep(3)

    captcha_input = page.locator(
        "input[id*='captcha' i], input[name*='captcha' i], input#CaptchaCode, "
        "input#txtCaptcha, input#imgCaptcha, input.CaptchaCode"
    ).first
    captcha_img_locator = page.locator(
        "img[src*='captcha' i], img[id*='captcha' i], #imgCaptcha, .captcha-image"
    ).first
    has_captcha = captcha_input.count() > 0 and captcha_input.is_visible()

    if has_captcha:
        print("[!] CAPTCHA field detected. Sending to server for solving...")

        error_modal = page.locator("#dlgMessage").first
        if error_modal.is_visible():
            page.locator(
                "#dlgMessage .close, #dlgMessage button[data-dismiss='modal'], #dlgMessage .btn"
            ).first.click()
            time.sleep(1.5)

        captcha_path = "captcha_retry.png"
        if captcha_img_locator.is_visible():
            captcha_img_locator.screenshot(path=captcha_path)
        else:
            viewport = page.viewport_size
            clip = {"x": 0, "y": viewport["height"] // 2, "width": viewport["width"], "height": viewport["height"] // 2}
            page.screenshot(path=captcha_path, clip=clip)

        captcha_code = _solve_captcha_local(captcha_path)

        current_nationality = page.locator("#NationalityId").input_value()
        if not current_nationality or current_nationality in ("", "0"):
            page.locator("#ddlFirstValue").select_option("PassPortNo")
            page.locator("#tbFirstValue").fill(passport_no)
            page.locator("#ddlSecondValue").select_option("fName")
            page.locator("#tbSecondValue").fill(first_name)
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
    page.wait_for_load_state("networkidle")
    time.sleep(2)

    page.pdf(
        path=output_filename,
        format="A4",
        print_background=True,
        margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
    )
    print(f"[+] PDF saved: {output_filename}")


def _run_once():
    records = _get_pending_records()
    logger.info(f"Found {len(records)} pending record(s) to download.")
    if not records:
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="ar-SA",
        )
        page = context.new_page()
        page.add_init_script("window.print = () => {};")

        try:
            for rec in records:
                passport_number = rec["passport_number"]
                first_name = rec["given_names"]
                nationality_code = rec["nationality"]
                group_name = _sanitize_group(rec["group_name"])

                group_dir = os.path.join(VISA_PROCESSED_ROOT, group_name)
                os.makedirs(group_dir, exist_ok=True)  # reuse existing folder if present
                output_filename = os.path.join(group_dir, f"{passport_number}_visa.pdf")

                logger.info(f"Downloading visa PDF for passport {passport_number} (group={group_name})...")
                try:
                    _download_one(page, passport_number, first_name, nationality_code, output_filename)
                    _mark_downloaded(rec["passport_id"])
                except PWTimeoutError as e:
                    logger.warning(f"Skipping passport {passport_number} (timeout): {e}")
                    continue
                except Exception as e:
                    logger.warning(f"Skipping passport {passport_number} due to error: {e}\n{traceback.format_exc()}")
                    continue
        finally:
            browser.close()

    logger.info("Finished processing all pending records for this run.")


import threading

_background_thread = None
_background_lock = threading.Lock()


def main():
    _ensure_column()
    while True:
        logger.info("=== Starting MOFA download run ===")
        try:
            _run_once()
        except Exception as e:
            logger.error(f"Unexpected error in run loop: {e}\n{traceback.format_exc()}")
        logger.info(f"Sleeping {RUN_INTERVAL_SECONDS // 3600} hours until next run...")
        time.sleep(RUN_INTERVAL_SECONDS)


def start_background():
    """
    Starts the login -> every-6-hours loop in a background thread, exactly
    once per app process. Safe to call on every login; subsequent calls
    after the first are no-ops.
    """
    global _background_thread
    with _background_lock:
        if _background_thread is not None and _background_thread.is_alive():
            logger.info("MOFA downloader background thread already running; not starting another.")
            return
        _background_thread = threading.Thread(target=main, name="mofa_downloader", daemon=True)
        _background_thread.start()
        logger.info("MOFA downloader background thread started.")


if __name__ == "__main__":
    import sys
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