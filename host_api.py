"""
host_api.py
-----------
Client module for talking to the remote host (pms.passposys.com).
The host now owns: user accounts, login validation, registration, OTP,
admin user management, quota/upload-limit enforcement, daily usage logs,
and billing records.

The local app keeps NO users table. After a successful login the host
returns a session token + user profile, which is cached in the Flask
session for the duration of the login. Quota checks and usage logging
hit the host on every relevant request; /account pulls a fresh summary
from the host each time the page is loaded.

Configure these in config.py (or override via environment variables):

    HOST_API_BASE   = "https://pms.passposys.com"
    HOST_API_SECRET = "<shared secret — must match the host's secret>"
"""

import requests
import os
import json
import threading

try:
    from config import HOST_API_BASE, HOST_API_SECRET
except ImportError:
    HOST_API_BASE   = os.environ.get("HOST_API_BASE", "https://pms.passposys.com/api")
    HOST_API_SECRET = os.environ.get("HOST_API_SECRET", "")
    if not HOST_API_SECRET:
        raise RuntimeError(
            "HOST_API_SECRET is not set. "
            "Add it to config.py or set the HOST_API_SECRET environment variable."
        )

REQUEST_TIMEOUT = 20  # seconds (raised from 12 - first request after idle can be slow on host)

# ---------------------------------------------------------------------------
# Pending-log retry queue
# Logs that failed due to network errors are stored here and retried on the
# next successful host call. Prevents quota bypass on transient network drops.
# ---------------------------------------------------------------------------
_pending_logs = []          # list of dicts: {user_id, token, count, duplicates, invalids}
_pending_lock = threading.Lock()


def _flush_pending_logs():
    """
    Attempt to send any logs that failed earlier. Called at the start of
    every successful _post() call. Failures stay in the queue for later.
    """
    with _pending_lock:
        if not _pending_logs:
            return
        remaining = []
        for entry in list(_pending_logs):
            try:
                _post_raw("quota/log.php", {
                    "user_id":    entry["user_id"],
                    "count":      entry["count"],
                    "duplicates": entry["duplicates"],
                    "invalids":   entry["invalids"],
                }, token=entry["token"])
                print(f"[host_api] Flushed pending log for user {entry['user_id']}")
            except Exception:
                remaining.append(entry)
        _pending_logs[:] = remaining


def _post_raw(path, payload, token=None):
    """
    Low-level POST — does NOT flush pending logs (avoids infinite recursion).
    """
    url = f"{HOST_API_BASE}/{path.lstrip('/')}"
    body = dict(payload)
    # Read the current value from os.environ first so a secret refreshed
    # at runtime via secrets_client.refresh_env_from_host() (after login)
    # takes effect immediately, without needing to restart the app.
    # Falls back to the value loaded at import time (from env.enc) if
    # os.environ hasn't been updated.
    body["secret"] = os.environ.get("HOST_API_SECRET", HOST_API_SECRET)
    if token:
        body["token"] = token
    from secrets_client import load_install_token
    install_token = load_install_token()
    if install_token:
        body["install_token"] = install_token

    headers = {
        "User-Agent": "PasspoSys/1.0",
        "Accept":     "application/json",
    }

    resp = requests.post(url, json=body, headers=headers, timeout=REQUEST_TIMEOUT, verify=True)

    try:
        data = resp.json()
    except ValueError:
        raise HostAPIError(
            f"Host API ({path}) returned a non-JSON response (HTTP {resp.status_code})."
        )
    return data


def _post(path, payload, token=None):
    """POST JSON to the host API. Flushes any pending logs first."""
    # Flush pending logs before every non-log call so failed logs are
    # retried as soon as connectivity is restored.
    if path != "quota/log.php":
        _flush_pending_logs()

    try:
        return _post_raw(path, payload, token=token)
    except requests.exceptions.RequestException as e:
        raise HostAPIError(f"Could not reach host API ({path}): {e}")


class HostAPIError(Exception):
    """Raised when the host API is unreachable or returns an unexpected response."""
    pass


# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------

def host_login(username, password):
    """
    Validate credentials against the host's users table.

    Returns a dict on success:
        {
          'success': True,
          'token': '...',                # session token, store in Flask session
          'install_token': '...',        # per-device token, persisted locally by this function
          'user_id': 12,
          'username': 'someuser',
          'full_name': 'Some User',
          'is_admin': False,
          'account_level': 'full',       # full | view_only | login_blocked | admin_disabled
          'upload_limit': 500,
        }

    Returns a dict on failure:
        {
          'success': False,
          'error': 'invalid_credentials' | 'account_locked' | 'account_suspended' | 'server_error',
          'message': 'Human readable message',
          'remaining_attempts': 3   # only for invalid_credentials
        }
    """
    from secrets_client import load_install_token, save_install_token, refresh_env_from_host

    payload = {"username": username, "password": password}
    existing_install_token = load_install_token()
    if existing_install_token:
        payload["install_token"] = existing_install_token

    try:
        data = _post("auth/login.php", payload)
    except HostAPIError as e:
        return {"success": False, "error": "server_error", "message": str(e)}

    if not isinstance(data, dict):
        return {"success": False, "error": "server_error", "message": "Unexpected response from host."}

    if data.get("success"):
        new_install_token = data.get("install_token")
        if new_install_token:
            save_install_token(new_install_token)
            # Best-effort: pick up any centrally-rotated secrets right after
            # login. Failure here is silent and non-fatal — the app keeps
            # running on whatever HOST_API_SECRET/OCR_API_SECRET env.enc
            # already loaded at startup.
            try:
                refresh_env_from_host(install_token=new_install_token)
            except Exception:
                pass

    return data


# ---------------------------------------------------------------------------
# QUOTA
# ---------------------------------------------------------------------------

def host_check_upload(user_id, token):
    """
    Ask the host whether this user is allowed to upload right now.

    Returns dict:
        {allowed, reason, amount_due, used, limit, remaining, is_admin}

    On host-unreachable errors, returns 'allowed': False with reason
    'host_unreachable' so the app fails closed rather than silently
    allowing unlimited uploads.
    """
    try:
        data = _post("quota/check.php", {"user_id": user_id}, token=token)
    except HostAPIError as e:
        return {
            "allowed": False, "reason": "host_unreachable", "amount_due": 0.0,
            "used": 0, "limit": 0, "remaining": 0, "error": str(e),
        }

    if not isinstance(data, dict) or "allowed" not in data:
        return {
            "allowed": False, "reason": "host_unreachable", "amount_due": 0.0,
            "used": 0, "limit": 0, "remaining": 0, "error": "Malformed response from host.",
        }

    return data


def host_log_upload(user_id, token, count=1, duplicates=0, invalids=0):
    """
    Tell the host to record today's usage.

    Counts are sanitised before sending:
      - count and duplicates are clamped to >= 0 (cannot reduce quota).
      - invalids >= -1 is allowed (-1 means "undo one previously logged invalid"
        when a user corrects a rejected passport). Values below -1 are clamped.

    If the host is unreachable the entry is queued and retried automatically
    on the next successful host call so quota is never silently skipped.
    """
    # Sanitise values
    count      = max(0, int(count))
    duplicates = max(0, int(duplicates))
    invalids   = max(-1, int(invalids))   # -1 allowed for correction undo; nothing lower

    payload = {
        "user_id":    user_id,
        "count":      count,
        "duplicates": duplicates,
        "invalids":   invalids,
    }

    try:
        _post_raw("quota/log.php", payload, token=token)
    except Exception as e:
        # Queue for retry rather than silently dropping.
        print(f"[host_api] Warning: failed to log usage, queuing for retry: {e}")
        with _pending_lock:
            _pending_logs.append(dict(payload, token=token))


# ---------------------------------------------------------------------------
# ACCOUNT SUMMARY (for /account page)
# ---------------------------------------------------------------------------

def host_account_summary(user_id, token):
    """
    Fetch plan, usage, and billing info for the /account page.

    Returns dict:
        {
          'success': True,
          'plan_summary': {...},     # same shape as old get_plan_usage_summary()
          'daily_stats': [...],      # last 30 days
          'billing_history': [...],
        }

    On failure returns {'success': False, 'message': '...'}.
    """
    try:
        data = _post("account/summary.php", {"user_id": user_id}, token=token)
    except HostAPIError as e:
        return {"success": False, "message": str(e)}

    if not isinstance(data, dict):
        return {"success": False, "message": "Unexpected response from host."}

    return data


# ---------------------------------------------------------------------------
# ACCOUNT — CHANGE USERNAME / PASSWORD
# ---------------------------------------------------------------------------

def host_change_credentials(user_id, token, action, **fields):
    """
    Change the logged-in user's own username or password via the host.

    action: 'change_username' | 'change_password'
    fields: new_username/current_password_u for change_username,
            current_password/new_password/confirm_password for change_password.

    Returns {'success': True, 'message': '...', 'username': '...' (optional)}
    or {'success': False, 'message': '...'}.
    """
    payload = {"user_id": user_id, "action": action}
    payload.update(fields)
    try:
        data = _post("account/change_credentials.php", payload, token=token)
    except HostAPIError as e:
        return {"success": False, "message": str(e)}

    if not isinstance(data, dict):
        return {"success": False, "message": "Unexpected response from host."}

    return data
