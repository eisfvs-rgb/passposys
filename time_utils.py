"""
time_utils.py
-------------
IST (UTC+5:30) time helper, synced against a real NTP server rather than
trusting the local system clock (a wrong local clock breaks ProvB Vision
auth tokens and MRZ date-of-birth century inference).

Shared by app.py (Flask before_request hook re-syncs it) and ocr_mrz.py
(MRZ date parsing needs "today" to decide 19xx vs 20xx).
"""

import time as _time
import threading as _ntp_threading
from datetime import datetime, timedelta, timezone

import ntplib as _ntplib

_IST        = timezone(timedelta(hours=5, minutes=30))
_ntp_offset = 0.0          # difference: NTP time - system time (seconds)
_ntp_lock   = _ntp_threading.Lock()


def _sync_ntp_once():
    """Fetch real UTC from internet time server and save the offset."""
    global _ntp_offset
    try:
        c = _ntplib.NTPClient()
        resp = c.request('pool.ntp.org', version=3)
        with _ntp_lock:
            _ntp_offset = resp.tx_time - _time.time()
    except Exception:
        pass  # keep previous offset; falls back to system clock


def sync_ntp_async():
    """Kick off an NTP re-sync in a daemon thread (non-blocking)."""
    _ntp_threading.Thread(target=_sync_ntp_once, daemon=True).start()


def ist_now() -> datetime:
    """Return the current datetime in Indian Standard Time (UTC+5:30).
    Offset is refreshed periodically via sync_ntp_async()."""
    utc_dt = datetime.fromtimestamp(_time.time() + _ntp_offset, tz=timezone.utc)
    return utc_dt.astimezone(_IST).replace(tzinfo=None)


# Sync once immediately at import time in the background.
sync_ntp_async()
