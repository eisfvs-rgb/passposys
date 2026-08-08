"""
quota.py
--------
Session-aware wrappers around host_api's quota/billing calls, plus the
usage-warning-popup logic (fires at 50%/90%/exceeded thresholds, re-firing
as the percentage climbs rather than only once).

Pulled out of app.py because these are plain helper functions with no
Flask routes of their own — every route that needs a quota check imports
them from here instead.
"""

import time
import logging
from flask import session

from host_api import host_account_summary, host_check_upload, host_log_upload

logger = logging.getLogger('passposys')


def get_plan_usage_summary(user_id, token=None):
    """
    Session-aware wrapper around the host's plan/usage summary, used for
    the usage-warning popup and the /api/quota endpoint.
    """
    token = token or session.get('host_token')
    if not token:
        return {}
    summary = host_account_summary(user_id, token)
    if not summary.get('success'):
        return {}
    return summary.get('plan_summary') or {}


def check_upload_allowed(user_id, token=None):
    """Session-aware wrapper around the host's upload-quota check."""
    token = token or session.get('host_token')
    if not token:
        return {'allowed': False, 'reason': 'not_authenticated', 'amount_due': 0.0,
                'used': 0, 'limit': 0, 'remaining': 0}
    return host_check_upload(user_id, token)


def log_passport_upload(user_id, count=1, duplicates=0, invalids=0, token=None):
    """Session-aware wrapper that records usage on the host (best-effort)."""
    token = token or session.get('host_token')
    if not token:
        return
    host_log_upload(user_id, token, count=count, duplicates=duplicates, invalids=invalids)

    # Clear the plan summary cache so the index page updates immediately
    session.pop('_plan_summary_ts', None)
    session.pop('_plan_summary_cache', None)


def _set_usage_warning(user_id, token=None):
    """
    Store a usage warning level in the session so the popup fires on the next
    page load. Fetches plan/usage data from the host API.

      - 'warning'  fires once on entering the  50-89 % band.
      - 'critical' fires once on entering the  90 %+ band.
      - 'exceeded' fires every time the limit is over (until admin resets).

    This means the popup shows once per threshold band crossing, not once
    per percentage point. If usage climbs from 80% to 85% to 89% across
    several uploads, the popup only fired once, on first crossing 50%. It
    only fires again if usage drops back below a band (e.g. the admin
    raises the limit) and later crosses into it again.
    """
    token = token or session.get('host_token')
    if not token:
        return
    ps = get_plan_usage_summary(user_id, token)
    logger.info("_set_usage_warning: plan_summary for user %s = %s", user_id, ps)
    if not ps:
        return

    try:
        # Refresh the index-page cache too, since this call already paid the
        # cost of a host round trip — avoids a duplicate fetch on next index load.
        session['_plan_summary_cache'] = ps
        session['_plan_summary_ts'] = time.time()
        if not ps.get('limit') or ps.get('is_admin_user'):
            return
        pct       = ps.get('usage_pct', 0)
        remaining = int(ps.get('remaining', 0))
        limit     = int(ps.get('limit', 0))
        used      = int(ps.get('monthly_used', 0))
        pct_int   = int(pct)
    except Exception:
        logger.exception(
            "_set_usage_warning: failed to parse plan_summary for user %s. Raw ps=%s",
            user_id, ps
        )
        return

    # Last threshold BAND we showed a popup for (not the exact percentage) —
    # 'warning' (50-89%), 'critical' (90%+), 'exceeded', or '' if none shown
    # yet / usage has since dropped back below a band. Tracking the band
    # instead of the exact percentage means the popup fires once when you
    # cross into a band, not again on every subsequent point increase
    # within that same band (which, on a limit of 100, was firing on
    # almost every single upload).
    shown_band = session.get('_usage_warn_shown_band', '')

    if ps.get('is_over_limit'):
        # Always show exceeded — re-fires until the admin resets the account
        session['_usage_warning'] = {
            'level': 'exceeded',
            'pct': pct_int, 'remaining': remaining,
            'limit': limit, 'used': used
        }
        session['_usage_warn_shown_band'] = 'exceeded'
    elif pct >= 90:
        if shown_band != 'critical' and shown_band != 'exceeded':
            session['_usage_warning'] = {
                'level': 'critical',
                'pct': pct_int, 'remaining': remaining,
                'limit': limit, 'used': used
            }
        session['_usage_warn_shown_band'] = 'critical'
    elif pct >= 50:
        if shown_band != 'warning' and shown_band != 'critical' and shown_band != 'exceeded':
            session['_usage_warning'] = {
                'level': 'warning',
                'pct': pct_int, 'remaining': remaining,
                'limit': limit, 'used': used
            }
        session['_usage_warn_shown_band'] = 'warning'
    else:
        # Usage dropped back below 50% (e.g. admin raised the limit) —
        # clear the band so the popup can fire again on the next crossing.
        session['_usage_warn_shown_band'] = ''
