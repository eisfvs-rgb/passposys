"""
ocr_client.py
-------------
LOCAL-SIDE drop-in replacement for the network-calling parts of
ocr_mrz.py. Runs on the desktop machine, alongside host_api.py, and
follows the exact same "talk to the VPS over HTTPS with a shared secret"
pattern.

What stays here (needs the full local image file, no Vision credentials
involved at all):
  - _resource_path, _find_cascade, _face_cascade   (face-detection setup)
  - _find_mrz_zone_opencv, _crop_bottom_strip_for_path  (MRZ-strip cropping)
  - convert_mrz_date, estimate_issue_date          (pure date logic)

What moved to the VPS (ocr_scan_service.py) — this module only crops the
strip locally, then POSTs it there and returns the JSON result:
  - ProvA Computer Vision + ProvB Cloud Vision client setup
  - Batch + individual OCR calls
  - Both MRZ-line assembly algorithms

Every public function below has the SAME NAME AND SIGNATURE as the
matching function used to have in ocr_mrz.py, so app_core.py /
app_routes.py / reparse_routes.py only need their import line changed
from `from ocr_mrz import (...)` to `from ocr_client import (...)` —
nothing else in those files changes.

Configure in config.py (or override via environment variables), same
convention as host_api.py:

    OCR_API_BASE    = "https://pms.passposys.com/ocr"
    OCR_API_SECRET  = "<shared secret — must match ocr_scan_service.py's OCR_SERVICE_SECRET>"
"""

import os
import re
import io
import sys
import time
import json
import base64
import logging
import traceback
import requests
import numpy as np
import cv2
import threading

from PIL import Image, ImageOps
from datetime import datetime, timedelta, timezone, date
from logging.handlers import RotatingFileHandler

from time_utils import ist_now
_crop_lock = threading.Lock()
# =====================================================================
# DEBUG LOGGING — writes full upload→crop→HTTP→response trace to
# ocr_debug.log (next to this file). Rotates at 10MB, keeps 5 backups,
# so it never grows unbounded. Set OCR_DEBUG=0 in the environment to
# silence file logging (console prints from the rest of this module are
# unaffected either way).
# =====================================================================
_LOG_PATH = os.environ.get("OCR_DEBUG_LOG_PATH")
if not _LOG_PATH:
    # Match launch.py's pattern exactly: when frozen (PyInstaller exe),
    # __file__ resolves into the temporary _MEIPASS extraction folder,
    # which is deleted when the app closes — so the log must instead be
    # written next to the actual .exe (same folder as passposys.log).
    if getattr(sys, 'frozen', False):
        _LOG_DIR = os.path.dirname(os.path.abspath(sys.executable))
    else:
        _LOG_DIR = os.path.dirname(os.path.abspath(__file__))
    _LOG_PATH = os.path.join(_LOG_DIR, "ocr_debug.log")

ocr_logger = logging.getLogger("ocr_client")
ocr_logger.setLevel(logging.DEBUG)
ocr_logger.propagate = False  # don't double-print through root logger

if not ocr_logger.handlers:
    # File logging is disabled by default — no ocr_debug.log is written.
    # To re-enable, set the environment variable OCR_DEBUG=1.
    if os.environ.get("OCR_DEBUG", "0") == "1":
        _file_handler = RotatingFileHandler(
            _LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        _file_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        ))
        ocr_logger.addHandler(_file_handler)
    else:
        ocr_logger.addHandler(logging.NullHandler())


def _new_request_id():
    """Short unique id so every log line for one image can be grepped together."""
    return f"{int(time.time() * 1000) % 1000000:06d}-{os.getpid()}"


def _safe_json_preview(obj, limit=800):
    """Stringify a response/body for logging without blowing up the log
    file on huge base64 payloads or binary content."""
    try:
        s = json.dumps(obj, default=str)
    except Exception:
        s = str(obj)
    if len(s) > limit:
        return s[:limit] + f"...<truncated, {len(s)} chars total>"
    return s

try:
    from config import OCR_API_BASE, OCR_API_SECRET
except ImportError:
    OCR_API_BASE = os.environ.get("OCR_API_BASE", "https://pms.passposys.com/ocr")
    OCR_API_SECRET = os.environ.get("OCR_API_SECRET", "")
    if not OCR_API_SECRET:
        raise RuntimeError(
            "OCR_API_SECRET is not set. "
            "Add it to config.py or set the OCR_API_SECRET environment variable."
        )

REQUEST_TIMEOUT = 90  # seconds — OCR calls can take longer than auth/quota calls

# BATCH_SIZE is still needed locally: app_routes.py's index() route chunks
# pending_items into groups of this size before calling the batch-scan
# functions, exactly as it did when ocr_mrz.py owned this constant.
BATCH_SIZE = 20

# `primary_client` used to be the ProvA ComputerVisionClient object itself.
# Nothing in app_core.py/app_routes.py/reparse_routes.py actually calls
# methods on it directly (grep confirms it's only ever imported, never
# used beyond that) — it's kept here as None so the import doesn't break
# if something does reference it, but the real client now lives only on
# the VPS.
primary_client = None

# ── Request-scoped user identity ────────────────────────────────────────
# The OCR service's _authorized() check verifies that an install_token
# actually belongs to the user making the request (not just that the
# token is valid for *someone*), to catch cross-account token reuse
# (stale cache, leaked token, wrong-user session, etc.). That check
# needs to know which user this request is acting as.
#
# IMPORTANT: this was originally implemented with threading.local(),
# which does NOT propagate into ThreadPoolExecutor worker threads.
# app_routes.py runs OCR calls (extract_mrz_from_image, scan_single,
# Phase 4/5 individual rescans) inside ThreadPoolExecutor.submit(...),
# which are brand new OS threads with no relation to the Flask request
# thread that called set_current_user_id(). With threading.local(), every
# one of those calls silently got user_id=None, which the server then
# read as "no user_id sent" -> ownership-mismatch -> 401 for EVERY scan,
# even with a perfectly valid, correctly-owned token. That is the "return
# stat is 0 / scanning fully blocked" bug.
#
# Fix: use contextvars.ContextVar instead of threading.local(). Context
# vars are captured via contextvars.copy_context() and DO propagate
# correctly when work is submitted with submit_with_context() below,
# which every ThreadPoolExecutor.submit(...) call site should use
# instead of calling .submit() directly whenever the submitted function
# eventually calls into ocr_client (i.e. makes an OCR request).
import contextvars
import threading

_current_user_id_var = contextvars.ContextVar("ocr_client_current_user_id", default=None)

# ── parse_mrz extras cache ────────────────────────────────────────────────────
# parse_mrz_route on the VPS already computes check_issue_date_rule() and
# estimate_issue_date() and returns them in the same response. Instead of
# throwing those away and then making two more separate HTTP calls to fetch
# the same data, we store them here (thread-local, one slot per worker thread)
# and let check_issue_date_rule() / estimate_issue_date() consume them before
# falling back to a real HTTP call.
#
# Thread-local is the right scope: Phase 5 worker threads each process exactly
# one passport, so storing per-thread is equivalent to per-passport. The value
# is consumed (cleared) on first read to avoid accidentally reusing a previous
# passport's data.
_mrz_extras = threading.local()
_PRE_PARSED_CACHE = {}


def set_current_user_id(user_id):
    """Call this once at the start of handling a request (e.g. right
    after the user is authenticated in app_routes.py) so every OCR call
    made using this context -- including work submitted to a
    ThreadPoolExecutor via submit_with_context() -- is tagged with the
    acting user's id. Required for the server-side cross-user token check."""
    _current_user_id_var.set(user_id)


def clear_current_user_id():
    """Call at the end of a request to avoid a stale user_id leaking
    into a different request if this worker thread is reused."""
    _current_user_id_var.set(None)


def _get_current_user_id():
    return _current_user_id_var.get()


def submit_with_context(executor, fn, *args, **kwargs):
    """Use this instead of executor.submit(fn, *args, **kwargs) for any
    submitted function that (directly or indirectly) ends up calling
    _post_ocr / extract_mrz_from_image / scan_single / etc.

    ThreadPoolExecutor workers are plain new threads and do NOT inherit
    contextvars automatically -- submit() alone will lose the current
    user_id, reproducing the "every scan gets 401'd" bug. Wrapping the
    call with the caller's copied context (contextvars.copy_context())
    fixes that by running the submitted function with the same user_id
    visible to _get_current_user_id().
    """
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, fn, *args, **kwargs)


def with_user_context(user_id_getter):
    """Decorator factory: wraps a Flask route function so that
    set_current_user_id() is applied before the route runs and
    clear_current_user_id() is guaranteed to run afterward -- on every
    return path and on exceptions -- regardless of how many early
    returns the route has. This avoids needing to manually thread a
    try/finally through routes with many existing exit points.

    user_id_getter: a zero-arg callable returning the current user_id
    (e.g. `lambda: session.get('user_id')`), evaluated fresh on each
    call since the decorator is applied once at import time.

    NOTE: this sets the contextvar on the request thread. Any work
    handed off to a ThreadPoolExecutor from inside the route MUST use
    submit_with_context() (not executor.submit() directly) or the
    worker thread will not see this user_id -- see submit_with_context
    docstring above.
    """
    import functools

    def _decorator(fn):
        @functools.wraps(fn)
        def _wrapped(*args, **kwargs):
            try:
                set_current_user_id(user_id_getter())
            except Exception:
                pass
            try:
                return fn(*args, **kwargs)
            finally:
                clear_current_user_id()
        return _wrapped
    return _decorator


ocr_logger.info(
    f"===== ocr_client.py loaded | OCR_API_BASE={OCR_API_BASE} | "
    f"secret_set={bool(OCR_API_SECRET)} | log_file={_LOG_PATH} ====="
)


# =====================================================================
# LOCAL: face-detection cascade + MRZ-strip cropping
# (verbatim from ocr_mrz.py — needs the full local image file, no
# Vision API credentials involved)
# =====================================================================

def _resource_path(rel):
    import sys
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def _find_cascade():
    # 1. Bundled next to exe / script (--add-data "haarcascade...;.")
    p1 = _resource_path("haarcascade_frontalface_default.xml")
    if os.path.exists(p1):
        return p1
    # 2. OpenCV's own built-in data folder (fallback)
    try:
        p2 = os.path.join(os.path.dirname(cv2.__file__), "data", "haarcascade_frontalface_default.xml")
        if os.path.exists(p2):
            return p2
    except Exception:
        pass
    return p1  # return anyway, CascadeClassifier will handle missing gracefully


_CASCADE_PATH = _find_cascade()
_face_cascade = cv2.CascadeClassifier(_CASCADE_PATH)  # loaded once, reused per request


def _find_mrz_zone_opencv(img, w, h):
    """
    OpenCV morphological fallback to locate the MRZ zone.
    Searches the bottom 60% of the image for the widest horizontal
    contour (must span at least 60% of image width) — that is the MRZ band.
    Returns the absolute y coordinate to start the crop from.
    Falls back to h * 0.48 if nothing is found.
    """
    try:
        search_top = int(h * 0.40)
        region = img.crop((0, search_top, w, h))
        cv_img = cv2.cvtColor(np.array(region), cv2.COLOR_RGB2GRAY)

        _, thresh = cv2.threshold(cv_img, 0, 255,
                                  cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

        kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT, (int(w * 0.05), 2)
        )
        closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)

        v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 8))
        dilated = cv2.dilate(closed, v_kernel)

        contours, _ = cv2.findContours(
            dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        if contours:
            wide = [c for c in contours
                    if cv2.boundingRect(c)[2] >= w * 0.60]
            if wide:
                wide.sort(key=lambda c: cv2.boundingRect(c)[1], reverse=True)
                x, y, cw, ch = cv2.boundingRect(wide[0])
                return search_top + max(0, y - 10)
    except Exception as e:
        print(f"  [CropStrip] OpenCV morph failed: {e}")

    return int(h * 0.48)


# Add this constant near the other constants at the top of the file
FACE_PADDING_PX = 40

def _crop_bottom_strip_for_path(image_path, _cached_strip=None, use_face_detection=True):
    """
    Return the MRZ strip from the image.

    OPTIMIZED: If a pre-cropped strip is passed via `_cached_strip` (a PIL Image
    already in memory), that is returned directly, avoiding disk I/O entirely.

    use_face_detection=True  → full smart crop (matches stack_test_app.py):
        1. Detect passport face photo → use its bottom edge minus FACE_PADDING_PX
        2. OpenCV morphological detection as fallback
        3. Sanity clamp to ensure crop is between 35% and 85% of image height
        4. Fixed h*0.52 as last resort
        5. Bottom dead-space trim in all cases
    use_face_detection=True → original behaviour (h*0.52 fixed crop).
        Used by reparse rescan to preserve its existing behaviour exactly.
    """
    if _cached_strip is not None:
        ocr_logger.debug(f"[CropStrip] Using cached strip (in-memory), size={_cached_strip.size}")
        return _cached_strip

    ocr_logger.debug(
        f"[CropStrip] Opening image_path={image_path} | exists={os.path.exists(image_path)} | "
        f"size_on_disk={os.path.getsize(image_path) if os.path.exists(image_path) else 'N/A'} bytes | "
        f"use_face_detection={use_face_detection}"
    )
    with Image.open(image_path) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        ocr_logger.debug(f"[CropStrip] Image loaded: {w}x{h}, mode after convert=RGB")

        with _crop_lock:
            crop_top = None

            # ── Step 1: face detection ──────────────────────────────────
            try:
                cv_full = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
                faces = _face_cascade.detectMultiScale(
                    cv_full,
                    scaleFactor=1.05,
                    minNeighbors=3,
                    minSize=(int(w * 0.08), int(h * 0.08)),
                    maxSize=(int(w * 0.40), int(h * 0.40))
                )
                if len(faces) > 0:
                    faces_sorted = sorted(faces, key=lambda f: f[0] + f[1])
                    fx, fy, fw, fh = faces_sorted[0]
                    face_bottom = fy + fh
                    if face_bottom < h * 0.70:
                        face_padding = max(FACE_PADDING_PX, int(h * 0.05))
                        crop_top = max(0, face_bottom - face_padding)
            except Exception as e:
                print(f"  [CropStrip] Face detection failed: {e}")
                crop_top = None

            # ── Step 2: OpenCV morph fallback ───────────────────────────
            if crop_top is None:
                crop_top = _find_mrz_zone_opencv(img, w, h)

        # ── Step 3: Sanity clamp ────────────────────────────────────
        if not (h * 0.35 <= crop_top <= h * 0.85):
            print(f"  [CropStrip] Crop anchor out of bounds, falling back to 48%")
            crop_top = int(h * 0.48)

        strip = img.crop((0, crop_top, w, h)).copy()

        # ── Step 4: trim bottom dead space ──────────────────────────
        try:
            cv_strip = cv2.cvtColor(np.array(strip), cv2.COLOR_RGB2GRAY)
            _, thresh = cv2.threshold(cv_strip, 0, 255,
                                      cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            row_scores = thresh.mean(axis=1)
            text_rows = np.where(row_scores > 2)[0]
            if len(text_rows) >= 5:
                trimmed_bottom = min(strip.height, int(text_rows[-1]) + 15)
                strip = strip.crop((0, 0, w, trimmed_bottom))
                print(f"  [CropStrip] Bottom trimmed to row {trimmed_bottom}")
        except Exception as e:
            print(f"  [CropStrip] Bottom trim failed: {e}")

        ocr_logger.debug(f"[CropStrip] Final strip size={strip.size} from image {image_path}")
        return strip


# =====================================================================
# LOCAL: pure MRZ date helpers
# (verbatim from ocr_mrz.py — plain date math, no network call)
# =====================================================================




# =====================================================================
# NEW: HTTP client — talks to ocr_scan_service.py on the VPS.
# This logic did not exist before (the scan used to happen in-process);
# it follows the same request/error pattern as host_api.py's _post().
# =====================================================================

def _strip_to_b64(strip_img):
    """Encode a PIL Image strip as a base64 JPEG string for the request body."""
    buf = io.BytesIO()
    strip_img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _post_ocr(path, payload, _request_id=None):
    """POST JSON to the OCR service on the VPS. Raises OCRServiceError on
    any network failure or malformed response, same convention as
    host_api.py's HostAPIError.

    Every call is fully logged: endpoint, payload size/shape, timing,
    HTTP status, response body (truncated), and the full exception
    traceback on failure — written to ocr_debug.log.
    """
    rid = _request_id or _new_request_id()
    url = f"{OCR_API_BASE}/{path.lstrip('/')}"
    body = dict(payload)
    # Read the current value from os.environ first so a secret refreshed
    # at runtime via secrets_client.refresh_env_from_host() (after login)
    # takes effect immediately, without needing to rebuild/restart the app.
    body["secret"] = os.environ.get("OCR_API_SECRET", OCR_API_SECRET)
    try:
        from secrets_client import load_install_token
        install_token = load_install_token()
        if install_token:
            body["install_token"] = install_token
    except Exception:
        pass

    # MAC-lock: attach this machine's MAC address, same as host_api.py's
    # _post_raw does for auth/login and config/secrets calls. Without this,
    # the OCR service's _authorized() check rejects every request with
    # "missing mac_address", even when install_token/user_id are both
    # correct -- this field was previously never sent on this code path.
    try:
        from mac_lock import get_local_mac
        mac_address = get_local_mac()
        if mac_address:
            body["mac_address"] = mac_address
    except Exception:
        pass

    # Tag this request with the acting user's id so the server can verify
    # the install_token actually belongs to this user (catches cross-user
    # token reuse -- see set_current_user_id() above). If the caller never
    # set a current user for this thread, user_id will be omitted and the
    # server-side check will reject the request rather than guess.
    current_user_id = _get_current_user_id()
    if current_user_id is not None:
        body["user_id"] = current_user_id

    # Describe payload shape without dumping base64 image data into the log
    if "strips" in body and isinstance(body["strips"], list):
        payload_desc = f"{len(body['strips'])} strip(s), sizes=" + \
            str([len(s.get("strip_b64", "")) for s in body["strips"]])
    elif "strip_b64" in body:
        payload_desc = f"1 strip, size={len(body.get('strip_b64', ''))} chars (b64)"
    else:
        payload_desc = _safe_json_preview({k: v for k, v in body.items() if k != "secret"})

    ocr_logger.debug(f"[{rid}] --> POST {url} | endpoint={path} | payload={payload_desc}")
    t0 = time.time()

    try:
        resp = requests.post(url, json=body, timeout=REQUEST_TIMEOUT, verify=True)
    except requests.exceptions.RequestException as e:
        elapsed = time.time() - t0
        ocr_logger.error(
            f"[{rid}] <-- NETWORK FAILURE after {elapsed:.2f}s | endpoint={path} | "
            f"url={url} | exception_type={type(e).__name__} | error={e}\n"
            f"[{rid}] Traceback:\n{traceback.format_exc()}"
        )
        raise OCRServiceError(f"Could not reach OCR service ({path}): {e}")

    elapsed = time.time() - t0
    ocr_logger.debug(
        f"[{rid}] <-- HTTP {resp.status_code} in {elapsed:.2f}s | endpoint={path} | "
        f"content-type={resp.headers.get('Content-Type')} | body_len={len(resp.content)}"
    )

    # Log non-2xx responses at ERROR level with as much of the body as we can get,
    # since these are almost certainly the "invalid / server error" cases you're chasing.
    if not resp.ok:
        body_preview = resp.text[:2000] if resp.text else "<empty body>"
        ocr_logger.error(
            f"[{rid}] SERVER ERROR RESPONSE | endpoint={path} | HTTP {resp.status_code} | "
            f"body={body_preview}"
        )

    # Token mismatch / auth failure: stop immediately rather than letting the
    # caller treat this like a transient server error and cascade through the
    # ProvA -> ProvB -> individual-rescan fallback chain (each hop costing
    # several seconds) for a request that will never succeed until the token
    # is fixed. Surface a specific, actionable error instead.
    if resp.status_code == 401:
        try:
            err_body = resp.json()
        except ValueError:
            err_body = {}
        message = err_body.get("message") or "Install token mismatch or invalid. Please log out and log back in."
        ocr_logger.error(f"[{rid}] AUTH REJECTED | endpoint={path} | {message}")
        raise TokenMismatchError(message)

    try:
        data = resp.json()
    except ValueError:
        body_preview = resp.text[:2000] if resp.text else "<empty body>"
        ocr_logger.error(
            f"[{rid}] NON-JSON RESPONSE | endpoint={path} | HTTP {resp.status_code} | "
            f"raw_body={body_preview}"
        )
        raise OCRServiceError(f"OCR service ({path}) returned a non-JSON response (HTTP {resp.status_code}).")

    ocr_logger.debug(f"[{rid}] Parsed JSON response: {_safe_json_preview(data)}")

    if isinstance(data, dict) and data.get("error"):
        ocr_logger.error(f"[{rid}] OCR SERVICE RETURNED ERROR FIELD | endpoint={path} | error={data.get('error')}")

    return data


class OCRServiceError(Exception):
    """Raised when the VPS OCR service is unreachable or returns an unexpected response."""
    pass


class TokenMismatchError(OCRServiceError):
    """Raised when the install token is invalid, stale, or rejected by the
    server (HTTP 401). This is not a transient/network failure -- retrying
    or falling back to another OCR provider will not help, since the same
    token is used for every provider. Callers should stop the scan
    immediately and surface this to the user rather than cascading through
    Phase 2/3/4 fallbacks."""
    pass


def _reset_secondary_client():
    """Drop-in replacement for the old in-process call — tells the VPS to
    re-initialise its ProvB Vision client (e.g. after a clock-skew /
    ACCESS_TOKEN_EXPIRED error). Best-effort: failures are logged, not raised,
    since callers previously treated this as a fire-and-forget reset."""
    try:
        _post_ocr("reset_secondary_client", {})
    except OCRServiceError as e:
        print(f"[ocr_client] Warning: could not reset remote ProvB Vision client: {e}")
        ocr_logger.warning(f"Could not reset remote ProvB Vision client: {e}")


def _do_stacked_primary_scan_from_paths(batch_items):
    """
    Phase 2: Drop-in replacement for the old in-process stacked ProvA scan.
    Crops each item's MRZ strip LOCALLY (identical to the original —
    face-detection-anchored crop, inverted), sends the batch to the VPS in
    one request, and returns the same (results, primary_unreachable) shape
    the rest of the app already expects.

    batch_items: list of {'resized_path', 'index', 'cached_strip'} — same
    shape index()/app_routes.py has always built for this call.
    """
    rid = _new_request_id()
    ocr_logger.debug(f"[{rid}] === Phase 2 (ProvA batch) start | {len(batch_items)} item(s) ===")
    strips = []
    results = {}
    for item in batch_items:
        idx = item['index']
        try:
            strip = _crop_bottom_strip_for_path(
                item['resized_path'],
                _cached_strip=item.get('cached_strip'),
                use_face_detection=True
            )
            strip = ImageOps.invert(strip)
            strips.append({"index": idx, "strip_b64": _strip_to_b64(strip)})
        except Exception as e:
            print(f"Strip load error for index {idx}: {e}")
            ocr_logger.error(
                f"[{rid}] Strip load error for index {idx} | path={item.get('resized_path')} | "
                f"error={e}\n{traceback.format_exc()}"
            )
            results[idx] = None

    if not strips:
        ocr_logger.warning(f"[{rid}] No strips prepared successfully — nothing to send to VPS.")
        return results, False, {}

    try:
        data = _post_ocr("scan_batch_primary", {"strips": strips}, _request_id=rid)
    except TokenMismatchError:
        # Not a transient/provider failure -- retrying against ProvB or any
        # other fallback will fail with the same token. Let this propagate so
        # the caller aborts the whole scan immediately instead of burning
        # time cascading through Phase 3/4.
        ocr_logger.error(f"[{rid}] Phase 2 (ProvA batch) ABORTED — token mismatch, not retrying.")
        raise
    except OCRServiceError as e:
        print(f"[ocr_client] Stacked ProvA batch scan failed: {e}")
        ocr_logger.error(f"[{rid}] Phase 2 (ProvA batch) FAILED for all {len(strips)} strip(s) | error={e}")
        for s in strips:
            results[s["index"]] = None
        return results, True, {}

    remote_results = data.get("results", {})
    remote_raw_text = data.get("raw_text", {})
    ocr_logger.debug(f"[DEBUG issue-date] Phase 2 (ProvA) raw_text keys={list(remote_raw_text.keys())} | non_empty={[bool(v) for v in remote_raw_text.values()]}")
    
    raw_text_results = {}
    for s in strips:
        idx = s["index"]
        item_data = remote_results.get(str(idx))
        
        # INTERCEPT COMBINED SERVER RESPONSE
        if isinstance(item_data, dict) and "mrz_lines" in item_data:
            mrz_lines = item_data.get("mrz_lines")
            results[idx] = mrz_lines
            if mrz_lines:
                _PRE_PARSED_CACHE[tuple(mrz_lines)] = item_data
        else:
            results[idx] = item_data # Fallback
            
        raw_text_results[idx] = remote_raw_text.get(str(idx))
    none_count = sum(1 for v in results.values() if v is None)
    ocr_logger.debug(
        f"[{rid}] === Phase 2 (ProvA batch) done | {len(results) - none_count}/{len(results)} "
        f"assembled MRZ | primary_unreachable={data.get('primary_unreachable', False)} ==="
    )
    return results, bool(data.get("primary_unreachable", False)), raw_text_results


def _do_stacked_secondary_scan(batch_items):
    """
    Phase 3 fallback: Drop-in replacement for the old in-process stacked
    ProvB Vision scan. Same local-crop-then-send pattern as the ProvA
    version above. Returns (results, secondary_calls_used, secondary_unreachable).
    """
    rid = _new_request_id()
    ocr_logger.debug(f"[{rid}] === Phase 3 (ProvB batch fallback) start | {len(batch_items)} item(s) ===")
    strips = []
    results = {}
    for item in batch_items:
        idx = item['index']
        try:
            strip = _crop_bottom_strip_for_path(
                item['resized_path'],
                _cached_strip=item.get('cached_strip'),
                use_face_detection=True
            )
            strip = ImageOps.invert(strip)
            strips.append({"index": idx, "strip_b64": _strip_to_b64(strip)})
        except Exception as e:
            print(f"[ProvB Vision] Strip load error for index {idx}: {e}")
            ocr_logger.error(
                f"[{rid}] Strip load error for index {idx} | path={item.get('resized_path')} | "
                f"error={e}\n{traceback.format_exc()}"
            )
            results[idx] = None

    if not strips:
        ocr_logger.warning(f"[{rid}] No strips prepared successfully — nothing to send to VPS.")
        return results, 0, False, {}

    try:
        data = _post_ocr("scan_batch_secondary", {"strips": strips}, _request_id=rid)
    except TokenMismatchError:
        ocr_logger.error(f"[{rid}] Phase 3 (ProvB batch) ABORTED — token mismatch, not retrying.")
        raise
    except OCRServiceError as e:
        print(f"[ocr_client] Stacked ProvB batch scan failed: {e}")
        ocr_logger.error(f"[{rid}] Phase 3 (ProvB batch) FAILED for all {len(strips)} strip(s) | error={e}")
        for s in strips:
            results[s["index"]] = None
        return results, 0, True, {}

    remote_results = data.get("results", {})
    remote_raw_text = data.get("raw_text", {})
    ocr_logger.debug(f"[DEBUG issue-date] Phase 3 (ProvB) raw_text keys={list(remote_raw_text.keys())} | non_empty={[bool(v) for v in remote_raw_text.values()]}")
    
    raw_text_results = {}
    for s in strips:
        idx = s["index"]
        item_data = remote_results.get(str(idx))
        
        # INTERCEPT COMBINED SERVER RESPONSE
        if isinstance(item_data, dict) and "mrz_lines" in item_data:
            mrz_lines = item_data.get("mrz_lines")
            results[idx] = mrz_lines
            if mrz_lines:
                _PRE_PARSED_CACHE[tuple(mrz_lines)] = item_data
        else:
            results[idx] = item_data # Fallback
            
        raw_text_results[idx] = remote_raw_text.get(str(idx))
    none_count = sum(1 for v in results.values() if v is None)
    ocr_logger.debug(
        f"[{rid}] === Phase 3 (ProvB batch) done | {len(results) - none_count}/{len(results)} "
        f"assembled MRZ | secondary_calls_used={data.get('secondary_calls_used', 0)} | "
        f"secondary_unreachable={data.get('secondary_unreachable', False)} ==="
    )
    return results, int(data.get("secondary_calls_used", 0)), bool(data.get("secondary_unreachable", False)), raw_text_results


def extract_mrz_from_image(image_path, scan_index=None):
    """
    Phase 4: Drop-in replacement for the old in-process individual ProvA
    rescan (+ ProvB fallback). Crops locally with face detection (matching
    the original exactly), sends the single strip to the VPS.

    scan_index (optional, int, 1-based): this passport's position within
    the current batch's Phase 4 individual-scan loop (i.e. i+1 from the
    caller's enumerate() loop). The service only tries LlmB for the first
    4 individual scans of a batch; from the 5th onward it goes straight
    to ProvA -> ProvB. Passing None preserves the old always-try-LlmB
    behavior.
    Returns: (mrz_lines, primary_units_used, raw_text)
    """
    rid = _new_request_id()
    ocr_logger.debug(f"[{rid}] === Phase 4 (ProvA individual) start | image_path={image_path} | scan_index={scan_index} ===")
    try:
        strip = _crop_bottom_strip_for_path(image_path, use_face_detection=True)
        payload = {"strip_b64": _strip_to_b64(strip)}
        if scan_index is not None:
            payload["scan_index"] = scan_index
            
        data = _post_ocr("scan_single", payload, _request_id=rid)
        
        mrz_lines = data.get("mrz_lines")
        
        # --- ADD THIS INTERCEPT BLOCK ---
        fully_parsed = data.get("fully_parsed")
        if mrz_lines and fully_parsed:
            _PRE_PARSED_CACHE[tuple(mrz_lines)] = fully_parsed
        # --------------------------------
        
        ocr_logger.debug(
            f"[{rid}] === Phase 4 done | mrz_assembled={mrz_lines is not None} | "
            f"primary_units_used={data.get('primary_units_used', 0)} ==="
        )
        return mrz_lines, int(data.get("primary_units_used", 0)), data.get("raw_text")
    
    except TokenMismatchError:
        ocr_logger.error(f"[{rid}] Phase 4 ABORTED — token mismatch, not retrying | image_path={image_path}")
        raise
    except OCRServiceError as e:
        print(f"  [ProvA Individual] OCR service error: {e}")
        ocr_logger.error(f"[{rid}] Phase 4 OCRServiceError | image_path={image_path} | error={e}")
        return None, 0, None
    except Exception as e:
        print(f"  [ProvA Individual] Scan error: {e}")
        ocr_logger.error(f"[{rid}] Phase 4 unexpected error | image_path={image_path} | error={e}\n{traceback.format_exc()}")
        return None, 0, None


def extract_mrz_from_image_crop_rescan(image_path):
    """
    "Rescan after crop" on the reparse page. Crops locally with face
    detection (matching the original exactly), sends the single strip to
    the VPS, which assembles with the checksum-validated v2 algorithm.
    Returns: (mrz_lines, primary_units_used, raw_text)
    """
    rid = _new_request_id()
    ocr_logger.debug(f"[{rid}] === Crop Rescan start | image_path={image_path} ===")
    try:
        strip = _crop_bottom_strip_for_path(image_path, use_face_detection=True)
        data = _post_ocr("scan_single_crop_rescan", {"strip_b64": _strip_to_b64(strip)}, _request_id=rid)
        mrz_lines = data.get("mrz_lines")
        # --- ADD THIS INTERCEPT BLOCK ---
        fully_parsed = data.get("fully_parsed")
        if mrz_lines and fully_parsed:
            _PRE_PARSED_CACHE[tuple(mrz_lines)] = fully_parsed
        # --------------------------------
        ocr_logger.debug(
            f"[{rid}] === Crop Rescan done | mrz_assembled={mrz_lines is not None} | "
            f"primary_units_used={data.get('primary_units_used', 0)} ==="
        )
        return mrz_lines, int(data.get("primary_units_used", 0)), data.get("raw_text")
    except OCRServiceError as e:
        print(f"  [Crop Rescan] OCR service error: {e}")
        ocr_logger.error(f"[{rid}] Crop Rescan OCRServiceError | image_path={image_path} | error={e}")
        return None, 0, None
    except Exception as e:
        print(f"  [Crop Rescan] Scan error: {e}")
        ocr_logger.error(f"[{rid}] Crop Rescan unexpected error | image_path={image_path} | error={e}\n{traceback.format_exc()}")
        return None, 0, None


def extract_mrz_from_region_reparse_rescan(region_b64, skip_llmB=False):
    """
    Manual region-select "Re-Scan" on the reparse page. Unlike
    extract_mrz_from_image_reparse_rescan(), this does NOT crop the image
    locally with face detection — the caller has already selected the
    exact region to scan (drawn by the user in the browser), so that
    region is sent to the VPS as-is. Uses the same VPS endpoint/assembler
    as the whole-image reparse rescan.

    region_b64: base64-encoded JPEG bytes of the user-selected crop (no
    data-URL prefix).
    skip_llmB: if True, tells the VPS to skip LlmB and use ProvA directly
    (used for the 2nd rescan/crop-scan click on the same passport).
    Returns: (mrz_lines, primary_units_used, raw_text)
    """
    rid = _new_request_id()
    ocr_logger.debug(f"[{rid}] === Region Rescan start | skip_llmB={skip_llmB} ===")
    try:
        data = _post_ocr("scan_single_reparse_rescan", {"strip_b64": region_b64, "skip_llmB": skip_llmB}, _request_id=rid)
        mrz_lines = data.get("mrz_lines")
        # --- ADD THIS INTERCEPT BLOCK ---
        fully_parsed = data.get("fully_parsed")
        if mrz_lines and fully_parsed:
            _PRE_PARSED_CACHE[tuple(mrz_lines)] = fully_parsed
        # --------------------------------
        ocr_logger.debug(
            f"[{rid}] === Region Rescan done | mrz_assembled={mrz_lines is not None} | "
            f"primary_units_used={data.get('primary_units_used', 0)} ==="
        )
        return mrz_lines, int(data.get("primary_units_used", 0)), data.get("raw_text")
    except OCRServiceError as e:
        print(f"  [Region Rescan] OCR service error: {e}")
        ocr_logger.error(f"[{rid}] Region Rescan OCRServiceError | error={e}")
        return None, 0, None
    except Exception as e:
        print(f"  [Region Rescan] Scan error: {e}")
        ocr_logger.error(f"[{rid}] Region Rescan unexpected error | error={e}\n{traceback.format_exc()}")
        return None, 0, None


def extract_mrz_from_image_reparse_rescan(image_path, skip_llmB=False):
    """
    Manual "Rescan" button on the reparse page. Crops locally WITH
    face detection (same smart crop as every other scan path), sends the
    single strip to the VPS, which assembles with the checksum-validated
    v2 algorithm.
    skip_llmB: if True, tells the VPS to skip LlmB and use ProvA directly
    (used for the 2nd rescan click on the same passport).
    Returns: (mrz_lines, primary_units_used, raw_text)
    """
    rid = _new_request_id()
    ocr_logger.debug(f"[{rid}] === Reparse Rescan start | image_path={image_path} | skip_llmB={skip_llmB} ===")
    try:
        strip = _crop_bottom_strip_for_path(image_path, use_face_detection=True)
        data = _post_ocr("scan_single_reparse_rescan", {"strip_b64": _strip_to_b64(strip), "skip_llmB": skip_llmB}, _request_id=rid)
        mrz_lines = data.get("mrz_lines")
        # --- ADD THIS INTERCEPT BLOCK ---
        fully_parsed = data.get("fully_parsed")
        if mrz_lines and fully_parsed:
            _PRE_PARSED_CACHE[tuple(mrz_lines)] = fully_parsed
        # --------------------------------
        ocr_logger.debug(
            f"[{rid}] === Reparse Rescan done | mrz_assembled={mrz_lines is not None} | "
            f"primary_units_used={data.get('primary_units_used', 0)} ==="
        )
        return mrz_lines, int(data.get("primary_units_used", 0)), data.get("raw_text")
    except OCRServiceError as e:
        print(f"  [Reparse Rescan] OCR service error: {e}")
        ocr_logger.error(f"[{rid}] Reparse Rescan OCRServiceError | image_path={image_path} | error={e}")
        return None, 0, None
    except Exception as e:
        print(f"  [Reparse Rescan] Scan error: {e}")
        ocr_logger.error(f"[{rid}] Reparse Rescan unexpected error | image_path={image_path} | error={e}\n{traceback.format_exc()}")
        return None, 0, None


def _is_token_expired_error(e):
    """Kept for import compatibility — no longer meaningful locally since
    the ProvB Vision client now lives entirely on the VPS. Always returns
    False; the VPS handles its own token-expiry detection and resets."""
    return False


def convert_mrz_date(datestr, is_dob=False):
    """
    Drop-in compatibility shim for the old LOCAL convert_mrz_date() in
    ocr_client.py.

    IMPORTANT: as of the MRZ-parsing migration, parse_mrz() above now
    returns dob/expiry ALREADY CONVERTED to 'YYYY-MM-DD' (the server
    does the conversion in the same response, to avoid a second
    network round-trip). Every existing call site still calls
    convert_mrz_date() again right afterward, e.g.:

        parsed["dob"] = convert_mrz_date(parsed["dob"], is_dob=True) or "1900-01-01"

    Since the input at every real call site is now already a converted
    'YYYY-MM-DD' string (not a raw 6-digit MRZ string), this function's
    only remaining job is to pass that value through unchanged rather
    than re-running a conversion that no longer applies. This keeps
    every call site working with NO changes needed there.

    If a genuinely raw 6-digit MRZ string is ever passed in (e.g. any
    future/unforeseen call site that hasn't gone through parse_mrz()
    first), this returns None rather than guessing -- that call site
    would need its own explicit fix rather than silently relying on
    this shim, since date conversion is no longer available as a
    standalone local operation post-migration.
    """
    if not datestr:
        return None

    # Already-converted 'YYYY-MM-DD' (the expected case, from parse_mrz's
    # bundled conversion) -- pass through unchanged.
    if len(datestr) == 10 and datestr.count('-') == 2:
        return datestr

    # A raw 6-digit MRZ string reached here directly, bypassing
    # parse_mrz() -- this shouldn't happen at any current call site.
    # Log it so it's visible if some future code path needs a real fix,
    # rather than silently returning a wrong/fallback value.
    ocr_logger.warning(
        f"convert_mrz_date() received a non-converted value ({datestr!r}); "
        "this call site may be bypassing parse_mrz() and needs review."
    )
    return None


def parse_mrz(mrz_lines, force=False, raw_text=""):
    rid = _new_request_id()
    ocr_logger.debug(f"[{rid}] === parse_mrz (remote) start | lines={len(mrz_lines)} | force={force} ===")

    # === INSTANT CACHE INTERCEPT ===
    # === INSTANT CACHE INTERCEPT ===
    cache_key = tuple(mrz_lines)
    if not force and cache_key in _PRE_PARSED_CACHE:
        ocr_logger.debug(f"[{rid}] INSTANT CACHE HIT! Bypassing redundant network call.")
        cached_data = _PRE_PARSED_CACHE[cache_key]
        
        _mrz_extras.issue_date_error = cached_data.get("issue_date_error")
        _mrz_extras.estimated_issue_date = cached_data.get("estimated_issue_date")
        
        # UPDATE THIS LINE: Pull the exact source from the server's cache
        _mrz_extras.issue_date_source = cached_data.get("issue_date_source")
        
        return cached_data.get("parsed"), cached_data.get("errors") or []


    try:
        data = _post_ocr(
            "parse_mrz",
            {"mrz_lines": list(mrz_lines), "force": bool(force), "raw_text": raw_text or ""},
            _request_id=rid
        )
    except OCRServiceError as e:
        print(f"  [MRZ Parse] OCR service error: {e}")
        ocr_logger.error(f"[{rid}] parse_mrz OCRServiceError | error={e}")
        return None, ["Could not reach MRZ parsing service. Please check your connection and try again."]
    except Exception as e:
        print(f"  [MRZ Parse] Unexpected error: {e}")
        ocr_logger.error(f"[{rid}] parse_mrz unexpected error | error={e}\n{traceback.format_exc()}")
        return None, ["Unexpected error while parsing MRZ. Please try again."]

    if not isinstance(data, dict):
        ocr_logger.error(f"[{rid}] parse_mrz malformed response (not a dict): {data!r}")
        return None, ["MRZ parsing service returned an unexpected response."]

    parsed = data.get("parsed")
    errors = data.get("errors") or []

    # Cache the extras the VPS already computed so check_issue_date_rule() and
    # estimate_issue_date() can use them without making additional HTTP calls.
    #
    # NOTE: as of the "never estimate, only extracted" change, the VPS's
    # /ocr/parse_mrz no longer calls estimate_issue_date() as a fallback --
    # "estimated_issue_date" here now only ever holds an OCR-EXTRACTED date
    # (or None), despite the legacy key name kept for response compatibility.
    # issue_date_source tells you which: "ocr" or None. Call sites that want
    # the ONLY-extracted issue date should read _mrz_extras.estimated_issue_date
    # right after calling parse_mrz() (or use get_last_issue_date_source()
    # below), rather than calling the separate estimate_issue_date() function,
    # which still performs a real heuristic estimate and should only be used
    # by call sites that explicitly want that fallback behavior.
    _mrz_extras.issue_date_error = data.get("issue_date_error")      # str or None
    _mrz_extras.estimated_issue_date = data.get("estimated_issue_date")  # "YYYY-MM-DD" or None (OCR-extracted only)
    _mrz_extras.issue_date_source = data.get("issue_date_source")    # "ocr" or None
    ocr_logger.debug(
        f"[{rid}] === parse_mrz (remote) done | success={data.get('success')} | "
        f"errors={errors} | cached issue_date_error={_mrz_extras.issue_date_error!r} "
        f"extracted_issue_date={_mrz_extras.estimated_issue_date!r} "
        f"issue_date_source={_mrz_extras.issue_date_source!r} ==="
    )
    ocr_logger.debug(f"[DEBUG issue-date] parse_mrz() VPS response: estimated_issue_date={data.get('estimated_issue_date')!r} "
                     f"| issue_date_source={data.get('issue_date_source')!r} | issue_date_error={data.get('issue_date_error')!r}")
    return parsed, errors


def get_last_extracted_issue_date():
    """
    Returns the OCR-extracted issue date ("YYYY-MM-DD" str, or None) that
    the most recent parse_mrz() call in this thread got back from the VPS,
    as a date object -- or None if parse_mrz() wasn't called first, or no
    printed issue date could be found in the raw OCR text.

    This does NOT estimate. It is a pure read of what parse_mrz() already
    received. Call sites that used to do:

        estimated_issue = estimate_issue_date(parsed.get("expiry"), parsed.get("dob"), parsed.get("country"))
        final_issue_date = estimated_issue if estimated_issue else now.date()

    right after a parse_mrz() call (i.e. raw_text was available) should
    instead call this function and NOT call estimate_issue_date() at all,
    per the "never estimate, only extracted" policy. Single-use: reading
    this clears the cache, same pattern as estimate_issue_date()'s existing
    cache-consumption below, so a later parse_mrz()-less call in the same
    thread doesn't silently reuse a stale value.
    """
    from datetime import datetime as _dt

    if not hasattr(_mrz_extras, "estimated_issue_date"):
        return None

    cached = _mrz_extras.estimated_issue_date
    source = getattr(_mrz_extras, "issue_date_source", None)
    del _mrz_extras.estimated_issue_date
    if hasattr(_mrz_extras, "issue_date_source"):
        del _mrz_extras.issue_date_source

    if not cached or source != "ocr":
        return None
    try:
        return _dt.strptime(cached, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        ocr_logger.warning(f"get_last_extracted_issue_date: malformed cached date ({cached!r})")
        return None


def estimate_issue_date(expiry_date_str, dob_str=None, country_code=None):
    """
    Drop-in replacement for the old LOCAL estimate_issue_date() in
    ocr_client.py. Same signature and return contract as before (a
    date object, or None).

    IMPORTANT: as of the "never estimate, only extracted" policy change,
    /ocr/parse_mrz no longer computes or caches a real estimate -- the
    _mrz_extras.estimated_issue_date value it leaves behind is now the
    OCR-EXTRACTED issue date only (see get_last_extracted_issue_date()
    above), not a heuristic estimate. Reusing that cache here would be
    wrong (an OCR-extracted date is not an estimate, and a None here
    could wrongly short-circuit a real estimate request). This function
    therefore ALWAYS calls the standalone HTTP endpoint below and no
    longer has a "fast path" that consumes parse_mrz()'s cache.

    Call sites should prefer get_last_extracted_issue_date() after
    parse_mrz() and only fall back to this function where an explicit,
    heuristic estimate is genuinely wanted (this function still performs
    the real expiry-minus-validity-years calculation; the "never estimate"
    policy just means most callers should no longer be calling it at all
    for their primary passport_issue_date value).
    """
    from datetime import datetime as _dt

    rid = _new_request_id()
    try:
        data = _post_ocr("estimate_issue_date", {
            "expiry": expiry_date_str,
            "dob": dob_str,
            "country": country_code,
        }, _request_id=rid)
    except Exception as e:
        print(f"  [Issue Date] OCR service error: {e}")
        ocr_logger.error(f"[{rid}] estimate_issue_date error | error={e}")
        return None

    if not isinstance(data, dict) or not data.get("success"):
        return None

    result = data.get("estimated_issue_date")
    if not result:
        return None
    try:
        return _dt.strptime(result, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        ocr_logger.error(f"[{rid}] estimate_issue_date malformed date in response: {result!r}")
        return None


def check_issue_date_rule(parsed):
    """
    Drop-in replacement for the old LOCAL check_issue_date_rule() in
    mrz_utils.py. Same signature/contract — takes the parsed dict,
    returns an error message string if the expiry looks implausible,
    or None if it's fine.

    Fast path: if parse_mrz() was called in this thread just before this
    function, _mrz_extras.issue_date_error holds the result computed by
    the VPS in the same request. Consume it and skip the HTTP call.

    Slow path: no cached value → HTTP call to /ocr/check_issue_date_rule,
    which covers call sites that run outside the parse_mrz → save flow.
    """
    if not parsed:
        return "No parsed data available."

    if hasattr(_mrz_extras, "issue_date_error"):
        result = _mrz_extras.issue_date_error
        # Delete (not just set to None) so the next call in this thread that
        # does NOT go through parse_mrz() first correctly falls back to HTTP.
        del _mrz_extras.issue_date_error
        ocr_logger.debug(f"check_issue_date_rule: using cached value ({result!r})")
        return result   # may be None (no error) or a string (error message)

    rid = _new_request_id()
    try:
        data = _post_ocr("check_issue_date_rule", {"expiry": parsed.get("expiry")}, _request_id=rid)
    except Exception as e:
        print(f"  [Issue Date Rule] OCR service error: {e}")
        ocr_logger.error(f"[{rid}] check_issue_date_rule error | error={e}")
        return "Could not validate issue date (service unreachable)."

    if not isinstance(data, dict) or not data.get("success"):
        return "Could not validate issue date (unexpected response)."

    return data.get("issue_date_error")



def extract_issue_date_provA_remote(image_path):
    """
    Crops the bottom strip locally (anchored by face detection to include the issue date) 
    and asks the VPS to run it through ProvA Vision.
    """
    rid = _new_request_id()
    ocr_logger.debug(f"[{rid}] === ProvA Issue Date Extraction Start | path={image_path} ===")
    try:
        # Use face detection to ensure the crop captures the space just above the MRZ
        strip = _crop_bottom_strip_for_path(image_path, use_face_detection=True)
        
        # Use the existing secure poster to hit the new VPS route
        data = _post_ocr("extract_issue_date_provA", {"strip_b64": _strip_to_b64(strip)}, _request_id=rid)
        
        ocr_logger.debug(f"[{rid}] === ProvA Extraction Done | Success: {data.get('success')} ===")
        return data
    except OCRServiceError as e:
        ocr_logger.error(f"[{rid}] ProvA Extraction OCRServiceError | error={e}")
        return {"success": False, "message": str(e)}
    except Exception as e:
        ocr_logger.error(f"[{rid}] ProvA Extraction unexpected error | error={e}\n{traceback.format_exc()}")
        return {"success": False, "message": str(e)}