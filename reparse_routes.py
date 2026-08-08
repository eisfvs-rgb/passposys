"""
reparse_routes.py
------------------
Invalid-passport reparse and rescan routes: the UI flow for fixing up a
passport that failed initial MRZ validation, sitting in invalid_passports.

Covers:
  - /reparse_mrz_ajax        quick client-side MRZ re-parse (no OCR call)
  - /reparse/<id>            renders the manual reparse/correction page
  - /reparse_action/<id>     submits a corrected/reparsed MRZ for insertion
  - /rotate_reparse_image/<id>   rotate the stored image (no API call)
  - /update_reparse_image_ajax/<id>  permanently save a user-cropped image (no API call)
  - /rescan_reparse_image/<id>   re-run OCR (ProvA Vision) on the stored image
  - /rescan_after_crop/<id>      re-run OCR after a manual crop on the reparse UI

Imports the Flask app object plus every shared helper/import from
app_core.py via a wildcard import, same as app_routes.py.

app.py wires this in alongside app_routes.py:
    from app_core import app
    import app_routes      # noqa: F401
    import reparse_routes  # noqa: F401
"""

from app_core import *  # noqa: F401,F403  -- app, helpers, constants, and 3rd-party imports

# NOTE: underscore-prefixed names are never picked up by wildcard imports
# (Python language rule) - must be imported explicitly or NameError results.
from ocr_client import _reset_secondary_client, with_user_context, get_last_extracted_issue_date
from app_core import _logger, _apply_issue_date_day_rule


@app.route("/reparse_mrz_ajax", methods=["POST"])
@login_required
def reparse_mrz_ajax():
    mrz_text = request.form.get("mrz_text", "")
    mrz_lines = [l.strip() for l in mrz_text.split("\n") if l.strip()]
    parsed, errors = parse_mrz(mrz_lines)
    if errors:
        return jsonify({"success": False, "errors": errors})
    if parsed.get("dob") and len(parsed["dob"]) == 6:
        parsed["dob"] = convert_mrz_date(parsed["dob"], is_dob=True)
    if parsed.get("expiry") and len(parsed["expiry"]) == 6:
        parsed["expiry"] = convert_mrz_date(parsed["expiry"], is_dob=False)
    return jsonify({"success": True, "parsed": parsed})




@app.route("/reparse/<int:invalid_id>", methods=["GET"])
@login_required
def reparse_invalid(invalid_id):
    user_id = session['user_id']
    invalid_passport = get_invalid_passport_by_id(invalid_id, user_id)
    if not invalid_passport:
        return "Invalid passport not found", 404

    session['current_invalid_id'] = invalid_id
    mrz_lines = []
    if invalid_passport['mrz_text']:
        mrz_lines = [l.strip() for l in invalid_passport['mrz_text'].split("\n") if l.strip()]

    parsed, errors = parse_mrz(mrz_lines) if mrz_lines else ({}, ["No MRZ data available"])
    db_defaults = get_user_settings(user_id)

    response = make_response(render_template(
        "reparse.html",
        invalid_passport=invalid_passport,
        mrz_lines=mrz_lines, parsed=parsed, errors=errors,
        nationality_options=NATIONALITY_OPTIONS,
        marital_status_options=MARITAL_STATUS_OPTIONS,
        defaults=db_defaults,
        NATIONALITY_CODE_MAP=NATIONALITY_CODE_MAP
    ))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response




def _check_duplicate_and_process(invalid_id, invalid_passport, parsed, mrz_text, mrz_lines, form_data, user_id):
    """
    Shared duplicate guard used before saving a successfully-parsed invalid
    record (normal "reparse" success and "insert_anyway"). Mirrors the same
    is_passport_number_exists_in_group() check enforced on upload/restore,
    so a clean MRZ re-parse can't silently bypass duplication rules.

    Emergency exception: if this invalid record was originally uploaded
    with the "Emergency upload" checkbox checked (invalid_passport
    ['is_emergency']), the cross-group duplication rules are skipped here
    too -- same relaxation already applied at upload time (see
    is_emergency_upload branch in app_routes.py). Only a duplicate within
    the SAME group is blocked; a match in a different group is allowed
    through.

    Renders the reparse page with the duplicate reason if blocked, otherwise
    proceeds to save via process_valid_invalid_passport().
    """
    passport_number = (parsed or {}).get("passport_number", "").strip()
    if passport_number and passport_number != "UNKNOWN":
        _reparse_settings = get_user_settings(user_id) or {}
        _reparse_group = _reparse_settings.get('group_name', 'GROUP 1')
        _reparse_visa_type = _reparse_settings.get('visa_type', 'nusuk')
        _reparse_is_emergency = bool((invalid_passport or {}).get('is_emergency'))
        if _reparse_is_emergency:
            _reparse_dup_hit = is_passport_number_exists_in_group_same_group_only(
                passport_number, user_id, _reparse_group
            )
        else:
            _reparse_dup_hit = is_passport_number_exists_in_group(
                passport_number, user_id, _reparse_group, _reparse_visa_type
            )
        if _reparse_dup_hit:
            _reparse_dup_groups = ", ".join(dict.fromkeys(
                m['group_name'] for m in _reparse_dup_hit.get('matches', [_reparse_dup_hit])
            ))
            return render_template(
                "reparse.html",
                invalid_passport=invalid_passport, mrz_lines=mrz_lines, parsed=parsed,
                errors=[f"Duplicate passport number: {passport_number}. Already exists in group \"{_reparse_dup_groups}\"."],
                nationality_options=NATIONALITY_OPTIONS,
                marital_status_options=MARITAL_STATUS_OPTIONS,
                defaults=get_user_settings(user_id),
                NATIONALITY_CODE_MAP=NATIONALITY_CODE_MAP
            )

    return process_valid_invalid_passport(invalid_id, parsed, mrz_text, form_data)


@app.route("/reparse_action/<int:invalid_id>", methods=["POST"])
@login_required
@with_user_context(lambda: session.get('user_id'))
def reparse_action(invalid_id):
    user_id = session['user_id']
    action = request.form.get("action")
    mrz_text = request.form.get("mrz_text", "")
    mrz_lines = [l.strip() for l in mrz_text.split("\n") if l.strip()]

    invalid_passport = get_invalid_passport_by_id(invalid_id, user_id)
    if not invalid_passport:
        return redirect(url_for("index"))

    if action == "skip":
        try:
            hard_delete_invalid_passport(invalid_id, user_id)
        except Exception as e:
            print(f"Skip deletion error: {e}")
        remaining = get_total_invalid_count(user_id)
        parent_redirect = url_for('results') if remaining == 0 else url_for('view_invalid_passports')
        return f'''
        <!DOCTYPE html><html><head><title>Skipped</title>
        <script>
            if (window.opener && !window.opener.closed && !window.opener._reparseActive) {{
                window.opener.location.href = "{parent_redirect}";
            }}
            setTimeout(() => {{ window.close(); }}, 100);
        </script></head>
        <body style="font-family:Arial,sans-serif;text-align:center;padding:40px;background:#f8fdf8">
            <div style="background:#d4edda;border:1px solid #c3e6cb;border-radius:8px;padding:25px;max-width:400px;margin:0 auto">
                <h2 style="color:#155724;margin-top:0">✅ Skipped Successfully</h2>
                <p style="color:#155724">Passport removed from error list.</p>
            </div>
        </body></html>
        ''', 200

    elif action == "reparse":
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
            if parsed is None:
                parsed = {}
            new_error_msg = ', '.join(errors)

            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("USE passport_db")
            cursor.execute("""
                UPDATE invalid_passports
                SET mrz_text = %s, error_message = %s
                WHERE id = %s AND user_id = %s
            """, ("\n".join(mrz_lines), new_error_msg, invalid_id, user_id))
            conn.commit()
            cursor.close()
            conn.close()
            invalid_passport['error_message'] = new_error_msg

            return render_template(
                "reparse.html",
                invalid_passport=invalid_passport,
                mrz_lines=mrz_lines, parsed=parsed, errors=errors,
                nationality_options=NATIONALITY_OPTIONS,
                marital_status_options=MARITAL_STATUS_OPTIONS,
                defaults=get_user_settings(user_id),
                NATIONALITY_CODE_MAP=NATIONALITY_CODE_MAP
            )

        return _check_duplicate_and_process(invalid_id, invalid_passport, parsed, mrz_text, mrz_lines, request.form, user_id)

    elif action == "insert_anyway":
        if len(mrz_lines) < 2:
            return render_template(
                "reparse.html",
                invalid_passport=invalid_passport, mrz_lines=mrz_lines, parsed={},
                errors=["Cannot force-insert: at least 2 MRZ lines required."],
                nationality_options=NATIONALITY_OPTIONS,
                marital_status_options=MARITAL_STATUS_OPTIONS,
                defaults=get_user_settings(user_id),
                NATIONALITY_CODE_MAP=NATIONALITY_CODE_MAP
            )

        parsed, _ = parse_mrz(mrz_lines, force=True)
        if not parsed:
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
                "surname": surname, "given_names": given_names, "middle_name": middle_name,
                "passport_number": (l2[0:9].replace("<", "") if len(l2) >= 9 else "UNKNOWN"),
                "nationality": (l2[10:13] if len(l2) >= 13 else "XXX"),
                "dob": (l2[13:19] if len(l2) >= 19 else "000000"),
                "sex": (l2[20] if len(l2) > 20 else "X"),
                "expiry": (l2[21:27] if len(l2) >= 27 else "000000")
            }

        if "dob" in parsed:
            parsed["dob"] = convert_mrz_date(parsed["dob"], is_dob=True) or "1900-01-01"
        if "expiry" in parsed:
            parsed["expiry"] = convert_mrz_date(parsed["expiry"], is_dob=False) or "2030-01-01"

        # Never force-insert an incomplete/unreadable MRZ — a genuinely
        # blank/garbled passport number means the record isn't ready for
        # insertion at all, regardless of the "force" override.
        _fi_passport_number = (parsed.get("passport_number") or "").strip()
        if not _fi_passport_number or _fi_passport_number == "UNKNOWN":
            return render_template(
                "reparse.html",
                invalid_passport=invalid_passport, mrz_lines=mrz_lines, parsed=parsed,
                errors=["Cannot force-insert: passport number is missing or unreadable in the MRZ."],
                nationality_options=NATIONALITY_OPTIONS,
                marital_status_options=MARITAL_STATUS_OPTIONS,
                defaults=get_user_settings(user_id),
                NATIONALITY_CODE_MAP=NATIONALITY_CODE_MAP
            )

        # Same-group duplicate check still applies — force-insert only
        # overrides the "unparseable/incomplete MRZ" block, never the
        # duplicate-passport-number guard.
        return _check_duplicate_and_process(invalid_id, invalid_passport, parsed, mrz_text, mrz_lines, request.form, user_id)




@app.route("/force_insert_duplicate/<int:invalid_id>", methods=["POST"])
@login_required
@with_user_context(lambda: session.get('user_id'))
def force_insert_duplicate(invalid_id):
    """
    Force-insert a record that was rejected by a cross-group duplication
    match against an existing NUSUK record (see
    is_passport_number_exists_in_group, rule 'cross_group_1year' in
    db.py) — whether the record being uploaded is Nusuk or Visit Visa.
    Triggered by the "Force Insert" button shown on the duplicate card(s)
    in Invalid Passports whenever any matched colliding record is Nusuk
    and within its 365-day validity window (processed_at, or created_at as
    a fallback for unprocessed records).

    Force Insert is NEVER offered for: the same-group rule, or a
    cross-group match against a still-valid Visit Visa record — those
    cases don't call this route.

    This intentionally skips the duplicate check entirely and applies the
    SAME re-parse + insert logic as the normal "reparse" action, saving
    into the user's current default group — i.e. the same group the
    original upload targeted (and where the colliding record lives).

    Does NOT bypass the strict same-group rule's *outcome* — if another
    active record with the same passport number genuinely still sits in
    the same target group (same_group rule), the insert will still fail
    at the database/application level for data-integrity reasons elsewhere
    in the app; this route only removes the cross-group Nusuk 1-year
    block.
    """
    user_id = session['user_id']
    invalid_passport = get_invalid_passport_by_id(invalid_id, user_id)
    if not invalid_passport:
        return jsonify({"success": False, "message": "Record not found."}), 404

    mrz_text = invalid_passport.get('mrz_text') or ""
    mrz_lines = [l.strip() for l in mrz_text.split("\n") if l.strip()]
    if len(mrz_lines) < 2:
        return jsonify({"success": False, "message": "No MRZ data available to insert."}), 400

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

    if errors or not parsed:
        return jsonify({
            "success": False,
            "message": "Cannot force-insert: MRZ no longer parses cleanly (" + ", ".join(errors or ["unknown error"]) + "). Use Manual Reparse instead."
        }), 400

    # Deliberately NOT calling is_passport_number_exists_in_group here —
    # this route exists specifically to bypass the Nusuk cross_group_1year
    # rule after the user has reviewed and confirmed the force insert.
    # It still respects the group visa-type lock (see
    # _assert_group_visa_type_matches in app_core.py) — Force Insert must
    # never let a Nusuk record land inside a Visit Visa group or vice
    # versa, so that guard is NOT skipped here.
    try:
        process_valid_invalid_passport(invalid_id, parsed, mrz_text, request.form)
        # process_valid_invalid_passport() hard-deletes the invalid_passports
        # row on success (and returns popup-window HTML meant for the old
        # Manual Reparse flow, which isn't useful here) — so success is
        # confirmed by checking that the row is gone.
        still_present = get_invalid_passport_by_id(invalid_id, user_id)
        if still_present:
            return jsonify({"success": False, "message": "Force insert did not complete. Please try Manual Reparse instead."}), 500
        return jsonify({"success": True, "message": "Passport force-inserted into Results."})
    except GroupVisaTypeMismatch as e:
        return jsonify({"success": False, "message": str(e)}), 400
    except Exception as e:
        return jsonify({"success": False, "message": f"Force insert failed: {str(e)}"}), 500





@app.route("/rotate_reparse_image/<int:invalid_id>", methods=["POST"])
@login_required
def rotate_reparse_image(invalid_id):
    """Rotates the stored image. Zero API calls."""
    user_id = session['user_id']
    direction = request.form.get("direction", "right")
    angle = -90 if direction == "right" else 90

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("USE passport_db")
        cursor.execute(
            "SELECT original_image FROM invalid_passports WHERE id = %s AND user_id = %s",
            (invalid_id, user_id)
        )
        row = cursor.fetchone()
        if not row or not row['original_image']:
            return jsonify({"success": False, "message": "Image not found"})

        image = ImageOps.exif_transpose(Image.open(io.BytesIO(row['original_image'])))
        if image.mode != "RGB":
            image = image.convert("RGB")
        rotated = image.rotate(angle, expand=True, fillcolor=(255, 255, 255))

        buf = io.BytesIO()
        rotated.save(buf, format='JPEG', quality=95)
        rotated_bytes = buf.getvalue()

        cursor.execute(
            "UPDATE invalid_passports SET original_image = %s WHERE id = %s AND user_id = %s",
            (rotated_bytes, invalid_id, user_id)
        )
        conn.commit()

        return jsonify({
            "success": True,
            "image_b64": base64.b64encode(rotated_bytes).decode('utf-8')
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        cursor.close()
        conn.close()


@app.route("/update_reparse_image_ajax/<int:invalid_id>", methods=["POST"])
@login_required
def update_reparse_image_ajax(invalid_id):
    """Permanently replaces the stored original_image with a user-cropped
    version from the reparse UI's 'Crop Image' tool. Distinct from
    /rescan_after_crop, which uses a crop only as transient OCR input and
    never touches the stored image. Zero API calls."""
    user_id = session['user_id']
    body = request.get_json(silent=True) or {}
    image_b64 = body.get("image_base64", "")

    if not image_b64 or "," not in image_b64:
        return jsonify({"success": False, "message": "No image data provided"})

    try:
        _header, encoded = image_b64.split(",", 1)
        image_bytes = base64.b64decode(encoded)
    except Exception:
        return jsonify({"success": False, "message": "Invalid image data"})

    if len(image_bytes) == 0:
        return jsonify({"success": False, "message": "Empty image data"})

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("USE passport_db")
        cursor.execute(
            "SELECT id FROM invalid_passports WHERE id = %s AND user_id = %s",
            (invalid_id, user_id)
        )
        if not cursor.fetchone():
            return jsonify({"success": False, "message": "Record not found"})

        # Re-encode through Pillow to normalize format/strip bad EXIF and
        # validate that the bytes are actually a decodable image.
        try:
            image = Image.open(io.BytesIO(image_bytes))
            image.load()
            if image.mode != "RGB":
                image = image.convert("RGB")
        except Exception:
            return jsonify({"success": False, "message": "Could not decode cropped image"})

        buf = io.BytesIO()
        image.save(buf, format='JPEG', quality=95)
        final_bytes = buf.getvalue()

        cursor.execute(
            "UPDATE invalid_passports SET original_image = %s WHERE id = %s AND user_id = %s",
            (final_bytes, invalid_id, user_id)
        )
        conn.commit()

        return jsonify({
            "success": True,
            "image_b64": base64.b64encode(final_bytes).decode('utf-8')
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        cursor.close()
        conn.close()


@app.route("/rescan_reparse_image/<int:invalid_id>", methods=["POST"])
@login_required
@with_user_context(lambda: session.get('user_id'))
def rescan_reparse_image(invalid_id):
    """Individual ProvA Vision rescan from the reparse UI."""
    user_id = session['user_id']
    _reset_secondary_client()
    _body = request.get_json(silent=True) or {}
    _skip_llmB = bool(_body.get("skip_llmB", False))
    _logger.info(f"[Reparse Rescan] invalid_id={invalid_id} user_id={user_id} skip_llmB={_skip_llmB}")
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("USE passport_db")
        cursor.execute(
            "SELECT original_image, error_message FROM invalid_passports WHERE id = %s AND user_id = %s",
            (invalid_id, user_id)
        )
        row = cursor.fetchone()
        if not row or not row['original_image']:
            return jsonify({"success": False, "message": "Image not found"})

        temp_original = os.path.join(app.config["UPLOAD_FOLDER"], f"temp_rescan_{invalid_id}.jpg")
        with open(temp_original, "wb") as f:
            f.write(row['original_image'])

        with Image.open(temp_original) as _img:
            _img = ImageOps.exif_transpose(_img)
            if _img.mode != 'RGB':
                _img = _img.convert('RGB')
            _img.save(temp_original, format='JPEG', quality=100)

        resized_path = temp_original
        mrz_lines, _calls_used, _raw_text = extract_mrz_from_image_reparse_rescan(resized_path, skip_llmB=_skip_llmB)

        if os.path.exists(temp_original): os.remove(temp_original)

        new_mrz_text = "\n".join(mrz_lines) if mrz_lines else ""

        _issue_date = None
        if mrz_lines:
            _parsed, _ = parse_mrz(mrz_lines, raw_text=_raw_text or "")
            _issue_date = get_last_extracted_issue_date()

            if _issue_date and _parsed and _parsed.get("expiry"):
                _corrected_str = _apply_issue_date_day_rule(
                    _issue_date.strftime("%Y-%m-%d"),
                    _parsed.get("expiry"),
                    _parsed.get("country"),
                )
                if _corrected_str != _issue_date.strftime("%Y-%m-%d"):
                    _logger.info(
                        f"[Reparse Rescan] Issue-date day mismatch vs expiry — "
                        f"corrected {_issue_date} -> {_corrected_str}"
                    )
                    _issue_date = datetime.strptime(_corrected_str, "%Y-%m-%d").date()

        if new_mrz_text:
            cursor.execute(
                "UPDATE invalid_passports SET mrz_text = %s, extracted_issue_date = %s WHERE id = %s AND user_id = %s",
                (new_mrz_text, _issue_date, invalid_id, user_id)
            )
        # (no error_message update needed — [RESCANNED] flag no longer used)
        conn.commit()

        return jsonify({
            "success": True,
            "mrz_text": new_mrz_text,
            "mrz_found": bool(new_mrz_text),
            "extracted_issue_date": _issue_date.strftime("%Y-%m-%d") if _issue_date else None
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        cursor.close()
        conn.close()




@app.route("/rescan_after_crop/<int:invalid_id>", methods=["POST"])
@login_required
@with_user_context(lambda: session.get('user_id'))
def rescan_after_crop(invalid_id):
    """Auto-rescan triggered after a manual crop save on the reparse page.
    Uses Phase 4 smart crop (face detection) + v2 assembler + v2 ProvB fallback."""
    user_id = session['user_id']
    _reset_secondary_client()

    # If the client sent a cropped image (from the crop-select UI), OCR that
    # directly. This is used only as OCR input for this request and is never
    # written back to invalid_passports.original_image.
    _crop_data = request.get_json(silent=True) or {}
    _crop_b64 = _crop_data.get("image_base64")
    _skip_llmB_log = bool(_crop_data.get("skip_llmB", False))
    _logger.info(f"[Crop Rescan] invalid_id={invalid_id} user_id={user_id} skip_llmB={_skip_llmB_log} has_crop={bool(_crop_b64)}")
    _skip_llmB = bool(_crop_data.get("skip_llmB", False))

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("USE passport_db")
        cursor.execute(
            "SELECT original_image, error_message FROM invalid_passports WHERE id = %s AND user_id = %s",
            (invalid_id, user_id)
        )
        row = cursor.fetchone()
        if not row or not row['original_image']:
            return jsonify({"success": False, "message": "Image not found"})

        if _crop_b64:
            # User already selected the exact region in the browser — send
            # it to the VPS as-is (no local re-crop/face-detection), matching
            # extract_mrz_from_region_reparse_rescan()'s contract.
            _header, _encoded = _crop_b64.split(",", 1)
            mrz_lines, _calls_used, _raw_text = extract_mrz_from_region_reparse_rescan(_encoded, skip_llmB=_skip_llmB)
        else:
            temp_original = os.path.join(app.config["UPLOAD_FOLDER"], f"temp_crop_rescan_{invalid_id}.jpg")
            with open(temp_original, "wb") as f:
                f.write(row['original_image'])

            with Image.open(temp_original) as _img:
                _img = ImageOps.exif_transpose(_img)
                if _img.mode != 'RGB':
                    _img = _img.convert('RGB')
                _img.save(temp_original, format='JPEG', quality=100)

            mrz_lines, _calls_used, _raw_text = extract_mrz_from_image_crop_rescan(temp_original)
            if os.path.exists(temp_original): os.remove(temp_original)

        new_mrz_text = "\n".join(mrz_lines) if mrz_lines else ""

        _issue_date = None
        if mrz_lines:
            _parsed, _ = parse_mrz(mrz_lines, raw_text=_raw_text or "")
            _issue_date = get_last_extracted_issue_date()

            if _issue_date and _parsed and _parsed.get("expiry"):
                _corrected_str = _apply_issue_date_day_rule(
                    _issue_date.strftime("%Y-%m-%d"),
                    _parsed.get("expiry"),
                    _parsed.get("country"),
                )
                if _corrected_str != _issue_date.strftime("%Y-%m-%d"):
                    _logger.info(
                        f"[Crop Rescan] Issue-date day mismatch vs expiry — "
                        f"corrected {_issue_date} -> {_corrected_str}"
                    )
                    _issue_date = datetime.strptime(_corrected_str, "%Y-%m-%d").date()

        if new_mrz_text:
            cursor.execute(
                "UPDATE invalid_passports SET mrz_text = %s, extracted_issue_date = %s WHERE id = %s AND user_id = %s",
                (new_mrz_text, _issue_date, invalid_id, user_id)
            )
        conn.commit()

        return jsonify({
            "success": True,
            "mrz_text": new_mrz_text,
            "mrz_found": bool(new_mrz_text),
            "extracted_issue_date": _issue_date.strftime("%Y-%m-%d") if _issue_date else None
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        cursor.close()
        conn.close()