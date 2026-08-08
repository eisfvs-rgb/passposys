"""
app_routes.py
-------------
Route half of the application: every Flask @app.route view function
except the invalid-passport reparse/rescan routes, which live in
reparse_routes.py.

Imports the Flask app object plus every shared helper/import from
app_core.py via a wildcard import, then registers all views on `app`
simply by virtue of the @app.route decorators executing at import time.

app.py does:
    from app_core import app
    import app_routes      # noqa: F401
    import reparse_routes  # noqa: F401
    (app is then fully wired up and ready to serve)
"""

from app_core import *  # noqa: F401,F403  -- app, helpers, constants, and 3rd-party imports

# NOTE: names starting with an underscore are never picked up by
# `from app_core import *` (Python's wildcard-import rule) - this applies
# both to names app_core.py imports from elsewhere AND names defined
# directly in app_core.py itself. All must be imported explicitly here.
from quota import _set_usage_warning
from mofa_downloader import start_background as _start_mofa_downloader
from mofa_downloader import trigger_single_download as _trigger_mofa_single_download
from mofa_downloader import trigger_batch_download as _trigger_mofa_batch_download
from mofa_downloader import get_job_progress as _mofa_get_job_progress
from ocr_client import (
    TokenMismatchError,
    with_user_context,
    submit_with_context,
    _reset_secondary_client,
    _do_stacked_primary_scan_from_paths,
    _do_stacked_secondary_scan,
    get_last_extracted_issue_date,
    estimate_issue_date,
    _crop_bottom_strip_for_path,
    ocr_logger,
    _new_request_id,  # <-- Add this
    _post_ocr         # <-- Add this
)
from app_core import (
    _cancel_flags,
    _cancel_flags_lock,
    _extract_passports,
    _face_cascade,
    _generate_csv_response,
    _get_passport_ids_by_group_names,
    _get_passports_by_group_names,
    _resolve_default_arrival_departure,
    _resource_path,
    _rollback_session,
    _RESAMPLE,
    _apply_issue_date_day_rule,
)
import json
from pdf_extractor import process_image_upload
from backup import _find_mysqldump

# subprocess.CREATE_NO_WINDOW (0x08000000) is Windows-only. On macOS/Linux
# there's no console window to suppress, so this is simply omitted there —
# passing it unconditionally raises AttributeError on non-Windows platforms.
_MYSQLDUMP_POPEN_KWARGS = {'creationflags': 0x08000000} if sys.platform.startswith('win') else {}
@app.route("/db_passport/<int:passport_id>")
def db_passport_image(passport_id):
    if not session.get('user_id'): return "Unauthorized", 401
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT filename FROM passports WHERE id = %s AND user_id = %s",
                   (passport_id, session['user_id']))
    result = cursor.fetchone()
    cursor.close()
    conn.close()
    if not result or not result[0]:
        return "Image not found", 404
    img_path, _ = resolve_passport_paths(result[0])
    if not img_path or not os.path.exists(img_path):
        return "Image not found", 404
    response = make_response(send_file(img_path, mimetype='image/jpeg', as_attachment=False))
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@app.route("/db_face/<int:passport_id>")
def db_face_image(passport_id):
    if not session.get('user_id'): return "Unauthorized", 401
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT filename FROM passports WHERE id = %s AND user_id = %s",
                   (passport_id, session['user_id']))
    result = cursor.fetchone()
    cursor.close()
    conn.close()
    if not result or not result[0]:
        return "", 404
    _, face_path = resolve_passport_paths(result[0])
    if not face_path or not os.path.exists(face_path):
        return "", 404
    response = make_response(send_file(face_path, mimetype='image/jpeg', as_attachment=False))
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


# =====================================================
# MAIN ROUTES
# =====================================================

@app.route("/uploads/<filename>")
def serve_upload(filename):
    # Images now live inside per-group subfolders, so resolve the real path first.
    original_path, _ = resolve_passport_paths(filename)
    if not original_path or not os.path.exists(original_path):
        return send_from_directory(app.config["UPLOAD_FOLDER"], filename)  # 404 via normal flow
    directory, fname = os.path.split(original_path)
    return send_from_directory(directory, fname)


@app.route("/skip", methods=["POST"])
def skip_correction():
    filename = request.form.get("filename")
    if filename:
        original_path, face_path = resolve_passport_paths(filename)
        resized_path = os.path.join(app.config["UPLOAD_FOLDER"], f"{os.path.splitext(filename)[0]}_ocr.jpg")
        for path in [original_path, resized_path, face_path]:
            if os.path.exists(path):
                os.remove(path)
    session.pop("pending_correction", None)
    return redirect(url_for("index"))


@app.route("/save_defaults", methods=["POST"])
def save_defaults():
    if not session.get('user_id'): return redirect(url_for('login'))
    update_user_settings(session['user_id'], request.form)
    flash("Global default settings updated successfully!", "success")
    return redirect(url_for("index"))


# =====================================================
# AUTHENTICATION LOGIC
# =====================================================

@app.route("/update_group_data", methods=["POST"])
@login_required
def update_group_data():
    """
    Bulk-updates the general_data fields for every passport record that
    belongs to the selected group_name (scoped to the logged-in user).
    Used by the 'Update' button on the General Data Defaults card, which
    only appears once an existing group has been chosen from the list.
    """
    user_id = session['user_id']
    group_name = (request.form.get('group_name') or '').strip()
    # Optional — when provided, only records currently of this visa_type
    # inside the group are touched (e.g. 'visit_visa'). Used by the
    # Nusuk -> Visit Visa transfer flow so Nusuk records left in a mixed
    # group are never overwritten with Visit Visa general data.
    visa_type_filter = (request.form.get('visa_type_filter') or '').strip()
    if visa_type_filter not in ('nusuk', 'visit_visa'):
        visa_type_filter = ''

    if not group_name:
        return jsonify({"success": False, "message": "Group name is required."}), 400

    values = []
    set_clauses = []
    arrival_val_for_departure = '__unset__'
    for field in GROUP_BULK_UPDATE_FIELDS:
        # When scoping to a specific visa type, don't let this call also
        # overwrite visa_type itself — that field is managed separately
        # by the transfer step, not by the general-data form.
        if visa_type_filter and field == 'visa_type':
            continue
        set_clauses.append(f"gd.{field} = %s")
        raw_val = request.form.get(field, '').strip()
        # marital_status / passport_type are INT columns — an empty string
        # (e.g. left blank while Nusuk mode hides these fields) would fail
        # to cast, so fall back to the column's sane default instead.
        if field == 'marital_status':
            raw_val = safe_int(raw_val, 5)
        elif field == 'passport_type':
            raw_val = safe_int(raw_val, 1)
        elif field == 'expected_arrival':
            # DATE column — an empty string (e.g. Nusuk mode, where this
            # field is hidden/blank) must become NULL, not '' (which MySQL
            # would reject for a DATE column).
            raw_val = raw_val or None
            arrival_val_for_departure = raw_val
        values.append(raw_val)

    # expected_departure is always arrival + 365 days (never entered
    # independently anywhere in the app) — mirrors the same rule used by
    # /change_group and /merge_groups_into. Without this, expected_departure
    # was left stale/untouched here even though expected_arrival was just
    # changed. Cleared to NULL alongside arrival when arrival is cleared
    # (e.g. Nusuk mode).
    if arrival_val_for_departure != '__unset__':
        set_clauses.append("gd.expected_departure = %s")
        if arrival_val_for_departure:
            try:
                _arr_dt = datetime.strptime(str(arrival_val_for_departure), "%Y-%m-%d").date()
                values.append(_arr_dt + timedelta(days=365))
            except ValueError:
                values.append(None)
        else:
            values.append(None)

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("USE passport_db")

        # Confirm the group (optionally scoped to a visa type) exists for this user before updating
        count_sql = """
            SELECT COUNT(*) FROM general_data gd
            JOIN passports p ON gd.passport_id = p.id
            WHERE p.user_id = %s AND gd.group_name = %s
        """
        count_params = [user_id, group_name]
        if visa_type_filter:
            count_sql += " AND gd.visa_type = %s"
            count_params.append(visa_type_filter)
        cursor.execute(count_sql, tuple(count_params))
        (match_count,) = cursor.fetchone()
        if not match_count:
            return jsonify({"success": False, "message": f"No records found for group '{group_name}'."}), 404

        sql = f"""
            UPDATE general_data gd
            JOIN passports p ON gd.passport_id = p.id
            SET {', '.join(set_clauses)}
            WHERE p.user_id = %s AND gd.group_name = %s
        """
        sql_params = values + [user_id, group_name]
        if visa_type_filter:
            sql += " AND gd.visa_type = %s"
            sql_params.append(visa_type_filter)
        cursor.execute(sql, sql_params)
        conn.commit()
        return jsonify({
            "success": True,
            "message": f"Updated {match_count} record(s) in group '{group_name}'.",
            "updated_count": match_count
        })
    except mysql.connector.Error as db_err:
        conn.rollback()
        return jsonify({"success": False, "message": f"Database error: {str(db_err)[:150]}"}), 500
    finally:
        cursor.close()
        conn.close()


import logging
logger = logging.getLogger('passposys')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('user_id'):
        return redirect(url_for('index'))

    # ── Credential check via host API (OTP and local users table removed) ──
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        try:
            result = host_login(username, password)
            logger.info("host_login() returned: %s", result)
        except Exception:
            logger.exception("host_login() raised an exception during login POST")
            flash("An unexpected error occurred while logging in. Check passposys.log for details.", 'danger')
            return render_template('login.html')

        if result.get('success'):
            try:
                level = result.get('account_level', 'full')
                session['user_id']       = result['user_id']
                session['username']      = result['username']
                session['full_name']     = result.get('full_name') or result['username']
                session['is_admin']      = bool(result.get('is_admin'))
                session['host_token']    = result['token']
                session['account_level'] = level
            except KeyError as ke:
                logger.error(
                    "host_login() success=True but missing expected key %s. Full result: %s",
                    ke, result
                )
                flash(f"Login response from server was missing expected field: {ke}. Check passposys.log.", 'danger')
                return render_template('login.html')

            if level == 'login_blocked':
                session.clear()
                flash('🔒 Your account has been locked or suspended. Please contact your administrator.', 'danger')
                return render_template('login.html')

            if level == 'admin_disabled':
                flash('Your account has been disabled by the administrator. You can view your account but cannot upload.', 'warning')
            else:
                _set_usage_warning(result['user_id'], result.get('token'))

            try:
                cleared = reconcile_mofa_pdf_downloads(user_id=result['user_id'])
                if cleared:
                    logger.info(
                        "reconcile_mofa_pdf_downloads: cleared %d stale mofa_pdf_downloaded_at "
                        "value(s) for user_id=%s (PDF missing on disk).",
                        cleared, result['user_id']
                    )
            except Exception:
                logger.exception("reconcile_mofa_pdf_downloads() failed during login; continuing anyway.")

            _start_mofa_downloader(user_id=result['user_id'])

            next_url = request.form.get('next') or request.args.get('next')
            return redirect(next_url or url_for('index'))
        else:
            err = result.get('error')
            if err == 'account_locked':
                flash('🔒 Your account has been locked due to too many failed login attempts. Please contact your administrator to unlock it.', 'danger')
            elif err == 'account_suspended':
                flash('Your account access has been suspended. Please contact support.', 'danger')
            elif err == 'invalid_credentials':
                remaining = result.get('remaining_attempts')
                if remaining is not None:
                    flash(f'Invalid username or password. {remaining} attempt(s) remaining before account lockout.', 'danger')
                else:
                    flash('Invalid username or password.', 'danger')
            else:
                flash(f"⚠️ Could not reach the login server: {result.get('message', 'unknown error')}", 'danger')

    return render_template('login.html')



@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# Registration is now handled entirely by the host at
# https://pms.passposys.com/register.php — the local app no longer
# creates users.


@app.route("/", methods=["GET", "POST"])
@login_required
@with_user_context(lambda: session.get('user_id'))
def index():
    try:
        progress = get_progress(session.get('user_id')) or {"current_": 0, "total": 0}
    except Exception:
        logger.exception("get_progress() failed - using default")
        progress = {"current_": 0, "total": 0}

    pending = session.get("pending_correction")
    now = ist_now()
    _now_ts = now.timestamp()
    one_year_later = now + timedelta(days=365)

    user_id = session['user_id']

    try:
        total_invalid = get_total_invalid_count(user_id)
    except Exception:
        logger.exception("get_total_invalid_count() failed - using 0")
        total_invalid = 0

    try:
        defaults = get_user_settings(user_id) or {}
    except Exception:
        logger.exception("get_user_settings() failed - using {}")
        defaults = {}

    try:
        group_names = get_distinct_group_names() or []
    except Exception:
        logger.exception("get_distinct_group_names() failed - using []")
        group_names = []

    try:
        group_visa_types = get_distinct_group_visa_types() or {}
    except Exception:
        logger.exception("get_distinct_group_visa_types() failed - using {}")
        group_visa_types = {}

    try:
        group_emergency_flags = get_distinct_group_emergency_flags() or {}
    except Exception:
        logger.exception("get_distinct_group_emergency_flags() failed - using {}")
        group_emergency_flags = {}

    # Cache plan_summary for 5 minutes to avoid a host round trip
    # (and a bill-generation write) on every single page load.
    _plan_ts = session.get('_plan_summary_ts', 0)
    if _now_ts - _plan_ts > 300 or '_plan_summary_cache' not in session:
        try:
            plan_summary = get_plan_usage_summary(user_id) or {}
        except Exception:
            logger.exception("get_plan_usage_summary() failed on index load - using cached/empty")
            plan_summary = session.get('_plan_summary_cache', {})
        session['_plan_summary_cache'] = plan_summary
        session['_plan_summary_ts'] = _now_ts
    else:
        plan_summary = session.get('_plan_summary_cache', {})

    defaults_departure_display = one_year_later.strftime('%d/%m/%Y')
    if defaults and defaults.get('expected_arrival'):
        try:
            defaults_departure_display = (defaults['expected_arrival'] + timedelta(days=365)).strftime('%d/%m/%Y')
        except (TypeError, ValueError):
            pass

    template_context = {
        "pending_correction": pending,
        "progress": progress,
        "now": now,
        "one_year_later": one_year_later,
        "today_iso": now.strftime('%Y-%m-%d'),
        "today_display": now.strftime('%d/%m/%Y'),
        "today_plus_365_display": one_year_later.strftime('%d/%m/%Y'),
        "defaults_departure_display": defaults_departure_display,
        "nationality_options": NATIONALITY_OPTIONS,
        "marital_status_options": MARITAL_STATUS_OPTIONS,
        'total_invalid': total_invalid,
        "defaults": defaults,
        "group_names": group_names,
        "group_visa_types": group_visa_types,
        "group_emergency_flags": group_emergency_flags,
        "plan_summary": plan_summary,
    }

    if request.method == "POST":
        # Detect chunked upload up front so both the quota check below and the
        # later blocked/cancelled checks can respond with JSON instead of an
        # HTML redirect (a redirect on a chunked/AJAX request was silently
        # swallowed by the frontend, which then displayed a false "Processing
        # Complete" dialog with all-zero stats instead of a blocked message).
        is_chunk_upload = request.headers.get('X-Chunk-Upload') == '1'
        # A plain (non-chunked) upload can still be sent via fetch/XHR by the
        # frontend, not just a classic HTML form post. X-Chunk-Upload alone
        # missed that case: the browser follows the redirect silently (fetch
        # follows redirects by default) instead of surfacing the block, and
        # the JS then renders a false "Processing Complete" with all-zero
        # stats. Treat any AJAX-style request the same as a chunked one.
        is_ajax_upload = (
            is_chunk_upload
            or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        )

        # ── Upload quota check ────────────────────────────────────────────
        upload_check = check_upload_allowed(user_id)
        if not upload_check['allowed']:
            _set_usage_warning(user_id)
            blocked_payload = {
                'reason':     upload_check['reason'],
                'amount':     upload_check.get('amount_due', 0),
                'used':       upload_check.get('used', 0),
                'limit':      upload_check.get('limit', 0),
                'selected':   0,
                'remaining':  upload_check.get('remaining', 0),
                'allow_extra_usage': upload_check.get('allow_extra_usage', False),
            }
            if is_ajax_upload:
                return jsonify({"blocked": True, **blocked_payload})
            session['_upload_blocked'] = blocked_payload
            return redirect(url_for('index'))

        files = request.files.getlist("images")
        files = [f for f in files if f.filename]
        if not files:
            template_context["error"] = "No files selected"
            return render_template("index.html", **template_context)

        # ── Quota enforcement: block entirely if selected files exceed remaining ──
        # `remaining` from the host already accounts for the 50/day extra
        # allowance (folded into one combined number server-side), so no
        # separate extra_mode branch is needed here.
        remaining_quota = upload_check.get('remaining')
        if remaining_quota is not None:
            if remaining_quota <= 0 or len(files) > remaining_quota:
                _set_usage_warning(user_id)
                blocked_payload = {
                    'reason':    'limit_exceeded',
                    'amount':    upload_check.get('amount_due', 0),
                    'used':      upload_check.get('used', 0),
                    'limit':     upload_check.get('limit', 0),
                    'selected':  len(files),
                    'remaining': remaining_quota,
                    'allow_extra_usage': upload_check.get('allow_extra_usage', False),
                }
                if is_chunk_upload:
                    return jsonify({"blocked": True, **blocked_payload})
                # Store in session and redirect so the loading overlay is properly
                # dismissed before the blocked popup is shown.
                session['_upload_blocked'] = blocked_payload
                return redirect(url_for('index'))
        # ─────────────────────────────────────────────────

        db_defaults = get_user_settings(user_id) or {}
        is_emergency_upload = request.form.get('emergency_upload') == '1'

        # ── Group visa-type lock: an existing group can only ever contain
        # ONE visa type. Previously, uploading Nusuk into an existing
        # Visit Visa group (or vice-versa) was silently allowed, creating
        # a mixed group. Block the whole upload up front with a clear
        # error instead of letting it partially apply. A brand-new group
        # (no active records yet) has no locked type, so it's unaffected.
        _upload_group_name = db_defaults.get('group_name', 'GROUP 1')
        _upload_visa_type_req = db_defaults.get('visa_type', 'nusuk')
        _existing_group_visa_type = get_group_visa_type(user_id, _upload_group_name)
        if _existing_group_visa_type and _existing_group_visa_type != _upload_visa_type_req:
            _visa_label = {'nusuk': 'Nusuk', 'visit_visa': 'Visit Visa'}
            _mismatch_msg = (
                f'Group "{_upload_group_name}" is already a '
                f'{_visa_label.get(_existing_group_visa_type, _existing_group_visa_type)} group. '
                f'You are trying to upload {_visa_label.get(_upload_visa_type_req, _upload_visa_type_req)} '
                f'records into it — only matching visa types can be added to the same group. '
                f'Please choose a different group name or match the group\'s visa type.'
            )
            if is_chunk_upload:
                return jsonify({"error": True, "reason": "group_visa_type_mismatch", "message": _mismatch_msg}), 400
            flash(_mismatch_msg, 'danger')
            return redirect(url_for('index'))

        # ── Reset ProvB Vision client at the start of every scan ─────────
        # Guarantees a fresh OAuth token each session. Without this, a stale
        # client from a previous wrong-clock session stays in memory and keeps
        # returning ACCESS_TOKEN_EXPIRED even after the system time is fixed.
        _reset_secondary_client()

        # Total steps = Phase1 (1 per file) + Phase2 (1 per file) + Phase5 (1 per pending file)
        # Phases 3 & 4 are fallbacks; this split makes the bar move visibly through all stages.
        _phase_total = len(files) * 3
        set_progress(user_id, current=0, total=_phase_total, success=0, invalid=0, duplicate=0, phase='Uploading...')

        # ── Cancel / rollback tracking ────────────────────────────────────
        with _cancel_flags_lock:
            _cancel_flags.pop(user_id, None)   # clear any stale flag from prev session

        import threading as _thr_session
        _session_lock         = _thr_session.Lock()
        _session_passport_ids = []   # passport IDs inserted this chunk (for rollback)
        _session_invalid_ids  = []   # invalid_passport IDs inserted this chunk
        _session_file_paths   = []   # disk files written this chunk (passport + face)

        def _is_cancelled():
            with _cancel_flags_lock:
                return _cancel_flags.get(user_id, False)

        def _abort_for_token_mismatch(e):
            """Shared handler for TokenMismatchError raised from any scan
            phase. Same install_token is used for ProvA and ProvB, so a
            mismatch here means every remaining fallback will fail too --
            stop the whole upload immediately instead of cascading."""
            clear_progress(user_id)
            for _item in pending_items:
                for _p in [_item.get('original_path'), _item.get('resized_path')]:
                    try:
                        if _p and os.path.exists(_p): os.remove(_p)
                    except Exception: pass
            error_payload = {
                "error": True,
                "reason": "token_mismatch",
                "message": str(e),
            }
            if is_chunk_upload:
                return jsonify(error_payload), 401
            session['_upload_blocked'] = error_payload
            return redirect(url_for('index'))

        def _do_rollback_and_abort():
            """Call when cancellation is detected: rollback DB + disk, return response."""
            clear_progress(user_id)
            with _cancel_flags_lock:
                _cancel_flags.pop(user_id, None)
            with _session_lock:
                _rollback_session(
                    user_id,
                    list(_session_passport_ids),
                    list(_session_invalid_ids),
                    list(_session_file_paths),
                )
            print(f"[Cancel] Upload cancelled for user {user_id} — rollback complete.")
        # ─────────────────────────────────────────────────────────────────

        validation_errors = []
        duplicates = []
        skipped_files = []
        successfully_processed = 0
        progress_counter = 0
        total_provA_batch_units = 0    # Phase 2+3: ProvA batch fallback
        total_provB_calls = 0  # Phase 2+3: ProvB Vision stacked batch
        total_provA_individual_units = 0  # Phase 4: ProvA individual rescans
        # Per-phase breakdown for /usage display
        phase2_provB_calls = 0
        phase2_provA_calls  = 0
        phase3_provB_calls = 0
        phase3_provA_calls  = 0
        # Per-phase file counts (how many files went through each step)
        phase2_file_count = 0
        phase3_file_count = 0

        # ── Throttle progress DB writes ────────────────────────────────────
        # update_progress_field() is a local MySQL UPDATE. Calling it once per
        # file (across ~3 phases) means dozens of extra DB round trips during
        # an upload. The progress bar is polled by the browser at most once
        # per second, so writing more often than that is wasted work.
        _last_progress_write_ts = [0.0]

        def _push_progress(**fields):
            now_ts = time.time()
            if now_ts - _last_progress_write_ts[0] >= 0.4:
                update_progress_field(user_id, **fields)
                _last_progress_write_ts[0] = now_ts

        # =================================================================
        # PHASE 1 – Pre-processing & Validation (Zero API cost)
        # OPTIMIZED: Multi-threading + OpenCV Resizing
        # =================================================================
        update_progress_field(user_id, phase='Phase 1: Validating')
        import concurrent.futures
        import os
        
        pending_items = []
        file_data_list = []

        # 1. Quickly read all files into memory synchronously. 
        # (This prevents Flask from closing the file stream while threads are running)
        for i, file in enumerate(files):
            file_bytes = file.read()
            if len(file_bytes) == 0:
                skipped_files.append(file.filename)
                progress_counter += 1
                _push_progress(current_=progress_counter)
                continue

            unique_prefix = uuid.uuid4().hex[:8]
            filename = f"{unique_prefix}_{secure_filename(file.filename)}"
            original_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)

            file_data_list.append({
                'index': i,
                'filename': filename,
                'original_filename': file.filename,
                'bytes': file_bytes,
                'original_path': original_path
            })

        # 2. Define the heavy-lifting worker function
        def _process_single_passport(data):
            i = data['index']
            filename = data['filename']
            file_bytes = data['bytes']
            original_path = data['original_path']
            
            result = {
                'success': False,
                'pending_item': None,
                'validation_error': None,
                'skipped': False
            }
            
            try:
                # Compress before writing to disk if over 1 MB. Applies to
                # both valid and invalid uploads, since invalid uploads read
                # their blob back from this same disk file afterward.
                file_bytes = compress_image_bytes_for_disk(file_bytes)

                # Write to disk
                with open(original_path, 'wb') as f:
                    f.write(file_bytes)

                # Verify image integrity
                try:
                    _pil_open = Image.open(io.BytesIO(file_bytes))
                    _pil_open.verify()
                except Exception:
                    if os.path.exists(original_path): os.remove(original_path)
                    result['skipped'] = True
                    return result

                # Re-open and apply EXIF rotation
                try:
                    _pil_img = ImageOps.exif_transpose(Image.open(io.BytesIO(file_bytes))).convert("RGB")
                except Exception:
                    if os.path.exists(original_path): os.remove(original_path)
                    result['skipped'] = True
                    return result

                is_upright = True

                # ── Pre-cache bottom strip ──
                # Uses the same face-detection-anchored smart crop as Phase 4
                # (instead of a blind h*0.65 cutoff), so the cached strip
                # reliably includes the printed issue-date line above the
                # MRZ. Falls back to the old fixed crop if smart-crop fails
                # for any reason (e.g. no face detected and OpenCV also
                # fails), preserving previous behaviour as a safety net.
                try:
                    cached_strip = _crop_bottom_strip_for_path(original_path, use_face_detection=True)
                except Exception:
                    try:
                        _img_w, _img_h = _pil_img.size
                        cached_strip = _pil_img.crop((0, int(_img_h * 0.65), _img_w, _img_h)).copy()
                    except Exception:
                        cached_strip = None

                result['success'] = True
                result['pending_item'] = {
                    'file_index': i,
                    'filename': filename,
                    'original_path': original_path,
                    'resized_path': original_path, # Using original as resized
                    'cached_strip': cached_strip,   
                }
                return result

            except Exception as e:
                result['validation_error'] = {
                    'filename': filename,
                    'error': f"Processing error: {str(e)[:150]}",
                    'original_path': original_path if os.path.exists(original_path) else None,
                    'error_msg': f"Pre-processing error: {str(e)[:200]}"
                }
                return result

        # 3. Dynamic Scaling Execution
        # Detect CPU threads, max out at 8 to protect your 10-connection DB pool
        available_threads = os.cpu_count() or 4
        optimal_workers = min(available_threads, 8)
        print(f"[Phase 1] Spinning up {optimal_workers} threads for {len(file_data_list)} files...")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=optimal_workers) as executor:
            # Send all files to the worker pool
            futures = {executor.submit(_process_single_passport, data): data for data in file_data_list}
            
            # As each worker finishes, process the result in the MAIN thread
            # (This safely protects your database connection pool)
            for future in concurrent.futures.as_completed(futures):
                res = future.result()
                data_ref = futures[future]
                
                if res['success']:
                    pending_items.append(res['pending_item'])
                    
                elif res['validation_error']:
                    err_data = res['validation_error']
                    inv_id = save_invalid_to_db(
                        user_id=user_id,
                        filename=err_data['filename'],
                        image_source=err_data['original_path'],
                        mrz_text="",
                        error_message=err_data['error_msg'],
                        upload_group_name=db_defaults.get('group_name', 'GROUP 1'),
                        upload_visa_type=db_defaults.get('visa_type', 'nusuk')
                    )
                    validation_errors.append({
                        "filename": err_data['filename'],
                        "error": err_data['error'],
                        "invalid_id": inv_id
                    })
                    if err_data['original_path'] and os.path.exists(err_data['original_path']):
                        os.remove(err_data['original_path'])
                        
                elif res['skipped']:
                    skipped_files.append(data_ref['original_filename'])
                    
                # Safely update the progress bar from the main thread
                progress_counter += 1
                _push_progress(current_=progress_counter)

        # Sort the pending items back into their original order
        # (Threads finish at random times, this puts them back in 1, 2, 3 sequence)
        pending_items.sort(key=lambda x: x['file_index'])

        # ── Cancel check after Phase 1 ────────────────────────────────────
        if _is_cancelled():
            _do_rollback_and_abort()
            # Clean up Phase 1 disk files (not yet in DB, so no DB rollback needed)
            for _item in pending_items:
                for _p in [_item.get('original_path'), _item.get('resized_path')]:
                    try:
                        if _p and os.path.exists(_p): os.remove(_p)
                    except Exception: pass
            if is_chunk_upload:
                return jsonify({"cancelled": True})
            return redirect(url_for("index"))
        # ─────────────────────────────────────────────────────────────────

# =================================================================
        # PHASE 2 – ProvA Stacked Batch (Primary, ProvB Fallback)
        # =================================================================
        update_progress_field(user_id, phase=f'Phase 2: Scanning [{len(pending_items)}]')
        phase2_file_count = len(pending_items)
        mrz_results = {}
        raw_text_results = {}

        def _mrz_is_valid(mrz_lines):
            if not mrz_lines:
                return False
            _, errors_check = parse_mrz(mrz_lines)
            get_last_extracted_issue_date()  # drain cache this call set; not used here
            return not errors_check

        # We still process in chunks (BATCH_SIZE) to prevent creating an image
        # so tall that it exceeds maximum pixel height limits.
        import threading as _threading

        for batch_start in range(0, len(pending_items), BATCH_SIZE):
            batch = pending_items[batch_start: batch_start + BATCH_SIZE]
            batch_input = [
                {
                    'resized_path': item['resized_path'],
                    'index': item['file_index'],
                    'cached_strip': item.get('cached_strip'),
                }
                for item in batch
            ]

            # ── Ticker thread: nudges progress bar every 1.5 s while API call blocks ──
            _p2_done = [False]
            _p2_batch_size = len(batch)
            def _p2_tick(batch_sz=_p2_batch_size):
                ticks = 0
                while not _p2_done[0] and ticks < batch_sz - 1:
                    time.sleep(1.5)
                    if _p2_done[0]:
                        break
                    ticks += 1
                    _push_progress(current_=progress_counter + ticks)
            _p2_ticker = _threading.Thread(target=_p2_tick, daemon=True)
            _p2_ticker.start()

            # --- PROVA PRIMARY SCAN ---
            try:
                az_results, az_unreachable, az_raw_text = _do_stacked_primary_scan_from_paths(batch_input)
            except TokenMismatchError as e:
                _p2_done[0] = True
                return _abort_for_token_mismatch(e)
            _p2_done[0] = True
            
            if not az_unreachable:
                total_provA_batch_units += 1
                phase2_provA_calls += 1
                batch_mrz = az_results
                batch_raw_text = az_raw_text
            else:
                print("  [PHASE 2] ProvA server fail — falling back to ProvB Vision stacked batch.")
                _p2_done2 = [False]
                def _p2_tick2(batch_sz=_p2_batch_size):
                    ticks = 0
                    while not _p2_done2[0] and ticks < batch_sz - 1:
                        time.sleep(1.5)
                        if _p2_done2[0]:
                            break
                        ticks += 1
                        _push_progress(current_=progress_counter + ticks)
                _p2_ticker2 = _threading.Thread(target=_p2_tick2, daemon=True)
                _p2_ticker2.start()
                
                # --- PROVB FALLBACK SCAN ---
                try:
                    gv_results, gv_calls, gv_unreachable, gv_raw_text = _do_stacked_secondary_scan(batch_input)
                except TokenMismatchError as e:
                    _p2_done2[0] = True
                    return _abort_for_token_mismatch(e)
                _p2_done2[0] = True
                
                if not gv_unreachable:
                    total_provB_calls += gv_calls
                    phase2_provB_calls += gv_calls
                    batch_mrz = gv_results
                    batch_raw_text = gv_raw_text
                else:
                    print("  [PHASE 2] ProvB stacked also failed for this batch — items marked invalid.")
                    batch_mrz = {}
                    batch_raw_text = {}

            for item in batch:
                mrz_results[item['filename']] = batch_mrz.get(item['file_index'])
                raw_text_results[item['filename']] = batch_raw_text.get(item['file_index'])
                ocr_logger.debug(
                    f"[DEBUG issue-date] WRITE raw_text_results[{item['filename']!r}] "
                    f"(file_index={item['file_index']}) = "
                    f"{str(batch_raw_text.get(item['file_index']))[:60]!r}"
                )
                progress_counter += 1
                _push_progress(current_=progress_counter)
                
            # Force-write final counter for this batch unconditionally
            update_progress_field(user_id, current_=progress_counter)

            # ── Cancel check inside Phase 2 batch loop ──────────────────
            if _is_cancelled():
                _do_rollback_and_abort()
                for _item in pending_items:
                    for _p in [_item.get('original_path'), _item.get('resized_path')]:
                        try:
                            if _p and os.path.exists(_p): os.remove(_p)
                        except Exception: pass
                if is_chunk_upload:
                    return jsonify({"cancelled": True})
                return redirect(url_for("index"))
            # ─────────────────────────────────────────────────────────────

        print(f"✅ Phase 2 complete: {total_provA_batch_units} ProvA batch unit(s), "
              f"{total_provB_calls} ProvB Vision call(s) used so far.")

        # ── Determine Phase-2 failures ──
        phase2_failures = [
            item for item in pending_items
            if not _mrz_is_valid(mrz_results.get(item['filename']))
        ]

        # =================================================================
        # PHASE 3 – ProvB Stacked Batch (2nd pass, ProvA fallback)
        #   Runs unconditionally for ALL Phase 2 failures — no count
        #   threshold. Every item that wasn't resolved in Phase 2 goes
        #   through a 2nd ProvB stacked pass (falling back to ProvA
        #   stacked if ProvB is unreachable).
        # =================================================================
        phase3_failures = []

        if phase2_failures:
            # Expand the total so progress_counter increments for Phase 3 items
            # don't prematurely trigger the JS "done" check
            _phase_total += len(phase2_failures)
            phase3_file_count = len(phase2_failures)
            update_progress_field(user_id, total=_phase_total,
                                  phase=f'Phase 3: Scanning [{len(phase2_failures)}]')
            print(f"  [PHASE 3] {len(phase2_failures)} passport(s) going to 2nd ProvB Stacked batch.")

            for batch_start in range(0, len(phase2_failures), BATCH_SIZE):
                batch = phase2_failures[batch_start: batch_start + BATCH_SIZE]
                batch_input = [
                    {
                        'resized_path': item['resized_path'],
                        'index': item['file_index'],
                        'cached_strip': item.get('cached_strip'),
                    }
                    for item in batch
                ]

                # ── Ticker thread for Phase 3 API wait ──
                _p3_done = [False]
                _p3_batch_size = len(batch)
                def _p3_tick(batch_sz=_p3_batch_size):
                    ticks = 0
                    while not _p3_done[0] and ticks < batch_sz - 1:
                        time.sleep(1.5)
                        if _p3_done[0]:
                            break
                        ticks += 1
                        _push_progress(current_=progress_counter + ticks)
                _p3_ticker = _threading.Thread(target=_p3_tick, daemon=True)
                _p3_ticker.start()

                try:
                    gv_results, gv_calls, gv_unreachable, gv_raw_text = _do_stacked_secondary_scan(batch_input)
                except TokenMismatchError as e:
                    _p3_done[0] = True
                    return _abort_for_token_mismatch(e)
                total_provB_calls += gv_calls
                phase3_provB_calls += gv_calls
                batch_mrz = gv_results
                batch_raw_text = gv_raw_text
                _p3_done[0] = True

                if gv_unreachable:
                    print("  [PHASE 3] ProvB server fail — falling back to ProvA stacked batch for this batch.")
                    _p3_done2 = [False]
                    def _p3_tick2(batch_sz=_p3_batch_size):
                        ticks = 0
                        while not _p3_done2[0] and ticks < batch_sz - 1:
                            time.sleep(1.5)
                            if _p3_done2[0]:
                                break
                            ticks += 1
                            _push_progress(current_=progress_counter + ticks)
                    _p3_ticker2 = _threading.Thread(target=_p3_tick2, daemon=True)
                    _p3_ticker2.start()
                    try:
                        az_results, az_unreachable, az_raw_text = _do_stacked_primary_scan_from_paths(batch_input)
                    except TokenMismatchError as e:
                        _p3_done2[0] = True
                        return _abort_for_token_mismatch(e)
                    _p3_done2[0] = True
                    if not az_unreachable:
                        total_provA_batch_units += 1
                        phase3_provA_calls += 1
                        batch_mrz = az_results
                        batch_raw_text = az_raw_text
                    else:
                        print("  [PHASE 3] ProvA stacked also failed for this batch — items marked invalid for this phase.")
                        batch_mrz = {}
                        batch_raw_text = {}

                for item in batch:
                    new_mrz = batch_mrz.get(item['file_index'])
                    if new_mrz:
                        mrz_results[item['filename']] = new_mrz
                        raw_text_results[item['filename']] = batch_raw_text.get(item['file_index'])
                    # Tick progress so bar keeps moving through Phase 3
                    progress_counter += 1
                    _push_progress(current_=progress_counter)
                # Force-write final counter for this batch unconditionally (same
                # fix as Phase 2 — throttle blocks the rapid post-API loop writes)
                update_progress_field(user_id, current_=progress_counter)

                # ── Cancel check inside Phase 3 batch loop ──────────────
                if _is_cancelled():
                    _do_rollback_and_abort()
                    for _item in pending_items:
                        for _p in [_item.get('original_path'), _item.get('resized_path')]:
                            try:
                                if _p and os.path.exists(_p): os.remove(_p)
                            except Exception: pass
                    if is_chunk_upload:
                        return jsonify({"cancelled": True})
                    return redirect(url_for("index"))
                # ─────────────────────────────────────────────────────────

            print(f"✅ Phase 3 complete: {total_provB_calls} ProvB Vision call(s), "
                  f"{total_provA_batch_units} ProvA batch unit(s) used so far.")

            # ── Determine Phase-3 failures ──
            phase3_failures = [
                item for item in phase2_failures
                if not _mrz_is_valid(mrz_results.get(item['filename']))
            ]
        else:
            print("  [PHASE 3] Skipped: no Phase 2 failures.")
            phase3_failures = []

        # =================================================================
        # PHASE 4 – Individual ProvA Scan
        #   Only runs if MORE THAN 3 items failed Phase 3.
        #   ALL of those items go to individual scanning — whether the MRZ
        #   was unreadable or read-but-invalid (bad country/nationality
        #   code, name format, checksum, etc.). Only expiry, duplicate, and
        #   rotation failures are excluded — and those never reach this
        #   list in the first place (see Phase 2/3 comments above).
        # =================================================================
        if len(phase3_failures) > 3:
            # Expand total so Phase 4 increments don't prematurely fire the "done" check
            _phase_total += len(phase3_failures)
            update_progress_field(user_id, total=_phase_total,
                                  phase=f'Phase 4: Individual Scanning [0/{len(phase3_failures)}]')
            print(f"  [PHASE 4] {len(phase3_failures)} passport(s) (> 3) going to individual ProvA scan.")

            for i, item in enumerate(phase3_failures):
                filename = item['filename']
                resized_path = item['resized_path']

                import concurrent.futures as _cfe
                with _cfe.ThreadPoolExecutor(max_workers=1) as _p4_ex:
                    # NOTE: use submit_with_context (not .submit directly) so the
                    # worker thread inherits the current user_id contextvar -- otherwise
                    # this call goes out with no user_id, the server treats that as an
                    # ownership mismatch, and the request gets 401'd even with a valid
                    # token (this previously caused Phase 4 to abort every scan).
                    # scan_index is 1-based position of this passport within THIS
                    # batch's Phase 4 pass -- the server only tries LlmB for the
                    # first 4 individual scans of a batch, then goes straight to
                    # ProvA -> ProvB for the rest.
                    _p4_fut = submit_with_context(_p4_ex, extract_mrz_from_image, resized_path, i + 1)
                    try:
                        mrz, provA_units, p4_raw_text = _p4_fut.result(timeout=65)
                    except _cfe.TimeoutError:
                        print(f"  [Phase 4] 65s timeout for {filename} — marking as invalid.")
                        mrz, provA_units, p4_raw_text = None, 0, None
                    except TokenMismatchError as e:
                        # Same token is used for every remaining item in this
                        # loop -- abort now instead of retrying N more times.
                        return _abort_for_token_mismatch(e)
                total_provA_individual_units += provA_units

                if mrz:
                    mrz_results[filename] = mrz
                raw_text_results[filename] = p4_raw_text
                # NOTE: extract_mrz_from_image() already falls back to ProvB
                # individual scan internally if ProvA is unreachable
                # (server fail). If both fail, mrz stays None → invalid.

                # Real per-item progress — each individual scan is its own API
                # call (2-5 s), so the throttle won't block these writes
                progress_counter += 1
                update_progress_field(user_id, current_=progress_counter,
                                      phase=f'Phase 4: Individual Scanning [{i + 1}/{len(phase3_failures)}]')

                # ── Cancel check inside Phase 4 item loop ───────────────
                if _is_cancelled():
                    _do_rollback_and_abort()
                    for _item in pending_items:
                        for _p in [_item.get('original_path'), _item.get('resized_path')]:
                            try:
                                if _p and os.path.exists(_p): os.remove(_p)
                            except Exception: pass
                    if is_chunk_upload:
                        return jsonify({"cancelled": True})
                    return redirect(url_for("index"))
                # ─────────────────────────────────────────────────────────

            print(f"✅ Phase 4 complete: {total_provA_individual_units} ProvA individual unit(s) used.")
        elif phase3_failures:
            print(f"  [PHASE 4] Skipped: only {len(phase3_failures)} failure(s) (<= 3 threshold). "
                  f"Saving directly as invalid.")

        # =================================================================
        # PHASE 5 – Final DB processing  (threaded — same pattern as Phase 1)
        # =================================================================
        _n_save = len(pending_items)
        update_progress_field(user_id, phase=f'Phase 5: Saving [0/{_n_save}]')
        accounted_filenames = set()

        # ── Worker: all expensive work (face-crop + DB writes) done in threads ──
        def _save_single_item(item):
            _filename      = item['filename']
            _original_path = item['original_path']
            _resized_path  = item['resized_path']
            _mrz_lines     = mrz_results.get(_filename)
            _raw_text      = raw_text_results.get(_filename) or ""
            _face_path     = None
            try:
                if not _mrz_lines:
                    _err = f"MRZ not detected after ProvA batch + ProvB Vision scan (Group: {db_defaults.get('group_name', 'GROUP 1')})"
                    _inv = save_invalid_to_db(user_id, _filename, _original_path, "", _err,
                                               upload_group_name=db_defaults.get('group_name', 'GROUP 1'),
                                               upload_visa_type=db_defaults.get('visa_type', 'nusuk'))
                    for _p in [_original_path, _resized_path]:
                        try:
                            if _p and os.path.exists(_p): os.remove(_p)
                        except Exception: pass
                    return {'outcome': 'invalid', 'filename': _filename, 'error': "MRZ not detected", 'invalid_id': _inv}

                ocr_logger.debug(
                    f"[DEBUG issue-date] {_filename}: raw_text_results has key={_filename in raw_text_results} "
                    f"| _raw_text length={len(_raw_text)} | preview={_raw_text[:80]!r}"
                )
                _parsed, _errors = parse_mrz(_mrz_lines, raw_text=_raw_text)
                # Never estimate: only use the issue date actually read off
                # the document via extract_issue_date_from_text() (consumed
                # from the parse_mrz() call above, raw_text was available
                # here). Single-use getter, so capture it once now and reuse
                # this same value on both the success and invalid paths below
                # -- it must NOT be called a second time later in this
                # function, since the cache is cleared on first read.
                _issue_date = get_last_extracted_issue_date()
                if _parsed:
                    if "dob"    in _parsed: _parsed["dob"]    = convert_mrz_date(_parsed["dob"],    is_dob=True)  or "1900-01-01"
                    if "expiry" in _parsed: _parsed["expiry"] = convert_mrz_date(_parsed["expiry"], is_dob=False) or "2030-01-01"
                    # Fallback: if no printed issue date could be extracted from
                    # OCR text, estimate one from expiry/dob/country instead of
                    # leaving passport_issue_date NULL. This is a deliberate
                    # policy change from "never estimate, only extracted" --
                    # the estimate is a heuristic (10yr/5yr validity, +1 day,
                    # KGZ/RUS offset removed) and can be wrong for passports
                    # that don't follow standard validity periods.
                    _issue_date_estimated = False
                    if not _issue_date:
                        _est = estimate_issue_date(
                            _parsed.get("expiry"), _parsed.get("dob"), _parsed.get("country")
                        )
                        if _est:
                            _issue_date = _est
                            _issue_date_estimated = True
                    if not _errors:
                        _ie = check_issue_date_rule(_parsed)
                        if _ie: _errors = [_ie]
                    if not _errors:
                        _exp_str = _parsed.get("expiry", "")
                        if _exp_str and _exp_str != "2030-01-01":
                            try:
                                _exp_dt = datetime.strptime(_exp_str, "%Y-%m-%d").date()
                                if _exp_dt <= (ist_now().date() + timedelta(days=183)):
                                    _errors = [f"Passport expires {_exp_dt.strftime('%Y-%m-%d')} — within 6 months of expiry"]
                            except ValueError:
                                pass

                if _errors:
                    _err = f"{'; '.join(_errors)} (Group: {db_defaults.get('group_name', 'GROUP 1')})"
                    _inv = save_invalid_to_db(user_id, _filename, _original_path, "\n".join(_mrz_lines), _err,
                                               upload_group_name=db_defaults.get('group_name', 'GROUP 1'),
                                               upload_visa_type=db_defaults.get('visa_type', 'nusuk'),
                                               extracted_issue_date=_issue_date,
                                               is_emergency=is_emergency_upload)
                    for _p in [_original_path, _resized_path]:
                        try:
                            if _p and os.path.exists(_p): os.remove(_p)
                        except Exception: pass
                    return {'outcome': 'invalid', 'filename': _filename, 'error': _err, 'invalid_id': _inv}

                _pn = _parsed.get("passport_number", "")
                _upload_group = db_defaults.get('group_name', 'GROUP 1')
                _upload_visa_type = db_defaults.get('visa_type', 'nusuk')

                # ── Emergency uploads (checkbox checked) skip the full
                # cross-group duplication rules and are only checked for a
                # duplicate passport number within the SAME group being
                # uploaded into. Regular uploads keep the full check. ──
                if is_emergency_upload:
                    _dup_hit = is_passport_number_exists_in_group_same_group_only(_pn, user_id, _upload_group)
                else:
                    _dup_hit = is_passport_number_exists_in_group(_pn, user_id, _upload_group, _upload_visa_type)

                if _dup_hit:
                    _dup_groups = ", ".join(dict.fromkeys(m['group_name'] for m in _dup_hit.get('matches', [_dup_hit])))
                    _err = f"Duplicate passport number: {_pn} (Group: {_dup_groups})"
                    _inv = save_invalid_to_db(user_id, _filename, _original_path, "\n".join(_mrz_lines), _err,
                                               upload_group_name=_upload_group,
                                               upload_visa_type=_upload_visa_type,
                                               extracted_issue_date=_issue_date,
                                               is_emergency=is_emergency_upload)
                    for _p in [_original_path, _resized_path]:
                        try:
                            if _p and os.path.exists(_p): os.remove(_p)
                        except Exception: pass
                    return {'outcome': 'duplicate', 'filename': _filename, 'passport_number': _pn, 'invalid_id': _inv}

                # ── Face crop (CPU-intensive — benefits most from threading) ──
                _face_path    = os.path.join(app.config["FACE_FOLDER"], f"face_{_filename}")
                _face_success = False
                try:
                    _face_success = crop_passport_face(_original_path, _face_path)
                except Exception:
                    pass

                if not _face_success or not os.path.exists(_face_path):
                    try:
                        _pil_fb = Image.open(_original_path)
                        _pil_fb = ImageOps.exif_transpose(_pil_fb).convert("RGB")
                        _fb_w, _fb_h = _pil_fb.size
                        _img_cv = cv2.cvtColor(np.array(_pil_fb), cv2.COLOR_RGB2BGR)
                        _gray   = cv2.cvtColor(_img_cv, cv2.COLOR_BGR2GRAY)
                        _faces  = _face_cascade.detectMultiScale(_gray, scaleFactor=1.05, minNeighbors=3, minSize=(40, 40))
                        if len(_faces) > 0:
                            _x, _y, _w, _h = sorted(_faces, key=lambda f: f[2]*f[3], reverse=True)[0]
                            _pad = int(max(_w, _h) * 0.2)
                            _pil_fb.crop((max(0,_x-_pad), max(0,_y-_pad),
                                          min(_fb_w,_x+_w+_pad), min(_fb_h,_y+_h+_pad))
                                        ).save(_face_path, 'JPEG', quality=90)
                        else:
                            _pil_fb.crop((0, int(_fb_h*0.15), int(_fb_w*0.38), int(_fb_h*0.85))
                                        ).save(_face_path, 'JPEG', quality=90)
                    except Exception as _fe:
                        print(f"  ⚠️ Face crop fallback failed for {_filename}: {_fe}")

                # ── DB inserts (I/O — also benefits from threading) ──
                _nat_id      = NATIONALITY_CODE_MAP.get(_parsed.get("nationality"), 197)
                # NOTE: _issue_date was already captured once, right after the
                # parse_mrz() call earlier in this function (single-use cache
                # -- do NOT call get_last_extracted_issue_date() again here,
                # it would return None and silently overwrite the real value).
                _arr_date, _dep_date = _resolve_default_arrival_departure(db_defaults, now, one_year_later)

                _pid = insert_passport(user_id, _parsed, None, None, "\n".join(_mrz_lines), _filename)
                insert_general_data(
                    passport_id=_pid,
                    nationality_id=_nat_id,
                    marital_status=safe_int(db_defaults.get('marital_status'), 5),
                    group_name=db_defaults.get('group_name', 'GROUP 1'),
                    city_of_birth=db_defaults.get('city_of_birth', 'MAIN STREET'),
                    profession=db_defaults.get('profession', 'TOURISM'),
                    city=db_defaults.get('city', 'MAIN STREET'),
                    zip_postal_code=db_defaults.get('zip_postal_code', '676542'),
                    address=db_defaults.get('address', 'ADDRESS'),
                    passport_type=safe_int(db_defaults.get('passport_type'), 1),
                    passport_issue_place=db_defaults.get('passport_issue_place', 'PLACE'),
                    passport_issue_date=_issue_date,
                    issue_date_estimated=locals().get('_issue_date_estimated', False),
                    expected_arrival=_arr_date,
                    expected_departure=_dep_date,
                    hotel_name=db_defaults.get('hotel_name', 'Hayat Mall Gate 6, Riyadh'),
                    contact_number=db_defaults.get('contact_number', ''),
                    email=db_defaults.get('email', ''),
                    visa_type=db_defaults.get('visa_type', 'nusuk'),
                    is_emergency=is_emergency_upload
                )
                # ── Move the original + face image into the group's own folder ──
                _group_for_save = db_defaults.get('group_name', 'GROUP 1')
                _final_original_path = get_passport_path(_filename, _group_for_save, kind="original")
                try:
                    if os.path.exists(_original_path) and _original_path != _final_original_path:
                        shutil.move(_original_path, _final_original_path)
                except Exception as _move_e:
                    print(f"  ⚠️ Could not move original into group folder for {_filename}: {_move_e}")
                    _final_original_path = _original_path  # fall back to wherever it actually is

                _face_disk = os.path.join(app.config["FACE_FOLDER"], f"face_{_filename}")
                _final_face_path = get_passport_path(_filename, _group_for_save, kind="face")
                try:
                    if os.path.exists(_face_disk) and _face_disk != _final_face_path:
                        shutil.move(_face_disk, _final_face_path)
                except Exception as _move_e:
                    print(f"  ⚠️ Could not move face image into group folder for {_filename}: {_move_e}")
                    _final_face_path = _face_disk

                # Collect disk paths created for this passport (rollback support)
                _saved_file_paths = [_final_original_path]
                if os.path.exists(_final_face_path):
                    _saved_file_paths.append(_final_face_path)
                return {'outcome': 'success', 'filename': _filename,
                        'passport_id': _pid, 'saved_file_paths': _saved_file_paths}

            except Exception as _e:
                import traceback; traceback.print_exc()
                _err = f"Processing error: {str(_e)[:200]} (Group: {db_defaults.get('group_name', 'GROUP 1')})"
                try:
                    _inv = save_invalid_to_db(user_id, _filename, _original_path,
                                              "\n".join(_mrz_lines) if _mrz_lines else "", _err,
                                              upload_group_name=db_defaults.get('group_name', 'GROUP 1'),
                                              upload_visa_type=db_defaults.get('visa_type', 'nusuk'),
                                              extracted_issue_date=locals().get('_issue_date'))
                except Exception: _inv = None
                for _p in [_original_path, _resized_path, _face_path]:
                    try:
                        if _p and os.path.exists(_p): os.remove(_p)
                    except Exception: pass
                return {'outcome': 'error', 'filename': _filename,
                        'error': f"Processing error: {str(_e)[:150]}", 'invalid_id': _inv}

        # ── Spin up thread pool — same sizing logic as Phase 1 ──
        _p5_threads = min(os.cpu_count() or 4, 8)
        print(f"[Phase 5] Spinning up {_p5_threads} threads for {_n_save} items...")
        _p5_done = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=_p5_threads) as _p5_ex:
            _p5_futures = {submit_with_context(_p5_ex, _save_single_item, item): item for item in pending_items}

            for _p5_fut in concurrent.futures.as_completed(_p5_futures):
                _res = _p5_fut.result()
                _p5_done += 1
                accounted_filenames.add(_res['filename'])

                if _res['outcome'] == 'success':
                    successfully_processed += 1
                    # Track IDs + file paths for rollback
                    with _session_lock:
                        _pid_saved = _res.get('passport_id')
                        if _pid_saved:
                            _session_passport_ids.append(_pid_saved)
                        _session_file_paths.extend(_res.get('saved_file_paths', []))
                    update_progress_field(user_id, success=successfully_processed,
                                          phase=f'Phase 5: Saving [{_p5_done}/{_n_save}]')
                elif _res['outcome'] == 'duplicate':
                    duplicates.append({"filename": _res['filename'],
                                       "passport_number": _res.get('passport_number', '')})
                    with _session_lock:
                        _inv_id = _res.get('invalid_id')
                        if _inv_id: _session_invalid_ids.append(_inv_id)
                    update_progress_field(user_id, duplicate=len(duplicates),
                                          phase=f'Phase 5: Saving [{_p5_done}/{_n_save}]')
                else:
                    validation_errors.append({"filename": _res['filename'],
                                              "error": _res.get('error', ''),
                                              "invalid_id": _res.get('invalid_id')})
                    with _session_lock:
                        _inv_id = _res.get('invalid_id')
                        if _inv_id: _session_invalid_ids.append(_inv_id)
                    update_progress_field(user_id, invalid=len(validation_errors),
                                          phase=f'Phase 5: Saving [{_p5_done}/{_n_save}]')

                progress_counter += 1
                _push_progress(current_=progress_counter)

                # ── Cancel check inside Phase 5 (between-file granular) ──
                if _is_cancelled():
                    # Let the executor drain remaining in-flight futures naturally,
                    # then roll back everything that was already saved.
                    break
                # ─────────────────────────────────────────────────────────

        # Unconditional final write — ensures rawCur == rawTotal so the JS
        # completion check (rawCur >= rawTotal) always fires after Phase 5.
        update_progress_field(user_id, current_=progress_counter,
                              phase=f'Phase 5: Saving [{_n_save}/{_n_save}]')

        for item in pending_items:
            if item['filename'] not in accounted_filenames:
                fname = item['filename']
                inv_id = save_invalid_to_db(user_id, fname, item['original_path'], "\n".join(mrz_results.get(fname) or []), "Image fell through processing pipeline (safety net)",
                                             upload_group_name=db_defaults.get('group_name', 'GROUP 1'),
                                             upload_visa_type=db_defaults.get('visa_type', 'nusuk'))
                validation_errors.append({"filename": fname, "error": "Unhandled processing failure (safety net)", "invalid_id": inv_id})
                for _p in [item['original_path'], item['resized_path']]:
                    try:
                        if _p and os.path.exists(_p): os.remove(_p)
                    except Exception: pass

        # ── Post-Phase 5 cancel check (covers break out of the loop) ────
        if _is_cancelled():
            _do_rollback_and_abort()
            if is_chunk_upload:
                return jsonify({"cancelled": True})
            return redirect(url_for("index"))
        # ─────────────────────────────────────────────────────────────────

        # Clean up the cancel flag now that processing completed successfully
        with _cancel_flags_lock:
            _cancel_flags.pop(user_id, None)

        clear_progress(user_id)
        session.pop("pending_correction", None)

        _this_chunk_stats = {
            "processed": successfully_processed,
            "emergency": 0,
            "duplicates": len(duplicates),
            "invalid": len(validation_errors),
            "provA_batch_units": total_provA_batch_units,
            "provA_individual_units": total_provA_individual_units,
            "provB_calls": total_provB_calls,
            "total_provA_units": total_provA_batch_units + total_provA_individual_units,
            "ocr_units": total_provA_batch_units + total_provA_individual_units + total_provB_calls,
            "skipped_files": skipped_files,
            "phase2_provB_calls": phase2_provB_calls,
            "phase2_provA_calls": phase2_provA_calls,
            "phase3_provB_calls": phase3_provB_calls,
            "phase3_provA_calls": phase3_provA_calls,
            "phase2_file_count": phase2_file_count,
            "phase3_file_count": phase3_file_count,
        }
        session["upload_stats"] = _this_chunk_stats

        # Accumulate API usage stats across chunks of the same batch.
        # Chunks in one upload arrive seconds apart; a new upload session
        # starts fresh after a 5-minute gap (300 s window).
        _prev_stats = session.get('latest_api_stats', {})
        _now_ts     = time.time()
        _same_batch = (
            is_chunk_upload
            and _prev_stats
            and (_now_ts - _prev_stats.get('_ts', 0)) < 300
        )
        _ACCUM_KEYS = [
            'processed', 'emergency', 'duplicates', 'invalid',
            'provA_batch_units', 'provA_individual_units', 'provB_calls',
            'total_provA_units', 'ocr_units',
            'phase2_provB_calls', 'phase2_provA_calls',
            'phase3_provB_calls', 'phase3_provA_calls',
            'phase2_file_count', 'phase3_file_count',
        ]
        if _same_batch:
            _accum = {}
            for _k in _ACCUM_KEYS:
                _accum[_k] = _prev_stats.get(_k, 0) + _this_chunk_stats.get(_k, 0)
            _accum['skipped_files'] = _prev_stats.get('skipped_files', []) + skipped_files
            _accum['_ts'] = _now_ts
            session["latest_api_stats"] = _accum
        else:
            _this_chunk_stats['_ts'] = _now_ts
            session["latest_api_stats"] = _this_chunk_stats.copy()

        # Report usage to the host ONCE for the whole batch (replaces the
        # previous per-file host calls, which caused major upload lag on
        # large batches). log_passport_upload() already reports to the host
        # internally via host_log_upload() — do NOT also call
        # report_usage_to_host() here, as that double-counts every upload
        # against the server-side quota.
        log_passport_upload(
            user_id,
            count=successfully_processed,
            duplicates=0,
            invalids=len(validation_errors),
        )

        # Refresh usage warning after every upload so 50% / 90% / exceeded
        # popups fire in the same session, not only on the next login.
        _set_usage_warning(user_id)

        if is_chunk_upload:
            # Return JSON so the frontend can chain multiple chunks without a reload
            return jsonify({
                "success": True,
                "processed": successfully_processed,
                "emergency": 0,
                "duplicates": len(duplicates),
                "invalid": len(validation_errors),
                "skipped_files": skipped_files,
            })

        return redirect(url_for("index") + "?show_stats=1")

    if request.args.get("show_stats") == "1":
        upload_stats = session.pop("upload_stats", None)
        if upload_stats:
            template_context["upload_stats"] = upload_stats

    # ── Show upload-blocked popup on page load (GET) ──────────────────────────
    # Only fires when a POST just redirected here with block data in the session.
    # We do NOT re-check quota on plain index loads — warning shows on login only.
    blocked_data = session.pop('_upload_blocked', None)
    if blocked_data:
        template_context['upload_blocked']            = True
        template_context['upload_blocked_reason']     = blocked_data.get('reason', 'limit_exceeded')
        template_context['upload_blocked_amount']     = blocked_data.get('amount', 0)
        template_context['upload_blocked_used']       = blocked_data.get('used', 0)
        template_context['upload_blocked_limit']      = blocked_data.get('limit', 0)
        template_context['upload_blocked_selected']   = blocked_data.get('selected', 0)
        template_context['upload_blocked_remaining']  = blocked_data.get('remaining', 0)
        template_context['upload_blocked_allow_extra'] = blocked_data.get('allow_extra_usage', False)

    # Pass usage warning popup — only deferred if the upload-blocked overlay is
    # showing (that overlay has its own separate flow). When the stats modal is
    # showing, the frontend (see closeStatsModal()) chains straight into the
    # warning popup on the same page load, so we consume it here too.
    if template_context.get('upload_blocked'):
        usage_warning = session.get("_usage_warning", None)   # peek, don't pop
    else:
        usage_warning = session.pop("_usage_warning", None)   # consume it
    if usage_warning:
        template_context["usage_warning"] = usage_warning

    return render_template("index.html", **template_context)


@app.route("/correct", methods=["POST"])
@login_required
def correct_mrz():
    user_id = session['user_id']
    filename = request.form["filename"]
    mrz_text = request.form["mrz_edit"]
    action = request.form.get("action", "reparse")

    mrz_lines = [l.strip() for l in mrz_text.split("\n") if l.strip()]

    if action == "insert_anyway":
        parsed, _ = parse_mrz(mrz_lines, force=True)
        if parsed:
            if "dob" in parsed:
                parsed["dob"] = convert_mrz_date(parsed["dob"], is_dob=True) or "1900-01-01"
            if "expiry" in parsed:
                parsed["expiry"] = convert_mrz_date(parsed["expiry"], is_dob=False) or "2030-01-01"
        if not parsed:
            if len(mrz_lines) >= 2:
                l1, l2 = mrz_lines[0].upper(), mrz_lines[1].upper()
                surname = given_names = middle_name = ""
                if "<<" in l1[5:]:
                    parts = l1[5:].split("<<", 1)
                    surname = parts[0].replace("<", "").strip()
                    g_clean = parts[1].strip('<')
                    g_parts = [p for p in g_clean.split('<') if p]
                    given_names = g_parts[0] if len(g_parts) > 0 else ""
                    middle_name = " ".join(g_parts[1:]) if len(g_parts) > 1 else ""
                parsed = {
                    "doc_type": "P",
                    "country": (l1[2:5] if len(l1) >= 5 else "XXX"),
                    "surname": surname,
                    "given_names": given_names,
                    "middle_name": middle_name,
                    "passport_number": (l2[0:9].replace("<", "") if len(l2) >= 9 else "UNKNOWN"),
                    "nationality": (l2[10:13] if len(l2) >= 13 else "XXX"),
                    "dob": (l2[13:19] if len(l2) >= 19 else "000000"),
                    "sex": (l2[20] if len(l2) > 20 else "X"),
                    "expiry": (l2[21:27] if len(l2) >= 27 else "000000")
                }
            else:
                session["pending_correction"] = {
                    "filename": filename,
                    "mrz_text": mrz_text,
                    "errors": ["Cannot parse MRZ – at least 2 lines required."]
                }
                return redirect(url_for("index"))
    else:
        parsed, errors = parse_mrz(mrz_lines)
        if parsed:
            if "dob" in parsed:
                parsed["dob"] = convert_mrz_date(parsed["dob"], is_dob=True) or "1900-01-01"
            if "expiry" in parsed:
                parsed["expiry"] = convert_mrz_date(parsed["expiry"], is_dob=False) or "2030-01-01"
            if not errors:
                issue_error = check_issue_date_rule(parsed)
                if issue_error:
                    errors = [issue_error]
        if errors:
            session["pending_correction"] = {
                "filename": filename,
                "mrz_text": mrz_text,
                "errors": errors
            }
            return redirect(url_for("index"))

    passport_number = parsed.get("passport_number", "").strip()
    if not passport_number or passport_number == "UNKNOWN":
        session["pending_correction"] = {
            "filename": filename,
            "mrz_text": mrz_text,
            "errors": ["Passport number is missing or invalid – cannot insert."]
        }
        return redirect(url_for("index"))

    _dup_check_settings = get_user_settings(user_id) or {}
    _target_group_for_dup_check = _dup_check_settings.get('group_name', 'GROUP 1')
    _visa_type_for_dup_check = _dup_check_settings.get('visa_type', 'nusuk')

    # ── Group visa-type lock (same rule as the main upload flow): an
    # existing group can only ever hold one visa type. ──
    _existing_group_visa_type_manual = get_group_visa_type(user_id, _target_group_for_dup_check)
    if _existing_group_visa_type_manual and _existing_group_visa_type_manual != _visa_type_for_dup_check:
        _visa_label_manual = {'nusuk': 'Nusuk', 'visit_visa': 'Visit Visa'}
        session["pending_correction"] = {
            "filename": filename,
            "mrz_text": mrz_text,
            "errors": [
                f'Group "{_target_group_for_dup_check}" is already a '
                f'{_visa_label_manual.get(_existing_group_visa_type_manual, _existing_group_visa_type_manual)} group. '
                f'You are trying to add a {_visa_label_manual.get(_visa_type_for_dup_check, _visa_type_for_dup_check)} '
                f'record — only matching visa types can be added to the same group.'
            ]
        }
        return redirect(url_for("index"))

    _manual_dup_hit = is_passport_number_exists_in_group(passport_number, user_id, _target_group_for_dup_check, _visa_type_for_dup_check)
    if _manual_dup_hit:
        _manual_dup_groups = ", ".join(dict.fromkeys(
            m['group_name'] for m in _manual_dup_hit.get('matches', [_manual_dup_hit])
        ))
        session["pending_correction"] = {
            "filename": filename,
            "mrz_text": mrz_text,
            "errors": [f"Duplicate passport number: {passport_number}. Already exists in group \"{_manual_dup_groups}\"."]
        }
        return redirect(url_for("index"))

    try:
        original_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        face_path = os.path.join(app.config["FACE_FOLDER"], f"face_{filename}")
        try:
            crop_passport_face(original_path, face_path)
        except Exception:
            face_path = None

        # Files stay permanently on filesystem — no BLOBs, no deletion
        passport_id = insert_passport(user_id, parsed, None, None, mrz_text, filename)

        now = ist_now()
        one_year_later = now + timedelta(days=365)
        mrz_nationality_id = NATIONALITY_CODE_MAP.get(parsed["nationality"], 197)
        # This flow works from manually-typed/corrected MRZ text with no
        # raw OCR text available (parse_mrz() above was called without
        # raw_text) -- there is nothing to extract an issue date from, and
        # per policy we never estimate. passport_issue_date is a nullable
        # column, so store NULL rather than guessing a placeholder date;
        # the user can fill it in via the issue-date edit UI afterward.
        final_issue_date = None
        db_defaults = get_user_settings(user_id)

        _arr_date, _dep_date = _resolve_default_arrival_departure(db_defaults, now, one_year_later)
        _group_for_save = db_defaults.get('group_name', 'GROUP 1')
        insert_general_data(
            passport_id=passport_id,
            nationality_id=mrz_nationality_id,
            marital_status=safe_int(db_defaults.get('marital_status'), 5),
            group_name=_group_for_save,
            city_of_birth=db_defaults.get('city_of_birth', 'MAIN STREET'),
            profession=db_defaults.get('profession', 'TOURISM'),
            city=db_defaults.get('city', 'MAIN STREET'),
            zip_postal_code=db_defaults.get('zip_postal_code', '676542'),
            address=db_defaults.get('address', 'ADDRESS'),
            passport_type=safe_int(db_defaults.get('passport_type'), 1),
            passport_issue_place=db_defaults.get('passport_issue_place', 'PLACE'),
            passport_issue_date=final_issue_date,
            expected_arrival=_arr_date,
            expected_departure=_dep_date,
            hotel_name=db_defaults.get('hotel_name', 'Hayat Mall Gate 6, Riyadh'),
            visa_type=db_defaults.get('visa_type', 'nusuk')
        )

        # Move the original + face image into the group's own folder now that
        # the record is confirmed saved.
        _final_original_path = get_passport_path(filename, _group_for_save, kind="original")
        try:
            if os.path.exists(original_path) and original_path != _final_original_path:
                shutil.move(original_path, _final_original_path)
        except Exception as _move_e:
            print(f"  ⚠️ Could not move original into group folder for {filename}: {_move_e}")
        if face_path:
            _final_face_path = get_passport_path(filename, _group_for_save, kind="face")
            try:
                if os.path.exists(face_path) and face_path != _final_face_path:
                    shutil.move(face_path, _final_face_path)
            except Exception as _move_e:
                print(f"  ⚠️ Could not move face image into group folder for {filename}: {_move_e}")

        session.pop("pending_correction", None)
        # log_passport_upload() already reports to the host internally via
        # host_log_upload() — do not also call report_usage_to_host() here.
        log_passport_upload(user_id, count=1, duplicates=0, invalids=-1)
        return redirect(url_for("results"))

    except Exception as e:
        session["pending_correction"] = {
            "filename": filename,
            "mrz_text": mrz_text,
            "errors": [f"Unexpected error during insert: {str(e)}"]
        }
        return redirect(url_for("index"))


@app.route("/extract_all_passports")
@login_required
def extract_all_passports():
    return _extract_passports(filtered=False)


@app.route("/extract_filtered_passports")
@login_required
def extract_filtered_passports():
    passport_number = request.args.get('passport_number')
    group_name = request.args.get('group_name')
    has_filters = bool(passport_number or group_name)
    return _extract_passports(filtered=has_filters, passport_number=passport_number, group_name=group_name)


@app.route("/extract_selected_passports", methods=["POST"])
@login_required
def extract_selected_passports():
    selected_ids_json = request.form.get('selected_ids')
    if not selected_ids_json:
        return redirect(url_for('results'))
    selected_ids = json.loads(selected_ids_json)
    if not selected_ids:
        return redirect(url_for('results'))

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")

    # Look up group name for the selected passports
    format_strings = ','.join(['%s'] * len(selected_ids))
    cursor.execute(f"""
        SELECT DISTINCT g.group_name
        FROM general_data g
        WHERE g.passport_id IN ({format_strings})
          AND g.group_name IS NOT NULL AND g.group_name != ''
        LIMIT 1
    """, tuple(selected_ids))
    group_row = cursor.fetchone()
    group_name = (group_row.get('group_name', '') or '').strip() if group_row else ''
    if group_name:
        zip_download_name = f"{group_name.replace('/', '-').replace('\\', '-')}.zip"
    else:
        zip_download_name = f'selected_passports_{ist_now().strftime("%Y%m%d_%H%M%S")}.zip'

    memory_file = io.BytesIO()
    seen_names = set()

    with zipfile.ZipFile(memory_file, 'w') as zf:
        for p_id in selected_ids:
            cursor.execute("""
                SELECT p.passport_number, p.filename, g.group_name
                FROM passports p
                LEFT JOIN general_data g ON p.id = g.passport_id
                WHERE p.id = %s AND p.user_id = %s
            """, (p_id, session['user_id']))
            row = cursor.fetchone()
            if row and row.get('filename'):
                img_path, _ = resolve_passport_paths(row['filename'], row.get('group_name'))
                if not img_path or not os.path.exists(img_path):
                    continue
                pn = str(row.get('passport_number', '')).strip() or f"unknown_id_{p_id}"
                filename = f"{pn}.jpg"
                counter = 1
                while filename in seen_names:
                    filename = f"{pn}_{counter}.jpg"
                    counter += 1
                seen_names.add(filename)
                zf.write(img_path, filename)

    cursor.close()
    conn.close()
    memory_file.seek(0)

    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name=zip_download_name
    )


@app.route("/groups/download_visa")
@login_required
def download_group_visa():
    """Zip and download a group's visa_processed folder (the downloaded
    MOFA visa PDFs saved by mofa.py/mofa_downloader.py as
    visa_processed/<group_name>/<passport_number>_visa.pdf). If the group
    has no visa_processed folder yet (or it's empty), respond with JSON so
    the page can show a "No visa processed" message instead of a broken
    download."""
    group_name = (request.args.get("group_name") or "").strip()
    if not group_name:
        return jsonify({"success": False, "message": "Invalid group name."}), 400

    pdf_paths = list_visa_processed_pdfs(group_name)
    if not pdf_paths:
        return jsonify({"success": False, "message": "No visa processed."}), 404

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w') as zf:
        for path in pdf_paths:
            zf.write(path, os.path.basename(path))
    memory_file.seek(0)

    safe_name = group_name.replace('/', '-').replace('\\', '-')
    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f"{safe_name}.zip"
    )


@app.route("/generate_visa", methods=["POST"])
@login_required
def generate_visa():
    """Zip the already-downloaded visa PDFs (visa_processed/<group>/
    <passport_number>_visa.pdf) for the selected passports and download
    them as <group_name>.zip. Only passports whose PDF already exists on
    disk are included; passports not yet processed are silently skipped
    (mirrors extract_selected_passports' tolerant behavior)."""
    selected_ids_json = request.form.get('selected_ids')
    if not selected_ids_json:
        return jsonify({"success": False, "message": "No passports selected."}), 400
    try:
        selected_ids = json.loads(selected_ids_json)
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Invalid selection."}), 400
    if not selected_ids:
        return jsonify({"success": False, "message": "No passports selected."}), 400

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")

    format_strings = ','.join(['%s'] * len(selected_ids))
    cursor.execute(f"""
        SELECT p.id, p.passport_number, g.group_name
        FROM passports p
        LEFT JOIN general_data g ON p.id = g.passport_id
        WHERE p.id IN ({format_strings}) AND p.user_id = %s
    """, tuple(selected_ids) + (session['user_id'],))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    if not rows:
        return jsonify({"success": False, "message": "Selected passports not found."}), 404

    # Zip name follows the (first, non-empty) selected group's name, same
    # convention as extract_selected_passports.
    group_names = [r['group_name'] for r in rows if (r.get('group_name') or '').strip()]
    group_name = group_names[0].strip() if group_names else ''
    zip_download_name = (
        f"{group_name.replace('/', '-').replace(chr(92), '-')}.zip"
        if group_name else
        f'visas_{ist_now().strftime("%Y%m%d_%H%M%S")}.zip'
    )

    memory_file = io.BytesIO()
    seen_names = set()
    found_any = False

    with zipfile.ZipFile(memory_file, 'w') as zf:
        for row in rows:
            pn = (row.get('passport_number') or '').strip()
            grp = row.get('group_name')
            if not pn or not grp:
                continue
            pdf_path = get_visa_pdf_path(grp, pn)
            if not os.path.exists(pdf_path):
                continue
            found_any = True
            filename = f"{pn}_visa.pdf"
            counter = 1
            while filename in seen_names:
                filename = f"{pn}_visa_{counter}.pdf"
                counter += 1
            seen_names.add(filename)
            zf.write(pdf_path, filename)

    if not found_any:
        return jsonify({"success": False, "message": "No visa processed for the selected passport(s)."}), 404

    memory_file.seek(0)
    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name=zip_download_name
    )


# =====================================================
@app.route("/update_face_ajax/<int:passport_id>", methods=["POST"])
@login_required
def update_face_ajax(passport_id):
    try:
        data = request.json
        if not data or 'image_base64' not in data:
            return jsonify({"success": False, "message": "No image data provided"}), 400
        header, encoded = data['image_base64'].split(",", 1)
        face_bytes = base64.b64decode(encoded)
        # Get filename to build face path
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("USE passport_db")
        cursor.execute("SELECT filename FROM passports WHERE id = %s AND user_id = %s",
                       (passport_id, session['user_id']))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if not row or not row[0]:
            return jsonify({"success": False, "message": "Passport not found"}), 404
        _, face_path = resolve_passport_paths(row[0])
        with open(face_path, 'wb') as f:
            f.write(face_bytes)
        return jsonify({"success": True, "message": "Face updated successfully"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/rotate_passport_image_ajax/<int:passport_id>", methods=["POST"])
@login_required
def rotate_passport_image_ajax(passport_id):
    """Rotates the stored full passport image 90 degrees left or right and
    saves it back in-place, so the new orientation persists."""
    try:
        data = request.json or {}
        direction = data.get("direction", "right")
        angle = 90 if direction == "right" else -90  # PIL rotates counter-clockwise

        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("USE passport_db")
        cursor.execute("SELECT filename FROM passports WHERE id = %s AND user_id = %s",
                       (passport_id, session['user_id']))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if not row or not row[0]:
            return jsonify({"success": False, "message": "Passport not found"}), 404

        passport_path, _ = resolve_passport_paths(row[0])
        if not passport_path or not os.path.exists(passport_path):
            return jsonify({"success": False, "message": "Image file not found"}), 404

        with Image.open(passport_path) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            rotated = img.rotate(-angle, expand=True, fillcolor=(255, 255, 255))
            rotated.save(passport_path, format='JPEG', quality=95)

        return jsonify({"success": True, "message": "Passport image rotated successfully"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/update_passport_image_ajax/<int:passport_id>", methods=["POST"])
@login_required
def update_passport_image_ajax(passport_id):
    try:
        data = request.json
        if not data or 'image_base64' not in data:
            return jsonify({"success": False, "message": "No image data provided"}), 400
        header, encoded = data['image_base64'].split(",", 1)
        image_bytes = base64.b64decode(encoded)

        # Get filename to build passport image path
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("USE passport_db")
        cursor.execute("SELECT filename FROM passports WHERE id = %s AND user_id = %s",
                       (passport_id, session['user_id']))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if not row or not row[0]:
            return jsonify({"success": False, "message": "Passport not found"}), 404

        passport_path, _ = resolve_passport_paths(row[0])
        with open(passport_path, 'wb') as f:
            f.write(image_bytes)

        return jsonify({"success": True, "message": "Passport image updated successfully"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# =====================================================
# INLINE FIELD EDITING (Results page basic details)
# =====================================================

# Whitelist of fields that can be edited inline from the results page,
# mapped to the table that owns them and a few normalization rules.
EDITABLE_PASSPORT_FIELDS = {
    'passport_number':     {'table': 'passports',    'required': True,  'type': 'text'},
    'surname':             {'table': 'passports',    'required': True,  'type': 'text', 'upper': True},
    'middle_name':         {'table': 'passports',    'required': False, 'type': 'text', 'upper': True},
    'given_names':         {'table': 'passports',    'required': True,  'type': 'text', 'upper': True},
    'dob':                 {'table': 'passports',    'required': True,  'type': 'date'},
    'sex':                 {'table': 'passports',    'required': True,  'type': 'sex'},
    'expiry':              {'table': 'passports',    'required': True,  'type': 'date'},
    'nationality_id':      {'table': 'general_data', 'required': True,  'type': 'nationality'},
    'passport_issue_date': {'table': 'general_data', 'required': False, 'type': 'date'},
}


@app.route("/update_passport_field_ajax/<int:passport_id>", methods=["POST"])
@login_required
def update_passport_field_ajax(passport_id):
    """
    Generic single-field update used by the inline 'click-to-edit' fields on
    the results page (passport number, names, dob, sex, nationality, issue
    date, expiry date). Returns the saved value plus a display-formatted
    version so the front-end can refresh the label without a page reload.
    """
    user_id = session['user_id']
    data = request.json or {}
    field = data.get('field')
    raw_value = data.get('value', '')
    raw_value = raw_value.strip() if isinstance(raw_value, str) else raw_value

    spec = EDITABLE_PASSPORT_FIELDS.get(field)
    if not spec:
        return jsonify({"success": False, "message": "This field cannot be edited."}), 400

    field_type = spec.get('type')
    value = raw_value

    # ── Validation / normalization ──────────────────────────────────────
    if field_type == 'date':
        if raw_value:
            try:
                datetime.strptime(raw_value, '%Y-%m-%d')
            except ValueError:
                return jsonify({"success": False, "message": "Invalid date. Expected format YYYY-MM-DD."}), 400
            value = raw_value
        elif spec['required']:
            return jsonify({"success": False, "message": "This date is required and cannot be empty."}), 400
        else:
            value = None

    elif field_type == 'sex':
        value = raw_value.upper()
        if value not in ('M', 'F', 'X'):
            return jsonify({"success": False, "message": "Sex must be M, F, or X."}), 400

    elif field_type == 'nationality':
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "Invalid nationality selected."}), 400
        if value not in dict(NATIONALITY_OPTIONS):
            return jsonify({"success": False, "message": "Unknown nationality."}), 400

    else:  # plain text fields
        if spec['required'] and not raw_value:
            return jsonify({"success": False, "message": "This field cannot be empty."}), 400
        value = raw_value.upper() if spec.get('upper') else raw_value

    # ── Update DB ──────────────────────────────────────────────────────
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("USE passport_db")

        # Ownership check
        cursor.execute("SELECT id FROM passports WHERE id = %s AND user_id = %s", (passport_id, user_id))
        if not cursor.fetchone():
            return jsonify({"success": False, "message": "Passport record not found."}), 404

        # Guard against duplicate passport numbers (unique per user)
        if field == 'passport_number':
            cursor.execute(
                "SELECT id FROM passports WHERE passport_number = %s AND user_id = %s AND id != %s",
                (value, user_id, passport_id)
            )
            if cursor.fetchone():
                return jsonify({"success": False, "message": f"Passport number '{value}' already exists."}), 409

        if spec['table'] == 'passports':
            cursor.execute(
                f"UPDATE passports SET {field} = %s WHERE id = %s AND user_id = %s",
                (value, passport_id, user_id)
            )
        else:
            cursor.execute(f"""
                UPDATE general_data gd
                JOIN passports p ON gd.passport_id = p.id
                SET gd.{field} = %s
                WHERE gd.passport_id = %s AND p.user_id = %s
            """, (value, passport_id, user_id))

            # Keep the MRZ country code (passports.country) in sync when
            # the nationality is changed, so the "(XXX)" suffix updates too.
            new_country_code = None
            if field == 'nationality_id':
                new_country_code = NATIONALITY_ID_TO_COUNTRY_CODE.get(value)
                if new_country_code:
                    cursor.execute(
                        "UPDATE passports SET country = %s WHERE id = %s AND user_id = %s",
                        (new_country_code, passport_id, user_id)
                    )

        conn.commit()
    except mysql.connector.Error as db_err:
        conn.rollback()
        return jsonify({"success": False, "message": f"Database error: {str(db_err)[:150]}"}), 500
    finally:
        cursor.close()
        conn.close()

    # ── Friendly display value for the UI ────────────────────────────────
    if field_type == 'date':
        display_value = datetime.strptime(value, '%Y-%m-%d').strftime('%d-%m-%Y') if value else 'N/A'
    elif field_type == 'nationality':
        display_value = dict(NATIONALITY_OPTIONS).get(value, str(value))
    else:
        display_value = value if value not in (None, '') else 'N/A'

    response_payload = {"success": True, "value": value, "display_value": display_value}
    if field_type == 'nationality':
        response_payload["country_code"] = NATIONALITY_ID_TO_COUNTRY_CODE.get(value, '')

    return jsonify(response_payload)


@app.route("/update_issue_date/<int:passport_id>", methods=["POST"])
@login_required
def update_issue_date(passport_id):
    """Used by the 5yr/10yr quick-set buttons next to Issue Date."""
    user_id = session['user_id']
    data = request.json or {}
    issue_date = (data.get('issue_date') or '').strip()
    if not issue_date:
        return jsonify({"success": False, "message": "issue_date is required."}), 400
    try:
        datetime.strptime(issue_date, '%Y-%m-%d')
    except ValueError:
        return jsonify({"success": False, "message": "Invalid date format. Expected YYYY-MM-DD."}), 400

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("USE passport_db")
        cursor.execute("""
            UPDATE general_data gd
            JOIN passports p ON gd.passport_id = p.id
            SET gd.passport_issue_date = %s, gd.issue_date_estimated = FALSE
            WHERE gd.passport_id = %s AND p.user_id = %s
        """, (issue_date, passport_id, user_id))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        cursor.close()
        conn.close()


# =====================================================
@app.route("/results")
@login_required
def results():
    user_id = session['user_id']
    page = request.args.get('page', 1, type=int)
    passport_number = request.args.get('passport_number', '').strip()
    current_nationality = request.args.get('nationality_id', '')
    current_status = request.args.get('status', '')
    # exact=1 is sent by the duplicate-collision links on the Invalid
    # Passports page (per-group, Visit Visa expiry, and Nusuk 365-day
    # cards all use the same link pattern) so the filter matches only the
    # exact colliding passport number instead of a loose LIKE search that
    # could pull in unrelated partial matches.
    exact_match = request.args.get('exact') == '1'

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("USE passport_db")
        
        # 1. Fetch available Groups — ordered by the most recent activity
        #    (either a new passport added, or an existing record moved
        #    into the group via Change Group/Merge) so the truly most
        #    recently active group always appears first in every dropdown.
        cursor.execute("""
            SELECT g.group_name,
                   GREATEST(MAX(p.created_at), COALESCE(MAX(gb.last_activity_at), MAX(p.created_at))) AS last_created
            FROM general_data g
            JOIN passports p ON g.passport_id = p.id
            LEFT JOIN group_batches gb ON gb.group_name = g.group_name AND gb.user_id = p.user_id
            WHERE p.user_id = %s AND (p.is_recycled = FALSE OR p.is_recycled IS NULL)
              AND g.group_name IS NOT NULL AND g.group_name != ''
            GROUP BY g.group_name
            ORDER BY last_created DESC
        """, (user_id,))
        groups = [row['group_name'] for row in cursor.fetchall()]
        latest_group = groups[0] if groups else ''

        # 2. Handle default group selection logic (MOVED UP)
        if 'group_name' not in request.args and not passport_number and not current_nationality:
            current_group = groups[0] if groups else ''
        else:
            current_group = request.args.get('group_name', '')

        # 3. Fetch distinct Nationalities present in user's records (FILTERED BY GROUP)
        nat_query = """
            SELECT DISTINCT g.nationality_id
            FROM general_data g
            JOIN passports p ON g.passport_id = p.id
            WHERE p.user_id = %s AND (p.is_recycled = FALSE OR p.is_recycled IS NULL)
              AND g.nationality_id IS NOT NULL
        """
        nat_params = [user_id]
        
        # If a specific group is selected, only grab nationalities from that group
        if current_group:
            nat_query += " AND g.group_name = %s"
            nat_params.append(current_group)
            
        cursor.execute(nat_query, tuple(nat_params))
        distinct_nat_ids = [row['nationality_id'] for row in cursor.fetchall()]
        
        # Map nationality IDs to names for the dropdown (Alphabetical sort)
        nat_dict = dict(NATIONALITY_OPTIONS)
        active_nationalities = [(nid, nat_dict.get(nid, f"Unknown ({nid})")) for nid in distinct_nat_ids]
        active_nationalities.sort(key=lambda x: x[1])

        # 3. Handle default group selection logic
        if 'group_name' not in request.args and not passport_number and not current_nationality:
            current_group = groups[0] if groups else ''
        else:
            current_group = request.args.get('group_name', '')

        # 4. Build Filter Query
        where_clauses = ["p.user_id = %s", "(p.is_recycled = FALSE OR p.is_recycled IS NULL)"]
        params = [user_id]

        if passport_number:
            if exact_match:
                where_clauses.append("p.passport_number = %s")
                params.append(passport_number)
            else:
                where_clauses.append("""(
                    p.passport_number LIKE %s
                    OR p.given_names LIKE %s
                    OR p.middle_name LIKE %s
                    OR p.surname LIKE %s
                )""")
                like_val = f"%{passport_number}%"
                params.extend([like_val, like_val, like_val, like_val])
        if current_group:
            where_clauses.append("g.group_name = %s")
            params.append(current_group)
        if current_nationality:
            where_clauses.append("g.nationality_id = %s")
            params.append(current_nationality)
        if current_status == 'sent':
            where_clauses.append("(p.is_processed = TRUE OR p.is_visa_processed = TRUE)")
        elif current_status == 'unsent':
            where_clauses.append("(p.is_processed IS NOT TRUE AND p.is_visa_processed IS NOT TRUE)")

        where_sql = " AND ".join(where_clauses)
        is_filtered = bool(passport_number) or bool(current_group) or bool(current_nationality) or bool(current_status)
        per_page = 25
        total_pages = 0
        limit_sql = ""

        # Pagination logic
        if not is_filtered:
            cursor.execute(
                f"SELECT COUNT(*) as count FROM passports p LEFT JOIN general_data g ON p.id = g.passport_id WHERE {where_sql}",
                tuple(params)
            )
            total = cursor.fetchone()['count']
            total_pages = (total + per_page - 1) // per_page
            offset = (page - 1) * per_page
            limit_sql = f" LIMIT {per_page} OFFSET {offset}"

        # Main Data Fetch
        query = f"""
            SELECT p.*,
                   g.group_name, g.marital_status, g.city_of_birth, g.profession,
                   g.city, g.zip_postal_code, g.address, g.passport_issue_place,
                   g.hotel_name, g.passport_type, g.passport_issue_date, g.issue_date_estimated,
                   g.expected_arrival, g.expected_departure, g.nationality_id,
                   g.visa_type, g.is_emergency
            FROM passports p
            LEFT JOIN general_data g ON p.id = g.passport_id
            WHERE {where_sql}
            ORDER BY p.created_at DESC
            {limit_sql}
        """
        cursor.execute(query, tuple(params))
        data = cursor.fetchall()

        # Verify mofa_pdf_downloaded_at rows on THIS page still have their
        # PDF on disk -- catches files deleted/moved since the last login
        # sweep so the "Visa Available" badge never lies to the user. Only
        # checked for rows that claim the flag is set (cheap: <=25/page).
        _stale_mofa_ids = []
        for _row in data:
            if _row.get('mofa_pdf_downloaded_at'):
                _pdf_path = get_visa_pdf_path(_row.get('group_name'), _row.get('passport_number'))
                if not os.path.exists(_pdf_path):
                    _row['mofa_pdf_downloaded_at'] = None
                    _stale_mofa_ids.append(_row['id'])
        if _stale_mofa_ids:
            _fmt = ','.join(['%s'] * len(_stale_mofa_ids))
            cursor.execute(
                f"UPDATE passports SET mofa_pdf_downloaded_at = NULL WHERE id IN ({_fmt})",
                tuple(_stale_mofa_ids)
            )
            conn.commit()
            logger.info(
                "results(): cleared %d stale mofa_pdf_downloaded_at value(s) on this page "
                "(PDF missing on disk): ids=%s", len(_stale_mofa_ids), _stale_mofa_ids
            )

        cursor.execute(
            "SELECT COUNT(*) as cnt FROM passports WHERE user_id = %s AND is_recycled = TRUE", (user_id,)
        )
        recycled_passports_count = cursor.fetchone()['cnt']
        
        cursor.execute("SELECT api_key FROM users WHERE id = %s", (user_id,))
        user_row = cursor.fetchone()
        api_key = user_row['api_key'] if user_row and user_row.get('api_key') else "Error: Key missing"

        # Each existing group's own visa_type, so the Change Group modal can
        # warn client-side before a transfer that would flip a record's
        # visa_type (and thus reset its "sent" status) — without needing a
        # round trip to the server just to find out.
        try:
            group_visa_types = get_distinct_group_visa_types() or {}
        except Exception:
            logger.exception("get_distinct_group_visa_types() failed - using {}")
            group_visa_types = {}

    except Exception as e:
        import traceback
        traceback.print_exc()
        data, groups, current_group, total_pages = [], [], '', 0
        active_nationalities, current_nationality = [], ''
        is_filtered, recycled_passports_count, api_key = False, 0, "Error: Key missing"
        current_status = ''
        group_visa_types = {}
        flash(f"Database Error: {str(e)}", "danger")
    finally:
        cursor.close()
        conn.close()

    return render_template('results.html',
                           data=data, page=page, total_pages=total_pages,
                           groups=groups, current_group=current_group,
                           latest_group=latest_group,
                           active_nationalities=active_nationalities,
                           current_nationality=current_nationality,
                           current_status=current_status,
                           filtered=is_filtered,
                           recycled_passports_count=recycled_passports_count,
                           marital_status_options=MARITAL_STATUS_OPTIONS,
                           nationality_options=NATIONALITY_OPTIONS,
                           api_key=api_key,
                           user_defaults=get_user_settings(user_id) or {},
                           group_visa_types=group_visa_types,
                           today=ist_now().date(),
                           local_api_token=LOCAL_API_TOKEN,
                           local_api_host=request.host)


@app.route("/update_passport/<int:passport_id>", methods=["POST"])
@login_required
def update_passport(passport_id):
    user_id = session['user_id']
    try:
        new_face_image = request.files.get('new_face_image')
        if new_face_image and new_face_image.filename != '':
            img_bytes = new_face_image.read()
            try:
                Image.open(io.BytesIO(img_bytes)).verify()
            except Exception as e:
                flash(f'Invalid face image format: {str(e)}', 'danger')
                return redirect(url_for('edit_passport', passport_id=passport_id))
            # Save face to filesystem
            _conn = get_connection()
            _cur = _conn.cursor()
            _cur.execute("USE passport_db")
            _cur.execute("SELECT filename FROM passports WHERE id = %s AND user_id = %s",
                         (passport_id, user_id))
            _row = _cur.fetchone()
            _cur.close()
            _conn.close()
            if _row and _row[0]:
                _, _face_path = resolve_passport_paths(_row[0])
                with open(_face_path, 'wb') as _f:
                    _f.write(img_bytes)

        mrz_text = request.form.get("mrz_text", "")
        mrz_lines = [l.strip() for l in mrz_text.split("\n") if l.strip()]
        parsed, errors = parse_mrz(mrz_lines)

        if errors:
            flash(f'MRZ validation failed: {"; ".join(errors)}', 'danger')
            return redirect(url_for('edit_passport', passport_id=passport_id))

        if parsed.get("dob"):
            parsed["dob"] = convert_mrz_date(parsed["dob"], is_dob=True)
        if parsed.get("expiry"):
            parsed["expiry"] = convert_mrz_date(parsed["expiry"], is_dob=False)

        update_passport_data(passport_id, user_id, parsed, mrz_text)
        update_general_data(passport_id, user_id, request.form)

        flash('Passport updated successfully!', 'success')
    except Exception as e:
        flash(f'Error updating passport: {str(e)}', 'danger')

    return_url = session.pop('return_url', url_for('results'))
    return redirect(return_url)


@app.route("/edit/<int:passport_id>")
@login_required
def edit_passport(passport_id):
    passport = get_passport_by_id(passport_id, session['user_id'])
    if not passport:
        return "Not found", 404
    referrer = request.referrer
    if referrer and '/results' in referrer:
        session['return_url'] = referrer
    return render_template(
        "edit.html",
        passport=passport,
        nationality_options=NATIONALITY_OPTIONS,
        marital_status_options=MARITAL_STATUS_OPTIONS,
        NATIONALITY_CODE_MAP=NATIONALITY_CODE_MAP,
        group_names=get_distinct_group_names()
    )


# =====================================================
# INVALID PASSPORT REPARSE ROUTES
# =====================================================

@app.route("/invalid_passports")
@login_required
def view_invalid_passports():
    user_id = session['user_id']
    page = request.args.get("page", 1, type=int)
    per_page = 25
    invalid_passports = get_all_invalid_passports(user_id, page, per_page)
    total = get_total_invalid_count(user_id)
    total_pages = (total + per_page - 1) // per_page
    recycled_invalid_count = get_total_recycled_invalid_count(user_id)
    
    # ── Calculate counts for Invalid vs Duplicate (current page) ───────
    counts = {'invalid': 0, 'duplicate': 0}
    _fallback_group = (get_user_settings(user_id) or {}).get('group_name', 'GROUP 1')
    for passport in invalid_passports:
        err_msg = passport.get('error_message') or ''
        is_dup = 'Duplicate passport number' in err_msg

        # Normalize is_emergency to a real bool for the template — legacy
        # rows saved before this column existed will read as None/0.
        passport['is_emergency'] = bool(passport.get('is_emergency'))

        # The group name shown on the card (top-right pill) must always be
        # the group this file was UPLOADED into, never the group parsed out
        # of the error message — for duplicates, that embedded "(Group: ...)"
        # text is the EXISTING colliding group(s), a different thing. Set it
        # explicitly here so the template's fallback (which parses the error
        # string) is never needed for rows that have upload_group_name saved.
        if passport.get('upload_group_name'):
            passport['group_name'] = passport['upload_group_name']
        else:
            # Legacy rows saved before upload_group_name existed have no
            # reliable record of their upload group — fall back to the
            # user's current default rather than letting the template
            # parse the existing/colliding group out of the error text.
            passport['group_name'] = _fallback_group

        if is_dup:
            counts['duplicate'] += 1

            # The group/visa type that were ACTUALLY active at the time
            # THIS record was uploaded (stored on the row itself). Falls
            # back to the user's current settings only for older rows
            # saved before these columns existed.
            _current_group = passport.get('upload_group_name') or _fallback_group
            _uploaded_visa_type = passport.get('upload_visa_type') or 'nusuk'
            _uploaded_is_nusuk = _uploaded_visa_type == 'nusuk'

            # Legacy single-value fields kept for backward compatibility
            # with any code/template still reading them directly — they
            # mirror the FIRST entry of duplicate_matches below.
            passport['duplicate_platform'] = None
            passport['duplicate_existing_group'] = None
            passport['duplicate_passport_id'] = None
            passport['duplicate_number'] = None
            passport['duplicate_expected_departure'] = None
            passport['duplicate_processed_on'] = None
            passport['duplicate_eligible_after'] = None
            passport['duplicate_rule'] = None
            passport['can_force_insert'] = False
            # NEW: full list of colliding-group matches so BOTH messages
            # can be shown when a record hits more than one cross-group
            # rule at once (Nusuk 365-day AND Visit Visa validity).
            passport['duplicate_matches'] = []

            m = re.search(r'Duplicate passport number:\s*([A-Za-z0-9<]+)', err_msg)
            g = re.search(r'\(Group:\s*(.+?)\)', err_msg)
            if m:
                dup_number = m.group(1).strip()
                passport['duplicate_number'] = dup_number
                # The stored error message may list several colliding
                # group names comma-separated (one per rule that fired).
                dup_groups_raw = g.group(1).strip() if g else None
                dup_group_names = (
                    [x.strip() for x in dup_groups_raw.split(',') if x.strip()]
                    if dup_groups_raw else []
                )

                matches = []
                for dup_group in dup_group_names:
                    existing = get_active_passport_in_group(user_id, dup_number, dup_group)
                    if not existing:
                        continue

                    entry = {
                        'platform': existing.get('visa_type', 'nusuk'),
                        'passport_id': existing.get('id'),
                        # Group the duplicate is colliding with. This is
                        # the SAME group for the same_group rule, but for
                        # the cross_group rules (Visit Visa validity /
                        # Nusuk 365-day) it's the OTHER group where the
                        # colliding record actually lives — shown next to
                        # the filename, and linked to jump straight to
                        # that record on the Results page.
                        'existing_group': existing.get('group_name'),
                        'expected_departure': None,
                        'processed_on': None,
                        'eligible_after': None,
                        'created_on': None,
                        'eligible_after_created': None,
                        'can_force_insert': False,
                    }
                    # Which rule fired: if the colliding group is the same
                    # as this user's current upload group, it's the
                    # unconditional same-group rule (never Force Insert).
                    # Otherwise it's a cross-group rule (Visit Visa
                    # validity and/or Nusuk 365-day — both are surfaced
                    # independently, since either or both may match).
                    entry['rule'] = 'same_group' if dup_group == _current_group else 'cross_group'

                    # Visit Visa validity: surface expected_departure
                    # ("valid upto") when the colliding record lives in a
                    # visit_visa group and is still valid. Force Insert
                    # depends on the visa type being UPLOADED, not the
                    # matched record's type:
                    #   - uploaded record is Nusuk    -> Force Insert shown
                    #   - uploaded record is Visit Visa -> never shown
                    if entry['platform'] == 'visit_visa' and existing.get('expected_departure'):
                        _dep = existing.get('expected_departure')
                        try:
                            entry['expected_departure'] = _dep.strftime('%d-%m-%Y')
                        except AttributeError:
                            entry['expected_departure'] = str(_dep)
                        if entry['rule'] == 'cross_group' and _uploaded_is_nusuk:
                            entry['can_force_insert'] = True

                    # Nusuk 365-day: surface the validity window that
                    # applies — processed_at+365 if the existing record
                    # was processed, otherwise created_at+365 as a
                    # fallback. Force Insert is never offered for a
                    # Nusuk match.
                    if entry['platform'] == 'nusuk' and entry['rule'] == 'cross_group':
                        if existing.get('is_processed') and existing.get('processed_at'):
                            _proc = existing.get('processed_at')
                            try:
                                entry['processed_on'] = _proc.strftime('%d-%m-%Y')
                                entry['eligible_after'] = (_proc + timedelta(days=365)).strftime('%d-%m-%Y')
                            except AttributeError:
                                entry['processed_on'] = str(_proc)
                        elif existing.get('created_at'):
                            _created = existing.get('created_at')
                            try:
                                entry['created_on'] = _created.strftime('%d-%m-%Y')
                                entry['eligible_after_created'] = (_created + timedelta(days=365)).strftime('%d-%m-%Y')
                            except AttributeError:
                                entry['created_on'] = str(_created)

                    matches.append(entry)

                passport['duplicate_matches'] = matches

                if matches:
                    first = matches[0]
                    passport['duplicate_platform'] = first['platform']
                    passport['duplicate_passport_id'] = first['passport_id']
                    passport['duplicate_existing_group'] = first['existing_group']
                    passport['duplicate_rule'] = first['rule']
                    passport['duplicate_expected_departure'] = first['expected_departure']
                    passport['duplicate_processed_on'] = first['processed_on']
                    passport['duplicate_eligible_after'] = first['eligible_after']
                    # Force Insert should be offered if ANY matched entry
                    # allows it (same_group and Nusuk cross-group entries
                    # never set this True; a Visit-Visa match does, when
                    # the uploaded record is Nusuk).
                    passport['can_force_insert'] = any(mm['can_force_insert'] for mm in matches)
        else:
            counts['invalid'] += 1
    # ────────────────────────────────────────────────────────────────────
    
    return render_template(
        "invalid_passports.html",
        invalid_passports=invalid_passports,
        page=page, total_pages=total_pages, total=total,
        recycled_invalid_count=recycled_invalid_count,
        counts=counts  # ── PASS THIS TO TEMPLATE ──
    )

@app.route("/skip_closed")
def skip_closed():
    return '''
    <!DOCTYPE html><html><head><title>Skipped</title>
    <script>
        if (window.opener && !window.opener.closed && !window.opener._reparseActive) { window.opener.location.reload(); }
        setTimeout(() => { window.close(); }, 100);
    </script></head>
    <body style="font-family:Arial,sans-serif;text-align:center;padding:40px;background:#f8fdf8">
        <div style="background:#d4edda;border:1px solid #c3e6cb;border-radius:8px;padding:25px;max-width:400px;margin:0 auto">
            <h2 style="color:#155724;margin-top:0">✅ Skipped Successfully</h2>
            <p style="color:#155724">Passport removed from error list</p>
        </div>
    </body></html>
    ''', 200


@app.route('/delete_passport/<int:passport_id>', methods=['POST'])
@login_required
def delete_passport(passport_id):
    try:
        passport = get_passport_by_id(passport_id, session['user_id'])
        if not passport:
            flash(f'Passport #{passport_id} not found!', 'warning')
            return redirect(url_for('results'))
        delete_passport_record(passport_id, session['user_id'])
        flash(f'✅ Passport #{passport_id} moved to the Recycle Bin!', 'success')
    except mysql.connector.Error as db_err:
        error_msg = str(db_err)
        if "foreign key constraint" in error_msg.lower():
            flash('❌ Cannot delete: Related records exist', 'danger')
        else:
            flash(f'❌ Database error: {error_msg[:100]}', 'danger')
    except Exception as e:
        flash(f'❌ Unexpected error: {str(e)[:150]}', 'danger')

    passport_filter = request.args.get('passport_number')
    group_filter = request.args.get('group_name')
    page = request.args.get('page', 1)

    if passport_filter or group_filter:
        return redirect(url_for('results', passport_number=passport_filter, group_name=group_filter))
    return redirect(url_for('results', page=page))


@app.route("/delete_invalid/<int:invalid_id>", methods=["POST"])
@login_required
def delete_invalid(invalid_id):
    redirect_after = request.form.get('redirect_after')
    try:
        delete_invalid_passport(invalid_id, session['user_id'])
        flash(f'✅ Record #{invalid_id} moved to the Recycle Bin!', 'success')
        remaining = get_total_invalid_count(session['user_id'])
        if remaining == 0:
            flash('All invalid records cleared! ✅', 'success')
            return redirect(url_for('results'))
    except Exception as e:
        flash(f'❌ Error moving record: {str(e)}', 'danger')
    if redirect_after == 'results':
        return redirect(url_for('results'))
    return redirect(url_for('view_invalid_passports'))




# =====================================================
# RECYCLE BIN ROUTES (INVALID)
# =====================================================

@app.route("/invalid_recycle_bin")
@login_required
def invalid_recycle_bin():
    user_id = session['user_id']
    page = request.args.get("page", 1, type=int)
    per_page = 25
    recycled_passports = get_all_recycled_invalid_passports(user_id, page, per_page)
    total = get_total_recycled_invalid_count(user_id)
    total_pages = (total + per_page - 1) // per_page
    return render_template(
        "invalid_recycle_bin.html",
        invalid_passports=recycled_passports,
        page=page, total_pages=total_pages, total=total
    )


def _duplicate_restore_message(c):
    """
    Builds a rejection reason for a blocked restore, tailored to which
    duplication rule fired (mirrors the rules in
    is_passport_number_exists_in_group()).
    """
    rule = c.get('rule')
    base = (
        f'Cannot restore: passport number {c["passport_number"]} is already '
        f'active in group "{c["group_name"]}" (record #{c["existing_passport_id"]}).'
    )
    if rule == 'same_group':
        return base + ' Reason: an active record with the same passport number already exists in this group.'
    if rule == 'cross_group_visit_visa_valid':
        return base + ' Reason: the existing record is an active Visit Visa passport whose validity has not expired yet.'
    if rule == 'cross_group_1year':
        return base + ' Reason: the existing Nusuk record is still within its 365-day re-eligibility window.'
    return base


@app.route("/restore_invalid/<int:invalid_id>", methods=["POST"])
@login_required
def restore_invalid(invalid_id):
    user_id = session['user_id']
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    force = request.form.get('confirm_duplicate') == '1'
    try:
        restore_invalid_passport(invalid_id, user_id, force=force)
        if is_ajax:
            return jsonify({"success": True, "message": "Record restored successfully!"})
        flash(f'✅ Record restored successfully!', 'success')
    except DuplicateRestoreConflict as e:
        c = e.conflict
        message = _duplicate_restore_message(c)
        if is_ajax:
            return jsonify({
                "success": False,
                "duplicate_conflict": True,
                "conflict": c,
                "message": message
            }), 409
        flash(f'❌ {message}', 'danger')
    except Exception as e:
        if is_ajax:
            return jsonify({"success": False, "message": f"Error restoring record: {str(e)}"}), 500
        flash(f'❌ Error restoring record: {str(e)}', 'danger')
    return redirect(url_for('recycle_bin', tab='invalid'))


@app.route("/hard_delete_invalid/<int:invalid_id>", methods=["POST"])
@login_required
def hard_delete_invalid(invalid_id):
    try:
        hard_delete_invalid_passport(invalid_id, session['user_id'])
        flash(f'✅ Record permanently deleted!', 'success')
    except Exception as e:
        flash(f'❌ Error deleting record: {str(e)}', 'danger')
    return redirect(url_for('recycle_bin', tab='invalid'))


@app.route("/empty_invalid_recycle_bin", methods=["POST"])
@login_required
def empty_invalid_recycle_bin():
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("USE passport_db")
        cursor.execute("DELETE FROM invalid_passports WHERE is_recycled = TRUE AND user_id = %s", (session['user_id'],))
        conn.commit()
        cursor.close()
        conn.close()
        flash('✅ Recycle bin permanently emptied!', 'success')
    except Exception as e:
        flash(f'❌ Error emptying recycle bin: {str(e)}', 'danger')
    return redirect(url_for('recycle_bin', tab='invalid'))


# =====================================================
# PDF EXTRACTOR
# =====================================================

@app.route('/pdf_extractor')
@login_required
def pdf_extractor():
    return render_template('pdf_extractor.html')


@app.route('/pdf_upload', methods=['POST'])
@login_required
def pdf_upload():
    if 'pdf_file' not in request.files:
        flash('No file selected', 'error')
        return redirect(url_for('pdf_extractor'))
    file = request.files['pdf_file']
    page_range = request.form.get('page_range', '').strip()
    if file.filename == '':
        flash('No file selected', 'error')
        return redirect(url_for('pdf_extractor'))
    if not file.filename.lower().endswith('.pdf'):
        flash('Invalid file type. Please upload a PDF.', 'error')
        return redirect(url_for('pdf_extractor'))
    if not page_range:
        flash('Please specify a page range (e.g., 1 or 1-3,5).', 'error')
        return redirect(url_for('pdf_extractor'))

    success, result = process_pdf_upload(file, page_range, app.config["UPLOAD_FOLDER"])
    if success:
        return send_file(
            result, mimetype='application/zip', as_attachment=True,
            download_name=f"{secure_filename(file.filename).rsplit('.', 1)[0]}_cropped_passports.zip"
        )
    else:
        flash(result, 'error')
        return redirect(url_for('pdf_extractor'))


# =====================================================
# AUTOMATION API
# =====================================================

@app.route("/api/download_automation_file", methods=["POST"])
@login_required
def download_automation_file():
    """
    Body:  { selected_ids: [...], credentials: {username, password} }
    Returns: downloadable  automation_queue.json  with full passport data.
    Order: 18+ applicants first, then under 18.
    """
    data         = request.json or {}
    selected_ids = data.get("selected_ids", [])
    credentials  = data.get("credentials", {})
    user_id      = session["user_id"]
    if not selected_ids:
        return jsonify({"status": "error", "message": "No IDs provided"}), 400

    # ── Nusuk group-size cap ────────────────────────────────────────────────
    # Nusuk itself rejects/silently truncates groups larger than 50 mutamers.
    # The UI already blocks this in results.html (the Send button is disabled
    # and a "Create Group for Remaining" flow is shown instead once selection
    # exceeds 50), but that's client-side only — re-enforce it here too, since
    # this endpoint can be called directly and nothing else in the stack
    # checks group size.
    NUSUK_GROUP_LIMIT = 50
    if len(selected_ids) > NUSUK_GROUP_LIMIT:
        return jsonify({
            "status": "error",
            "message": f"Only {NUSUK_GROUP_LIMIT} passports are allowed per Nusuk group. "
                       f"{len(selected_ids) - NUSUK_GROUP_LIMIT} passport(s) must be moved to a new "
                       f"group before sending.",
            "group_limit": NUSUK_GROUP_LIMIT,
            "overflow_count": len(selected_ids) - NUSUK_GROUP_LIMIT
        }), 400
    
    applicants = []
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    
    # Calculate today's date once for age calculation
    today = ist_now().date()
    
    try:
        cursor.execute("USE passport_db")
        # ── 1. Fetch full passport data ───────────────────────────────────────
        for pid in selected_ids:
            try:
                pid_int = int(pid)
            except (ValueError, TypeError):
                continue
            cursor.execute("""
            SELECT p.id, p.filename, p.surname, p.given_names, p.middle_name,
            p.sex, p.dob, p.passport_number, p.expiry,
            g.nationality_id, g.passport_issue_place, g.passport_issue_date,
            g.city_of_birth, g.profession,
            g.group_name, g.hotel_name, g.passport_type,
            g.expected_arrival, g.expected_departure,
            g.contact_number, g.email,
            g.marital_status, g.city, g.zip_postal_code, g.address
            FROM passports p
            LEFT JOIN general_data g ON g.passport_id = p.id
            WHERE p.id = %s AND p.user_id = %s
            """, (pid_int, user_id))
            row = cursor.fetchone()
            if not row:
                continue

            # Read images from filesystem
            face_b64 = ""
            if row.get("filename"):
                _, face_path = resolve_passport_paths(row['filename'], row.get('group_name'))
                if face_path and os.path.exists(face_path):
                    try:
                        with open(face_path, 'rb') as _f:
                            face_b64 = base64.b64encode(_f.read()).decode("utf-8")
                    except Exception:
                        face_b64 = ""

            passport_image_b64 = ""
            if row.get("filename"):
                orig_path, _ = resolve_passport_paths(row['filename'], row.get('group_name'))
                if orig_path and os.path.exists(orig_path):
                    try:
                        with open(orig_path, 'rb') as _f:
                            passport_image_b64 = base64.b64encode(_f.read()).decode("utf-8")
                    except Exception:
                        passport_image_b64 = ""
            
            def ds(val):
                return str(val) if val else ""
            
            # ── AGE CALCULATION FOR SORTING ─────────────────────────────────
            dob = row.get("dob")
            is_adult = False
            if dob:
                try:
                    # Calculate age: year diff - 1 if birthday hasn't occurred yet this year
                    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
                    is_adult = age >= 18
                except Exception:
                    is_adult = False
            
            applicants.append({
                "id":                   pid_int,
                "group_name":           row.get("group_name") or "",
                "face_image":           face_b64,
                "passport_image":       passport_image_b64,
                "filename":             f"photo_{pid_int}.jpg",
                "nationality_id":       row.get("nationality_id") or "",
                "surname":              row.get("surname") or "",
                "given_names":          row.get("given_names") or "",
                "middle_name":          row.get("middle_name") or "",
                "sex":                  row.get("sex") or "",
                "marital_status":       row.get("marital_status") or "",
                "dob":                  ds(row.get("dob")),
                "city_of_birth":        row.get("city_of_birth") or "",
                "profession":           row.get("profession") or "",
                "city":                 row.get("city") or "",
                "zip_postal_code":      row.get("zip_postal_code") or "",
                "address":              row.get("address") or "",
                "passport_type":        row.get("passport_type") or 1,
                "passport_number":      row.get("passport_number") or "",
                "passport_issue_place": row.get("passport_issue_place") or "",
                "passport_issue_date":  ds(row.get("passport_issue_date")),
                "expiry":               ds(row.get("expiry")),
                "expected_arrival":     ds(row.get("expected_arrival")),
                "expected_departure":   ds(row.get("expected_departure")),
                "hotel_name":           row.get("hotel_name") or "",
                "contact_number":       row.get("contact_number") or "",
                "email":                row.get("email") or "",
                # Temporary key for sorting (will be removed before JSON export)
                "__is_adult":           is_adult,
                "_issue_date_raw":      row.get("passport_issue_date"),
                "_dob_raw":             row.get("dob"),
            })
        
        if not applicants:
            return jsonify({"status": "error", "message": "No valid records found"}), 404

        # ── Reject records that only have ONE of given_names / surname ────────
        # (i.e. exactly one is blank). Same rule as the Visit Visa send path.
        incomplete = [
            a for a in applicants
            if bool((a.get("surname") or "").strip()) != bool((a.get("given_names") or "").strip())
        ]
        if incomplete:
            incomplete_records = [
                {
                    "id":               a["id"],
                    "passport_number":  a.get("passport_number") or "—",
                    "name":             (a.get("surname") or a.get("given_names") or "").strip(),
                }
                for a in incomplete
            ]
            return jsonify({
                "status": "error",
                "message": (
                    f"{len(incomplete_records)} selected record(s) have only a given name "
                    "or only a surname (not both) and cannot be sent."
                ),
                "incomplete_records": incomplete_records,
            }), 400

        # ── Reject records missing an issue date, or with an issue date that
        # is in the future or before the applicant's date of birth ──────────
        def _bad_issue_date(a):
            issue = a.get("_issue_date_raw")
            if not issue:
                return "missing"
            if issue > today:
                return "future"
            dob_val = a.get("_dob_raw")
            if dob_val and issue < dob_val:
                return "before_dob"
            return None

        issue_date_problems = [(a, _bad_issue_date(a)) for a in applicants]
        issue_date_problems = [(a, r) for a, r in issue_date_problems if r]

        if issue_date_problems:
            reason_messages = {
                "missing":    "missing an issue date",
                "future":     "an issue date after today",
                "before_dob": "an issue date before the applicant's date of birth",
            }
            incomplete_records = [
                {
                    "id":               a["id"],
                    "passport_number":  a.get("passport_number") or "—",
                    "name":             (f"{a.get('surname','')} {a.get('given_names','')}").strip() or "Unnamed",
                    "reason":           reason_messages[reason],
                }
                for a, reason in issue_date_problems
            ]
            counts = {}
            for _, reason in issue_date_problems:
                counts[reason] = counts.get(reason, 0) + 1
            summary_parts = [f"{n} {reason_messages[r]}" for r, n in counts.items()]
            return jsonify({
                "status": "error",
                "message": (
                    f"{len(incomplete_records)} selected record(s) cannot be sent: "
                    + "; ".join(summary_parts) + "."
                ),
                "incomplete_records": incomplete_records,
            }), 400

        for a in applicants:
            a.pop("_issue_date_raw", None)
            a.pop("_dob_raw", None)

        # ── SORTING: 18+ First, Then Under 18 ───────────────────────────────
        # True (18+) sorts before False (Under 18) when reverse=True
        # ── SORTING: 18+ First, Then Under 18 ──
        applicants.sort(key=lambda x: x.get('__is_adult', False), reverse=True)
        for a in applicants:
            a.pop('__is_adult', None)

    finally:
        cursor.close()
        conn.close()
    
    # ── 3b. Record group batch — save email + mark as Sent ────────────────────
    try:
        group_name_for_batch = applicants[0].get("group_name", "").strip() if applicants else ""
        login_email = (credentials.get("username") or "").strip()
        if group_name_for_batch:
            upsert_group_batch(user_id, group_name_for_batch, login_email)
    except Exception as e:
        print(f"[download_automation_file] Could not save group batch: {e}")
    
    # 🚀 NEW: Persist the full batch to the NUSUK queue table
    import uuid
    batch_id = str(uuid.uuid4())
    try:
        create_nusuk_queue(user_id, batch_id, applicants, credentials)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Could not persist send queue: {e}"}), 500

    # ── 4. Stream ONLY the first applicant to the browser ───────────────────────
    raw_group_name = (applicants[0].get("group_name") or "automation_queue").strip()
    safe_group_name = re.sub(r'[^\w\-. ]', '_', raw_group_name).strip().replace(' ', '_')
    json_filename = f"{safe_group_name}.json" if safe_group_name else "automation_queue.json"

    queue_data = {
        "created_at":  ist_now().isoformat(timespec="seconds"),
        "total":       len(applicants),
        "credentials": credentials,
        "applicants":  [applicants[0]] if applicants else [], # 👈 ONLY SEND FIRST!
        "local_api_token": LOCAL_API_TOKEN,
        "local_api_host": request.host,
        "batch_id": batch_id
    }
    
    payload  = json.dumps(queue_data, ensure_ascii=False, indent=2)
    response = make_response(payload)
    response.headers["Content-Type"]        = "application/json"
    response.headers["Content-Disposition"] = f'attachment; filename="{json_filename}"'
    response.headers["X-Filename"]          = json_filename
    return response
 
@app.route("/groups")
@login_required
def view_all_groups():
    user_id = session['user_id']
    groups = get_all_group_batches(user_id)
    # Show most recently created group at the top
    groups = sorted(groups, key=lambda g: g.get('created_at') or '', reverse=True)
    latest_group = groups[0]['group_name'] if groups else ''

    # ── Overall "Sent" summary card (across all groups) ─────────────────
    # sent_totals.total mirrors sent_count's definition per group (a record
    # counts as sent once its relevant flag — is_processed for Nusuk,
    # is_visa_processed for Visit Visa — is TRUE), just aggregated here
    # instead of per group. nusuk/visit_visa split uses the same
    # nusuk_processed_count / visa_processed_count columns already
    # computed per group in get_all_group_batches.
    sent_totals = {
        'nusuk': sum(g.get('nusuk_processed_count') or 0 for g in groups),
        'visit_visa': sum(g.get('visa_processed_count') or 0 for g in groups),
    }
    sent_totals['total'] = sent_totals['nusuk'] + sent_totals['visit_visa']

    return render_template(
        "groups.html",
        groups=groups,
        latest_group=latest_group,
        nationality_options=NATIONALITY_OPTIONS,
        # Used to prefill the "Visit Visa General Data" fields shown in the
        # Change Group modal when transferring/switching records to Visit Visa.
        defaults=get_user_settings(user_id),
        marital_status_options=MARITAL_STATUS_OPTIONS,
        sent_totals=sent_totals,
    )


@app.route("/groups/delete", methods=["POST"])
@login_required
def delete_group():
    user_id = session['user_id']
    group_name = request.form.get("group_name", "").strip()
    if not group_name:
        flash("Invalid group name.", "danger")
        return redirect(url_for("view_all_groups"))
    try:
        delete_group_and_records(user_id, group_name)
        flash(f"Group \"{group_name}\" and all its records have been deleted.", "success")
    except Exception as e:
        flash(f"Error deleting group: {str(e)}", "danger")
    return redirect(url_for("view_all_groups"))


@app.route("/groups/edit", methods=["POST"])
@login_required
def edit_group():
    user_id = session['user_id']
    old_name = request.form.get("old_group_name", "").strip()
    new_name = request.form.get("new_group_name", "").strip()
    new_date = request.form.get("new_date", "").strip()
    if not old_name or not new_name:
        flash("Group name cannot be empty.", "danger")
        return redirect(url_for("view_all_groups"))
    try:
        rename_group(user_id, old_name, new_name, new_date or None)
        flash(f"Group updated successfully.", "success")
    except Exception as e:
        flash(f"Error updating group: {str(e)}", "danger")
    return redirect(url_for("view_all_groups"))


@app.route("/passports/move_group", methods=["POST"])
@login_required
def move_passports_group():
    """Move one or more passports to a different group (existing or brand
    new — the target folder is created automatically). Physically moves
    each passport's original + face image into the target group's folder.
    Body (form or JSON): selected_ids (list or JSON string), target_group_name."""
    user_id = session['user_id']

    if request.is_json:
        body = request.json or {}
        target_group_name = (body.get("target_group_name") or "").strip()
        raw_ids = body.get("selected_ids")
    else:
        target_group_name = (request.form.get("target_group_name") or "").strip()
        raw_ids = request.form.get("selected_ids")

    try:
        selected_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else (raw_ids or [])
    except Exception:
        selected_ids = []

    if not target_group_name:
        msg = "Target group name cannot be empty."
        if request.is_json:
            return jsonify({"success": False, "message": msg}), 400
        flash(msg, "danger")
        return redirect(url_for("view_all_groups"))

    if not selected_ids:
        msg = "No passports selected to move."
        if request.is_json:
            return jsonify({"success": False, "message": msg}), 400
        flash(msg, "danger")
        return redirect(url_for("view_all_groups"))

    try:
        passport_ids = [int(pid) for pid in selected_ids]
    except Exception:
        msg = "Invalid passport id(s)."
        if request.is_json:
            return jsonify({"success": False, "message": msg}), 400
        flash(msg, "danger")
        return redirect(url_for("view_all_groups"))

    # ── Same strict per-group duplicate check as /change_group ──────────
    # This route is a second, independent path to move passports between
    # groups (used from the Groups page instead of the Results page).
    # Always re-checked live — there is no "force through" override. The
    # only way past a conflict is to delete the colliding record first
    # (via /change_group/resolve_duplicate), so the next submission's
    # live check finds nothing.
    duplicates = find_duplicate_groups_for_passports(passport_ids, user_id, target_group_name)
    if duplicates:
        message = "Some selected passports already exist in the target group."
        if request.is_json:
            return jsonify({
                "success": False,
                "duplicate_conflict": True,
                "duplicates": duplicates,
                "message": message
            }), 409
        dup_list = ', '.join(f"{d['passport_number']} (in \"{d['group_name']}\")" for d in duplicates)
        flash(f'Cannot move: duplicate passport number(s) found — {dup_list}.', 'danger')
        return redirect(url_for("view_all_groups"))

    try:
        move_passports_to_group(passport_ids, user_id, target_group_name)
        msg = f"Moved {len(passport_ids)} passport(s) to group \"{target_group_name}\"."
        if request.is_json:
            return jsonify({"success": True, "message": msg})
        flash(msg, "success")
    except Exception as e:
        msg = f"Error moving passports: {str(e)}"
        if request.is_json:
            return jsonify({"success": False, "message": msg}), 500
        flash(msg, "danger")

    return redirect(url_for("view_all_groups"))


@app.route("/export_evisa_json", methods=["POST"])
@login_required
def export_evisa_json():
    user_id = session['user_id']
    data = request.get_json(silent=True) or {}
    group_names = data.get('group_names', [])
    if not group_names or not isinstance(group_names, list):
        return jsonify({"error": "No groups specified"}), 400

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("USE passport_db")
        placeholders = ','.join(['%s'] * len(group_names))
        cursor.execute(f"""
            SELECT p.passport_number,
                   g.nationality_id,
                   p.given_names,
                   g.group_name
            FROM passports p
            JOIN general_data g ON p.id = g.passport_id
            WHERE p.user_id = %s
              AND (p.is_recycled = FALSE OR p.is_recycled IS NULL)
              AND g.group_name IN ({placeholders})
            ORDER BY g.group_name, p.given_names
        """, [user_id] + group_names)
        rows = cursor.fetchall()

        applicants = []
        for row in rows:
            applicants.append({
                "passport_number": row.get("passport_number") or "",
                "nationality_id":  row.get("nationality_id") or "",
                "given_names":     row.get("given_names") or "",
            })

        result = {"applicants": applicants, "local_api_token": LOCAL_API_TOKEN}

        response = make_response(json.dumps(result, indent=2, ensure_ascii=False))
        response.headers['Content-Type'] = 'application/json'
        response.headers['Content-Disposition'] = 'attachment; filename=evisa_export.json'
        return response
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@app.route("/backup_database")
@login_required
def backup_database():
    import shutil as _shutil
    user_id = session['user_id']
    username = session.get('username')
    is_admin = bool(session.get('is_admin'))
    timestamp = ist_now().strftime("%Y%m%d_%H%M%S")
    prefix = "full_system_backup" if is_admin else f"workspace_backup_{username}"
    sql_filename = f"{prefix}_{timestamp}.sql"
    zip_filename = f"{prefix}_{timestamp}.zip"

    sql_path     = os.path.join(BACKUP_TEMP_DIR, sql_filename)
    zip_path     = os.path.join(BACKUP_TEMP_DIR, zip_filename)
    folders_root = os.path.join(BACKUP_TEMP_DIR, f"folders_manual_{timestamp}")

    os.makedirs(BACKUP_TEMP_DIR, exist_ok=True)

    MYSQLDUMP_PATH = _find_mysqldump()

    if not os.path.exists(MYSQLDUMP_PATH):
        flash(f"Backup Error: Could not find mysqldump at '{MYSQLDUMP_PATH}'.", "danger")
        return redirect(url_for("index"))

    try:
        db_name = DB_CONFIG.get('database', 'passport_db')
        base_cmd = [
        MYSQLDUMP_PATH,
        f"--host={DB_CONFIG.get('host', 'localhost')}",
        f"--port={DB_CONFIG.get('port', 3307)}",
        f"--user={DB_CONFIG.get('user', 'passport_user')}",
        f"--password={DB_CONFIG.get('password', 'test123')}",
        "--skip-add-drop-table",
        "--no-tablespaces",
        "--lock-tables=false",        # ← replaces --single-transaction + --skip-lock-tables
        "--column-statistics=0",      # ← ADD THIS
        "--hex-blob",
        "--default-character-set=utf8mb4"
    ]
        with open(sql_path, 'wb') as f:
            if is_admin:
                cmd = base_cmd + [db_name]
                subprocess.run(cmd, stdout=f, check=True, **_MYSQLDUMP_POPEN_KWARGS)
            else:
                # NOTE: 'users' table removed locally — user accounts now live
                # on the host. Only export this user's own passport-related data.
                tables_conditions = {
                    "user_settings": f"user_id={user_id}",
                    "passports": f"user_id={user_id}",
                    "general_data": f"passport_id IN (SELECT id FROM passports WHERE user_id={user_id})",
                    "invalid_passports": f"user_id={user_id}",
                    "automation_queue": f"user_id={user_id}",
                }
                f.write(f"-- Workspace Backup for User: {username} (ID: {user_id})\n".encode())
                f.write(f"-- Generated: {ist_now()}\n\n".encode())
                for table, condition in tables_conditions.items():
                    cmd = base_cmd + [db_name, table, f"--where={condition}"]
                    subprocess.run(cmd, stdout=f, check=True, **_MYSQLDUMP_POPEN_KWARGS)

        # ── Copy passport + face images into folders/<GROUP_NAME>/{passport,face}/ ──
        # (same layout as the scheduled backup)
        if os.path.exists(folders_root):
            _shutil.rmtree(folders_root, ignore_errors=True)
        os.makedirs(folders_root, exist_ok=True)

        try:
            filename_group_map = get_filename_group_map()
        except Exception as e:
            print(f"[Manual Backup] Could not load group mapping from DB: {e}")
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

        for src, fname in _iter_files(UPLOAD_FOLDER):
            if fname.lower().endswith("_ocr.jpg"):
                continue
            group_name, visa_type, passport_number = filename_group_map.get(fname, ("GROUP 1", "nusuk", ""))
            dest_dir = _group_dir(group_name, visa_type, "passport")
            try:
                dest_name = _backup_name(fname, passport_number, dest_dir)
                _shutil.copy2(src, os.path.join(dest_dir, dest_name))
                images_copied += 1
            except Exception as e:
                print(f"[Manual Backup] Could not copy passport image {fname}: {e}")

        for src, fname in _iter_files(FACE_FOLDER):
            orig_fname = fname[len("face_"):] if fname.startswith("face_") else fname
            group_name, visa_type, passport_number = filename_group_map.get(orig_fname, ("GROUP 1", "nusuk", ""))
            dest_dir = _group_dir(group_name, visa_type, "faces")
            try:
                dest_name = _backup_name(orig_fname, passport_number, dest_dir)
                _shutil.copy2(src, os.path.join(dest_dir, dest_name))
                images_copied += 1
            except Exception as e:
                print(f"[Manual Backup] Could not copy face image {fname}: {e}")

        print(f"[Manual Backup] Copied {images_copied} image(s) into group folders for backup.")

        # ── Zip db.sql + folders/ ──────────────────────────────────
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.write(sql_path, arcname=sql_filename)
            for root, _dirs, files_ in os.walk(folders_root):
                for f_ in files_:
                    full = os.path.join(root, f_)
                    arc = os.path.join("folders", os.path.relpath(full, folders_root))
                    zf.write(full, arcname=arc)

        with open(zip_path, 'rb') as f:
            zip_data = f.read()
        memory_file = io.BytesIO(zip_data)
        memory_file.seek(0)

    except Exception as e:
        flash(f"Database backup failed: {str(e)}", "danger")
        return redirect(url_for("index"))
    finally:
        if os.path.exists(sql_path): os.remove(sql_path)
        if os.path.exists(zip_path): os.remove(zip_path)
        if os.path.exists(folders_root): _shutil.rmtree(folders_root, ignore_errors=True)

    return send_file(
        memory_file, mimetype='application/zip', as_attachment=True,
        download_name=zip_filename
    )


@app.route("/api/ocr_stats")
@login_required
def get_ocr_api_usage():
    """
    Returns session-level stats for ProvA (Phase 2 batch + Phase 4 individual)
    and ProvB Vision (Phase 3 batch).
    """
    try:
        stats = session.get("latest_api_stats")
        if stats:
            return jsonify({
                "status": "success",
                "provA_batch_units": stats.get("provA_batch_units", 0),
                "provA_individual_units": stats.get("provA_individual_units", 0),
                "total_provA_units": stats.get("total_provA_units", 0),
                "provB_calls": stats.get("provB_calls", 0),
                "note": "Session-level counts. For full ProvA billing, check ProvA Portal."
            })
        return jsonify({
            "status": "success",
            "provA_batch_units": 0,
            "provA_individual_units": 0,
            "total_provA_units": 0,
            "provB_calls": 0,
            "note": "No upload session data available."
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/generate_badges", methods=["POST"])
@login_required
def generate_badges():
    try:
        selected_ids_json = request.form.get('selected_ids')
        if not selected_ids_json:
            flash("No records selected.", "danger")
            return redirect(url_for('results'))
        selected_ids = json.loads(selected_ids_json)
        if not selected_ids:
            flash("No records selected.", "danger")
            return redirect(url_for('results'))

        badge_images = []
        current_username = session.get('username', 'default')
        current_company_name = session.get('full_name', current_username)
        for p_id in selected_ids:
            passport_data = get_passport_by_id(p_id, session['user_id'])
            if passport_data:
                badge_bytes = generate_badge_image(passport_data, current_username, current_company_name)
                if badge_bytes:
                    badge_images.append(Image.open(io.BytesIO(badge_bytes)))

        
        badge_images = []
        current_username = session.get('username', 'default')
        current_company_name = session.get('full_name', current_username)
        group_name = ""                          # ← add here

        for p_id in selected_ids:
            passport_data = get_passport_by_id(p_id, session['user_id'])
            if passport_data:
                if not group_name:               # ← add here
                    group_name = passport_data.get('group_name', '') or ''
                badge_bytes = generate_badge_image(passport_data, current_username, current_company_name)
                if badge_bytes:
                    badge_images.append(Image.open(io.BytesIO(badge_bytes)))
                
        
        if not badge_images:
            flash("Failed to generate any badges.", "danger")
            return redirect(url_for('results'))

        A4_W, A4_H = 3508, 2480

        badge_width = A4_W // 4
        badge_height = A4_H
        pages = []
        for i in range(0, len(badge_images), 4):
            chunk = badge_images[i:i + 4]
            page = Image.new('RGB', (A4_W, A4_H), (255, 255, 255))
            for j, badge in enumerate(chunk):
                page.paste(badge.resize((badge_width, badge_height), _RESAMPLE), (j * badge_width, 0))
            pages.append(page)

        pdf_bytes = io.BytesIO()
        pages[0].save(
            pdf_bytes, format='PDF', resolution=300.0,
            save_all=True, append_images=pages[1:] if len(pages) > 1 else []
        )
        pdf_bytes.seek(0)
        return send_file(
            pdf_bytes, mimetype='application/pdf', as_attachment=True,
            download_name=f'Badges_{(group_name or "Group").replace(" ", "_").replace("/", "-")}.pdf'
        )
    except Exception as e:
        print(f"Generate Badges Error: {e}")
        flash(f"Error generating PDF: {str(e)}", "danger")
        return redirect(url_for('results'))


@app.route("/preview_badge/<int:passport_id>")
@login_required
def preview_badge(passport_id):
    try:
        passport_data = get_passport_by_id(passport_id, session['user_id'])
        if not passport_data:
            return "Passport not found", 404
        _preview_username = session.get('username', 'default')
        _preview_company_name = session.get('full_name', _preview_username)
        badge_bytes = generate_badge_image(passport_data, _preview_username, _preview_company_name)
        if not badge_bytes:
            return "Error generating badge image", 500
        return send_file(io.BytesIO(badge_bytes), mimetype='image/jpeg', as_attachment=False)
    except Exception as e:
        return str(e), 500


# =====================================================
# ACTIVE PASSPORTS RECYCLE BIN
# =====================================================

@app.route("/recycle_bin")
@login_required
def recycle_bin():
    user_id = session['user_id']

    # Processed passports tab
    page = request.args.get("page", 1, type=int)
    per_page = 25
    data = get_recycled_passports(user_id, page, per_page)
    total_processed = get_total_recycled_passports_count(user_id)
    total_pages = (total_processed + per_page - 1) // per_page

    # Invalid passports tab
        # Invalid passports tab
    invalid_page = request.args.get("invalid_page", 1, type=int)
    invalid_passports = get_all_recycled_invalid_passports(user_id, invalid_page, per_page)
    recycled_invalid_total = get_total_recycled_invalid_count(user_id)
    invalid_total_pages = (recycled_invalid_total + per_page - 1) // per_page

    return render_template(
        "recycle_bin.html",
        # processed
        data=data, page=page, total_pages=total_pages,
        total_processed=total_processed,
        # invalid
        invalid_passports=invalid_passports,
        invalid_page=invalid_page, invalid_total_pages=invalid_total_pages,
        recycled_invalid_total=recycled_invalid_total,
        nationality_options=NATIONALITY_OPTIONS,
    )


@app.route('/restore_passport/<int:passport_id>', methods=['POST'])
@login_required
def restore_passport_route(passport_id):
    user_id = session['user_id']
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    force = request.form.get('confirm_duplicate') == '1'
    try:
        restore_passport(passport_id, user_id, force=force)
        if is_ajax:
            return jsonify({"success": True, "message": f"Passport #{passport_id} restored successfully!"})
        flash(f'✅ Passport #{passport_id} restored successfully!', 'success')
    except DuplicateRestoreConflict as e:
        c = e.conflict
        message = _duplicate_restore_message(c)
        if is_ajax:
            return jsonify({
                "success": False,
                "duplicate_conflict": True,
                "conflict": c,
                "message": message
            }), 409
        flash(f'❌ {message}', 'danger')
    except Exception as e:
        if is_ajax:
            return jsonify({"success": False, "message": f"Error restoring passport: {str(e)}"}), 500
        flash(f'❌ Error restoring passport: {str(e)}', 'danger')
    return redirect(url_for('recycle_bin'))


@app.route('/hard_delete_passport/<int:passport_id>', methods=['POST'])
@login_required
def hard_delete_passport_route(passport_id):
    try:
        hard_delete_passport(passport_id, session['user_id'])
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"success": True})
        flash(f'✅ Passport #{passport_id} permanently deleted!', 'success')
    except Exception as e:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"success": False, "error": str(e)}), 500
        flash(f'❌ Error deleting passport: {str(e)}', 'danger')
    return redirect(url_for('recycle_bin'))


@app.route('/empty_recycle_bin', methods=['POST'])
@login_required
def empty_recycle_bin_route():
    try:
        empty_recycled_passports(session['user_id'])
        flash('✅ Processed recycle bin emptied successfully!', 'success')
    except Exception as e:
        flash(f'❌ Error emptying recycle bin: {str(e)}', 'danger')
    return redirect(url_for('recycle_bin'))


@app.route('/empty_all_bins', methods=['POST'])
@login_required
def empty_all_bins():
    """Permanently delete all records from both processed and invalid recycle bins."""
    try:
        user_id = session['user_id']
        # Empty processed passports bin
        empty_recycled_passports(user_id)
        # Empty invalid passports bin
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("USE passport_db")
        cursor.execute(
            "DELETE FROM invalid_passports WHERE is_recycled = TRUE AND user_id = %s",
            (user_id,)
        )
        conn.commit()
        cursor.close()
        conn.close()
        flash('✅ All bins emptied — every deleted record has been permanently removed.', 'success')
    except Exception as e:
        flash(f'❌ Error emptying bins: {str(e)}', 'danger')
    return redirect(url_for('recycle_bin'))


# =====================================================
# DATA SPREADSHEET EXPORT
# =====================================================

@app.route("/export_all_data")
@login_required
def export_all_data():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")
    cursor.execute("""
        SELECT p.passport_number, p.country, p.surname, p.given_names, p.middle_name, p.dob, p.sex,
               g.passport_issue_date, p.expiry, g.group_name, g.nationality_id
        FROM passports p
        LEFT JOIN general_data g ON p.id = g.passport_id
        WHERE p.user_id = %s AND (p.is_recycled = FALSE OR p.is_recycled IS NULL)
        ORDER BY p.created_at DESC
    """, (session['user_id'],))
    data = cursor.fetchall()
    cursor.close()
    conn.close()
    return _generate_csv_response(data, f'Passport_Data_All_{ist_now().strftime("%Y%m%d_%H%M%S")}.csv')


@app.route("/export_selected_data", methods=["POST"])
@login_required
def export_selected_data():
    selected_ids_json = request.form.get('selected_ids')
    if not selected_ids_json:
        return redirect(url_for('results'))
    selected_ids = json.loads(selected_ids_json)
    if not selected_ids:
        return redirect(url_for('results'))

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")
    format_strings = ','.join(['%s'] * len(selected_ids))
    cursor.execute(f"""
        SELECT p.passport_number, p.country, p.surname, p.given_names, p.middle_name, p.dob, p.sex,
               g.passport_issue_date, p.expiry, g.group_name, g.nationality_id
        FROM passports p
        LEFT JOIN general_data g ON p.id = g.passport_id
        WHERE p.id IN ({format_strings}) AND p.user_id = %s
    """, tuple(selected_ids) + (session['user_id'],))
    data = cursor.fetchall()
    cursor.close()
    conn.close()
    # Use the group name as the filename
    group_names_found = list(dict.fromkeys(
        row.get('group_name', '').strip()
        for row in data
        if row.get('group_name', '').strip()
    ))
    if group_names_found:
        safe_name = group_names_found[0].replace('/', '-').replace('\\', '-').strip()
        csv_filename = f'{safe_name}.csv'
    else:
        csv_filename = f'Passport_Data_Selected_{ist_now().strftime("%Y%m%d_%H%M%S")}.csv'
    return _generate_csv_response(data, csv_filename)



# =====================================================
# BULK GROUP OPERATIONS  (from groups.html)
# =====================================================

@app.route("/groups/export_data", methods=["POST"])
@login_required
def export_groups_data():
    """Export CSV of all passports in the selected groups.
       Filename is the group name (single group) or a combined name."""
    group_names_json = request.form.get('group_names', '[]')
    try:
        group_names = json.loads(group_names_json)
    except Exception:
        flash("Invalid group selection.", "danger")
        return redirect(url_for('view_all_groups'))
    if not group_names:
        flash("No groups selected.", "danger")
        return redirect(url_for('view_all_groups'))

    data = _get_passports_by_group_names(session['user_id'], group_names)
    # Filename: single group → group name; multiple → first group name
    filename = group_names[0] if len(group_names) == 1 else group_names[0]
    # Sanitise for filesystem
    safe_name = filename.replace('/', '-').replace('\\', '-').strip()
    return _generate_csv_response(data, f'{safe_name}.csv')


@app.route("/groups/export_image", methods=["POST"])
@login_required
def export_groups_image():
    """Export original passport images for selected groups as a ZIP of JPEGs.
       One JPEG per passport, named <passport_number>.jpg.
       ZIP filename is the group name."""
    group_names_json = request.form.get('group_names', '[]')
    try:
        group_names = json.loads(group_names_json)
    except Exception:
        flash("Invalid group selection.", "danger")
        return redirect(url_for('view_all_groups'))
    if not group_names:
        flash("No groups selected.", "danger")
        return redirect(url_for('view_all_groups'))

    try:
        passport_ids = _get_passport_ids_by_group_names(session['user_id'], group_names)
        if not passport_ids:
            flash("No passport records found in selected groups.", "danger")
            return redirect(url_for('view_all_groups'))

        zip_buffer = io.BytesIO()
        import zipfile
        generated = 0
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for p_id in passport_ids:
                passport_data = get_passport_by_id(p_id, session['user_id'])
                if not passport_data:
                    continue
                img_filename = passport_data.get('filename')
                if img_filename:
                    img_path, _ = resolve_passport_paths(img_filename, passport_data.get('group_name'))
                    if img_path and os.path.exists(img_path):
                        pnum = (passport_data.get('passport_number') or str(p_id)).replace('/', '-').replace('\\', '-').strip()
                        zf.write(img_path, f"{pnum}.jpg")
                        generated += 1

        if generated == 0:
            flash("No passport images found for the selected groups.", "danger")
            return redirect(url_for('view_all_groups'))

        zip_buffer.seek(0)
        safe_name = group_names[0].replace('/', '-').replace('\\', '-').strip()
        return send_file(
            zip_buffer,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f'{safe_name}.zip'
        )
    except Exception as e:
        print(f"Export Groups Image Error: {e}")
        flash(f"Error exporting images: {str(e)}", "danger")
        return redirect(url_for('view_all_groups'))


@app.route("/groups/generate_badges", methods=["POST"])
@login_required
def generate_groups_badges():
    """Generate a single badge PDF for all passports in selected groups."""
    group_names_json = request.form.get('group_names', '[]')
    try:
        group_names = json.loads(group_names_json)
    except Exception:
        flash("Invalid group selection.", "danger")
        return redirect(url_for('view_all_groups'))
    if not group_names:
        flash("No groups selected.", "danger")
        return redirect(url_for('view_all_groups'))

    try:
        passport_ids = _get_passport_ids_by_group_names(session['user_id'], group_names)
        if not passport_ids:
            flash("No passport records found in selected groups.", "danger")
            return redirect(url_for('view_all_groups'))

        current_username = session.get('username', 'default')
        current_company_name = session.get('full_name', current_username)
        badge_images = []
        for p_id in passport_ids:
            passport_data = get_passport_by_id(p_id, session['user_id'])
            if passport_data:
                badge_bytes = generate_badge_image(passport_data, current_username, current_company_name)
                if badge_bytes:
                    badge_images.append(Image.open(io.BytesIO(badge_bytes)))

        if not badge_images:
            flash("Failed to generate any badges.", "danger")
            return redirect(url_for('view_all_groups'))

        A4_W, A4_H    = 3508, 2480
        badge_width   = A4_W // 4
        badge_height  = A4_H
        pages = []
        for i in range(0, len(badge_images), 4):
            chunk = badge_images[i:i + 4]
            page  = Image.new('RGB', (A4_W, A4_H), (255, 255, 255))
            for j, badge in enumerate(chunk):
                page.paste(badge.resize((badge_width, badge_height), _RESAMPLE), (j * badge_width, 0))
            pages.append(page)

        pdf_bytes = io.BytesIO()
        pages[0].save(
            pdf_bytes, format='PDF', resolution=300.0,
            save_all=True, append_images=pages[1:] if len(pages) > 1 else []
        )
        pdf_bytes.seek(0)

        safe_name = group_names[0].replace('/', '-').replace('\\', '-').strip()
        return send_file(
            pdf_bytes, mimetype='application/pdf', as_attachment=True,
            download_name=f'{safe_name}.pdf'
        )
    except Exception as e:
        print(f"Generate Groups Badges Error: {e}")
        flash(f"Error generating badge PDF: {str(e)}", "danger")
        return redirect(url_for('view_all_groups'))


@app.route("/groups/generate_badges_from_excel", methods=["POST"])
@login_required
def generate_badges_from_excel():
    """
    Generate a single badge PDF from a list of passport numbers supplied in
    an uploaded .xlsx or .csv file — column A, starting at row 1, no header
    row. Each number is resolved to this user's ACTIVE passport record via
    get_passport_by_number(); numbers with no match are simply skipped
    (mirrors generate_groups_badges(), which silently skips passports with
    no record instead of failing the whole batch).
    """
    upload = request.files.get('badge_excel_file')
    if not upload or not upload.filename:
        flash("No file selected.", "danger")
        return redirect(url_for('view_all_groups'))

    filename = upload.filename.lower()
    if not (filename.endswith('.xlsx') or filename.endswith('.csv')):
        flash("Invalid file type. Please upload a .xlsx or .csv file.", "danger")
        return redirect(url_for('view_all_groups'))

    try:
        passport_numbers = []
        if filename.endswith('.xlsx'):
            wb = openpyxl.load_workbook(upload, data_only=True, read_only=True)
            ws = wb.active
            for row in ws.iter_rows(min_col=1, max_col=1, values_only=True):
                val = row[0]
                if val is not None and str(val).strip():
                    passport_numbers.append(str(val).strip())
        else:
            text_stream = io.TextIOWrapper(upload.stream, encoding='utf-8-sig')
            reader = csv.reader(text_stream)
            for row in reader:
                if row and str(row[0]).strip():
                    passport_numbers.append(str(row[0]).strip())

        if not passport_numbers:
            flash("The uploaded file contains no passport numbers.", "danger")
            return redirect(url_for('view_all_groups'))

        current_username = session.get('username', 'default')
        current_company_name = session.get('full_name', current_username)
        badge_images = []
        for pn in passport_numbers:
            passport_data = get_passport_by_number(pn, session['user_id'])
            if passport_data:
                badge_bytes = generate_badge_image(passport_data, current_username, current_company_name)
                if badge_bytes:
                    badge_images.append(Image.open(io.BytesIO(badge_bytes)))

        if not badge_images:
            flash("No matching passport records were found for the uploaded numbers.", "danger")
            return redirect(url_for('view_all_groups'))

        A4_W, A4_H    = 3508, 2480
        badge_width   = A4_W // 4
        badge_height  = A4_H
        pages = []
        for i in range(0, len(badge_images), 4):
            chunk = badge_images[i:i + 4]
            page  = Image.new('RGB', (A4_W, A4_H), (255, 255, 255))
            for j, badge in enumerate(chunk):
                page.paste(badge.resize((badge_width, badge_height), _RESAMPLE), (j * badge_width, 0))
            pages.append(page)

        pdf_bytes = io.BytesIO()
        pages[0].save(
            pdf_bytes, format='PDF', resolution=300.0,
            save_all=True, append_images=pages[1:] if len(pages) > 1 else []
        )
        pdf_bytes.seek(0)

        return send_file(
            pdf_bytes, mimetype='application/pdf', as_attachment=True,
            download_name='badges_from_excel.pdf'
        )
    except Exception as e:
        print(f"Generate Badges From Excel Error: {e}")
        flash(f"Error generating badge PDF: {str(e)}", "danger")
        return redirect(url_for('view_all_groups'))


@app.route("/groups/delete_bulk", methods=["POST"])
@login_required
def delete_groups_bulk():
    """Delete all selected groups and their passport records."""
    group_names_json = request.form.get('group_names', '[]')
    try:
        group_names = json.loads(group_names_json)
    except Exception:
        flash("Invalid group selection.", "danger")
        return redirect(url_for('view_all_groups'))
    if not group_names:
        flash("No groups selected.", "danger")
        return redirect(url_for('view_all_groups'))

    deleted = []
    errors  = []
    for gn in group_names:
        try:
            delete_group_and_records(session['user_id'], gn)
            deleted.append(gn)
        except Exception as e:
            errors.append(f"{gn}: {str(e)}")

    if deleted:
        flash(f"Deleted {len(deleted)} group(s): {', '.join(deleted)}.", "success")
    if errors:
        flash(f"Errors: {'; '.join(errors)}", "danger")
    return redirect(url_for('view_all_groups'))


@app.route('/change_group', methods=['POST'])
@login_required
def change_group():
    """Move selected passport RECORDS (not whole groups) into a group.

    Two mutually-exclusive destination modes, same as the Groups page merge:
      - Transfer into an EXISTING group (`target_group`): the selected records
        automatically adopt that group's own visa_type, detected live from its
        existing records — mirrors merge_groups_into's auto-align behavior:
          * Target is Visit Visa -> records get visa_type='visit_visa' and their
            general-data fields are copied from the target group's existing values.
          * Target is Nusuk -> records get visa_type='nusuk' and those
            general-data fields are cleared to NULL.
        Any `visa_type` / general-data fields submitted alongside are ignored on
        this path so they can't conflict with the auto-alignment.
      - Create a NEW group (`new_group_name`): behaves like the previous
        change_group logic — `visa_type` is optional; if left unset the
        records keep their current visa_type unchanged; if 'visit_visa' is
        chosen, the general-data fields submitted in the form are applied
        as-is (typed by the user, not copied from anywhere).
    """
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json
    user_id = session['user_id']
    try:
        selected_ids_json = request.form.get('selected_ids')
        target_group_existing = (request.form.get('target_group') or '').strip()
        new_group_name_input = (request.form.get('new_group_name') or '').strip()

        # Exactly one destination mode: an existing target group, or a
        # brand-new group name. If both were somehow submitted, the existing
        # target group wins (mirrors the "picking one clears the other"
        # behavior enforced client-side).
        do_transfer_existing = bool(target_group_existing)
        new_group_name = '' if do_transfer_existing else new_group_name_input
        destination_group = target_group_existing if do_transfer_existing else new_group_name

        if not selected_ids_json or not destination_group:
            msg = 'Missing data. Cannot update group.'
            if is_ajax:
                return jsonify({"success": False, "message": msg}), 400
            flash(msg, 'danger')
            return redirect(url_for('results'))
        selected_ids = json.loads(selected_ids_json)
        if not selected_ids:
            if is_ajax:
                return jsonify({"success": False, "message": "No records selected."}), 400
            return redirect(url_for('results'))

        # ── Reject "Create New Group" if that name already exists ──
        # Client-side JS already blocks this with an inline error, but the
        # same check is enforced here too since the client can be bypassed.
        # Only applies to the Create-New-Group path — picking an existing
        # target group from the dropdown is a legitimate transfer, not a
        # duplicate-creation attempt.
        if not do_transfer_existing and new_group_name:
            _dup_conn = get_connection()
            _dup_cursor = _dup_conn.cursor()
            _dup_cursor.execute("USE passport_db")
            _dup_cursor.execute("""
                SELECT 1 FROM general_data g
                JOIN passports p ON g.passport_id = p.id
                WHERE p.user_id = %s AND LOWER(g.group_name) = LOWER(%s)
                LIMIT 1
            """, (user_id, new_group_name))
            _dup_exists = _dup_cursor.fetchone() is not None
            _dup_cursor.close()
            _dup_conn.close()
            if _dup_exists:
                msg = f'A group named "{new_group_name}" already exists. Choose a different name, or transfer into the existing group instead.'
                if is_ajax:
                    return jsonify({"success": False, "duplicate_group_name": True, "message": msg}), 409
                flash(msg, 'danger')
                return redirect(url_for('results'))

        # ── Strict per-group duplicate check ────────────────────────────
        # Before moving anything, check whether any of the selected
        # passports still have an ACTIVE duplicate (same passport number)
        # sitting in the TARGET group. This is always re-checked live, on
        # every submission — there is no "force through" override. The
        # only way past a conflict is to actually delete the colliding
        # record (via /change_group/resolve_duplicate) so it's no longer
        # active; the next submission's live check will then find nothing.
        duplicates = find_duplicate_groups_for_passports(selected_ids, user_id, destination_group)
        if duplicates:
            if is_ajax:
                return jsonify({
                    "success": False,
                    "duplicate_conflict": True,
                    "duplicates": duplicates,
                    "message": "Some selected passports already exist in the target group."
                }), 409
            dup_list = ', '.join(f"{d['passport_number']} (in \"{d['group_name']}\")" for d in duplicates)
            flash(f'Cannot change group: duplicate passport number(s) found — {dup_list}.', 'danger')
            return redirect(url_for('results'))

        # order_index is sent by the visa group creation dialog so that groups
        # typed first get a higher timestamp offset and appear first on the groups
        # page (ordering uses MAX(p.created_at) DESC). Defaults to 0 so all
        # existing callers that don't send this field are completely unaffected.
        order_index = int(request.form.get('order_index', 0))

        # New group type: 'nusuk' or 'visit_visa', or '' if the user left the
        # New Group Type toggle unselected. Only meaningful on the
        # Create-New-Group path — ignored when transferring into an existing
        # group, since that path auto-detects the target's own type instead.
        visa_type = (request.form.get('visa_type') or '').strip()
        if visa_type not in ('nusuk', 'visit_visa'):
            visa_type = ''
        if do_transfer_existing:
            visa_type = ''

        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("USE passport_db")
        format_strings = ','.join(['%s'] * len(selected_ids))

        # Fetch each passport's filename + its CURRENT group name before the
        # update, so the files can be physically moved into the new group's
        # folder afterwards.
        cursor.execute(
            f"""SELECT p.id, p.filename, g.group_name, g.visa_type
                FROM passports p
                LEFT JOIN general_data g ON g.passport_id = p.id
                WHERE p.id IN ({format_strings})""",
            tuple(selected_ids)
        )
        _pre_move_rows = cursor.fetchall()  # list of (id, filename, old_group_name, old_visa_type)
        file_moves = [(r[1], r[2]) for r in _pre_move_rows]
        old_visa_type_by_id = {r[0]: r[3] for r in _pre_move_rows}

        # Move records to the destination group. Only stamp visa_type here on
        # the Create-New-Group path if the user actually picked one;
        # otherwise leave each record's existing visa_type untouched. On the
        # Transfer-into-existing-group path, visa_type is aligned separately
        # below (auto-detected from the target group).
        if visa_type:
            cursor.execute(
                f"UPDATE general_data SET group_name = %s, visa_type = %s WHERE passport_id IN ({format_strings})",
                tuple([destination_group, visa_type] + selected_ids)
            )
        else:
            cursor.execute(
                f"UPDATE general_data SET group_name = %s WHERE passport_id IN ({format_strings})",
                tuple([destination_group] + selected_ids)
            )

        # Create-New-Group path: if the user picked a visa_type, reset the
        # Nusuk/Visit-Visa "sent" flags — but ONLY for the records whose
        # visa_type is actually flipping (e.g. was 'nusuk', now 'visit_visa').
        # Records that were already the same type as the new group (or had
        # no visa_type yet) are left untouched so real sent history isn't
        # wiped out by a same-type move.
        if visa_type:
            _flipped_ids = [
                pid for pid in selected_ids
                if old_visa_type_by_id.get(pid) and old_visa_type_by_id.get(pid) != visa_type
            ]
            if _flipped_ids:
                _flip_fmt = ','.join(['%s'] * len(_flipped_ids))
                cursor.execute(
                    f"""UPDATE passports
                        SET is_processed = FALSE, is_visa_processed = FALSE, processed_at = NULL, visa_processed_at = NULL
                        WHERE id IN ({_flip_fmt})""",
                    tuple(_flipped_ids)
                )

        target_visa_type = None

        if do_transfer_existing:
            # ── Auto-align moved records to the TARGET group's visa type ──
            # Same logic as merge_groups_into's transfer path: detect the
            # target group's own visa_type from its existing records (must
            # look at a record other than the ones we just moved in, so
            # exclude the just-moved passport IDs from the probe).
            cursor.execute(
                f"""
                SELECT g.visa_type
                FROM general_data g
                JOIN passports p ON g.passport_id = p.id
                WHERE p.user_id = %s AND g.group_name = %s
                  AND g.visa_type IN ('nusuk', 'visit_visa')
                  AND p.id NOT IN ({format_strings})
                LIMIT 1
                """,
                tuple([user_id, destination_group] + selected_ids)
            )
            row = cursor.fetchone()
            target_visa_type = row[0] if row else None

            if target_visa_type == 'visit_visa':
                # Use the user's own "General Data Defaults" (Account
                # settings) rather than copying an existing target-group
                # record, so every Nusuk->Visit-Visa transfer gets a
                # consistent, admin-configured set of values.
                db_defaults = get_user_settings(user_id) or {}
                _now = ist_now()
                _one_year_later = _now + timedelta(days=365)
                _arr_date, _dep_date = _resolve_default_arrival_departure(db_defaults, _now, _one_year_later)

                cursor.execute(
                    f"""
                    UPDATE general_data
                    SET visa_type = 'visit_visa',
                        marital_status = %s,
                        city_of_birth = %s,
                        profession = %s,
                        passport_issue_place = %s,
                        hotel_name = %s,
                        address = %s,
                        city = %s,
                        zip_postal_code = %s,
                        expected_arrival = %s,
                        expected_departure = %s
                    WHERE passport_id IN ({format_strings})
                    """,
                    tuple([
                        safe_int(db_defaults.get('marital_status'), 5),
                        db_defaults.get('city_of_birth', 'MAIN STREET'),
                        db_defaults.get('profession', 'TOURISM'),
                        db_defaults.get('passport_issue_place', 'PLACE'),
                        db_defaults.get('hotel_name', 'Hayat Mall Gate 6, Riyadh'),
                        db_defaults.get('address', 'ADDRESS'),
                        db_defaults.get('city', 'MAIN STREET'),
                        db_defaults.get('zip_postal_code', '676542'),
                        _arr_date,
                        _dep_date,
                    ] + selected_ids)
                )

            elif target_visa_type == 'nusuk':
                cursor.execute(
                    f"""
                    UPDATE general_data
                    SET visa_type = 'nusuk',
                        marital_status = 5,
                        city_of_birth = NULL,
                        profession = NULL,
                        passport_issue_place = NULL,
                        hotel_name = NULL,
                        address = NULL,
                        city = NULL,
                        zip_postal_code = NULL,
                        expected_arrival = NULL,
                        expected_departure = NULL
                    WHERE passport_id IN ({format_strings})
                    """,
                    tuple(selected_ids)
                )

            # Transfer-into-existing-group path: reset the "sent" flags only
            # for records whose visa_type is actually flipping relative to
            # what they had before the move. A same-type transfer (e.g.
            # Nusuk group A -> Nusuk group B) must NOT touch sent status.
            if target_visa_type in ('nusuk', 'visit_visa'):
                _flipped_ids = [
                    pid for pid in selected_ids
                    if old_visa_type_by_id.get(pid) and old_visa_type_by_id.get(pid) != target_visa_type
                ]
                if _flipped_ids:
                    _flip_fmt = ','.join(['%s'] * len(_flipped_ids))
                    cursor.execute(
                        f"""UPDATE passports
                            SET is_processed = FALSE, is_visa_processed = FALSE, processed_at = NULL, visa_processed_at = NULL
                            WHERE id IN ({_flip_fmt})""",
                        tuple(_flipped_ids)
                    )

        # When switching to Nusuk on the Create-New-Group path, clear any
        # leftover Visit Visa general-data fields the record may still be
        # carrying (e.g. moving a Visit Visa record into a brand-new Nusuk
        # group) — mirrors the clear-on-Nusuk step used on the
        # transfer-into-existing-group path above, so stale Visit Visa data
        # never lingers on a record that's now Nusuk.
        if (not do_transfer_existing) and visa_type == 'nusuk':
            cursor.execute(
                f"""
                UPDATE general_data
                SET marital_status = 5,
                    city_of_birth = NULL,
                    profession = NULL,
                    passport_issue_place = NULL,
                    hotel_name = NULL,
                    address = NULL,
                    city = NULL,
                    zip_postal_code = NULL,
                    expected_arrival = NULL,
                    expected_departure = NULL
                WHERE passport_id IN ({format_strings})
                """,
                tuple(selected_ids)
            )

        # When switching to Visit Visa on the Create-New-Group path, apply
        # the general-data defaults submitted alongside the group change
        # (email/contact number are intentionally not collected here and are
        # left untouched). Not used on the transfer-into-existing-group path
        # — those fields are auto-aligned above instead.
        if (not do_transfer_existing) and visa_type == 'visit_visa':
            arrival_raw = (request.form.get('expected_arrival') or '').strip()
            set_clauses = [
                "marital_status = %s", "city_of_birth = %s", "profession = %s",
                "passport_issue_place = %s", "hotel_name = %s", "address = %s",
                "city = %s", "zip_postal_code = %s"
            ]
            values = [
                safe_int(request.form.get('marital_status', '').strip(), 5),
                request.form.get('city_of_birth', '').strip(),
                request.form.get('profession', '').strip(),
                request.form.get('passport_issue_place', '').strip(),
                request.form.get('hotel_name', '').strip(),
                request.form.get('address', '').strip(),
                request.form.get('city', '').strip(),
                request.form.get('zip_postal_code', '').strip(),
            ]
            if arrival_raw:
                try:
                    _arr_dt = datetime.strptime(arrival_raw, "%Y-%m-%d").date()
                    _dep_dt = _arr_dt + timedelta(days=365)
                    set_clauses.append("expected_arrival = %s")
                    values.append(_arr_dt)
                    set_clauses.append("expected_departure = %s")
                    values.append(_dep_dt)
                except ValueError:
                    pass
            cursor.execute(
                f"UPDATE general_data SET {', '.join(set_clauses)} WHERE passport_id IN ({format_strings})",
                tuple(values + selected_ids)
            )

        # Mark the destination group as recently active so it sorts to
        # the top of every dropdown/list — WITHOUT touching each moved
        # passport's own created_at (which must stay accurate: it's the
        # record's real upload time, and the Nusuk 365-day duplicate rule
        # reads it directly for unprocessed records).
        conn.commit()
        cursor.close()
        conn.close()
        touch_group_activity(user_id, destination_group)

        # Physically move each passport's original + face image into the
        # new group's folder now that the DB update has committed.
        for fname, old_group_name in file_moves:
            if fname:
                move_passport_files_to_group(fname, old_group_name, destination_group)

        if do_transfer_existing and target_visa_type in ('nusuk', 'visit_visa'):
            msg = f'Successfully transferred {len(selected_ids)} passport(s) into "{destination_group}" (auto-aligned to "{target_visa_type}").'
        else:
            msg = f'Successfully changed group to "{destination_group}" for {len(selected_ids)} passport(s).'
        if is_ajax:
            return jsonify({"success": True, "message": msg})
        flash(msg, 'success')
    except Exception as e:
        msg = f'Failed to change group: {str(e)}'
        if is_ajax:
            return jsonify({"success": False, "message": msg}), 500
        flash(msg, 'danger')
    return redirect(url_for('results'))


@app.route('/groups/duplicate', methods=['POST'])
@login_required
def duplicate_group():
    """Duplicate an ENTIRE group into a brand-new group.

    - New group name = "<original_name>_<DD-MM-YYYY>" (today's date). If that
      exact name already exists (e.g. duplicated twice in one day), a numeric
      suffix (_2, _3, ...) is appended until a free name is found.
    - Every passport record in the source group is COPIED (not moved) —
      including its `passports` row, its `general_data` row, and its physical
      image files (original + face) — into the new group.
    - On the COPIES ONLY: is_processed, is_visa_processed, processed_at, and
      visa_processed_at are reset to their "unprocessed" defaults, regardless
      of what the source records' status was (fully processed, partially
      processed, or unprocessed).
    - The SOURCE group and every one of its records are left 100% untouched —
      no status change, no renaming, nothing deleted or modified.
    """
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json
    user_id = session['user_id']

    source_group = (request.form.get('group_name') or '').strip()
    if not source_group:
        msg = 'Missing source group name.'
        if is_ajax:
            return jsonify({"success": False, "message": msg}), 400
        flash(msg, 'danger')
        return redirect(url_for('view_all_groups'))

    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("USE passport_db")

        # ── Confirm the source group actually has records for this user ──
        cursor.execute(
            """SELECT p.id FROM passports p
               JOIN general_data g ON g.passport_id = p.id
               WHERE p.user_id = %s AND g.group_name = %s""",
            (user_id, source_group)
        )
        source_ids = [r[0] for r in cursor.fetchall()]
        if not source_ids:
            cursor.close(); conn.close()
            msg = f'Group "{source_group}" has no records to duplicate.'
            if is_ajax:
                return jsonify({"success": False, "message": msg}), 404
            flash(msg, 'danger')
            return redirect(url_for('view_all_groups'))

        # ── Build a free destination name: "<name>_<DD-MM-YYYY>", then
        # "_2", "_3", ... if that exact name is already taken. ──
        today_suffix = ist_now().strftime('%d-%m-%Y')
        base_new_name = f"{source_group}_{today_suffix}"

        def _group_name_exists(name):
            cursor.execute(
                """SELECT 1 FROM general_data g
                   JOIN passports p ON g.passport_id = p.id
                   WHERE p.user_id = %s AND LOWER(g.group_name) = LOWER(%s)
                   LIMIT 1""",
                (user_id, name)
            )
            return cursor.fetchone() is not None

        new_group_name = base_new_name
        _suffix_n = 2
        while _group_name_exists(new_group_name):
            new_group_name = f"{base_new_name}_{_suffix_n}"
            _suffix_n += 1

        # ── Introspect real column lists so every field the schema actually
        # has gets copied, without needing to hardcode (and risk missing)
        # any columns. Skip the primary key on `passports` (auto-generated
        # for the new row) and the FK on `general_data` (rewired below). ──
        cursor.execute("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = 'passport_db' AND TABLE_NAME = 'passports'
            ORDER BY ORDINAL_POSITION
        """)
        passport_cols = [r[0] for r in cursor.fetchall() if r[0] != 'id']

        cursor.execute("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = 'passport_db' AND TABLE_NAME = 'general_data'
            ORDER BY ORDINAL_POSITION
        """)
        general_cols = [r[0] for r in cursor.fetchall() if r[0] not in ('id', 'passport_id')]

        # Columns on `passports` that must be reset on the COPY regardless
        # of the source row's current value (fully/partially/unprocessed).
        _reset_defaults = {
            'is_processed': False,
            'is_visa_processed': False,
            'processed_at': None,
            'visa_processed_at': None,
        }

        pcol_fmt = ', '.join(f'`{c}`' for c in passport_cols)
        gcol_fmt = ', '.join(f'`{c}`' for c in general_cols)

        new_passport_ids = []
        old_to_new_id = {}
        file_copies = []  # (filename, group_name) for original + face image copy

        for old_id in source_ids:
            # Fetch this passport's full row + its filename for file copying.
            cursor.execute(
                f"SELECT {pcol_fmt}, filename FROM passports WHERE id = %s",
                (old_id,)
            )
            prow = cursor.fetchone()
            if not prow:
                continue
            *pvals, fname = prow
            pvals = list(pvals)

            # Apply the unprocessed-reset defaults onto the copy only.
            for reset_col, reset_val in _reset_defaults.items():
                if reset_col in passport_cols:
                    idx = passport_cols.index(reset_col)
                    pvals[idx] = reset_val

            insert_p_cols = ', '.join(f'`{c}`' for c in passport_cols)
            insert_p_ph = ', '.join(['%s'] * len(passport_cols))
            cursor.execute(
                f"INSERT INTO passports ({insert_p_cols}) VALUES ({insert_p_ph})",
                tuple(pvals)
            )
            new_id = cursor.lastrowid
            new_passport_ids.append(new_id)
            old_to_new_id[old_id] = new_id

            if fname:
                file_copies.append((fname, new_id))

            # Copy this passport's general_data row, rewired to the new
            # passport_id and the new group_name.
            cursor.execute(
                f"SELECT {gcol_fmt} FROM general_data WHERE passport_id = %s",
                (old_id,)
            )
            grow = cursor.fetchone()
            if grow:
                gvals = list(grow)
                if 'group_name' in general_cols:
                    gvals[general_cols.index('group_name')] = new_group_name
                insert_g_cols = ', '.join(f'`{c}`' for c in (['passport_id'] + general_cols))
                insert_g_ph = ', '.join(['%s'] * (len(general_cols) + 1))
                cursor.execute(
                    f"INSERT INTO general_data ({insert_g_cols}) VALUES ({insert_g_ph})",
                    tuple([new_id] + gvals)
                )

        conn.commit()
        cursor.close()
        conn.close()

        touch_group_activity(user_id, new_group_name)

        # ── Physically COPY (never move/delete) each passport's original +
        # face image into the new group's folder. Each file is copied
        # independently so one bad/missing file can't abort copying the
        # rest of the group -- the DB rows are already committed above, so
        # silently stopping partway would leave later records in the new
        # group with no images at all. ──
        copy_failures = 0
        missing_files = 0
        for fname, _new_id in file_copies:
            try:
                result = copy_passport_files_to_group(fname, source_group, new_group_name)
                if result["original_missing"] or result["face_missing"]:
                    missing_files += 1
                    logging.warning(
                        "duplicate_group: source file(s) missing for %r while copying "
                        "group %r -> %r (original_missing=%s, face_missing=%s)",
                        fname, source_group, new_group_name,
                        result["original_missing"], result["face_missing"],
                    )
            except Exception:
                copy_failures += 1
                logging.exception(
                    "duplicate_group: failed to copy files for %r from group %r to %r",
                    fname, source_group, new_group_name,
                )

        msg = f'Duplicated "{source_group}" into new group "{new_group_name}" ({len(new_passport_ids)} record(s), unprocessed).'
        if missing_files or copy_failures:
            msg += f' Warning: {missing_files} record(s) had missing source image(s) and {copy_failures} image copy operation(s) failed — check the new group.'
        if is_ajax:
            return jsonify({
                "success": True, "message": msg, "new_group_name": new_group_name,
                "image_warnings": {"missing_files": missing_files, "copy_failures": copy_failures},
            })
        flash(msg, 'success' if not (missing_files or copy_failures) else 'warning')
    except Exception as e:
        msg = f'Failed to duplicate group: {str(e)}'
        if is_ajax:
            return jsonify({"success": False, "message": msg}), 500
        flash(msg, 'danger')
    return redirect(url_for('view_all_groups'))


@app.route('/change_group/resolve_duplicate', methods=['POST'])
@login_required
def resolve_change_group_duplicate():
    """
    Called from the "Delete" button in the duplicate-conflict popup shown
    by /change_group, /passports/move_group, and /merge_groups_into.
    Deletes (moves to Recycle Bin) the EXISTING record that occupies the
    target group slot, freeing it up. Body: existing_passport_id.

    This never moves anything itself — the actual move only happens when
    the user presses "Submit" in the popup, which re-runs the original
    move/merge request. That request re-checks duplicates live, so once
    the colliding record is deleted here, it naturally won't reappear in
    the conflict list and the move proceeds.
    """
    user_id = session['user_id']
    try:
        existing_passport_id = int(request.form.get('existing_passport_id'))
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Invalid passport id."}), 400

    try:
        passport = get_passport_by_id(existing_passport_id, user_id)
        if not passport:
            return jsonify({"success": False, "message": "Passport not found."}), 404
        delete_passport_record(existing_passport_id, user_id)
        return jsonify({
            "success": True,
            "message": "Duplicate record deleted."
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"Failed to delete duplicate record: {str(e)}"}), 500



# =====================================================
# MERGE GROUPS INTO TARGET GROUP
# =====================================================

@app.route('/merge_groups_into', methods=['POST'])
@login_required
def merge_groups_into():
    """Transfer all passport records from one or more source groups into a target group,
    and/or change the visa type (Nusuk / Visit Visa) of the selected groups' records.

    - If a target group is provided (different from the source groups), records are
      moved into it via general_data.group_name. Moved records automatically adopt
      the TARGET group's visa_type (detected from the target group's own existing
      records) — no manual "Change Visa Type" selection needed:
        * Target is Visit Visa -> moved records get visa_type='visit_visa' and their
          general-data fields (marital_status, city_of_birth, profession,
          passport_issue_place, hotel_name, address, city, zip_postal_code,
          expected_arrival) are copied from the target group's existing values.
        * Target is Nusuk -> moved records get visa_type='nusuk' and all of the
          above general-data fields are cleared to NULL.
      The "Change Visa Type" dropdown is ignored on this transfer path so it can't
      conflict with the auto-alignment.
    - If NO transfer happens (same/no target group) and a visa type is provided,
      every record in the selected source groups is updated to that type in place —
      this is the existing "Change Visa Type only" behavior and is unaffected.
    """
    is_ajax_early = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    try:
        user_id      = session['user_id']
        source_json  = request.form.get('source_groups', '[]')
        target_group = (request.form.get('target_group') or '').strip()
        new_visa_type = (request.form.get('new_visa_type') or '').strip()

        # Only allow known visa types; anything else is ignored (keeps
        # each record's current type unchanged).
        if new_visa_type not in ('nusuk', 'visit_visa'):
            new_visa_type = ''

        source_groups = json.loads(source_json)
        source_groups = [s.strip() for s in source_groups if s.strip()]

        if not source_groups:
            if is_ajax_early:
                return jsonify({"success": False, "message": "Missing source group(s)."}), 400
            flash('Missing source group(s).', 'danger')
            return redirect(url_for('view_all_groups'))

        # A target group is only required if we're actually transferring records.
        # If the person only picked a visa type (no target / same target as source),
        # skip the transfer step and just apply the type change in place.
        do_transfer = bool(target_group) and any(s != target_group for s in source_groups)

        if not do_transfer and not new_visa_type:
            if is_ajax_early:
                return jsonify({"success": False, "message": "Missing target group."}), 400
            flash('Missing target group.', 'danger')
            return redirect(url_for('view_all_groups'))

        conn   = get_connection()
        cursor = conn.cursor()
        cursor.execute("USE passport_db")

        moved = 0

        if do_transfer:
            transfer_sources = [s for s in source_groups if s != target_group]
            fmt = ','.join(['%s'] * len(transfer_sources))

            # ── Detect the target group's own visa type BEFORE moving any
            # records into it. Doing this first (rather than after the move)
            # guarantees the query only ever sees genuinely pre-existing
            # target-group records — never one of the records we're about to
            # move in. Querying after the move was the bug: with no rows yet
            # existing in a brand-new target group, the post-move query would
            # just read back one of the just-moved records' OLD type
            # (almost always 'nusuk'), silently overriding whatever visa type
            # the user explicitly picked in "Change Visa Type".
            cursor.execute("""
                SELECT g.visa_type
                FROM general_data g
                JOIN passports p ON g.passport_id = p.id
                WHERE p.user_id = %s AND g.group_name = %s
                  AND g.visa_type IN ('nusuk', 'visit_visa')
                LIMIT 1
            """, (user_id, target_group))
            _pre_row = cursor.fetchone()
            target_visa_type = _pre_row[0] if _pre_row else None

            # ── Strict per-group duplicate check ─────────────────────────
            # Unlike /change_group or /passports/move_group, this merge is a
            # single bulk UPDATE — it can't skip individual colliding rows.
            # So before running it, find any passport_number that is active
            # in BOTH a transfer-source group and the target group; those
            # would silently become duplicates once merged. Always
            # re-checked live on every submission — there is no "force
            # through" override. The only way past a conflict is to delete
            # the colliding record first (via /change_group/resolve_duplicate),
            # so the next submission's live check finds nothing.
            cursor.execute(f"""
                SELECT p_src.id AS passport_id, p_src.passport_number,
                       g_src.group_name AS source_group,
                       p_tgt.id AS existing_passport_id
                FROM passports p_src
                JOIN general_data g_src ON g_src.passport_id = p_src.id
                JOIN passports p_tgt ON p_tgt.user_id = p_src.user_id
                                     AND p_tgt.passport_number = p_src.passport_number
                                     AND p_tgt.id != p_src.id
                JOIN general_data g_tgt ON g_tgt.passport_id = p_tgt.id AND g_tgt.group_name = %s
                WHERE p_src.user_id = %s
                  AND g_src.group_name IN ({fmt})
                  AND (p_src.is_recycled = FALSE OR p_src.is_recycled IS NULL)
                  AND (p_tgt.is_recycled = FALSE OR p_tgt.is_recycled IS NULL)
            """, tuple([target_group, user_id] + transfer_sources))
            merge_duplicates = [
                {
                    'passport_id': row[0],
                    'passport_number': row[1],
                    'group_name': row[2],
                    'existing_passport_id': row[3],
                }
                for row in cursor.fetchall()
            ]

            if merge_duplicates:
                cursor.close()
                conn.close()
                message = "Some passports in the source group(s) already exist in the target group."
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return jsonify({
                        "success": False,
                        "duplicate_conflict": True,
                        "duplicates": merge_duplicates,
                        "message": message
                    }), 409
                dup_list = ', '.join(f"{d['passport_number']} (source \"{d['group_name']}\")" for d in merge_duplicates)
                flash(f'Cannot merge: duplicate passport number(s) found — {dup_list}.', 'danger')
                return redirect(url_for('view_all_groups'))

            # Move all passport records from source groups → target group (scoped to user)
            cursor.execute(f"""
                UPDATE general_data g
                JOIN passports p ON g.passport_id = p.id
                SET g.group_name = %s
                WHERE p.user_id = %s AND g.group_name IN ({fmt})
            """, tuple([target_group, user_id] + transfer_sources))

            moved = cursor.rowcount
            # Target-group last_activity_at is marked after the outer commit below.

            # target_visa_type is None here whenever the target group had no
            # pre-existing typed records — i.e. it's a brand-new group being
            # created by this same request. In that case there's nothing to
            # "auto-align" to, so fall back to applying whatever visa type
            # the user explicitly chose in "Change Visa Type" (if any),
            # exactly like the non-transfer path below does.
            if target_visa_type is None and new_visa_type:
                target_visa_type = new_visa_type

            # Capture which moved-in records will actually flip visa_type
            # BEFORE running the align UPDATE below (visa_type on these rows
            # is still their pre-move value at this point — only group_name
            # has been updated so far). Records whose type doesn't change
            # are excluded, so a same-type merge never touches sent status.
            merge_flip_ids = []
            if target_visa_type in ('nusuk', 'visit_visa'):
                cursor.execute("""
                    SELECT p.id
                    FROM passports p
                    JOIN general_data g ON g.passport_id = p.id
                    WHERE p.user_id = %s AND g.group_name = %s
                      AND (g.visa_type IS NULL OR g.visa_type != %s)
                """, (user_id, target_group, target_visa_type))
                merge_flip_ids = [row[0] for row in cursor.fetchall()]

            if target_visa_type == 'visit_visa':
                # Use the user's own "General Data Defaults" (Account
                # settings) rather than copying an existing target-group
                # record, so every Nusuk->Visit-Visa transfer gets a
                # consistent, admin-configured set of values.
                db_defaults = get_user_settings(user_id) or {}
                _now = ist_now()
                _one_year_later = _now + timedelta(days=365)
                _arr_date, _dep_date = _resolve_default_arrival_departure(db_defaults, _now, _one_year_later)

                cursor.execute("""
                    UPDATE general_data g
                    JOIN passports p ON g.passport_id = p.id
                    SET g.visa_type = 'visit_visa',
                        g.marital_status = %s,
                        g.city_of_birth = %s,
                        g.profession = %s,
                        g.passport_issue_place = %s,
                        g.hotel_name = %s,
                        g.address = %s,
                        g.city = %s,
                        g.zip_postal_code = %s,
                        g.expected_arrival = %s,
                        g.expected_departure = %s
                    WHERE p.user_id = %s AND g.group_name = %s
                """, (
                    safe_int(db_defaults.get('marital_status'), 5),
                    db_defaults.get('city_of_birth', 'MAIN STREET'),
                    db_defaults.get('profession', 'TOURISM'),
                    db_defaults.get('passport_issue_place', 'PLACE'),
                    db_defaults.get('hotel_name', 'Hayat Mall Gate 6, Riyadh'),
                    db_defaults.get('address', 'ADDRESS'),
                    db_defaults.get('city', 'MAIN STREET'),
                    db_defaults.get('zip_postal_code', '676542'),
                    _arr_date,
                    _dep_date,
                    user_id, target_group
                ))

            elif target_visa_type == 'nusuk':
                cursor.execute("""
                    UPDATE general_data g
                    JOIN passports p ON g.passport_id = p.id
                    SET g.visa_type = 'nusuk',
                        g.marital_status = 5,
                        g.city_of_birth = NULL,
                        g.profession = NULL,
                        g.passport_issue_place = NULL,
                        g.hotel_name = NULL,
                        g.address = NULL,
                        g.city = NULL,
                        g.zip_postal_code = NULL,
                        g.expected_arrival = NULL,
                        g.expected_departure = NULL
                    WHERE p.user_id = %s AND g.group_name = %s
                """, (user_id, target_group))

            # Reset "sent" status only for the records that actually flipped
            # visa_type as part of this transfer.
            if merge_flip_ids:
                _mf_fmt = ','.join(['%s'] * len(merge_flip_ids))
                cursor.execute(
                    f"""UPDATE passports
                        SET is_processed = FALSE, is_visa_processed = FALSE, processed_at = NULL, visa_processed_at = NULL
                        WHERE id IN ({_mf_fmt})""",
                    tuple(merge_flip_ids)
                )

        # Groups whose records should end up with the new visa type: the
        # target group (if we transferred), otherwise all selected source groups.
        # NOTE: when do_transfer is true, visa_type/general-data for the
        # target group were already auto-aligned above based on the target
        # group's own type — the "Change Visa Type" dropdown is ignored on
        # the transfer path so it can't fight with the auto-align logic.
        type_target_groups = [] if do_transfer else source_groups

        if new_visa_type and type_target_groups:
            fmt2 = ','.join(['%s'] * len(type_target_groups))

            # Capture which records in the source group(s) will actually
            # flip visa_type BEFORE running the UPDATE below, so a
            # same-type "change" (re-picking the type a group already has)
            # never touches sent status.
            cursor.execute(f"""
                SELECT p.id
                FROM passports p
                JOIN general_data g ON g.passport_id = p.id
                WHERE p.user_id = %s AND g.group_name IN ({fmt2})
                  AND (g.visa_type IS NULL OR g.visa_type != %s)
            """, tuple([user_id] + type_target_groups + [new_visa_type]))
            inplace_flip_ids = [row[0] for row in cursor.fetchall()]

            if new_visa_type == 'nusuk':
                # Switching in place to Nusuk must also clear any leftover
                # Visit Visa general-data fields — otherwise stale values
                # (marital_status, city_of_birth, hotel_name, etc.) from
                # before the switch keep sitting on the now-Nusuk records.
                cursor.execute(f"""
                    UPDATE general_data g
                    JOIN passports p ON g.passport_id = p.id
                    SET g.visa_type = %s,
                        g.marital_status = 5,
                        g.city_of_birth = NULL,
                        g.profession = NULL,
                        g.passport_issue_place = NULL,
                        g.hotel_name = NULL,
                        g.address = NULL,
                        g.city = NULL,
                        g.zip_postal_code = NULL,
                        g.expected_arrival = NULL,
                        g.expected_departure = NULL
                    WHERE p.user_id = %s AND g.group_name IN ({fmt2})
                """, tuple([new_visa_type, user_id] + type_target_groups))
            else:
                cursor.execute(f"""
                    UPDATE general_data g
                    JOIN passports p ON g.passport_id = p.id
                    SET g.visa_type = %s
                    WHERE p.user_id = %s AND g.group_name IN ({fmt2})
                """, tuple([new_visa_type, user_id] + type_target_groups))

            if inplace_flip_ids:
                _if_fmt = ','.join(['%s'] * len(inplace_flip_ids))
                cursor.execute(
                    f"""UPDATE passports
                        SET is_processed = FALSE, is_visa_processed = FALSE, processed_at = NULL, visa_processed_at = NULL
                        WHERE id IN ({_if_fmt})""",
                    tuple(inplace_flip_ids)
                )

        conn.commit()
        cursor.close()
        conn.close()

        # Mark the target group (transfer) and/or type-changed groups as
        # recently active so they surface at the top of every dropdown —
        # without rewriting created_at on existing records (which would
        # corrupt real upload history and break the Nusuk 365-day rule).
        if do_transfer:
            touch_group_activity(user_id, target_group)
        elif new_visa_type and type_target_groups:
            for _g in type_target_groups:
                touch_group_activity(user_id, _g)

        # Physically merge each source group's folder into the target
        # group's folder (originals + faces, live folders).
        if do_transfer:
            for src_group in transfer_sources:
                move_group_folder(src_group, target_group)

        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

        if do_transfer:
            src_label = ', '.join(f'"{s}"' for s in transfer_sources)
            if target_visa_type in ('nusuk', 'visit_visa'):
                success_message = f'Successfully transferred {moved} record(s) from {src_label} into "{target_group}" (auto-aligned to "{target_visa_type}").'
            else:
                success_message = f'Successfully transferred {moved} record(s) from {src_label} into "{target_group}".'
        else:
            grp_label = ', '.join(f'"{s}"' for s in source_groups)
            success_message = f'Successfully updated {grp_label} to type "{new_visa_type}".'

        if is_ajax:
            return jsonify({"success": True, "message": success_message})
        flash(success_message, 'success')

    except Exception as e:
        message = f'Failed to update groups: {str(e)}'
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"success": False, "message": message}), 500
        flash(message, 'danger')

    return redirect(url_for('view_all_groups'))


# =====================================================
# ACCOUNT PAGE & PLAN MANAGEMENT
# =====================================================

@app.route('/account')
@login_required
def account():
    user_id = session['user_id']
    token = session.get('host_token')
    total_invalid = get_total_invalid_count(user_id)

    summary = host_account_summary(user_id, token)

    if not summary.get('success'):
        flash(f"⚠️ Could not load account info from server: {summary.get('message', 'unknown error')}", 'danger')
        plan_summary = {
            'plan': 'subscription', 'details': dict(PLAN_DETAILS['subscription']),
            'monthly_used': 0, 'daily_used': 0, 'limit': None, 'remaining': None,
            'is_over_limit': False, 'extra_count': 0, 'rate_total': 0,
            'account_level': session.get('account_level', 'full'),
            'days_until_blocked': 0, 'is_admin_user': bool(session.get('is_admin')),
            'usage_pct': 0,
        }
        daily_stats = []
        billing_history = []
    else:
        plan_summary = summary.get('plan_summary', {})
        daily_stats = summary.get('daily_stats', [])
        billing_history = summary.get('billing_history', [])

    if daily_stats:
        daily_stats.reverse()
        running_total = 0
        for row in daily_stats:
            row_total = (row.get('upload_count', 0)
                         + (row.get('duplicate_count') or 0)
                         + (row.get('invalid_count') or 0))
            running_total += row_total
            row['running_total'] = running_total
        daily_stats.reverse()

    for bill in billing_history:
        # 1. Convert billing_month to datetime and calculate billing_period_end
        if bill.get('billing_month'):
            try:
                start = (bill['billing_month']
                         if hasattr(bill['billing_month'], 'strftime')
                         else datetime.strptime(str(bill['billing_month'])[:10], '%Y-%m-%d'))
                
                # Overwrite the string with the new datetime object
                bill['billing_month'] = start 
                bill['billing_period_end'] = start + timedelta(days=29)
            except Exception:
                bill['billing_period_end'] = None
        else:
            bill['billing_period_end'] = None

        # 2. Convert generated_at from string to datetime object
        if bill.get('generated_at') and isinstance(bill['generated_at'], str):
            try:
                # Parse MySQL format 'YYYY-MM-DD HH:MM:SS' into a datetime object
                bill['generated_at'] = datetime.strptime(bill['generated_at'][:19], '%Y-%m-%d %H:%M:%S')
            except Exception:
                pass

        # 3. Cast numeric fields that may come back as strings from JSON
        for num_field in ('total_price', 'base_price', 'extra_price', 'extra_rate'):
            if bill.get(num_field) is not None:
                try:
                    bill[num_field] = float(bill[num_field])
                except (TypeError, ValueError):
                    pass

    # Convert daily_stats JSON string dates back to datetime objects
    for stat in daily_stats:
        if stat.get('log_date') and isinstance(stat['log_date'], str):
            try:
                # Parse the 'YYYY-MM-DD' string into a real datetime object
                stat['log_date'] = datetime.strptime(stat['log_date'][:10], '%Y-%m-%d')
            except Exception:
                pass

    return render_template('account.html',
                           plan_summary=plan_summary, daily_stats=daily_stats,
                           billing_history=billing_history, plans=PLAN_DETAILS,
                           total_invalid=total_invalid,
                           today=ist_now().date())


# =====================================================
# CHANGE USERNAME / PASSWORD (handled by host)
# =====================================================

@app.route('/account/change_credentials', methods=['POST'])
@login_required
def change_credentials():
    user_id = session['user_id']
    token = session.get('host_token')
    action = request.form.get('action')

    if action == 'change_username':
        new_username = request.form.get('new_username', '').strip()
        current_password = request.form.get('current_password_u', '').strip()
        result = host_change_credentials(user_id, token, action='change_username',
                                          new_username=new_username,
                                          current_password=current_password)
    elif action == 'change_password':
        result = host_change_credentials(user_id, token, action='change_password',
                                          current_password=request.form.get('current_password_p', '').strip(),
                                          new_password=request.form.get('new_password', '').strip(),
                                          confirm_password=request.form.get('confirm_password', '').strip())
    else:
        result = {'success': False, 'message': 'Invalid action.'}

    if result.get('success'):
        if action == 'change_username' and result.get('username'):
            session['username'] = result['username']
        flash(result.get('message', 'Updated successfully.'), 'success')
    else:
        flash(result.get('message', 'Update failed.'), 'danger')

    return redirect(url_for('account'))


# =====================================================
# REPARSE IMAGE TOOLS (Rotate / Rescan)
# =====================================================

@app.route("/api/batch_ocr_stats")
@login_required
def batch_ocr_stats():
    stats = session.get("latest_api_stats")
    if not stats:
        return jsonify({"status": "error", "message": "No recent batch upload data found."}), 404
    return jsonify({
        "status": "success",
        # ProvA batch (Phase 2) — 1 unit per batch of 13
        "provA_batch_units": stats.get("provA_batch_units", 0),
        # ProvA individual (Phase 4) — 1 unit per failed image
        "provA_individual_units": stats.get("provA_individual_units", 0),
        # Total ProvA units consumed
        "total_provA_units": stats.get("total_provA_units", stats.get("provA_batch_units", 0) + stats.get("provA_individual_units", 0)),
        # ProvB Vision batch (Phase 3) — 1 call for all batch-2 failures stacked together
        "provB_calls": stats.get("provB_calls", 0),
        "passports_processed": stats.get("processed", 0),
        "duplicates": stats.get("duplicates", 0),
        "invalid": stats.get("invalid", 0)
    })


@app.route("/api/progress")
@login_required
def get_upload_progress():
    """Returns live upload progress from DB — works across all gunicorn workers."""
    user_id = session.get("user_id")
    progress = get_progress(user_id)
    return jsonify({
        "current":   progress.get("current_", 0),
        "total":     progress.get("total", 0),
        "success":   progress.get("success", 0),
        "invalid":   progress.get("invalid", 0),
        "duplicate": progress.get("duplicate", 0),
        "phase":     progress.get("phase", ""),
    })


@app.route("/api/cancel", methods=["POST"])
@login_required
def cancel_upload():
    """Sets the cancel flag for the current user's upload session.

    Called via navigator.sendBeacon('/api/cancel') on the pagehide event
    whenever the loading overlay is visible (i.e. an upload is in progress).

    Effect:
    - The processing loop sees the flag at its next between-file check point.
    - Any DB records inserted so far in the current chunk are rolled back.
    - Uploaded files for the current chunk are deleted from disk.
    - Parts that had already completed (earlier chunks) are NOT touched —
      those records were committed and visible before the refresh happened.

    Limitation: if the server is mid-way through an ProvA/ProvB OCR call
    (Phases 2-4), that one API call finishes first (up to ~60 s), then the
    cancel is applied. No additional records beyond that one are saved.
    """
    user_id = session.get("user_id")
    if user_id:
        with _cancel_flags_lock:
            _cancel_flags[user_id] = True
        print(f"[Cancel] Cancel signal received for user {user_id}.")
    # Return 204 — sendBeacon ignores the response body anyway.
    return '', 204


@app.route("/api/quota")
@login_required
def get_quota_status():
    """Returns live quota/usage summary for the current user.
    Called by the progress-overlay JS every 5 s to show inline warnings,
    and by the pre-upload quota pre-flight check before form submission.
    `limit`/`remaining` already fold in the 50/day extra-usage allowance
    (if enabled) as one combined number computed on the host."""
    user_id = session.get("user_id")
    ps = get_plan_usage_summary(user_id)
    if not ps:
        return jsonify({"limit": None})
    return jsonify({
        "limit":            ps.get("limit"),
        "monthly_used":     ps.get("monthly_used", 0),
        "remaining":        ps.get("remaining"),
        "usage_pct":        ps.get("usage_pct", 0),
        "is_over_limit":    ps.get("is_over_limit", False),
        "is_admin_user":    ps.get("is_admin_user", False),
        "allow_extra_usage": ps.get("allow_extra_usage", False),
    })


@app.route("/update_invalid_image_ajax/<int:invalid_id>", methods=["POST"])
@login_required
def update_invalid_image_ajax(invalid_id):
    try:
        data = request.json
        if not data or 'image_base64' not in data:
            return jsonify({"success": False, "message": "No image data provided"}), 400
        
        header, encoded = data['image_base64'].split(",", 1)
        image_blob = base64.b64decode(encoded)
        
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("USE passport_db")
        
        # Updates the cropped image
        cursor.execute(
            "UPDATE invalid_passports SET original_image = %s WHERE id = %s AND user_id = %s",
            (image_blob, invalid_id, session['user_id'])
        )
        conn.commit()
        cursor.close()
        conn.close()
        
        return jsonify({"success": True, "message": "Image cropped successfully"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
def _resolve_visa_exe_target(user_id):
    """
    Returns (host, port, secret_or_None, is_remote) for this user_id's exe.

    - If this user_id has an active registration in visit_visa_exe_registry
      (i.e. their passposys.exe called /api/register_visa_exe from a
      different PC), use that host/port/secret.
    - Otherwise fall back to the local same-machine exe on 127.0.0.1:9001
      with no secret required (legacy behavior, Scenario 1 unchanged).
    """
    reg = get_visit_visa_exe_registration(user_id)
    if reg:
        return reg["exe_host"], reg["exe_port"], reg["exe_secret"], True
    return "127.0.0.1", 9001, None, False


def _visa_socket_ready(host, port):
    """True if an exe already has its listener open on host:port."""
    import socket as _socket
    try:
        s = _socket.create_connection((host, port), timeout=3)
        s.close()
        return True
    except Exception:
        return False


def _ensure_visa_exe_running(user_id):
    """
    Resolves this user's exe target. If it's the local same-machine exe and
    it isn't already running, launches it and waits for it to become ready.
    If it's a remote exe (registered from another PC), Flask cannot launch
    it — the operator must have it already running there — so this just
    checks reachability and returns a clear error if it isn't.

    Returns (ok: bool, error_message: str|None, host: str, port: int, secret: str|None).
    """
    import time as _time

    host, port, secret, is_remote = _resolve_visa_exe_target(user_id)

    if _visa_socket_ready(host, port):
        return True, None, host, port, secret

    if is_remote:
        return (False,
                f"passposys.exe is registered at {host}:{port} but isn't reachable. "
                "Make sure it's running on that PC and the two machines can reach "
                "each other over the network.",
                host, port, secret)

    # Local same-machine exe: auto-launch it, same as before.
    exe_path = os.path.join(BASE_DIR, "passposys.exe")
    if not os.path.exists(exe_path):
        return False, "passposys.exe not found in the application directory.", host, port, secret
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([exe_path], cwd=BASE_DIR, **kwargs)
    except Exception as e:
        return False, f"Could not launch passposys.exe: {e}", host, port, secret

    # Wait indefinitely for the exe to boot and open its socket (poll every 0.5 s)
    while not _visa_socket_ready(host, port):
        _time.sleep(0.5)

    # Give the socket server one extra second to fully settle after binding
    _time.sleep(1)
    return True, None, host, port, secret


def _push_visa_applicant_to_exe(user_id, applicant, credentials, total_hint=1, is_new_batch=False):
    """
    Shared single-applicant socket push, used both for the first applicant
    (send_visit_visa) and for every subsequent one (mark_processed_single,
    once the previous applicant is confirmed done/skipped).

    Resolves the target exe for this user_id — local same-machine exe by
    default, or a remote PC's exe if one has registered via
    /api/register_visa_exe. Sends a payload containing exactly ONE
    applicant, framed with the same 4-byte big-endian length header the
    exe's VisaSocketServer expects, plus a leading secret token so a
    network-reachable exe can reject pushes from anyone who doesn't know
    LOCAL_API_TOKEN.

    The target is RE-RESOLVED from the registry on every retry attempt
    (not just once upfront). This closes a real race: if the exe was
    restarted moments before this call and its /api/register_visa_exe
    call hadn't finished/committed yet when the first attempt looked up
    visit_visa_exe_registry, that first attempt would silently fall back
    to the legacy local-mode target (127.0.0.1, no secret) and get
    rejected by the exe's now-remote-mode socket — previously requiring
    the operator to click Send a second time before the registration had
    caught up. Re-resolving on each retry means attempt 2 (one second
    later) picks up the by-then-completed registration automatically.

    Returns (ok: bool, error_message: str|None).
    """
    import socket as _socket
    import struct as _struct
    import time as _time

    last_error = ""
    for attempt in range(3):
        ok, err, host, port, secret = _ensure_visa_exe_running(user_id)
        if not ok:
            last_error = err
            _time.sleep(1)
            continue

        payload = json.dumps({
            "created_at":  ist_now().isoformat(timespec="seconds"),
            "total":       total_hint,
            # Explicit flag so the exe doesn't have to infer "new batch vs.
            # continuation" from total_hint == 1, which is ambiguous for a
            # 1-applicant batch (its first push also has total_hint == 1
            # and was being misread as a continuation — see load_from_payload
            # in visitvisa.py).
            "is_new_batch": is_new_batch,
            "credentials": credentials,
            "applicants":  [applicant],
            # Present (and required) only for remote exe instances — the local
            # same-machine exe keeps trusting loopback-only reachability as
            # its authentication, same as before this change.
            "secret": secret or "",
        }, ensure_ascii=False).encode("utf-8")
        framed_payload = _struct.pack("!I", len(payload)) + payload

        try:
            sock = _socket.create_connection((host, port), timeout=10)
            sock.sendall(framed_payload)
            ack = b""
            sock.settimeout(15)
            while len(ack) < 12:
                part = sock.recv(64)
                if not part:
                    break
                ack += part
            sock.close()
            if not ack:
                # Connection accepted but no ack — this is exactly what a
                # secret-rejected push looks like from the sender's side
                # (the exe closes the connection immediately after
                # checking the secret, without acking). Re-resolving next
                # attempt gives a freshly-registered secret a chance to
                # be picked up instead of retrying the same wrong one.
                last_error = "connection accepted but no acknowledgment received (possible auth rejection)"
                print(f"[visa_queue] Push attempt {attempt + 1} got no ack from {host}:{port} — will re-resolve target and retry")
                _time.sleep(1)
                continue
            print(f"[visa_queue] Push succeeded on attempt {attempt + 1} for passport id={applicant.get('id')}, ack={ack}")
            if secret:
                touch_visit_visa_exe_registration(user_id)
            return True, None
        except Exception as e:
            last_error = str(e)
            print(f"[visa_queue] Push attempt {attempt + 1} failed: {e}")
            _time.sleep(1)

    return False, f"passposys.exe is open but data could not be sent after 3 attempts: {last_error}"


@app.route("/api/register_visa_exe", methods=["POST"])
def register_visa_exe():
    """
    Called by passposys.exe on startup so it can run on a PC other than
    the one hosting Flask. Body: { user_id, exe_host, exe_port, token }.

    Auth: token must equal LOCAL_API_TOKEN (same shared secret the exe
    already needs to fetch the automation script) — this endpoint is not
    session-authenticated since the exe has no browser session of its own.

    exe_host is supplied by the exe itself (it should report its own LAN
    IP — e.g. via socket.gethostbyname(socket.gethostname()) — since Flask
    cannot reliably infer it from this request alone). A random per-
    registration secret is generated here and returned to the exe; the
    exe must echo it back on the VisaSocketServer for every push it
    accepts, so its now-network-reachable socket isn't an open door to
    anyone who can merely reach the port.
    """
    import secrets as _secrets

    data = request.json or {}
    token = data.get("token", "")
    if token != LOCAL_API_TOKEN:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    user_id = data.get("user_id")
    exe_host = (data.get("exe_host") or "").strip()
    exe_port = data.get("exe_port")
    if not user_id or not exe_host or not exe_port:
        return jsonify({"success": False, "error": "user_id, exe_host and exe_port are required"}), 400

    exe_secret = _secrets.token_hex(24)
    try:
        register_visit_visa_exe(int(user_id), exe_host, int(exe_port), exe_secret)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    return jsonify({"success": True, "secret": exe_secret})


@app.route("/api/unregister_visa_exe", methods=["POST"])
def unregister_visa_exe():
    """Called by passposys.exe on clean shutdown so send_visit_visa falls
    back to the local-exe path again instead of targeting a now-closed
    remote instance. Best-effort — if the exe crashes instead of closing
    cleanly, the stale row is harmless: the next push attempt will simply
    fail with a clear 'not reachable' error rather than silently going to
    the wrong PC, and the operator can re-register by restarting the exe."""
    data = request.json or {}
    token = data.get("token", "")
    if token != LOCAL_API_TOKEN:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"success": False, "error": "user_id is required"}), 400

    try:
        delete_visit_visa_exe_registration(int(user_id))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    return jsonify({"success": True})


@app.route("/api/send_visit_visa", methods=["POST"])
@login_required
def send_visit_visa():
    """
    Sends the selected applicants to passposys.exe ONE AT A TIME:
      1. Builds full applicant payload (adults first, minors after).
      2. Persists the whole batch to visit_visa_queue (MySQL — visible to
         all gunicorn workers, since the exe calls back on a different
         request/worker than this one).
      3. Pushes ONLY the first applicant to the exe over the socket.
      4. Each subsequent applicant is pushed by mark_processed_single,
         once the exe confirms the previous one is fully done (success
         or skipped) — see pop_next_visit_visa_queue_item().
    No file is written to disk at any point.
    """
    import uuid as _uuid

    data         = request.json or {}
    selected_ids = data.get("selected_ids", [])
    credentials  = data.get("credentials", {})
    user_id      = session["user_id"]

    if not selected_ids:
        return jsonify({"status": "error", "message": "No IDs provided"}), 400

    # ── Visit Visa group-size cap ───────────────────────────────────────────
    # Mirrors the Nusuk 50-cap in download_automation_file: the UI already
    # blocks selections over 25 in results.html (Send button disabled +
    # "Create Group for Remaining" flow), but re-enforce it here too since
    # this endpoint can be called directly.
    VISA_GROUP_LIMIT = 25
    if len(selected_ids) > VISA_GROUP_LIMIT:
        return jsonify({
            "status": "error",
            "message": f"Only {VISA_GROUP_LIMIT} passports are allowed per Visit Visa group. "
                       f"{len(selected_ids) - VISA_GROUP_LIMIT} passport(s) must be moved to a new "
                       f"group before sending.",
            "group_limit": VISA_GROUP_LIMIT,
            "overflow_count": len(selected_ids) - VISA_GROUP_LIMIT
        }), 400

    # ── 1. Build applicant list ───────────────────────────────────────────────
    applicants = []
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    today  = ist_now().date()

    try:
        cursor.execute("USE passport_db")
        for pid in selected_ids:
            try:
                pid_int = int(pid)
            except (ValueError, TypeError):
                continue
            cursor.execute("""
                SELECT p.id, p.filename, p.surname, p.given_names, p.middle_name,
                       p.sex, p.dob, p.passport_number, p.expiry,
                       g.nationality_id, g.passport_issue_place, g.passport_issue_date,
                       g.city_of_birth, g.profession,
                       g.group_name, g.hotel_name, g.passport_type,
                       g.expected_arrival, g.expected_departure,
                       g.contact_number, g.email,
                       g.marital_status, g.city, g.zip_postal_code, g.address
                FROM passports p
                LEFT JOIN general_data g ON g.passport_id = p.id
                WHERE p.id = %s AND p.user_id = %s
            """, (pid_int, user_id))
            row = cursor.fetchone()
            if not row:
                continue

            face_b64 = ""
            if row.get("filename"):
                _, face_path = resolve_passport_paths(row['filename'], row.get('group_name'))
                if face_path and os.path.exists(face_path):
                    try:
                        with open(face_path, 'rb') as _f:
                            face_b64 = base64.b64encode(_f.read()).decode("utf-8")
                    except Exception:
                        face_b64 = ""

            def ds(val):
                return str(val) if val else ""

            dob      = row.get("dob")
            is_adult = False
            if dob:
                try:
                    age      = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
                    is_adult = age >= 18
                except Exception:
                    pass

            applicants.append({
                "id":                   pid_int,
                "group_name":           row.get("group_name") or "",
                "face_image":           face_b64,
                "filename":             f"photo_{pid_int}.jpg",
                "nationality_id":       row.get("nationality_id") or "",
                "surname":              row.get("surname") or "",
                "given_names":          row.get("given_names") or "",
                "middle_name":          row.get("middle_name") or "",
                "sex":                  row.get("sex") or "",
                "marital_status":       row.get("marital_status") or "",
                "dob":                  ds(row.get("dob")),
                "city_of_birth":        row.get("city_of_birth") or "",
                "profession":           row.get("profession") or "",
                "city":                 row.get("city") or "",
                "zip_postal_code":      row.get("zip_postal_code") or "",
                "address":              row.get("address") or "",
                "passport_type":        row.get("passport_type") or 1,
                "passport_number":      row.get("passport_number") or "",
                "passport_issue_place": row.get("passport_issue_place") or "",
                "passport_issue_date":  ds(row.get("passport_issue_date")),
                "expiry":               ds(row.get("expiry")),
                "expected_arrival":     ds(row.get("expected_arrival")),
                "expected_departure":   ds(row.get("expected_departure")),
                "hotel_name":           row.get("hotel_name") or "",
                "contact_number":       row.get("contact_number") or "",
                "email":                row.get("email") or "",
                "__is_adult":           is_adult,
                "_issue_date_raw":      row.get("passport_issue_date"),
                "_dob_raw":             row.get("dob"),
            })
    finally:
        cursor.close()
        conn.close()

    if not applicants:
        return jsonify({"status": "error", "message": "No valid records found"}), 404

    # ── Reject records that only have ONE of given_names / surname ────────────
    # (i.e. exactly one is blank). Records with both filled, or both blank,
    # are allowed through here; this only blocks the "half a name" case.
    incomplete = [
        a for a in applicants
        if bool((a.get("surname") or "").strip()) != bool((a.get("given_names") or "").strip())
    ]
    if incomplete:
        incomplete_records = [
            {
                "id":               a["id"],
                "passport_number":  a.get("passport_number") or "—",
                "name":             (a.get("surname") or a.get("given_names") or "").strip(),
            }
            for a in incomplete
        ]
        return jsonify({
            "status": "error",
            "message": (
                f"{len(incomplete_records)} selected record(s) have only a given name "
                "or only a surname (not both) and cannot be sent."
            ),
            "incomplete_records": incomplete_records,
        }), 400

    # ── Reject records missing an issue date, or with an issue date that is
    # in the future or before the applicant's date of birth ──────────────────
    def _bad_issue_date(a):
        issue = a.get("_issue_date_raw")
        if not issue:
            return "missing"
        if issue > today:
            return "future"
        dob_val = a.get("_dob_raw")
        if dob_val and issue < dob_val:
            return "before_dob"
        return None

    issue_date_problems = [(a, _bad_issue_date(a)) for a in applicants]
    issue_date_problems = [(a, r) for a, r in issue_date_problems if r]

    if issue_date_problems:
        reason_messages = {
            "missing":    "missing an issue date",
            "future":     "an issue date after today",
            "before_dob": "an issue date before the applicant's date of birth",
        }
        incomplete_records = [
            {
                "id":               a["id"],
                "passport_number":  a.get("passport_number") or "—",
                "name":             (f"{a.get('surname','')} {a.get('given_names','')}").strip() or "Unnamed",
                "reason":           reason_messages[reason],
            }
            for a, reason in issue_date_problems
        ]
        counts = {}
        for _, reason in issue_date_problems:
            counts[reason] = counts.get(reason, 0) + 1
        summary_parts = [f"{n} {reason_messages[r]}" for r, n in counts.items()]
        return jsonify({
            "status": "error",
            "message": (
                f"{len(incomplete_records)} selected record(s) cannot be sent: "
                + "; ".join(summary_parts) + "."
            ),
            "incomplete_records": incomplete_records,
        }), 400

    # Drop the internal raw-date helper keys now that validation is done —
    # they were only added for the checks above and aren't part of the
    # payload sent on to passposys.exe.
    for a in applicants:
        a.pop("_issue_date_raw", None)
        a.pop("_dob_raw", None)

    # Adults first, minors after
    applicants.sort(key=lambda x: x.pop("__is_adult", False), reverse=True)

    # Record group batch and bump created_at so this group sorts to the top
    try:
        group_name_for_batch = applicants[0].get("group_name", "").strip() if applicants else ""
        login_email = (credentials.get("username") or "").strip()
        if group_name_for_batch:
            upsert_group_batch(user_id, group_name_for_batch, login_email)
            # Stamp all passports in this send with NOW() so the group surfaces
            # at the top of every dropdown/list (ordering uses MAX(p.created_at) DESC)
            _vv_ids = [int(pid) for pid in selected_ids if str(pid).isdigit()]
            if _vv_ids:
                _vv_conn = get_connection()
                _vv_cur  = _vv_conn.cursor()
                _vv_cur.execute("USE passport_db")
                _vv_fmt  = ','.join(['%s'] * len(_vv_ids))
                _vv_cur.execute(
                    f"UPDATE passports SET created_at = NOW() WHERE id IN ({_vv_fmt}) AND user_id = %s",
                    tuple(_vv_ids) + (user_id,)
                )
                _vv_conn.commit()
                _vv_cur.close()
                _vv_conn.close()
    except Exception as e:
        print(f"[send_visit_visa] Could not save group batch: {e}")

    # ── Persist the full batch to MySQL so mark_processed_single (called by
    #    the exe, possibly on a different gunicorn worker) can advance it ──
    batch_id = str(_uuid.uuid4())
    try:
        create_visit_visa_queue(user_id, batch_id, applicants, credentials)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Could not persist send queue: {e}"}), 500

    # ── Push ONLY the first applicant now; the rest follow one at a time
    #    from mark_processed_single as each prior one is confirmed done ──
    ok, err = _push_visa_applicant_to_exe(
        user_id, applicants[0], credentials,
        total_hint=len(applicants), is_new_batch=True
    )
    if not ok:
        return jsonify({"status": "error", "message": err}), 500

    return jsonify({"status": "ok", "count": len(applicants), "batch_id": batch_id})


@app.route("/api/visa_processed_status", methods=["POST"])
@login_required
def visa_processed_status():
    """Poll which passport IDs have been marked visa-processed by passposys.exe."""
    data = request.json or {}
    ids  = data.get("ids", [])
    if not ids:
        return jsonify({"done_ids": []})
    try:
        conn   = get_connection()
        cursor = conn.cursor()
        cursor.execute("USE passport_db")
        fmt = ','.join(['%s'] * len(ids))
        cursor.execute(
            f"""SELECT id FROM passports
                WHERE id IN ({fmt}) AND user_id = %s AND is_visa_processed = TRUE
                ORDER BY visa_processed_at ASC, id ASC""",
            tuple(ids) + (session["user_id"],)
        )
        done_ids = [row[0] for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return jsonify({"done_ids": done_ids})
    except Exception as e:
        return jsonify({"done_ids": [], "error": str(e)}), 500


@app.route("/api/mofa_visa/trigger", methods=["POST"])
@login_required
def mofa_visa_trigger():
    """Kick off a one-off MOFA visa-PDF download for a single passport,
    used by the results-page 'Check Visa' badge. Starts the download in a
    background thread and returns immediately -- the frontend then polls
    /api/mofa_visa/status until mofa_pdf_downloaded_at is set (or the run
    finishes without success, in which case it just stays unset and the
    badge reverts to the clickable 'Check Visa' state)."""
    data        = request.json or {}
    passport_id = data.get("id")
    if not passport_id:
        return jsonify({"status": "error", "message": "Missing passport id"}), 400

    try:
        conn   = get_connection()
        cursor = conn.cursor()
        cursor.execute("USE passport_db")
        cursor.execute(
            "SELECT id FROM passports WHERE id = %s AND user_id = %s",
            (passport_id, session["user_id"])
        )
        owned = cursor.fetchone()
        cursor.close()
        conn.close()
        if not owned:
            return jsonify({"status": "error", "message": "Not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    result = _trigger_mofa_single_download(passport_id, user_id=session["user_id"])
    return jsonify({"status": "ok", "started": result["started"], "job_id": result["job_id"]})


@app.route("/api/mofa_visa/trigger_group", methods=["POST"])
@login_required
def mofa_visa_trigger_group():
    """Trigger a one-off MOFA download for every passport in the given
    group(s) that doesn't have its visa PDF yet. Used by the groups-page
    eVisa popup's 'Check for remaining visa' option. Runs all pending
    passports through ONE shared Playwright browser session (via
    trigger_batch_download, same as the results-page eVisa batch button)
    so CAPTCHA is solved once and the remaining records reuse that
    session -- and returns immediately; the frontend polls
    /api/mofa_visa/group_status until the groups move from 'remaining'
    to 'available'."""
    data = request.json or {}
    group_names = data.get("group_names", [])
    if not group_names:
        return jsonify({"status": "error", "message": "No groups specified."}), 400

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")
    fmt = ','.join(['%s'] * len(group_names))
    cursor.execute(f"""
        SELECT p.id
        FROM passports p
        JOIN general_data g ON g.passport_id = p.id
        WHERE g.group_name IN ({fmt}) AND p.user_id = %s
          AND p.mofa_pdf_downloaded_at IS NULL
    """, tuple(group_names) + (session["user_id"],))
    passport_ids = [row["id"] for row in cursor.fetchall()]
    cursor.close()
    conn.close()

    if not passport_ids:
        return jsonify({"status": "ok", "triggered": False, "total_pending": 0})

    result = _trigger_mofa_batch_download(passport_ids, user_id=session["user_id"])
    return jsonify({
        "status": "ok",
        "triggered": result["started"],
        "job_id": result["job_id"],
        "total_pending": len(passport_ids),
    })

@app.route("/api/mofa_visa/trigger_batch", methods=["POST"])
@login_required
def mofa_visa_trigger_batch():
    """Run all given passport IDs through ONE shared Playwright browser session.
    CAPTCHA is solved only for the first record; remaining records reuse the
    same session/cookies so no repeat CAPTCHA challenge appears.
    Used by the results-page eVisa button when 2+ passports are pending."""
    data = request.json or {}
    passport_ids = data.get("ids", [])
    if not passport_ids:
        return jsonify({"status": "error", "message": "No passport ids provided."}), 400

    # Verify all ids belong to the current user before triggering
    try:
        conn   = get_connection()
        cursor = conn.cursor()
        cursor.execute("USE passport_db")
        fmt = ','.join(['%s'] * len(passport_ids))
        cursor.execute(
            f"SELECT id FROM passports WHERE id IN ({fmt}) AND user_id = %s",
            tuple(passport_ids) + (session["user_id"],)
        )
        owned_ids = [row[0] for row in cursor.fetchall()]
        cursor.close()
        conn.close()
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    if not owned_ids:
        return jsonify({"status": "error", "message": "Not found"}), 404

    result = _trigger_mofa_batch_download(owned_ids, user_id=session["user_id"])
    return jsonify({
        "status": "ok",
        "started": result["started"],
        "job_id": result["job_id"],
        "count": len(owned_ids),
    })

@app.route("/api/mofa_visa/group_status", methods=["POST"])
@login_required
def mofa_visa_group_status():
    """For each given group name, report:
      - whether its visa_processed folder has at least one PDF ready to
        download ('available') or none yet ('remaining')
      - pending_counts[group]: how many of that group's passports still
        have no visa PDF (mofa_pdf_downloaded_at IS NULL). A group can be
        in 'available' (>=1 PDF on disk) while still having a nonzero
        pending count if it's only partially processed -- pending_counts
        is what distinguishes "fully ready" from "partially ready" for
        the groups-page eVisa popup (available/remaining alone can't).
    Used by the groups-page eVisa button popup to show 'Download Ready
    Group(s) (N)' / 'Check Remaining (N)' before the user commits."""
    data = request.json or {}
    group_names = data.get("group_names", [])
    if not group_names:
        return jsonify({"available": [], "remaining": [], "pending_counts": {}})

    available = []
    remaining = []
    for grp in group_names:
        try:
            pdfs = list_visa_processed_pdfs(grp)
        except Exception:
            pdfs = []
        if pdfs:
            available.append(grp)
        else:
            remaining.append(grp)

    pending_counts = {grp: 0 for grp in group_names}
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("USE passport_db")
        fmt = ','.join(['%s'] * len(group_names))
        cursor.execute(f"""
            SELECT g.group_name, COUNT(*) AS pending
            FROM passports p
            JOIN general_data g ON g.passport_id = p.id
            WHERE g.group_name IN ({fmt}) AND p.user_id = %s
              AND p.mofa_pdf_downloaded_at IS NULL
            GROUP BY g.group_name
        """, tuple(group_names) + (session["user_id"],))
        for row in cursor.fetchall():
            pending_counts[row["group_name"]] = row["pending"]
        cursor.close()
        conn.close()
    except Exception:
        # If the count query fails, fall back to treating any group with no
        # PDFs on disk as fully pending (better to over-count than silently
        # report a group as fully ready when we couldn't actually verify it).
        for grp in remaining:
            pending_counts[grp] = pending_counts.get(grp) or 1

    return jsonify({
        "available": available,
        "remaining": remaining,
        "pending_counts": pending_counts,
    })


@app.route("/api/mofa_visa/job_status", methods=["POST"])
@login_required
def mofa_visa_job_status():
    """Poll live per-passport progress for a batch/single job started by
    trigger_group / trigger_batch / trigger (each of those returns a job_id).
    Returns {"found": bool, "total": int, "done": int, "failed": int,
    "finished": bool, "last_error": str|None}. Used by the groups-page eVisa
    popup so it can show real 'X of N passports downloaded' counts instead
    of only a per-group has-any-pdf-yet signal, and last_error surfaces a
    concrete reason (e.g. "MOFA site is unreachable") when failed > 0
    instead of leaving the UI with only a silent count."""
    data   = request.json or {}
    job_id = data.get("job_id")
    if not job_id:
        return jsonify({"status": "error", "message": "Missing job_id"}), 400

    progress = _mofa_get_job_progress(job_id)
    if progress is None:
        return jsonify({"found": False})

    return jsonify({
        "found": True,
        "total": progress["total"],
        "done": progress["done"],
        "failed": progress["failed"],
        "finished": progress["finished"],
        "last_error": progress.get("last_error"),
    })


@app.route("/api/mofa_visa/status", methods=["POST"])
@login_required
def mofa_visa_status():
    """Poll whether MOFA has finished downloading the visa PDF (i.e.
    mofa_pdf_downloaded_at is set) for the given passport ids. Used both
    for the single-record 'Check Visa' trigger and, generally, to reflect
    the true 'visa available' state on the results page (distinct from
    is_visa_processed/is_processed, which only mean 'queued')."""
    data = request.json or {}
    ids  = data.get("ids", [])
    if not ids:
        return jsonify({"done_ids": []})
    try:
        conn   = get_connection()
        cursor = conn.cursor()
        cursor.execute("USE passport_db")
        fmt = ','.join(['%s'] * len(ids))
        cursor.execute(
            f"""SELECT id FROM passports
                WHERE id IN ({fmt}) AND user_id = %s AND mofa_pdf_downloaded_at IS NOT NULL
                ORDER BY mofa_pdf_downloaded_at ASC, id ASC""",
            tuple(ids) + (session["user_id"],)
        )
        done_ids = [row[0] for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return jsonify({"done_ids": done_ids})
    except Exception as e:
        return jsonify({"done_ids": [], "error": str(e)}), 500


@app.route("/api/mofa_visa/download/<int:passport_id>")
@login_required
def mofa_visa_download_single(passport_id):
    """Download the MOFA visa PDF for a single passport (the file the
    'Visa Available' badge links to once mofa_pdf_downloaded_at is set).
    404s with a JSON message if the DB says downloaded but the file isn't
    actually on disk -- that state also gets self-healed here by clearing
    mofa_pdf_downloaded_at, same as the results()/reconcile_mofa_pdf_downloads()
    checks, so the badge reverts to 'Check Visa' on the next page load."""
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")
    cursor.execute("""
        SELECT p.id, p.passport_number, g.group_name
        FROM passports p
        JOIN general_data g ON g.passport_id = p.id
        WHERE p.id = %s AND p.user_id = %s
    """, (passport_id, session["user_id"]))
    row = cursor.fetchone()
    cursor.close()

    if not row:
        conn.close()
        return jsonify({"success": False, "message": "Not found."}), 404

    pdf_path = get_visa_pdf_path(row["group_name"], row["passport_number"])
    if not os.path.exists(pdf_path):
        upd = conn.cursor()
        upd.execute("UPDATE passports SET mofa_pdf_downloaded_at = NULL WHERE id = %s", (passport_id,))
        conn.commit()
        upd.close()
        conn.close()
        return jsonify({"success": False, "message": "Visa PDF not found on disk."}), 404

    conn.close()
    safe_name = f"{row['passport_number']}_visa.pdf"
    return send_file(pdf_path, mimetype="application/pdf", as_attachment=True, download_name=safe_name)


@app.route("/api/mark_processed_single", methods=["POST"])
def mark_processed_single():
    data        = request.json or {}
    passport_id = data.get("id")
    platform    = data.get("platform", "nusuk")
    token       = data.get("token", "")
    success     = data.get("success", True)
    fetch_next  = data.get("fetch_next", False)

    is_local_exe = (token == LOCAL_API_TOKEN)
    if not is_local_exe and "user_id" not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    if not passport_id:
        return jsonify({"success": False}), 400

    try:
        conn   = get_connection()
        cursor = conn.cursor()
        cursor.execute("USE passport_db")
        if platform == "visa":
            if success:
                if is_local_exe:
                    cursor.execute("UPDATE passports SET is_visa_processed = TRUE, visa_processed_at = NOW() WHERE id = %s", (passport_id,))
                else:
                    cursor.execute("UPDATE passports SET is_visa_processed = TRUE, visa_processed_at = NOW() WHERE id = %s AND user_id = %s", (passport_id, session["user_id"]))
        else:
            if success:
                if is_local_exe:
                    cursor.execute("UPDATE passports SET is_processed = TRUE, processed_at = NOW() WHERE id = %s", (passport_id,))
                else:
                    cursor.execute("UPDATE passports SET is_processed = TRUE, processed_at = NOW() WHERE id = %s AND user_id = %s", (passport_id, session["user_id"]))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    # ── Advance the Nusuk Queue ────────────────────────
    if platform == "nusuk" and fetch_next:
        try:
            queue_user_id = session["user_id"] if not is_local_exe and "user_id" in session \
                else get_nusuk_queue_user(passport_id)

            if queue_user_id is not None:
                mark_nusuk_queue_item_finished(queue_user_id, passport_id, success=bool(success))
                next_applicant, next_credentials, _batch_id = pop_next_nusuk_queue_item(
                    queue_user_id, passport_id
                )
                
                remaining = 0
                if _batch_id:
                    conn = get_connection()
                    cursor = conn.cursor()
                    cursor.execute("USE passport_db")
                    cursor.execute("SELECT COUNT(*) FROM nusuk_queue WHERE user_id=%s AND batch_id=%s AND status='pending'", (queue_user_id, _batch_id))
                    remaining = cursor.fetchone()[0]
                    cursor.close()
                    conn.close()

                return jsonify({
                    "success": True, 
                    "next_applicant": next_applicant, 
                    "remaining": remaining + (1 if next_applicant else 0)
                })
        except Exception as e:
            print(f"[nusuk_queue] Queue advance error after id={passport_id}: {e}")

    # ── Advance the Visit Visa Queue ────────────────────────
    elif platform == "visa":
        try:
            queue_user_id = session["user_id"] if not is_local_exe and "user_id" in session \
                else get_visit_visa_queue_user(passport_id)

            if queue_user_id is not None:
                mark_visit_visa_queue_item_finished(queue_user_id, passport_id, success=bool(success))
                next_applicant, next_credentials, _batch_id = pop_next_visit_visa_queue_item(
                    queue_user_id, passport_id
                )
                if next_applicant is not None:
                    ok, err = _push_visa_applicant_to_exe(queue_user_id, next_applicant, next_credentials)
                    if not ok:
                        print(f"[visa_queue] Failed to push next applicant after id={passport_id}: {err}")
        except Exception as e:
            print(f"[visa_queue] Queue advance error after id={passport_id}: {e}")

    return jsonify({"success": True})

@app.route("/api/clear_nusuk_queue", methods=["POST"])
def clear_nusuk_queue():
    data = request.json or {}
    token = data.get("token", "")

    is_local_exe = (token == LOCAL_API_TOKEN)

    # Authorize either via the extension token or an active browser session
    if not is_local_exe and "user_id" not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    try:
        # Prefer the logged-in session's user_id when we have one. When the
        # call comes from the extension/local exe (token-authenticated, no
        # Flask session -- e.g. the results.html tab was closed), resolve
        # the owner from whoever currently has an active nusuk_queue instead
        # of silently doing nothing, which used to leave stale rows behind
        # and could stall automation waiting on totalRemaining to hit 0.
        user_id = session.get("user_id")
        if user_id is None and is_local_exe:
            user_id = get_active_nusuk_queue_user()

        if user_id is not None:
            deleted = clear_nusuk_queue_for_user(user_id)
            return jsonify({"success": True, "deleted": deleted})

        # No session AND no active queue rows to resolve an owner from --
        # nothing to clear, which is a legitimate (not an error) outcome.
        return jsonify({"success": True, "deleted": 0})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    
@app.route("/api/clear_visit_visa_queue", methods=["POST"])
def clear_visit_visa_queue():
    """
    Called by passposys.exe when it closes, to delete every visit_visa_queue
    row (any batch, any status) for the owning user. Safe to call any time —
    this table is only a hand-off buffer between requests and is never read
    for tick marks/results (those live on the passports table).

    Body: { id (a passport_id the exe last touched), token }
    """
    data        = request.json or {}
    passport_id = data.get("id")
    token       = data.get("token", "")

    if token != LOCAL_API_TOKEN:
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    if not passport_id:
        return jsonify({"success": False}), 400

    try:
        user_id = get_visit_visa_queue_user_any_status(passport_id)
        if user_id is None:
            return jsonify({"success": True, "deleted": 0})
        deleted = clear_visit_visa_queue_for_user(user_id)
        return jsonify({"success": True, "deleted": deleted})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/nusuk_automation_script", methods=["GET"])
def nusuk_automation_script():
    allowed_origins = {"https://masar.nusuk.sa", "https://visa.mofa.gov.sa"}
    origin = request.headers.get("Origin", "")

    token = request.args.get("token", "")
    if token != LOCAL_API_TOKEN:
        resp = make_response("Unauthorized", 401)
        if origin in allowed_origins:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Vary"] = "Origin"
        return resp

    try:
        resp = send_file(
            _resource_path("private/nusuk_automation.js"),
            mimetype="application/javascript",
        )
    except Exception as e:
        resp = make_response(f"// script unavailable: {e}", 500)

    if origin in allowed_origins:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    return resp


@app.route("/delete_all_invalid", methods=["POST"])
@login_required
def delete_all_invalid():
    redirect_after = request.form.get('redirect_after', 'view_invalid_passports')
    delete_type = request.form.get('delete_type', 'all')  # all, invalid, duplicate
    user_id = session['user_id']
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("USE passport_db")
        
        if delete_type == 'invalid':
            # Delete only non-duplicate invalid records
            cursor.execute("""
            UPDATE invalid_passports
            SET is_recycled = TRUE, created_at = CURRENT_TIMESTAMP
            WHERE user_id = %s AND (is_recycled = FALSE OR is_recycled IS NULL)
            AND error_message NOT LIKE '%Duplicate passport number%'
            """, (user_id,))
        elif delete_type == 'duplicate':
            # Delete only duplicate records
            cursor.execute("""
            UPDATE invalid_passports
            SET is_recycled = TRUE, created_at = CURRENT_TIMESTAMP
            WHERE user_id = %s AND (is_recycled = FALSE OR is_recycled IS NULL)
            AND error_message LIKE '%Duplicate passport number%'
            """, (user_id,))
        else:  # delete_type == 'all'
            # Delete all invalid & duplicate records
            cursor.execute("""
            UPDATE invalid_passports
            SET is_recycled = TRUE, created_at = CURRENT_TIMESTAMP
            WHERE user_id = %s AND (is_recycled = FALSE OR is_recycled IS NULL)
            """, (user_id,))
        
        deleted_count = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()
        
        if delete_type == 'invalid':
            flash(f'✅ {deleted_count} invalid record(s) moved to the Recycle Bin!', 'success')
        elif delete_type == 'duplicate':
            flash(f'✅ {deleted_count} duplicate record(s) moved to the Recycle Bin!', 'success')
        else:
            flash('✅ All invalid & duplicate records moved to the Recycle Bin!', 'success')
        
    except Exception as e:
        flash(f'❌ Error: {str(e)}', 'danger')
    
    if redirect_after == 'results':
        return redirect(url_for('results'))
    return redirect(url_for('view_invalid_passports'))


@app.route("/static/downloads/<filename>")
@login_required
def download_automator(filename):
    allowed = {'passposys.exe', 'passposys_Mac.zip'}
    if filename not in allowed:
        return "Not found", 404
    return send_file(
        os.path.join('static', 'downloads', filename),
        as_attachment=True,
        download_name=filename
    )

from flask import send_from_directory

@app.route('/usage')
@login_required
def usage():
    stats = session.get('latest_api_stats') or {}

    # Determine primary provider per phase from the counter values
    # Phase 2 (Scanning 1): ProvB stacked batch primary; ProvA batch = fallback
    # Phase 3 (Scanning 2): ProvB stacked batch primary; ProvA batch = fallback
    # Phase 4 (Scanning 3): ProvA individual primary; ProvB individual = fallback
    # Per-phase counters (new keys, fall back to 0 if old session data)
    p2_provB = stats.get('phase2_provB_calls', 0)
    p2_provA  = stats.get('phase2_provA_calls',  0)
    p3_provB = stats.get('phase3_provB_calls', 0)
    p3_provA  = stats.get('phase3_provA_calls',  0)
    provA_indiv  = stats.get('provA_individual_units', 0)

    # Per-phase file counts (how many images went through each step)
    p2_files = stats.get('phase2_file_count', 0)
    p3_files = stats.get('phase3_file_count', 0)

    # Provider label helper
    def _prov_label(primary_calls, fallback_calls, primary_name, fallback_name):
        if primary_calls > 0 and fallback_calls > 0:
            return f"{primary_name} + {fallback_name} (fallback)"
        if primary_calls > 0:
            return primary_name
        if fallback_calls > 0:
            return fallback_name
        return "—"

    def _marker(provB, provA):
        m = ""
        if provB > 0: m += "G"
        if provA  > 0: m += "A"
        return m

    # Phase 2: ProvA batch (primary), ProvB stacked (fallback)
    p2_api_calls = p2_provB + p2_provA
    p2_provider  = _prov_label(p2_provA, p2_provB, "Az", "Vi")
    p2_marker    = _marker(p2_provB, p2_provA)

    # Phase 3: ProvB stacked (primary), ProvA batch (fallback)
    p3_api_calls = p3_provB + p3_provA
    p3_provider  = _prov_label(p3_provB, p3_provA, "Vi", "Az")
    p3_marker    = _marker(p3_provB, p3_provA)

    # Phase 4: ProvA individual (primary), ProvB individual fallback
    p4_provider = "Az" if provA_indiv > 0 else "—"
    p4_marker   = "A" if provA_indiv > 0 else ""

    # Total API calls
    total_calls = p2_api_calls + p3_api_calls + provA_indiv

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>API Usage — Last Scan</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      margin: 0; padding: 0;
      font-family: 'Segoe UI', system-ui, sans-serif;
      background: #0f1117;
      color: #e2e8f0;
      min-height: 100vh;
      display: flex; align-items: flex-start; justify-content: center;
      padding: 48px 16px;
    }}
    .card {{
      background: #1a1f2e;
      border: 1px solid #2d3748;
      border-radius: 16px;
      padding: 36px 40px;
      width: 100%; max-width: 560px;
      box-shadow: 0 8px 32px rgba(0,0,0,.45);
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: 1.35rem;
      font-weight: 700;
      color: #f7fafc;
      letter-spacing: -.3px;
    }}
    .subtitle {{
      font-size: .82rem;
      color: #718096;
      margin: 0 0 32px;
    }}
    .row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 14px 16px;
      border-radius: 10px;
      margin-bottom: 10px;
      background: #242938;
      border: 1px solid #2d3748;
      gap: 12px;
    }}
    .row:last-of-type {{ margin-bottom: 0; }}
    .label {{
      font-size: .9rem;
      color: #a0aec0;
      font-weight: 500;
      min-width: 110px;
    }}
    .count {{
      font-size: 1.5rem;
      font-weight: 700;
      color: #f7fafc;
      min-width: 40px;
      text-align: center;
    }}
    .provider {{
      font-size: .78rem;
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 3px 10px;
      border-radius: 20px;
      font-size: .72rem;
      font-weight: 600;
      letter-spacing: .3px;
      white-space: nowrap;
    }}
    .badge-g  {{ background: #1a4731; color: #68d391; border: 1px solid #276749; }}
    .badge-a  {{ background: #1a3650; color: #63b3ed; border: 1px solid #2b6cb0; }}
    .badge-ga {{ background: #2d2b1a; color: #f6e05e; border: 1px solid #744210; }}
    .badge-na {{ background: #1e2130; color: #718096; border: 1px solid #2d3748; }}
    .divider {{ border: none; border-top: 1px solid #2d3748; margin: 20px 0; }}
    .total-row {{
      display: flex; justify-content: space-between; align-items: center;
      padding: 10px 4px 0;
    }}
    .total-label {{ font-size: .85rem; color: #718096; }}
    .total-val   {{ font-size: 1.1rem; font-weight: 700; color: #f7fafc; }}
    .no-data {{
      text-align: center;
      padding: 40px 0;
      color: #4a5568;
      font-size: .95rem;
    }}
    .back-link {{
      display: inline-block;
      margin-top: 28px;
      font-size: .82rem;
      color: #63b3ed;
      text-decoration: none;
      opacity: .8;
    }}
    .back-link:hover {{ opacity: 1; }}
  </style>
</head>
<body>
<div class="card">
  <h1>API Usage</h1>
  <p class="subtitle">Last scanned batch only</p>
"""

    if not stats:
        html += '<div class="no-data">No scan data available yet.<br>Upload passports to see API usage.</div>'
    else:
        def _badge(marker, provider):
            if not marker or provider == "—":
                return '<span class="badge badge-na">—</span>'
            if "G" in marker and "A" in marker:
                return '<span class="badge badge-ga">Vi + Az</span>'
            if "G" in marker:
                return '<span class="badge badge-g">Vi</span>'
            return '<span class="badge badge-a">Az</span>'

        # (file_count, api_calls, marker, phase_note)
        all_rows = [
            ("Scanning 1", p2_files, p2_api_calls, p2_marker, p2_provider, "Phase 2 — Scanning"),
            ("Scanning 2", p3_files, p3_api_calls, p3_marker, p3_provider, "Phase 3 — Extracting"),
            ("Scanning 3", provA_indiv, provA_indiv,      p4_marker, p4_provider, "Phase 4 — Reviewing"),
        ]
        # Only display steps that actually made API calls in this batch
        rows = [(l, fc, ac, m, p, n) for l, fc, ac, m, p, n in all_rows if ac > 0]

        # Batch summary for subtitle
        batch_files  = stats.get('processed', 0) + stats.get('duplicates', 0) + stats.get('invalid', 0)
        steps_used   = len(rows)
        step_word    = "step" if steps_used == 1 else "steps"
        file_word    = "file" if batch_files == 1 else "files"
        summary_line = f"{batch_files} {file_word} &nbsp;·&nbsp; {steps_used} {step_word} used"

        html += f'<p class="subtitle">{summary_line}</p>\n'

        for label, file_count, api_calls, marker, provider, phase_note in rows:
            badge_html  = _badge(marker, provider)
            calls_note  = f'<span style="color:#4a5568;font-size:.7rem">{api_calls} call{"s" if api_calls != 1 else ""}</span>'
            html += f"""
  <div class="row">
    <span class="label">{label}</span>
    <span class="count">{file_count}</span>
    <span class="provider">{badge_html}{calls_note}<span style="color:#4a5568;font-size:.7rem">{phase_note}</span></span>
  </div>"""

        html += f"""
  <hr class="divider">
  <div class="total-row">
    <span class="total-label">Total files scanned</span>
    <span class="total-val">{p2_files}</span>
  </div>
  <div class="total-row" style="padding-top:6px;">
    <span class="total-label" style="font-size:.8rem;color:#4a5568;">API calls made</span>
    <span class="total-val" style="font-size:.95rem;color:#718096;">{total_calls}</span>
  </div>"""

    html += """
  <a class="back-link" href="/">← Back to dashboard</a>
</div>
</body>
</html>"""

    return html


# NOTE: _apply_issue_date_day_rule now lives in app_core.py so both this
# module and reparse_routes.py can use it via `from app_core import *`.


@app.route("/api/extract_issue_date_provA/<int:passport_id>", methods=["POST"])
@login_required
@with_user_context(lambda: session.get('user_id'))
def extract_issue_date_provA_route(passport_id):
    """
    Receives base64 snippet from front-end, calls VPS OCR service, 
    and updates database record for normal passport.
    """
    try:
        user_id = session['user_id']
        data = request.json or {}

        if not data or 'image_base64' not in data:
            return jsonify({"success": False, "message": "No image data provided"}), 400

        header, encoded_b64 = data['image_base64'].split(",", 1)

        rid = _new_request_id()
        ocr_logger.debug(f"[{rid}] === ProvA Manual Snippet Issue Date Start | passport_id={passport_id} ===")

        # Call VPS OCR service
        vps_resp = _post_ocr("extract_issue_date_provA", {"strip_b64": encoded_b64}, _request_id=rid)

        if not vps_resp.get("success"):
            return jsonify({"success": False, "message": vps_resp.get("message", "Date extraction failed.")}), 400

        db_date = vps_resp.get("db_date")
        display_date = vps_resp.get("display_date")

        # Update local database
        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("USE passport_db")

            # Fetch expiry + country to sanity-check/correct the OCR day
            # against the passport's own expiry date (see
            # _apply_issue_date_day_rule for the rule itself).
            cursor.execute(
                "SELECT expiry, country FROM passports WHERE id = %s AND user_id = %s",
                (passport_id, user_id)
            )
            row = cursor.fetchone()
            expiry_str = str(row[0]) if row and row[0] else None
            country_code = row[1] if row else None

            corrected_date = _apply_issue_date_day_rule(db_date, expiry_str, country_code)
            if corrected_date != db_date:
                ocr_logger.info(
                    f"[{rid}] Issue-date day mismatch vs expiry — "
                    f"corrected {db_date} -> {corrected_date}"
                )
                db_date = corrected_date
                display_date = datetime.strptime(db_date, "%Y-%m-%d").strftime("%d-%m-%Y")

            cursor.execute("""
                UPDATE general_data gd
                JOIN passports p ON gd.passport_id = p.id
                SET gd.passport_issue_date = %s, gd.issue_date_estimated = FALSE
                WHERE gd.passport_id = %s AND p.user_id = %s
            """, (db_date, passport_id, user_id))
            conn.commit()
        except Exception as db_err:
            conn.rollback()
            return jsonify({"success": False, "message": f"Database update failed: {str(db_err)}"}), 500
        finally:
            cursor.close()
            conn.close()

        return jsonify({
            "success": True,
            "db_date": db_date,
            "display_date": display_date
        })

    except Exception as err:
        import traceback
        ocr_logger.error(f"Error in extract_issue_date_provA_route: {traceback.format_exc()}")
        return jsonify({"success": False, "message": str(err)}), 500

@app.route('/image_upload', methods=['POST'])
@login_required
def image_upload():
    if 'image_files' not in request.files:
        flash('No files selected', 'error')
        return redirect(url_for('pdf_extractor'))
        
    files = request.files.getlist('image_files')
    
    success, result, failed_files = process_image_upload(files, app.config["UPLOAD_FOLDER"])
    
    if success:
        response = make_response(send_file(
            result, mimetype='application/zip', as_attachment=True,
            download_name=f'cropped_images_{ist_now().strftime("%Y%m%d_%H%M%S")}.zip'
        ))
        
        # Pass skipped files to the frontend via a custom header
        if failed_files:
            response.headers['X-Failed-Files'] = json.dumps(failed_files)
            
        return response
    else:
        # If all failed, combine flash message and list
        error_msg = result
        if failed_files:
            error_msg += f" Skipped: {', '.join(failed_files)}"
        flash(error_msg, 'error')
        return redirect(url_for('pdf_extractor'))
    
@app.route('/robots.txt')
def static_from_root():
    return send_from_directory(app.static_folder, request.path[1:])

if __name__ == "__main__":
    import webbrowser, threading
    from waitress import serve

    port = 9000
    threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    print(f"Server running at http://127.0.0.1:{port}")
    serve(app, host="127.0.0.1", port=port)