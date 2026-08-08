"""
secrets_client.py
------------------
Fetches HOST_API_SECRET / OCR_API_SECRET / FLASK_SECRET_KEY / LOCAL_API_TOKEN
from the host's /api/config/secrets.php endpoint, authenticated with a
per-installation token (install_token) instead of relying solely on the
static secrets baked into env.enc at build time.

This lets a secret be rotated centrally (one UPDATE on the host's
runtime_secrets table) without rebuilding and redistributing the exe to
every user — the app just picks up the new value on its next login/fetch.

The install_token itself is a small, low-blast-radius, revocable
credential:
  - It is issued by the host on successful login (auth/login.php).
  - It is stored locally in %APPDATA%\\PasspoSys\\install_token.txt as
    plain text — a leaked install_token can be revoked instantly
    server-side without impacting any other installation, and it is not
    bound to any particular machine or MAC address, so it can be reused
    on any device the user logs in from.
  - On the next login, the app sends its existing token back so a device
    doesn't accumulate a new token row every time someone logs in.

This module does NOT replace env.enc entirely. FLASK_SECRET_KEY and
LOCAL_API_TOKEN are still required at process startup (before any
login has happened) and continue to come from env.enc, unchanged.
HOST_API_SECRET and OCR_API_SECRET — the two secrets actually used to
authenticate outbound requests to the host/OCR — can now additionally
be refreshed at runtime via this module once a user has logged in.
"""

import os
import logging

import requests

logger = logging.getLogger('passposys')

REQUEST_TIMEOUT = 10  # seconds

def _store_dir():
    appdata = os.environ.get('APPDATA')
    if appdata:
        base = os.path.join(appdata, 'PasspoSys')
    else:
        base = os.path.join(os.path.expanduser('~'), '.passposys')
    os.makedirs(base, exist_ok=True)
    return base


def _token_path():
    """Path to the plain-text install_token file."""
    return os.path.join(_store_dir(), 'install_token.txt')


def load_install_token():
    """
    Return the locally stored install_token, or None if not present.

    Stored as plain text on all platforms. If a pre-existing DPAPI-encrypted
    blob is found (from before this change), it can't be decoded as UTF-8
    text — treated the same as "no token", so the app simply re-logs-in and
    overwrites it with a fresh plain-text token via save_install_token().
    """
    path = _token_path()
    if not os.path.exists(path):
        return None

    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except Exception:
        logger.exception("Failed to read stored install_token.")
        return None

    if not raw:
        return None

    try:
        token = raw.decode('utf-8').strip()
    except Exception:
        logger.warning("Stored install_token is not valid text (likely an old encrypted blob) — ignoring.")
        return None

    return token or None


def save_install_token(token):
    """Persist a newly issued install_token as plain text, overwriting any previous one."""
    if not token:
        return
    try:
        with open(_token_path(), 'w', encoding='utf-8') as f:
            f.write(token)
    except Exception:
        logger.exception("Failed to save install_token.")


def clear_install_token():
    """Remove the stored install_token (e.g. if the server reports it as revoked)."""
    path = _token_path()
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        logger.exception("Failed to clear install_token.")


def fetch_runtime_secrets(install_token=None, host_api_base=None):
    """
    Fetch current secret values from the host's secrets endpoint.

    Returns a dict like:
        {"ocr_api_secret": "...", "flask_secret_key": "...", "local_api_token": "..."}
    on success, or None on any failure (missing token, network error,
    non-200 response) — callers should fall back to the env.enc-supplied
    values already loaded into os.environ in that case, so a transient
    network issue never blocks the app from starting or logging in.
    """
    token = install_token or load_install_token()
    if not token:
        logger.info("fetch_runtime_secrets: no install_token available, skipping fetch.")
        return None

    base = host_api_base or os.environ.get("HOST_API_BASE", "https://pms.passposys.com/api")
    # secrets.php lives under /api/config/, not /api/ocr/ or /api/auth/ —
    # strip a trailing /api if host_api_base already includes it, then
    # rebuild the path explicitly so this works regardless of how
    # HOST_API_BASE is configured.
    root = base.rsplit("/api", 1)[0] if base.rstrip("/").endswith("/api") else base
    url = f"{root.rstrip('/')}/api/config/secrets.php"

    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        logger.warning("fetch_runtime_secrets: request failed: %s", e)
        return None

    if resp.status_code == 401:
        logger.warning("fetch_runtime_secrets: install_token rejected (401) — clearing stored token.")
        clear_install_token()
        return None

    if resp.status_code == 403:
        logger.error("fetch_runtime_secrets: install_token rejected (403).")
        return None

    if resp.status_code != 200:
        logger.warning("fetch_runtime_secrets: unexpected status %s", resp.status_code)
        return None

    try:
        data = resp.json()
    except ValueError:
        logger.warning("fetch_runtime_secrets: response was not valid JSON.")
        return None

    if not isinstance(data, dict):
        return None

    return data


def check_install_token_valid(user_id, install_token=None, host_api_base=None):
    """
    Ask the host whether the locally stored install_token is still valid
    AND belongs to user_id -- used by login_required() on every request
    to detect a token that was invalidated after login (admin revoke,
    DB row changed, or the local install_token.txt was deleted/altered
    so it no longer matches).

    Returns one of:
        True   -- token verified as valid and owned by user_id
        False  -- token is missing, revoked, or belongs to someone else
                  (login_required() should force-logout on this)
        None   -- the check itself could not be completed (network error,
                  non-200/malformed response) -- callers should NOT treat
                  this as a mismatch, to avoid logging users out on a
                  transient connectivity blip.
    """
    token = install_token or load_install_token()
    if not token:
        # No local token to check at all -- this is a missing-token
        # situation, which IS a real mismatch (distinct from "couldn't
        # reach the host"). Report False so the caller can act on it.
        return False

    base = host_api_base or os.environ.get("HOST_API_BASE", "https://pms.passposys.com/api")
    root = base.rsplit("/api", 1)[0] if base.rstrip("/").endswith("/api") else base
    url = f"{root.rstrip('/')}/api/config/verify_token.php"

    try:
        resp = requests.post(
            url,
            json={"secret": os.environ.get("HOST_API_SECRET", ""), "user_id": user_id},
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        logger.warning("check_install_token_valid: request failed: %s", e)
        return None

    if resp.status_code != 200:
        logger.warning("check_install_token_valid: unexpected status %s", resp.status_code)
        return None

    try:
        data = resp.json()
    except ValueError:
        logger.warning("check_install_token_valid: response was not valid JSON.")
        return None

    if not isinstance(data, dict) or "valid" not in data:
        return None

    return bool(data["valid"])


def refresh_env_from_host(install_token=None):
    """
    Fetch current secrets from the host and, for any that were returned,
    override the corresponding os.environ value (which config.py has
    already loaded from env.enc). Safe to call multiple times — e.g.
    right after every successful login — since it's a pure overwrite of
    already-set values, not a first-time requirement.

    Returns True if secrets were successfully refreshed, False if the
    fetch failed for any reason (app continues running on the env.enc
    values already loaded, so this is never fatal).
    """
    secrets = fetch_runtime_secrets(install_token=install_token)
    if not secrets:
        return False

    changed = []
    mapping = {
        "ocr_api_secret":   "OCR_API_SECRET",
        "flask_secret_key": "FLASK_SECRET_KEY",
        "local_api_token":  "LOCAL_API_TOKEN",
    }
    for server_key, env_key in mapping.items():
        value = secrets.get(server_key)
        if value:
            os.environ[env_key] = value
            changed.append(env_key)

    if changed:
        logger.info("refresh_env_from_host: refreshed %s from host.", ", ".join(changed))

    return bool(changed)