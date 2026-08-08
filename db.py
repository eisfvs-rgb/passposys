import json
import mysql.connector
from mysql.connector import Error, pooling
from datetime import datetime, timedelta
from time_utils import ist_now
import re
from config import DB_CONFIG
import base64
import os
import shutil
from config import UPLOAD_FOLDER, FACE_FOLDER, VISA_PROCESSED_FOLDER

# =====================================================
# GROUP-FOLDER PATH HELPERS
# =====================================================
# Physical layout:
#   uploads/<GROUP_NAME>/<filename>
#   faces/<GROUP_NAME>/face_<filename>
#   uploads/_recycle_bin/<GROUP_NAME>/<filename>    (soft-deleted originals)
#   faces/_recycle_bin/<GROUP_NAME>/face_<filename> (soft-deleted faces)

_RECYCLE_DIRNAME = "_recycle_bin"
# Retained only so folder-cleanup/inference helpers keep ignoring any
# leftover "_emergency" folder from before emergency uploads were removed;
# no emergency-specific logic reads from or writes to this folder anymore.
_EMERGENCY_DIRNAME = "_emergency"

def _sanitize_group(group_name):
    """Make a group name safe to use as a folder name."""
    g = ''.join(
        c for c in (group_name or "GROUP 1") if c.isalnum() or c in (" ", "_", "-")
    ).strip()
    return g or "GROUP 1"

def get_group_dir(group_name, kind="original", recycled=False):
    """Return (and create) the folder for a group's original or face images."""
    base = UPLOAD_FOLDER if kind == "original" else FACE_FOLDER
    safe_group = _sanitize_group(group_name)
    if recycled:
        d = os.path.join(base, _RECYCLE_DIRNAME, safe_group)
    else:
        d = os.path.join(base, safe_group)
    os.makedirs(d, exist_ok=True)
    return d

def get_passport_path(filename, group_name, kind="original", recycled=False):
    """Return the full disk path for a passport's original or face image,
    inside its group's folder (or the recycle-bin mirror of it)."""
    d = get_group_dir(group_name, kind=kind, recycled=recycled)
    fname = filename if kind == "original" else f"face_{filename}"
    return os.path.join(d, fname)

def get_visa_processed_group_dir(group_name):
    """Return the visa_processed folder for a group (mofa.py /
    mofa_downloader.py write <passport_number>_visa.pdf files here), WITHOUT
    creating it -- callers must check os.path.isdir() themselves, since a
    missing folder means "no visa processed yet" rather than an error."""
    safe_group = _sanitize_group(group_name)
    return os.path.join(VISA_PROCESSED_FOLDER, safe_group)


def list_visa_processed_pdfs(group_name):
    """Return a sorted list of absolute paths to *_visa.pdf files in a
    group's visa_processed folder. Empty list if the folder doesn't exist
    or has no PDFs."""
    d = get_visa_processed_group_dir(group_name)
    if not os.path.isdir(d):
        return []
    return sorted(
        os.path.join(d, f) for f in os.listdir(d)
        if f.lower().endswith('.pdf')
    )


def reconcile_mofa_pdf_downloads(user_id=None):
    """
    Cross-check every passport with mofa_pdf_downloaded_at set against the
    actual visa_processed/<group>/<passport_number>_visa.pdf file on disk.
    If the DB says "downloaded" but the file is missing (deleted, moved,
    or the column was set without the download actually completing), clear
    mofa_pdf_downloaded_at back to NULL so the "Visa Available" badge
    correctly reverts to "Check Visa" and the single-record trigger will
    attempt the download again instead of trusting a stale DB value.

    Intended to run once at login (cheap: only rows with the column set
    are checked, and each is a single os.path.exists() call).

    user_id: if given, only reconciles that user's records; if None,
    reconciles every user's records (e.g. for a startup-wide check).

    Returns the number of rows cleared.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        if user_id is not None:
            cursor.execute("""
                SELECT p.id, p.passport_number, g.group_name
                FROM passports p
                JOIN general_data g ON g.passport_id = p.id
                WHERE p.mofa_pdf_downloaded_at IS NOT NULL AND p.user_id = %s
            """, (user_id,))
        else:
            cursor.execute("""
                SELECT p.id, p.passport_number, g.group_name
                FROM passports p
                JOIN general_data g ON g.passport_id = p.id
                WHERE p.mofa_pdf_downloaded_at IS NOT NULL
            """)
        rows = cursor.fetchall()

        missing_ids = []
        for row in rows:
            pdf_path = get_visa_pdf_path(row['group_name'], row['passport_number'])
            if not os.path.exists(pdf_path):
                missing_ids.append(row['id'])

        if missing_ids:
            upd_cursor = conn.cursor()
            fmt = ','.join(['%s'] * len(missing_ids))
            upd_cursor.execute(
                f"UPDATE passports SET mofa_pdf_downloaded_at = NULL WHERE id IN ({fmt})",
                tuple(missing_ids)
            )
            conn.commit()
            upd_cursor.close()

        return len(missing_ids)
    finally:
        cursor.close()
        conn.close()


def get_visa_pdf_path(group_name, passport_number):
    """Return the expected path for a single passport's visa PDF
    (visa_processed/<group>/<passport_number>_visa.pdf), matching the
    naming convention used by mofa.py/mofa_downloader.py. Does not check
    existence -- callers should os.path.exists() before using it."""
    d = get_visa_processed_group_dir(group_name)
    safe_pn = ''.join(c for c in str(passport_number or '') if c.isalnum())
    return os.path.join(d, f"{safe_pn}_visa.pdf")


def _find_passport_file(filename, kind="original"):
    """Locate a passport's original/face file on disk, searching the
    group folders, the recycle bin, and finally the legacy flat root
    (for files saved before the group-folder migration)."""
    base = UPLOAD_FOLDER if kind == "original" else FACE_FOLDER
    fname = filename if kind == "original" else f"face_{filename}"
    if not filename:
        return None

    # Legacy flat location (pre-migration)
    legacy = os.path.join(base, fname)
    if os.path.exists(legacy):
        return legacy

    # Search group subfolders and recycle-bin subfolders
    if os.path.isdir(base):
        for root, dirs, files in os.walk(base):
            if fname in files:
                return os.path.join(root, fname)
    return None

def resolve_passport_paths(filename, group_name=None):
    """Best-effort resolution of (original_path, face_path) for a filename.
    Prefers the known group_name if given, else searches for the file."""
    if group_name:
        orig = get_passport_path(filename, group_name, "original")
        face = get_passport_path(filename, group_name, "face")
        if os.path.exists(orig) or os.path.exists(face):
            return orig, face
    orig = _find_passport_file(filename, "original") or (os.path.join(UPLOAD_FOLDER, filename) if filename else None)
    face = _find_passport_file(filename, "face") or (os.path.join(FACE_FOLDER, f"face_{filename}") if filename else None)
    return orig, face


def copy_passport_files_to_group(filename, source_group, dest_group):
    """Copy a passport's original + face image from source_group's folder
    into dest_group's folder, without touching/removing the source files.

    Returns a dict describing what happened, e.g.:
        {"original_copied": True, "face_copied": False,
         "original_missing": False, "face_missing": True}

    Never raises for a missing source file (that's a legitimate, reportable
    state -- some records may not have a face crop, or an original may have
    been hard-deleted out from under a stale filename) -- only for actual
    I/O failures (permissions, disk full, etc.), which callers should catch
    and log per-file so one bad file doesn't abort copying the rest of a
    group.
    """
    result = {
        "original_copied": False, "face_copied": False,
        "original_missing": False, "face_missing": False,
    }
    if not filename:
        result["original_missing"] = True
        result["face_missing"] = True
        return result

    src_orig, src_face = resolve_passport_paths(filename, source_group)

    dest_orig = get_passport_path(filename, dest_group, kind="original")
    if src_orig and os.path.exists(src_orig):
        os.makedirs(os.path.dirname(dest_orig), exist_ok=True)
        shutil.copy2(src_orig, dest_orig)
        result["original_copied"] = True
    else:
        result["original_missing"] = True

    dest_face = get_passport_path(filename, dest_group, kind="face")
    if src_face and os.path.exists(src_face):
        os.makedirs(os.path.dirname(dest_face), exist_ok=True)
        shutil.copy2(src_face, dest_face)
        result["face_copied"] = True
    else:
        result["face_missing"] = True

    return result

def move_group_folder(old_group_name, new_group_name):
    """Rename/move a group's physical folders (both originals and faces)
    from old_group_name to new_group_name. Merges into the destination
    folder if it already exists."""
    if _sanitize_group(old_group_name) == _sanitize_group(new_group_name):
        return
    for kind in ("original", "face"):
        base = UPLOAD_FOLDER if kind == "original" else FACE_FOLDER
        old_dir = os.path.join(base, _sanitize_group(old_group_name))
        new_dir = os.path.join(base, _sanitize_group(new_group_name))
        if not os.path.isdir(old_dir):
            continue
        os.makedirs(new_dir, exist_ok=True)
        for fname in os.listdir(old_dir):
            src = os.path.join(old_dir, fname)
            dst = os.path.join(new_dir, fname)
            try:
                shutil.move(src, dst)
            except Exception as e:
                print(f"Warning: could not move {src} -> {dst}: {e}")
        try:
            os.rmdir(old_dir)
        except OSError:
            pass  # not empty or already gone

def move_passport_files_to_group(filename, old_group_name, new_group_name):
    """Move a single passport's original + face image from one group's
    folder to another's."""
    if _sanitize_group(old_group_name) == _sanitize_group(new_group_name):
        return
    for kind in ("original", "face"):
        src = get_passport_path(filename, old_group_name, kind=kind)
        if not os.path.exists(src):
            # fall back to searching in case it's already elsewhere
            found = _find_passport_file(filename, kind=kind)
            src = found if found else src
        if os.path.exists(src):
            dst = get_passport_path(filename, new_group_name, kind=kind)
            try:
                shutil.move(src, dst)
            except Exception as e:
                print(f"Warning: could not move {src} -> {dst}: {e}")
    # The old group's live folder may now be empty of images — clean it up.
    cleanup_empty_group_dir(old_group_name)

def cleanup_empty_group_dir(group_name):
    """Remove a group's LIVE folder (uploads/<GROUP> and faces/<GROUP>)
    if it has no image files left in it. The recycle-bin copy of the
    group is left untouched — a passport still sitting in the recycle
    bin means the group folder is still 'in use' there, but the live
    folder with nothing in it serves no purpose."""
    if not group_name:
        return
    for base in (UPLOAD_FOLDER, FACE_FOLDER):
        group_dir = os.path.join(base, _sanitize_group(group_name))
        _cleanup_dir_if_empty(group_dir)

# =====================================================
# CONNECTION POOL
# =====================================================
# OPTIMIZED: A single pool of 10 persistent connections replaces the old pattern

# of get_connection() inside every function.
# During a 50-image batch ~100+ DB calls are made; without pooling each call
# opens and tears down a TCP connection to MySQL, adding ~5-20 ms overhead each.
# With the pool that cost is paid once at startup and connections are reused.
#
# get_connection() is a drop-in replacement for get_connection().
# All existing code only needs: conn = get_connection()  (everything else unchanged).

_db_pool = None

def _init_pool():
    global _db_pool
    if _db_pool is None:
        pool_config = dict(DB_CONFIG)
        _db_pool = pooling.MySQLConnectionPool(
            pool_name="passport_pool",
            pool_size=10,  # per-worker: 4 threads + 2 buffer (6 workers x 6 = 36 total MySQL conns)
            pool_reset_session=True,
            **pool_config
        )

def get_connection():
    """Return a pooled MySQL connection. Use exactly like mysql.connector.connect()."""
    global _db_pool
    if _db_pool is None:
        _init_pool()
    return _db_pool.get_connection()

def _delete_passport_files(filename, permanent=True):
    """Delete passport image and face image from disk.
    permanent=True (default): hard-deletes the files — used for archive
    cleanup, empty-recycle-bin, and other places where the DB row is
    already gone for good.
    permanent=False: soft-delete — moves the files into the group's
    recycle-bin mirror folder instead of deleting them, so they can be
    restored later. Group is resolved by searching, since callers here
    often only have a bare filename."""
    if not filename:
        return
    if permanent:
        for path in [
            _find_passport_file(filename, "original") or os.path.join(UPLOAD_FOLDER, filename),
            _find_passport_file(filename, "face") or os.path.join(FACE_FOLDER, f"face_{filename}"),
        ]:
            if not path:
                continue
            parent_dir = os.path.dirname(path)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                print(f"Warning: could not delete file {path}: {e}")
                continue
            _cleanup_dir_if_empty(parent_dir)
    else:
        _recycle_passport_files(filename)


def _cleanup_dir_if_empty(dir_path):
    """Remove dir_path if it exists and has no files left in it — used
    for both live group folders and their recycle-bin counterparts.
    Never touches UPLOAD_FOLDER/FACE_FOLDER root, the _recycle_bin root,
    or the _emergency root itself — only actual group (or emergency)
    subfolders."""
    if not dir_path or not os.path.isdir(dir_path):
        return
    base_names = (
        os.path.basename(UPLOAD_FOLDER), os.path.basename(FACE_FOLDER),
        _RECYCLE_DIRNAME, _EMERGENCY_DIRNAME, "",
    )
    if os.path.basename(dir_path) in base_names:
        return  # never remove a root folder
    try:
        if not os.listdir(dir_path):
            os.rmdir(dir_path)
    except OSError as e:
        print(f"Warning: could not remove empty folder {dir_path}: {e}")


def _group_from_path(path, base):
    """Given a file's full path and its base folder (UPLOAD_FOLDER/FACE_FOLDER),
    infer the group name from the parent directory name, or None if the
    file sits flat in the base folder (legacy), in the recycle bin, or in
    the emergency holding area (which reuses the group-folder machinery
    but is never a real, user-facing group)."""
    if not path:
        return None
    parent = os.path.basename(os.path.dirname(path))
    if parent in ("", os.path.basename(base), _RECYCLE_DIRNAME, _EMERGENCY_DIRNAME):
        return None
    return parent


def _recycle_passport_files(filename, group_name=None):
    """Move a passport's original + face image into the recycle-bin
    mirror of their current group folder. Best-effort: if a file can't
    be located, it's silently skipped."""
    if not filename:
        return
    resolved_group = group_name
    for kind in ("original", "face"):
        base = UPLOAD_FOLDER if kind == "original" else FACE_FOLDER
        src = _find_passport_file(filename, kind=kind)
        if not src or not os.path.exists(src):
            continue
        grp = group_name or _group_from_path(src, base) or "GROUP 1"
        resolved_group = resolved_group or grp
        dst = get_passport_path(filename, grp, kind=kind, recycled=True)
        try:
            shutil.move(src, dst)
        except Exception as e:
            print(f"Warning: could not recycle {src} -> {dst}: {e}")
    # The live group folder may now be empty — clean it up (recycle-bin
    # copy of the group is untouched, so the file is still safe there).
    cleanup_empty_group_dir(resolved_group)


def _restore_passport_files(filename, group_name=None):
    """Move a passport's original + face image out of the recycle-bin
    mirror and back into its live group folder."""
    if not filename:
        return
    for kind in ("original", "face"):
        base = UPLOAD_FOLDER if kind == "original" else FACE_FOLDER
        recycle_root = os.path.join(base, _RECYCLE_DIRNAME)
        src = None
        fname = filename if kind == "original" else f"face_{filename}"
        if group_name:
            candidate = get_passport_path(filename, group_name, kind=kind, recycled=True)
            if os.path.exists(candidate):
                src = candidate
        if not src and os.path.isdir(recycle_root):
            for root, dirs, files in os.walk(recycle_root):
                if fname in files:
                    src = os.path.join(root, fname)
                    break
        if not src:
            continue
        grp = group_name or _group_from_path(src, os.path.join(base, _RECYCLE_DIRNAME)) or "GROUP 1"
        dst = get_passport_path(filename, grp, kind=kind, recycled=False)
        src_dir = os.path.dirname(src)
        try:
            shutil.move(src, dst)
        except Exception as e:
            print(f"Warning: could not restore {src} -> {dst}: {e}")
            continue
        # The recycle-bin group folder we just moved out of may now be empty.
        _cleanup_dir_if_empty(src_dir)
# =====================================================
# NATIONALITY MAPPING
# =====================================================

NATIONALITY_CODE_TO_ID = {
    'AFG': 2, 'ALB': 5, 'DZA': 59, 'ASM': 10, 'AND': 6, 'AGO': 3, 'AIA': 4,
    'ATA': 11, 'ATG': 13, 'ARG': 8, 'ARM': 9, 'ABW': 1, 'AUS': 15, 'AUT': 14,
    'AZE': 16, 'BHS': 24, 'BHR': 23, 'BGD': 21, 'BRB': 31, 'BLR': 26, 'BEL': 18,
    'BLZ': 27, 'BEN': 19, 'BMU': 28, 'BTN': 33, 'BOL': 29, 'BIH': 25, 'BWA': 231,
    'BVT': 34, 'BRA': 30, 'IOT': 241, 'BRN': 32, 'BGR': 22, 'BFA': 20, 'BDI': 17,
    'KHM': 106, 'CMR': 42, 'CAN': 36, 'CPV': 47, 'CYM': 51, 'CAF': 35, 'TCD': 194,
    'CHL': 39, 'CHN': 40, 'CXR': 50, 'CCK': 37, 'COL': 45, 'COM': 46, 'COG': 43,
    'COK': 44, 'CRI': 48, 'CIV': 41, 'HRV': 90, 'CUB': 49, 'CYP': 52, 'CZE': 53,
    'DNK': 57, 'DJI': 55, 'DMA': 56, 'DOM': 58, 'TLS': 228, 'ECU': 60, 'EGY': 61,
    'SLV': 181, 'GNQ': 79, 'ERI': 224, 'EST': 63, 'ETH': 64, 'FLK': 67, 'FRO': 69,
    'FJI': 66, 'FIN': 65, 'FRA': 68, 'GUF': 84, 'PYF': 167, 'GAB': 70, 'GMB': 77,
    'GEO': 72, 'DEU': 54, 'GHA': 73, 'GIB': 74, 'GRC': 80, 'GRL': 82, 'GRD': 81,
    'GLP': 76, 'GUM': 85, 'GTM': 83, 'GIN': 75, 'GNB': 78, 'GUY': 86, 'HTI': 91,
    'HMD': 88, 'HND': 89, 'HKG': 1278, 'HUN': 92, 'ISL': 98, 'IND': 94, 'IDN': 93,
    'IRN': 96, 'IRQ': 97, 'IRL': 95, 'ITA': 99, 'JAM': 100, 'JPN': 102, 'JOR': 101,
    'KAZ': 103, 'KEN': 104, 'KIR': 107, 'KOR': 109, 'PRK': 164, 'XKX': 87, 'KWT': 110,
    'KGZ': 105, 'LAO': 111, 'LVA': 121, 'LBN': 112, 'LSO': 118, 'LBR': 113, 'LBY': 114,
    'LIE': 116, 'LTU': 119, 'LUX': 120, 'MAC': 122, 'MKD': 236, 'MDG': 126, 'MWI': 140,
    'MYS': 141, 'MDV': 127, 'MLI': 130, 'MLT': 131, 'MHL': 129, 'MTQ': 138, 'MRT': 135,
    'MUS': 139, 'MYT': 142, 'MEX': 128, 'FSM': 229, 'MCO': 124, 'MNG': 133, 'MNE': 243,
    'MSR': 137, 'MAR': 123, 'MOZ': 134, 'MMR': 132, 'NAM': 143, 'NRU': 153, 'NPL': 152,
    'NLD': 150, 'NCL': 144, 'NZL': 154, 'NIC': 148, 'NER': 145, 'NGA': 147, 'NIU': 149,
    'NFK': 146, 'MNP': 227, 'NOR': 151, 'OMN': 155, 'PAK': 156, 'PLW': 235, 'PSE': 234,
    'PAN': 157, 'PNG': 161, 'PRY': 166, 'PER': 159, 'PHL': 160, 'PCN': 158, 'POL': 162,
    'PRT': 165, 'PRI': 163, 'QAT': 168, 'MDA': 125, 'SSD': 136, 'REU': 169, 'ROU': 170,
    'RUS': 171, 'RWA': 172, 'SHN': 233, 'KNA': 108, 'LCA': 115, 'SPM': 184, 'VCT': 212,
    'WSM': 226, 'SMR': 182, 'STP': 185, 'SEN': 175, 'SRB': 242, 'SYC': 191, 'SLE': 180,
    'SGP': 176, 'SVK': 187, 'SVN': 188, 'SLB': 179, 'SOM': 183, 'ZAF': 219, 'SGS': 177,
    'ESP': 62, 'LKA': 117, 'SDN': 174, 'SUR': 186, 'SJM': 178, 'SWZ': 190, 'SWE': 189,
    'CHE': 38, 'SYR': 192, 'TWN': 205, 'TJK': 197, 'THA': 196, 'TGO': 195, 'TKL': 198,
    'TON': 200, 'TTO': 201, 'TUN': 202, 'TUR': 203, 'TKM': 199, 'TCA': 193, 'TUV': 204,
    'UGA': 207, 'UKR': 208, 'ARE': 7, 'GBR': 71, 'USA': 210, 'TZA': 206, 'URY': 209,
    'UZB': 211, 'VUT': 216, 'VAT': 240, 'VEN': 213, 'VNM': 215, 'VGB': 214, 'VIR': 232,
    'WLF': 217, 'YEM': 218, 'ZMB': 221, 'ZWE': 222
}

# =====================================================
# DATABASE INITIALIZATION
# =====================================================

def init_db():
    """Initialize database and create all required tables for multi-tenant system"""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("CREATE DATABASE IF NOT EXISTS passport_db")
    cursor.execute("USE passport_db")


    # Migrate: add is_visa_processed column for existing databases
    cursor.execute("SHOW COLUMNS FROM passports LIKE 'is_visa_processed'")
    if not cursor.fetchone():
        cursor.execute(
            "ALTER TABLE passports ADD COLUMN is_visa_processed BOOLEAN DEFAULT FALSE AFTER is_processed"
        )

    # Migrate: add processed_at column (timestamp of when is_processed was
    # set TRUE) for existing databases. Used by the Nusuk 365-day
    # cross-group duplication rule — see is_passport_number_exists_in_group().
    cursor.execute("SHOW COLUMNS FROM passports LIKE 'processed_at'")
    if not cursor.fetchone():
        cursor.execute(
            "ALTER TABLE passports ADD COLUMN processed_at TIMESTAMP NULL DEFAULT NULL AFTER is_visa_processed"
        )

    # Migrate: add visa_processed_at column (timestamp of when
    # is_visa_processed was set TRUE) for existing databases. Lets
    # /api/visa_processed_status return completed passports in the exact
    # order passposys.exe actually finished them — rather than whatever
    # arbitrary order MySQL returns a plain WHERE...IN(...) match in —
    # so the results-page tick marks light up in true completion order
    # even when several finish inside the same 3-second poll window.
    cursor.execute("SHOW COLUMNS FROM passports LIKE 'visa_processed_at'")
    if not cursor.fetchone():
        cursor.execute(
            "ALTER TABLE passports ADD COLUMN visa_processed_at TIMESTAMP NULL DEFAULT NULL AFTER processed_at"
        )

    # Migrate: add mofa_pdf_downloaded_at column for existing databases.
    # Set by mofa_downloader.py (and by the single-record trigger in
    # app_routes.py) the moment the MOFA visa PDF is actually downloaded
    # and saved to visa_processed/<group>/<passport_number>_visa.pdf --
    # distinct from is_visa_processed/is_processed, which only mean the
    # record was sent into the Nusuk/Visit-Visa queue, not that MOFA has
    # produced the PDF yet. This is the true "visa available" signal.
    cursor.execute("SHOW COLUMNS FROM passports LIKE 'mofa_pdf_downloaded_at'")
    if not cursor.fetchone():
        cursor.execute(
            "ALTER TABLE passports ADD COLUMN mofa_pdf_downloaded_at TIMESTAMP NULL DEFAULT NULL AFTER visa_processed_at"
        )

    # Migrate: add upload_group_name / upload_visa_type columns to
    # invalid_passports for existing databases. These record the group
    # and visa type that were ACTIVE at the moment the record was
    # uploaded, so the Invalid Passports page and the duplicate-message
    # rebuild always know exactly what was being uploaded — instead of
    # guessing from the user's CURRENT settings, which may have since
    # changed (e.g. user switched their default group after uploading).
    cursor.execute("SHOW COLUMNS FROM invalid_passports LIKE 'upload_group_name'")
    if not cursor.fetchone():
        cursor.execute(
            "ALTER TABLE invalid_passports ADD COLUMN upload_group_name VARCHAR(100) DEFAULT NULL"
        )
    cursor.execute("SHOW COLUMNS FROM invalid_passports LIKE 'upload_visa_type'")
    if not cursor.fetchone():
        cursor.execute(
            "ALTER TABLE invalid_passports ADD COLUMN upload_visa_type VARCHAR(20) DEFAULT NULL"
        )

    # Migrate: add extracted_issue_date to invalid_passports for existing
    # databases. Populated by the reparse-page rescan/crop-rescan routes
    # from the OCR-extracted issue date (never estimated), so it's readily
    # available to include in the result after parsing.
    cursor.execute("SHOW COLUMNS FROM invalid_passports LIKE 'extracted_issue_date'")
    if not cursor.fetchone():
        cursor.execute(
            "ALTER TABLE invalid_passports ADD COLUMN extracted_issue_date DATE DEFAULT NULL"
        )

    # Migrate: add is_emergency flag to invalid_passports for existing
    # databases. Set TRUE only for records saved while the "Emergency
    # upload" checkbox was checked on the original upload, so the Invalid
    # Passports page can badge it and reparse can relax duplicate checks
    # to same-group-only for these records (mirrors upload-time behavior).
    cursor.execute("SHOW COLUMNS FROM invalid_passports LIKE 'is_emergency'")
    if not cursor.fetchone():
        cursor.execute(
            "ALTER TABLE invalid_passports ADD COLUMN is_emergency BOOLEAN DEFAULT FALSE"
        )

    # Migrate: add middle_name column to passports and archived_passports if upgrading
    cursor.execute("SHOW COLUMNS FROM passports LIKE 'middle_name'")
    if not cursor.fetchone():
        cursor.execute("ALTER TABLE passports ADD COLUMN middle_name VARCHAR(100) DEFAULT '' AFTER given_names")
    cursor.execute("SHOW TABLES LIKE 'archived_passports'")
    if cursor.fetchone():
        cursor.execute("SHOW COLUMNS FROM archived_passports LIKE 'middle_name'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE archived_passports ADD COLUMN middle_name VARCHAR(100) DEFAULT '' AFTER given_names")

    # Migrate: add visa_type column (Nusuk / Visit Visa) for existing databases
    cursor.execute("SHOW TABLES LIKE 'general_data'")
    if cursor.fetchone():
        cursor.execute("SHOW COLUMNS FROM general_data LIKE 'visa_type'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE general_data ADD COLUMN visa_type VARCHAR(20) DEFAULT 'nusuk'")
        # Migrate: add issue_date_estimated flag for existing databases.
        # True when passport_issue_date was heuristically estimated
        # (10yr/5yr validity from expiry) rather than OCR-extracted.
        cursor.execute("SHOW COLUMNS FROM general_data LIKE 'issue_date_estimated'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE general_data ADD COLUMN issue_date_estimated BOOLEAN DEFAULT FALSE")
        # Migrate: add is_emergency flag for existing databases. Set TRUE
        # only for records saved while the "Emergency upload" checkbox was
        # checked, used to mark a group/record red + badge it in the UI.
        cursor.execute("SHOW COLUMNS FROM general_data LIKE 'is_emergency'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE general_data ADD COLUMN is_emergency BOOLEAN DEFAULT FALSE")
    cursor.execute("SHOW TABLES LIKE 'user_settings'")
    if cursor.fetchone():
        cursor.execute("SHOW COLUMNS FROM user_settings LIKE 'visa_type'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE user_settings ADD COLUMN visa_type VARCHAR(20) DEFAULT 'nusuk'")
        cursor.execute("SHOW COLUMNS FROM user_settings LIKE 'expected_arrival'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE user_settings ADD COLUMN expected_arrival DATE DEFAULT NULL")

    # 2. Create Passports Table (Scoped to user_id)
    # Drop legacy BLOB columns if they exist (migration: images now stored on filesystem)
    for _tbl, _col in [('passports', 'original_image'), ('passports', 'face_image'),
                       ('archived_passports', 'original_image'), ('archived_passports', 'face_image')]:
        try:
            cursor.execute(f"SHOW COLUMNS FROM {_tbl} LIKE '{_col}'")
            if cursor.fetchone():
                cursor.execute(f"ALTER TABLE {_tbl} DROP COLUMN {_col}")
                print(f"[Migration] Dropped {_tbl}.{_col} (filesystem storage)")
        except Exception:
            pass  # table may not exist yet

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS passports (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            filename VARCHAR(255),
            mrz_text TEXT,
            doc_type CHAR(1),
            country CHAR(3),
            surname VARCHAR(100),
            given_names VARCHAR(100),
            middle_name VARCHAR(100),
            passport_number VARCHAR(20),
            nationality CHAR(3),
            dob DATE,
            sex CHAR(1),
            expiry DATE,
            is_processed BOOLEAN DEFAULT FALSE,
            is_visa_processed BOOLEAN DEFAULT FALSE,
            processed_at TIMESTAMP NULL DEFAULT NULL,
            visa_processed_at TIMESTAMP NULL DEFAULT NULL,
            is_recycled BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            -- NOTE: passport_number is intentionally NOT globally unique per
            -- user anymore. Duplicate checking is strict PER GROUP and is
            -- enforced at the application level (see
            -- is_passport_number_exists_in_group / find_duplicate_groups_for_passports
            -- in this file), since group_name lives on general_data which
            -- is only linked after this row is inserted.
        )
    """)

    # ── Safe migration: drop the old global UNIQUE(user_id, passport_number)
    # constraint if it exists from a previous version of this schema, since
    # duplicates are now allowed across different groups. ──
    try:
        cursor.execute("""
            SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS
            WHERE TABLE_SCHEMA = 'passport_db' AND TABLE_NAME = 'passports'
              AND CONSTRAINT_TYPE = 'UNIQUE'
        """)
        for (_constraint_name,) in cursor.fetchall():
            try:
                cursor.execute(f"ALTER TABLE passports DROP INDEX `{_constraint_name}`")
                print(f"✓ Migrated: dropped old unique constraint {_constraint_name} on passports")
            except Exception as _drop_e:
                print(f"⚠ Could not drop constraint {_constraint_name}: {_drop_e}")
    except Exception as _e:
        print(f"⚠ passports unique-constraint migration skipped: {_e}")

    # 3. Create General Data Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS general_data (
            id INT AUTO_INCREMENT PRIMARY KEY,
            passport_id INT,
            group_name VARCHAR(100) DEFAULT 'GROUP 1',
            nationality_id INT DEFAULT 197,
            marital_status INT DEFAULT 5,
            city_of_birth VARCHAR(100) DEFAULT 'MAIN STREET',
            profession VARCHAR(100) DEFAULT 'TOURISM',
            city VARCHAR(100) DEFAULT 'MAIN STREET',
            zip_postal_code VARCHAR(20) DEFAULT '676542',
            address VARCHAR(255) DEFAULT 'ADDRESS',
            passport_type INT DEFAULT 1,
            passport_issue_place VARCHAR(100) DEFAULT 'PLACE',
            passport_issue_date DATE,
            issue_date_estimated BOOLEAN DEFAULT FALSE,
            expected_arrival DATE,
            expected_departure DATE,
            hotel_name VARCHAR(255) DEFAULT 'Hayat Mall Gate 6, Riyadh',
            contact_number VARCHAR(20) DEFAULT '',
            email VARCHAR(100) DEFAULT '',
            visa_type VARCHAR(20) DEFAULT 'nusuk',
            is_emergency BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (passport_id) REFERENCES passports(id) ON DELETE CASCADE
        )
    """)

    # 4. Create Invalid Passports Table (Scoped to user_id)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS invalid_passports (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            filename VARCHAR(255),
            original_image LONGBLOB,
            mrz_text TEXT,
            error_message TEXT,
            is_recycled BOOLEAN DEFAULT FALSE,
            extracted_issue_date DATE DEFAULT NULL,
            is_emergency BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 5. Create User Settings Table (Replaces global_defaults)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INT PRIMARY KEY,
            group_name VARCHAR(100) DEFAULT 'GROUP 1',
            marital_status INT DEFAULT 5,
            city_of_birth VARCHAR(100) DEFAULT 'MAIN STREET',
            profession VARCHAR(100) DEFAULT 'TOURISM',
            city VARCHAR(100) DEFAULT 'MAIN STREET',
            zip_postal_code VARCHAR(20) DEFAULT '676542',
            address VARCHAR(255) DEFAULT 'ADDRESS',
            passport_type INT DEFAULT 1,
            passport_issue_place VARCHAR(100) DEFAULT 'PLACE',
            hotel_name VARCHAR(255) DEFAULT 'Hayat Mall Gate 6, Riyadh',
            contact_number VARCHAR(20) DEFAULT '',
            email VARCHAR(100) DEFAULT '',
            visa_type VARCHAR(20) DEFAULT 'nusuk',
            expected_arrival DATE DEFAULT NULL
        )
    """)

    # 6. Create Archived Passports Tables (Scoped to user_id)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS archived_passports (
            id INT PRIMARY KEY,
            user_id INT NOT NULL,
            filename VARCHAR(255),
            mrz_text TEXT,
            doc_type CHAR(1),
            country CHAR(3),
            surname VARCHAR(100),
            given_names VARCHAR(100),
            middle_name VARCHAR(100),
            passport_number VARCHAR(20),
            nationality CHAR(3),
            dob DATE,
            sex CHAR(1),
            expiry DATE,
            created_at TIMESTAMP,
            archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS archived_general_data (
            id INT PRIMARY KEY,
            passport_id INT,
            group_name VARCHAR(100),
            nationality_id INT,
            marital_status INT,
            city_of_birth VARCHAR(100),
            profession VARCHAR(100),
            city VARCHAR(100),
            zip_postal_code VARCHAR(20),
            address VARCHAR(255),
            passport_type INT,
            passport_issue_place VARCHAR(100),
            passport_issue_date DATE,
            expected_arrival DATE,
            expected_departure DATE,
            hotel_name VARCHAR(255),
            contact_number VARCHAR(20),
            email VARCHAR(100),
            created_at TIMESTAMP,
            FOREIGN KEY (passport_id) REFERENCES archived_passports(id) ON DELETE CASCADE
        )
    """)

    # 7. Create Automation Queue Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS automation_queue (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            passport_id INT NOT NULL,
            status VARCHAR(50) DEFAULT 'PENDING',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (passport_id) REFERENCES passports(id) ON DELETE CASCADE
        )
    """)

    # Passport Daily Usage Log and Billing Records are now hosted remotely
    # (pms.passposys.com) — local copies removed.

    # 10. Group Batches — tracks Contract Login ID and sent status per group
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS group_batches (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            group_name VARCHAR(255) NOT NULL,
            contract_login_id VARCHAR(255) DEFAULT '',
            status ENUM('Unsent','Sent') DEFAULT 'Unsent',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMP NULL,
            UNIQUE KEY user_group (user_id, group_name)
        )
    """)

    # 11. group_batches: add last_activity_at column (updated whenever
    # records are moved INTO a group via Change Group or Merge, so the
    # group's position in the /groups list reflects recent activity even
    # when the moved records themselves are old). Falls back to the
    # group's own created_at (or NULL) until the first move/merge touches
    # it. See touch_group_activity() below.
    cursor.execute("SHOW COLUMNS FROM group_batches LIKE 'last_activity_at'")
    if not cursor.fetchone():
        cursor.execute(
            "ALTER TABLE group_batches ADD COLUMN last_activity_at TIMESTAMP NULL DEFAULT NULL"
        )

    # 11a. Visit Visa Queue — one-applicant-at-a-time send queue for passposys.exe.
    # Lives in MySQL (not an in-process dict) because gunicorn runs 4 worker
    # processes/threads; the request that starts the batch (send_visit_visa)
    # and the request that advances it (mark_processed_single, called by the
    # exe after each applicant finishes) can land on different workers.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS visit_visa_queue (
            id                INT AUTO_INCREMENT PRIMARY KEY,
            user_id           INT NOT NULL,
            batch_id          VARCHAR(36) NOT NULL,
            passport_id       INT NOT NULL,
            position          INT NOT NULL,
            status            ENUM('pending','sent','done','skipped') NOT NULL DEFAULT 'pending',
            applicant_json    LONGTEXT NOT NULL,
            credentials_json  TEXT NOT NULL,
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            FOREIGN KEY (passport_id) REFERENCES passports(id) ON DELETE CASCADE,
            INDEX idx_user_batch (user_id, batch_id, position),
            INDEX idx_passport (passport_id)
        )
    """)
# Inside init_db() in db.py:
    
    # 11c. Nusuk Queue — one-applicant-at-a-time send queue for Chrome Extension.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS nusuk_queue (
            id                INT AUTO_INCREMENT PRIMARY KEY,
            user_id           INT NOT NULL,
            batch_id          VARCHAR(36) NOT NULL,
            passport_id       INT NOT NULL,
            position          INT NOT NULL,
            status            ENUM('pending','sent','done','skipped') NOT NULL DEFAULT 'pending',
            applicant_json    LONGTEXT NOT NULL,
            credentials_json  TEXT NOT NULL,
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            FOREIGN KEY (passport_id) REFERENCES passports(id) ON DELETE CASCADE,
            INDEX idx_user_batch (user_id, batch_id, position),
            INDEX idx_passport (passport_id)
        )
    """)
    # 11d. Visit Visa Exe Registry — lets passposys.exe run on a *different*
    # PC than Flask. Each exe instance registers the IP/port it's reachable
    # on (plus a per-registration secret) for the user_id whose results.html
    # session it was launched alongside. send_visit_visa()/mark_processed_single
    # look up this table instead of always assuming 127.0.0.1:9001.
    # One row per user_id: a fresh registration overwrites the previous one,
    # so only the most recently started exe for that user is targeted.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS visit_visa_exe_registry (
            user_id      INT PRIMARY KEY,
            exe_host     VARCHAR(255) NOT NULL,
            exe_port     INT NOT NULL,
            exe_secret   VARCHAR(128) NOT NULL,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        )
    """)
    # 11b. Upload Progress — live per-user progress visible across all gunicorn workers
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS upload_progress (
            user_id   INT PRIMARY KEY,
            current_  INT DEFAULT 0,
            total     INT DEFAULT 0,
            success   INT DEFAULT 0,
            invalid   INT DEFAULT 0,
            duplicate INT DEFAULT 0,
            phase     VARCHAR(60) DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        )
    """)

    # ── Safe migration: add phase column if not already present ──
    try:
        cursor.execute("SHOW COLUMNS FROM upload_progress LIKE 'phase'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE upload_progress ADD COLUMN phase VARCHAR(60) DEFAULT ''")
            print("✓ Migrated: added phase column to upload_progress")
    except Exception as _e:
        print(f"⚠ phase column migration skipped: {_e}")

    conn.commit()
    cursor.close()
    conn.close()

    # Heal any user_settings rows left with NULL/'' values (e.g. rows created
    # before a column existed, or a blank value saved previously) so the
    # Upload page's General Data panel never renders the literal text "None".
    try:
        backfill_user_settings_defaults()
    except Exception as _e:
        print(f"⚠ user_settings defaults backfill skipped: {_e}")

    print("✓ Database initialized successfully with Multi-Tenant Support")

# =====================================================
# ACTIVE PASSPORTS - SELECT FUNCTIONS
# =====================================================

def get_group_visa_type(user_id, group_name):
    """
    Returns the visa_type ('nusuk' or 'visit_visa') that is ALREADY in use
    by the given group for this user, based on any active (not recycled)
    record currently in it. Returns None if the group doesn't exist yet
    (i.e. it has no active records) — meaning it's free to be created with
    either visa type.

    Used to enforce that uploads/appends into an EXISTING group must match
    that group's own visa type. A group's visa type is fixed by whatever
    was first uploaded into it; mixing visa types within the same group
    name is not allowed.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")
    cursor.execute("""
        SELECT COALESCE(g.visa_type, 'nusuk') AS visa_type
        FROM general_data g
        JOIN passports p ON g.passport_id = p.id
        WHERE p.user_id = %s
          AND g.group_name = %s
          AND (p.is_recycled = FALSE OR p.is_recycled IS NULL)
        LIMIT 1
    """, (user_id, group_name))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row['visa_type'] if row else None


def is_passport_number_exists_in_group(passport_number, user_id, group_name, visa_type="nusuk"):
    """
    Duplicate check before saving a new passport record. Applies the SAME
    unified logic regardless of whether the incoming record is Nusuk or
    Visit Visa:

      1. Same-group rule: an active record with the same passport number
         exists in the SAME group being uploaded into — always a
         duplicate, regardless of visa type, processing status, or date.
         Blocks immediately. Never offers Force Insert.

      2. Cross-record rule (independent of #1, runs against ALL other
         active records for this user regardless of which group they're
         in), split by the MATCHED record's visa type:

           a. Matched record is Visit Visa: duplicate if its
              expected_departure is still valid (>= today). Never offers
              Force Insert (regardless of what visa type is being
              uploaded).

           b. Matched record is Nusuk: duplicate if within a 365-day
              validity window —
                - if the matched record is_processed = TRUE, use
                  processed_at (processed_at >= now - 365 days), else
                - fall back to created_at (created_at >= now - 365 days).
              Always offers Force Insert.

         Both (a) and (b) are checked independently; if both match
         (against different existing records), BOTH are returned so the
         caller can show both messages together. Force Insert is offered
         overall if ANY returned match allows it (i.e. any Nusuk match
         is present).

    Returns a dict with details of the blocking record(s) if a duplicate
    is found, otherwise None. The top-level keys mirror the FIRST match
    found (for backward compatibility with callers reading the single
    dict), and a 'matches' list carries every distinct hit:
        {
            'group_name': <group of the first/primary colliding record>,
            'rule': 'same_group' | 'cross_group_visit_visa_valid'
                    | 'cross_group_1year',
            'matched_visa_type': 'nusuk' | 'visit_visa' | None,
            'expected_departure': <date or None>,
            'processed_at': <datetime or None>,
            'created_at': <datetime or None>,
            'can_force_insert': <bool>,
            'matches': [ {..same keys..}, ... ]
        }
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")

    # ── Rule 1: same-group, any active record — blocks immediately,
    #    regardless of the visa type being uploaded or matched. ──
    cursor.execute("""
        SELECT g.group_name
        FROM passports p
        JOIN general_data g ON g.passport_id = p.id
        WHERE p.passport_number = %s
          AND p.user_id = %s
          AND g.group_name = %s
          AND (p.is_recycled = FALSE OR p.is_recycled IS NULL)
        LIMIT 1
    """, (passport_number, user_id, group_name))
    row = cursor.fetchone()
    if row:
        cursor.close()
        conn.close()
        match = {
            'group_name': row['group_name'],
            'rule': 'same_group',
            'matched_visa_type': None,
            'expected_departure': None,
            'processed_at': None,
            'created_at': None,
            'can_force_insert': False,
        }
        match['matches'] = [match]
        return match

    matches = []
    _incoming_is_nusuk = (visa_type or "nusuk") == "nusuk"

    # ── Rule 2a: matched record is Visit Visa, still valid (expiry not
    #    passed). Checked across ALL other groups. Force Insert depends on
    #    the INCOMING upload's visa type, not the matched record's type:
    #      - incoming upload is Nusuk  -> Force Insert offered
    #      - incoming upload is Visit Visa -> never Force Insert ──
    cursor.execute("""
        SELECT g.group_name, g.expected_departure
        FROM passports p
        JOIN general_data g ON g.passport_id = p.id
        WHERE p.passport_number = %s
          AND p.user_id = %s
          AND g.group_name != %s
          AND (p.is_recycled = FALSE OR p.is_recycled IS NULL)
          AND COALESCE(g.visa_type, 'nusuk') = 'visit_visa'
          AND g.expected_departure IS NOT NULL
          AND g.expected_departure >= CURDATE()
        LIMIT 1
    """, (passport_number, user_id, group_name))
    row = cursor.fetchone()
    if row:
        matches.append({
            'group_name': row['group_name'],
            'rule': 'cross_group_visit_visa_valid',
            'matched_visa_type': 'visit_visa',
            'expected_departure': row['expected_departure'],
            'processed_at': None,
            'created_at': None,
            'can_force_insert': _incoming_is_nusuk,
        })

    # ── Rule 2b: matched record is Nusuk, within 365-day validity.
    #    Checked across ALL other groups. First try processed_at (when
    #    is_processed = TRUE), else fall back to created_at. Force Insert
    #    is never offered for a Nusuk match. ──
    cursor.execute("""
        SELECT g.group_name, p.processed_at, p.created_at, p.is_processed
        FROM passports p
        JOIN general_data g ON g.passport_id = p.id
        WHERE p.passport_number = %s
          AND p.user_id = %s
          AND g.group_name != %s
          AND (p.is_recycled = FALSE OR p.is_recycled IS NULL)
          AND COALESCE(g.visa_type, 'nusuk') = 'nusuk'
          AND (
                (p.is_processed = TRUE AND p.processed_at IS NOT NULL
                 AND p.processed_at >= (NOW() - INTERVAL 365 DAY))
             OR (
                 (p.is_processed = FALSE OR p.is_processed IS NULL)
                 AND p.created_at IS NOT NULL
                 AND p.created_at >= (NOW() - INTERVAL 365 DAY)
               )
          )
        LIMIT 1
    """, (passport_number, user_id, group_name))
    row = cursor.fetchone()
    if row:
        _is_processed_match = bool(row.get('is_processed')) and row.get('processed_at') is not None
        matches.append({
            'group_name': row['group_name'],
            'rule': 'cross_group_1year',
            'matched_visa_type': 'nusuk',
            'expected_departure': None,
            'processed_at': row['processed_at'] if _is_processed_match else None,
            'created_at': None if _is_processed_match else row['created_at'],
            'can_force_insert': False,
        })

    cursor.close()
    conn.close()

    if not matches:
        return None

    primary = dict(matches[0])
    primary['matches'] = matches
    primary['can_force_insert'] = any(m['can_force_insert'] for m in matches)
    return primary


def is_passport_number_exists_in_group_same_group_only(passport_number, user_id, group_name):
    """
    Duplicate check scoped ONLY to the same group being uploaded into
    (mirrors Rule 1 of is_passport_number_exists_in_group(), without any
    cross-group / cross-visa-type checks). Used for "Emergency upload"
    checkbox uploads, where the record is saved into the normal
    passports/general_data tables under whatever group name the user
    provides, but duplication is only ever checked within that same group.

    An active (not recycled) record with the same passport number already
    in this exact group is always a duplicate. Blocks immediately, no
    Force Insert.

    Returns a dict with details of the blocking record if found, else None:
        {
            'group_name': <group of the colliding record>,
            'rule': 'same_group',
            'can_force_insert': False,
            'matches': [ {..same keys..} ]
        }
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")

    cursor.execute("""
        SELECT g.group_name
        FROM passports p
        JOIN general_data g ON g.passport_id = p.id
        WHERE p.passport_number = %s
          AND p.user_id = %s
          AND g.group_name = %s
          AND (p.is_recycled = FALSE OR p.is_recycled IS NULL)
        LIMIT 1
    """, (passport_number, user_id, group_name))
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        return None

    match = {
        'group_name': row['group_name'],
        'rule': 'same_group',
        'can_force_insert': False,
    }
    match['matches'] = [match]
    return match


def get_active_passport_in_group(user_id, passport_number, group_name):
    """
    Returns the currently active (not recycled) passport record matching this
    passport number for this user WITHIN a specific group. Used for
    per-group duplicate detection (e.g. before saving or before a group
    change is finalized).
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")
    cursor.execute("""
        SELECT p.id, p.passport_number, p.is_recycled, p.is_processed, p.processed_at,
               p.created_at,
               g.group_name, COALESCE(g.visa_type, 'nusuk') AS visa_type,
               g.expected_departure
        FROM passports p
        LEFT JOIN general_data g ON g.passport_id = p.id
        WHERE p.user_id = %s AND p.passport_number = %s AND g.group_name = %s
          AND (p.is_recycled = FALSE OR p.is_recycled IS NULL)
        LIMIT 1
    """, (user_id, passport_number, group_name))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def find_duplicate_groups_for_passports(passport_ids, user_id, target_group_name):
    """
    Given a list of passport IDs about to be moved into target_group_name,
    returns a list of dicts describing which of them already have an active
    duplicate (same passport_number) sitting in the target group. Used to
    warn the user before finalizing a 'Change Group' action.

    Each dict: {
        'passport_id': <id being moved>,
        'passport_number': <number>,
        'existing_passport_id': <id of the colliding record already in target group>,
        'group_name': <target_group_name>
    }
    """
    if not passport_ids:
        return []

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")
    fmt = ','.join(['%s'] * len(passport_ids))

    cursor.execute(f"""
        SELECT p.id AS passport_id, p.passport_number
        FROM passports p
        WHERE p.id IN ({fmt}) AND p.user_id = %s
    """, tuple(passport_ids) + (user_id,))
    moving = cursor.fetchall()

    duplicates = []
    for row in moving:
        cursor.execute("""
            SELECT p2.id AS existing_passport_id
            FROM passports p2
            JOIN general_data g2 ON g2.passport_id = p2.id
            WHERE p2.user_id = %s
              AND p2.passport_number = %s
              AND g2.group_name = %s
              AND p2.id != %s
              AND (p2.is_recycled = FALSE OR p2.is_recycled IS NULL)
            LIMIT 1
        """, (user_id, row['passport_number'], target_group_name, row['passport_id']))
        existing = cursor.fetchone()
        if existing:
            duplicates.append({
                'passport_id': row['passport_id'],
                'passport_number': row['passport_number'],
                'existing_passport_id': existing['existing_passport_id'],
                'group_name': target_group_name
            })

    cursor.close()
    conn.close()
    return duplicates


def get_all_passports_with_general_data(user_id, page=1, per_page=25, passport_number=None, group_name=None, include_images=True):
    """
    Images are now served from filesystem via /passport_image/<id> and /face_image/<id>.
    include_images parameter kept for backward compatibility but no longer fetches BLOBs.
    """
    offset = (page - 1) * per_page
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")

    where_clauses = ["p.user_id = %s", "(p.is_recycled = FALSE OR p.is_recycled IS NULL)"]
    params = [user_id]

    if passport_number:
        where_clauses.append("p.passport_number LIKE %s")
        params.append(f"%{passport_number}%")
    if group_name:
        where_clauses.append("g.group_name = %s")
        params.append(group_name)

    where_sql = " WHERE " + " AND ".join(where_clauses)
    select_cols = """
        p.id, p.user_id, p.filename, p.mrz_text, p.doc_type, p.country,
        p.surname, p.given_names, p.middle_name, p.passport_number,
        p.nationality, p.dob, p.sex, p.expiry, p.is_processed,
        p.is_recycled, p.created_at,
        g.id AS g_id, g.passport_id, g.group_name, g.nationality_id,
        g.marital_status, g.city_of_birth, g.profession, g.city,
        g.zip_postal_code, g.address, g.passport_type, g.passport_issue_place,
        g.passport_issue_date, g.issue_date_estimated, g.expected_arrival, g.expected_departure,
        g.hotel_name, g.contact_number, g.email, g.created_at AS g_created_at
    """

    query = f"""
        SELECT {select_cols}
        FROM passports p
        LEFT JOIN general_data g ON p.id = g.passport_id
        {where_sql}
        ORDER BY p.created_at DESC
        LIMIT %s OFFSET %s
    """
    params.extend([per_page, offset])

    cursor.execute(query, tuple(params))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    # Images served from filesystem — no BLOBs in DB
    for row in rows:
        row['original_image_b64'] = None
        row['face_image_b64'] = None

    return rows

def get_total_passport_count(user_id, passport_number=None, group_name=None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")

    where_clauses = ["p.user_id = %s", "(p.is_recycled = FALSE OR p.is_recycled IS NULL)"]
    params = [user_id]
    
    if passport_number:
        where_clauses.append("p.passport_number LIKE %s")
        params.append(f"%{passport_number}%")
    if group_name:
        where_clauses.append("g.group_name = %s")
        params.append(group_name)
        
    where_sql = " WHERE " + " AND ".join(where_clauses)

    query = f"""
        SELECT COUNT(*) 
        FROM passports p
        LEFT JOIN general_data g ON p.id = g.passport_id
        {where_sql}
    """
    
    cursor.execute(query, tuple(params))
    count = cursor.fetchone()[0]

    cursor.close()
    conn.close()
    return count

def get_passport_by_id(passport_id, user_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("USE passport_db")
        cursor.execute("""
            SELECT p.*, g.*,
                   p.id AS id
            FROM passports p
            LEFT JOIN general_data g ON p.id = g.passport_id
            WHERE p.id = %s AND p.user_id = %s
        """, (passport_id, user_id))
        row = cursor.fetchone()
        # Images served from filesystem — no face_image BLOB in passports table
        return row
    finally:
        cursor.close()
        conn.close()


def get_passport_by_number(passport_number, user_id):
    """
    Looks up an ACTIVE (non-recycled) passport by its passport number for
    this user. Used by the "Generate from Excel Data" badge flow, which
    resolves each uploaded passport number to a full record the same way
    get_passport_by_id() does for the group-based flow.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("USE passport_db")
        cursor.execute("""
            SELECT p.*, g.*,
                   p.id AS id
            FROM passports p
            LEFT JOIN general_data g ON p.id = g.passport_id
            WHERE p.passport_number = %s AND p.user_id = %s
              AND (p.is_recycled = FALSE OR p.is_recycled IS NULL)
            LIMIT 1
        """, (passport_number, user_id))
        return cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

# =====================================================
# ACTIVE PASSPORTS - INSERT FUNCTIONS
# =====================================================

def get_user_settings(user_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")
    cursor.execute("SELECT * FROM user_settings WHERE user_id = %s", (user_id,))
    row = cursor.fetchone()
    
    if not row:
        # Create default settings if user doesn't have them yet
        cursor.execute("INSERT IGNORE INTO user_settings (user_id) VALUES (%s)", (user_id,))
        conn.commit()
        cursor.execute("SELECT * FROM user_settings WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()

    cursor.close()
    conn.close()
    return row

# Matches the column DEFAULTs declared on `user_settings` in the CREATE TABLE
# above. Used so a saved setting can never regress to NULL/blank — if the
# caller submits an empty value for one of these fields, we fall back to the
# same default the column would get on a fresh INSERT, instead of persisting
# NULL/'' and having it silently show up as the literal text "None" later.
USER_SETTINGS_FIELD_DEFAULTS = {
    'group_name': 'GROUP 1',
    'marital_status': 5,
    'city_of_birth': 'MAIN STREET',
    'profession': 'TOURISM',
    'city': 'MAIN STREET',
    'zip_postal_code': '676542',
    'address': 'ADDRESS',
    'passport_type': 1,
    'passport_issue_place': 'PLACE',
    'hotel_name': 'Hayat Mall Gate 6, Riyadh',
    'visa_type': 'nusuk',
}


def _settings_value(data, field):
    val = data.get(field)
    if val is None or (isinstance(val, str) and val.strip() == ''):
        return USER_SETTINGS_FIELD_DEFAULTS.get(field)
    return val


def update_user_settings(user_id, data):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")

    # expected_arrival comes in as a 'YYYY-MM-DD' string (or blank) — store
    # NULL if it's empty/invalid so it doesn't break the DATE column.
    raw_arrival = (data.get('expected_arrival') or '').strip()
    arrival_val = raw_arrival if re.match(r'^\d{4}-\d{2}-\d{2}$', raw_arrival) else None

    cursor.execute("""
        UPDATE user_settings SET
            group_name=%s, marital_status=%s, city_of_birth=%s,
            profession=%s, city=%s, zip_postal_code=%s, address=%s,
            passport_type=%s, passport_issue_place=%s, hotel_name=%s,
            visa_type=%s, expected_arrival=%s
        WHERE user_id=%s
    """, (
        _settings_value(data, 'group_name'), _settings_value(data, 'marital_status'), _settings_value(data, 'city_of_birth'),
        _settings_value(data, 'profession'), _settings_value(data, 'city'), _settings_value(data, 'zip_postal_code'), _settings_value(data, 'address'),
        _settings_value(data, 'passport_type'), _settings_value(data, 'passport_issue_place'), _settings_value(data, 'hotel_name'),
        _settings_value(data, 'visa_type'),
        arrival_val,
        user_id
    ))
    conn.commit()
    cursor.close()
    conn.close()


def backfill_user_settings_defaults():
    """
    One-time repair for rows created before some `user_settings` columns
    existed (or where a value was previously saved as NULL/''): re-applies
    the same column defaults declared in the CREATE TABLE statement so old
    rows stop rendering the literal text "None" in the Upload page's
    General Data panel. Safe to run repeatedly — only touches NULL/''.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    for field, default in USER_SETTINGS_FIELD_DEFAULTS.items():
        cursor.execute(
            f"UPDATE user_settings SET {field}=%s WHERE {field} IS NULL OR {field} = ''",
            (default,)
        )
    conn.commit()
    cursor.close()
    conn.close()
    

def insert_passport(user_id, data, original_blob=None, face_blob=None, mrz_text="", filename=""):
    # NOTE: original_blob and face_blob are accepted but IGNORED.
    # Images are stored on the filesystem, not in the DB.
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("USE passport_db")

        cursor.execute("""
            INSERT INTO passports (
                user_id, filename, mrz_text,
                doc_type, country, surname, given_names, middle_name,
                passport_number, nationality, dob, sex, expiry
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            user_id,
            filename,
            mrz_text,
            data["doc_type"],
            data["country"],
            data["surname"],
            data["given_names"],
            data.get("middle_name", ""),
            data["passport_number"],
            data["nationality"],
            data["dob"],
            data["sex"],
            data["expiry"]
        ))

        passport_id = cursor.lastrowid
        conn.commit()
        return passport_id

    except mysql.connector.IntegrityError as e:
        # The global UNIQUE(user_id, passport_number) constraint has been
        # removed — duplicates are now allowed across different groups and
        # are instead blocked strictly per-group at the application level
        # (see is_passport_number_exists_in_group), checked BEFORE this
        # function is called. This branch is kept only as a safety net for
        # any other integrity error.
        raise e
    finally:
        cursor.close()
        conn.close()


def insert_general_data(
    passport_id,
    nationality_id=197,
    marital_status=5,
    group_name="GROUP 1",
    city_of_birth="MAIN STREET",
    profession="TOURISM",
    city="MAIN STREET",
    zip_postal_code="676542",
    address="ADDRESS",
    passport_type=1,
    passport_issue_place="PLACE",
    passport_issue_date=None,
    issue_date_estimated=False,
    expected_arrival=None,
    expected_departure=None,
    hotel_name="Hayat Mall Gate 6, Riyadh",
    contact_number="",
    email="",
    visa_type="nusuk",
    is_emergency=False
):
    today = ist_now().date()

    # Nusuk records don't track hotel stay dates, or any of the other
    # "general data" fields (city of birth, profession, city/zip, address,
    # issue place, hotel name) — leave them all empty rather than falling
    # back to the hardcoded/user-configured defaults. Visit Visa records
    # keep the usual defaults (today → +365 days, MAIN STREET, etc.) when
    # not explicitly provided.
    if (visa_type or "nusuk") == "nusuk":
        expected_arrival = None
        expected_departure = None
        city_of_birth = ""
        profession = ""
        city = ""
        zip_postal_code = ""
        address = ""
        passport_issue_place = ""
        hotel_name = ""
    else:
        if expected_arrival is None:
            expected_arrival = today
        if expected_departure is None:
            expected_departure = today + timedelta(days=365)

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")

    cursor.execute("""
        INSERT INTO general_data (
            passport_id, nationality_id, marital_status,
            group_name, city_of_birth, profession, city,
            zip_postal_code, address, passport_type,
            passport_issue_place, passport_issue_date, issue_date_estimated,
            expected_arrival, expected_departure,
            hotel_name, contact_number, email, visa_type, is_emergency
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        passport_id, nationality_id, marital_status, group_name,
        city_of_birth, profession, city, zip_postal_code, address,
        passport_type, passport_issue_place, passport_issue_date, bool(issue_date_estimated),
        expected_arrival, expected_departure, hotel_name, 
        contact_number or "", email or "", visa_type or "nusuk", bool(is_emergency)
    ))

    conn.commit()
    cursor.close()
    conn.close()

def update_passport_data(passport_id, user_id, data, mrz_text):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")

    cursor.execute("""
        UPDATE passports SET
            mrz_text=%s,
            country=%s,
            surname=%s,
            given_names=%s,
            middle_name=%s,
            passport_number=%s,
            nationality=%s,
            dob=%s,
            sex=%s,
            expiry=%s
        WHERE id=%s AND user_id=%s
    """, (
        mrz_text,
        data["country"],
        data["surname"],
        data["given_names"],
        data.get("middle_name", ""),
        data["passport_number"],
        data["nationality"],
        data["dob"],
        data["sex"],
        data["expiry"],
        passport_id,
        user_id
    ))

    conn.commit()
    cursor.close()
    conn.close()

def update_general_nationality(passport_id, user_id, nationality_code):
    nationality_id = NATIONALITY_CODE_TO_ID.get(nationality_code, 197)

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")

    # Only update if the passport belongs to the user
    cursor.execute("""
        UPDATE general_data gd
        JOIN passports p ON gd.passport_id = p.id
        SET gd.nationality_id = %s 
        WHERE gd.passport_id = %s AND p.user_id = %s
    """, (nationality_id, passport_id, user_id))

    conn.commit()
    cursor.close()
    conn.close()

def safe_int(val, default=0):
    try:
        if val is None or str(val).strip() == "":
            return default
        return int(float(val))
    except (ValueError, TypeError):
        return default
        
def update_general_data(passport_id, user_id, form_data):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")

    cursor.execute("""
        UPDATE general_data gd
        JOIN passports p ON gd.passport_id = p.id
        SET gd.group_name = %s,
            gd.nationality_id = %s,
            gd.marital_status = %s,
            gd.passport_issue_date = %s,
            gd.expected_arrival = %s,
            gd.expected_departure = %s,
            gd.city_of_birth = %s,
            gd.profession = %s,
            gd.city = %s,
            gd.zip_postal_code = %s,
            gd.address = %s,
            gd.passport_type = %s,
            gd.passport_issue_place = %s,
            gd.hotel_name = %s,
            gd.contact_number = %s,
            gd.email = %s
        WHERE gd.passport_id = %s AND p.user_id = %s
    """, (
        form_data.get("group_name", "GROUP 1"),
        safe_int(form_data.get("nationality_id"), 197),
        safe_int(form_data.get("marital_status"), 5),
        form_data.get("passport_issue_date"), 
        form_data.get("expected_arrival"),    
        form_data.get("expected_departure"),  
        form_data.get("city_of_birth", "MAIN STREET"),
        form_data.get("profession", "TOURISM"),
        form_data.get("city", "MAIN STREET"),
        form_data.get("zip_postal_code", "676542"),
        form_data.get("address", "ADDRESS"),
        safe_int(form_data.get("passport_type"), 1),
        form_data.get("passport_issue_place", "PLACE"),
        form_data.get("hotel_name", "Hayat Mall Gate 6, Riyadh"),
        form_data.get("contact_number", ""), 
        form_data.get("email", ""),          
        passport_id,
        user_id
    ))

    conn.commit()
    cursor.close()
    conn.close()


# =====================================================
# ACTIVE PASSPORTS - RECYCLE BIN & DELETE
# =====================================================

def delete_passport_record(passport_id, user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")

    # Fetch filename + group before recycling, so we can move the files.
    cursor.execute("""
        SELECT p.filename, g.group_name
        FROM passports p
        LEFT JOIN general_data g ON g.passport_id = p.id
        WHERE p.id=%s AND p.user_id=%s
    """, (passport_id, user_id))
    row = cursor.fetchone()

    # Only set is_recycled — do NOT overwrite created_at.
    # Overwriting created_at breaks sorting and the auto-cleanup timer.
    cursor.execute("""
        UPDATE passports 
        SET is_recycled = TRUE
        WHERE id=%s AND user_id=%s
    """, (passport_id, user_id))
    
    conn.commit()
    cursor.close()
    conn.close()

    if row:
        filename, group_name = row
        _recycle_passport_files(filename, group_name)


def get_recycled_passports(user_id, page=1, per_page=25, include_images=True):
    """Images served from filesystem. include_images kept for backward compat."""
    offset = (page - 1) * per_page
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")

    select_cols = """
        p.id, p.user_id, p.filename, p.mrz_text, p.doc_type, p.country,
        p.surname, p.given_names, p.middle_name, p.passport_number,
        p.nationality, p.dob, p.sex, p.expiry, p.is_processed,
        p.is_recycled, p.created_at,
        g.group_name, g.nationality_id, g.marital_status, g.city_of_birth,
        g.profession, g.city, g.zip_postal_code, g.address, g.passport_type,
        g.passport_issue_place, g.passport_issue_date,
        g.expected_arrival, g.expected_departure, g.hotel_name
    """

    cursor.execute(f"""
        SELECT {select_cols}
        FROM passports p
        LEFT JOIN general_data g ON p.id = g.passport_id
        WHERE p.is_recycled = TRUE AND p.user_id = %s
        ORDER BY p.created_at DESC
        LIMIT %s OFFSET %s
    """, (user_id, per_page, offset))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    for row in rows:
        row['original_image_b64'] = None
        row['face_image_b64'] = None
    return rows

def get_total_recycled_passports_count(user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    cursor.execute("SELECT COUNT(*) FROM passports WHERE is_recycled = TRUE AND user_id = %s", (user_id,))
    count = cursor.fetchone()[0]
    cursor.close()
    conn.close()
    return count

def get_restore_conflict_for_passport(passport_id, user_id):
    """
    Checks whether restoring this recycled passport would violate the SAME
    duplication rules enforced on upload/group-change (same-group, and the
    cross-group Visit-Visa-validity / Nusuk-365-day rules) — via
    is_passport_number_exists_in_group() — since restore must respect them
    too instead of silently reactivating a row without going through
    either path.

    Returns a dict describing the conflict, or None if the restore is safe:
    {
        'passport_id': <id being restored>,
        'passport_number': <number>,
        'group_name': <group of the colliding record>,
        'existing_passport_id': <id of the colliding ACTIVE record>,
        'rule': <the duplication rule that fired>,
        'matched_visa_type': <visa type of the colliding record>,
    }
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")
    cursor.execute("""
        SELECT p.passport_number, g.group_name, COALESCE(g.visa_type, 'nusuk') AS visa_type
        FROM passports p
        LEFT JOIN general_data g ON g.passport_id = p.id
        WHERE p.id = %s AND p.user_id = %s
    """, (passport_id, user_id))
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row or not row.get('passport_number') or not row.get('group_name'):
        # Nothing to compare against (e.g. no group linked) — allow restore.
        return None

    dup = is_passport_number_exists_in_group(
        row['passport_number'], user_id, row['group_name'], row['visa_type']
    )
    if not dup:
        return None

    return {
        'passport_id': passport_id,
        'passport_number': row['passport_number'],
        'group_name': dup.get('group_name'),
        'existing_passport_id': dup.get('existing_passport_id'),
        'rule': dup.get('rule'),
        'matched_visa_type': dup.get('matched_visa_type'),
    }


def restore_passport(passport_id, user_id, force=False):
    """
    Restores a recycled passport. Unless force=True, refuses the restore if
    an active duplicate (same passport_number, same group) already exists,
    raising ValueError with the conflict details so the caller can prompt
    the user (mirroring the group-change duplicate-conflict flow).
    """
    if not force:
        conflict = get_restore_conflict_for_passport(passport_id, user_id)
        if conflict:
            raise DuplicateRestoreConflict(conflict)

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")

    cursor.execute("""
        SELECT p.filename, g.group_name
        FROM passports p
        LEFT JOIN general_data g ON g.passport_id = p.id
        WHERE p.id=%s AND p.user_id=%s
    """, (passport_id, user_id))
    row = cursor.fetchone()

    cursor.execute("UPDATE passports SET is_recycled = FALSE WHERE id=%s AND user_id=%s", (passport_id, user_id))
    conn.commit()
    cursor.close()
    conn.close()

    if row:
        filename, group_name = row
        _restore_passport_files(filename, group_name)


class DuplicateRestoreConflict(Exception):
    """Raised when restoring a record would collide with an active duplicate."""
    def __init__(self, conflict):
        self.conflict = conflict
        _id = conflict.get('passport_id', conflict.get('invalid_id'))
        super().__init__(
            f"Restoring record {_id} would duplicate "
            f"{conflict['passport_number']} already active in group "
            f"\"{conflict['group_name']}\" (existing id {conflict['existing_passport_id']}, "
            f"rule: {conflict.get('rule')})."
        )

def hard_delete_passport(passport_id, user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    # Get filename before deleting
    cursor.execute("SELECT filename FROM passports WHERE id=%s AND user_id=%s", (passport_id, user_id))
    row = cursor.fetchone()
    cursor.execute("DELETE FROM passports WHERE id=%s AND user_id=%s", (passport_id, user_id))
    conn.commit()
    cursor.close()
    conn.close()
    if row:
        _delete_passport_files(row[0])

def empty_recycled_passports(user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    # Get all filenames first
    cursor.execute("SELECT filename FROM passports WHERE is_recycled = TRUE AND user_id = %s", (user_id,))
    filenames = [r[0] for r in cursor.fetchall()]
    cursor.execute("DELETE FROM passports WHERE is_recycled = TRUE AND user_id = %s", (user_id,))
    conn.commit()
    cursor.close()
    conn.close()
    for fname in filenames:
        _delete_passport_files(fname)

# =====================================================
# INVALID PASSPORTS (MAIN + RECYCLE BIN)
# =====================================================

def get_all_invalid_passports(user_id, page=1, per_page=25, include_images=True):
    """
    OPTIMIZED: include_images=False skips loading the LONGBLOB column entirely.
    Use when the template renders images via /db_invalid_image/<id> URL routes.
    """
    offset = (page - 1) * per_page
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")

    if include_images:
        select_cols = "*"
    else:
        select_cols = "id, user_id, filename, mrz_text, error_message, is_recycled, created_at, upload_group_name, upload_visa_type"

    cursor.execute(f"""
        SELECT {select_cols} FROM invalid_passports
        WHERE user_id = %s AND (is_recycled = FALSE OR is_recycled IS NULL)
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
    """, (user_id, per_page, offset))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    for row in rows:
        row['original_image_b64'] = (
            base64.b64encode(row['original_image']).decode('utf-8')
            if include_images and row.get('original_image') else None
        )
    return rows

def get_total_invalid_count(user_id):
    conn = get_connection()
    cursor = conn.cursor()
    
    # We add the condition to ignore anything flagged as recycled
    cursor.execute("""
        SELECT COUNT(*) 
        FROM invalid_passports 
        WHERE user_id = %s AND (is_recycled = FALSE OR is_recycled IS NULL)
    """, (user_id,))
    
    result = cursor.fetchone()
    cursor.close()
    conn.close()
    
    return result[0] if result else 0

def insert_invalid_passport(user_id, filename, original_blob, mrz_text, error_message,
                             upload_group_name=None, upload_visa_type=None, extracted_issue_date=None,
                             is_emergency=False):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    cursor.execute("""
    INSERT INTO invalid_passports (
        user_id, filename, original_image, mrz_text, error_message,
        upload_group_name, upload_visa_type, extracted_issue_date, is_emergency
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        user_id,
        filename,
        original_blob,
        mrz_text,
        error_message,
        upload_group_name,
        upload_visa_type,
        extracted_issue_date,
        bool(is_emergency)
    ))
    invalid_id = cursor.lastrowid
    conn.commit()
    cursor.close()
    conn.close()
    return invalid_id


def get_invalid_passport_by_id(invalid_id, user_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")
    cursor.execute("""
    SELECT * FROM invalid_passports WHERE id = %s AND user_id = %s
    """, (invalid_id, user_id))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    
    if row:
        if row.get('original_image'):
            row['original_image_b64'] = base64.b64encode(row['original_image']).decode('utf-8')
        else:
            row['original_image_b64'] = None

    return row

def delete_invalid_passport(invalid_id, user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    
    cursor.execute("""
        UPDATE invalid_passports 
        SET is_recycled = TRUE, created_at = CURRENT_TIMESTAMP 
        WHERE id = %s AND user_id = %s
    """, (invalid_id, user_id))
    
    conn.commit()
    cursor.close()
    conn.close()

def get_all_recycled_invalid_passports(user_id, page=1, per_page=25, include_images=True):
    """OPTIMIZED: include_images=False avoids LONGBLOB fetch on recycle-bin listing pages."""
    offset = (page - 1) * per_page
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")

    if include_images:
        select_cols = "*"
    else:
        select_cols = "id, user_id, filename, mrz_text, error_message, is_recycled, created_at"

    cursor.execute(f"""
        SELECT {select_cols} FROM invalid_passports
        WHERE user_id = %s AND is_recycled = TRUE
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
    """, (user_id, per_page, offset))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    for row in rows:
        row['original_image_b64'] = (
            base64.b64encode(row['original_image']).decode('utf-8')
            if include_images and row.get('original_image') else None
        )
    return rows

def get_total_recycled_invalid_count(user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    cursor.execute("SELECT COUNT(*) FROM invalid_passports WHERE user_id = %s AND is_recycled = TRUE", (user_id,))
    count = cursor.fetchone()[0]
    cursor.close()
    conn.close()
    return count

def get_restore_conflict_for_invalid(invalid_id, user_id):
    """
    invalid_passports rows have no passport_number/group_name columns — the
    only record of why a row was rejected is the free-text error_message
    (e.g. "Duplicate passport number: X12345 (Group: GROUP 1)"), the same
    string the Invalid Passports page already regex-parses for display.

    Re-parses that string to recover (passport_number, group_name) and runs
    the SAME duplication rules enforced on upload/group-change (same-group,
    and the cross-group Visit-Visa-validity / Nusuk-365-day rules) via
    is_passport_number_exists_in_group(). Non-duplicate invalid records
    (bad MRZ, expiry, etc.) have no such pattern and are always safe to
    restore.

    Returns a conflict dict (see get_restore_conflict_for_passport) or None.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")
    cursor.execute(
        "SELECT error_message, upload_visa_type FROM invalid_passports WHERE id = %s AND user_id = %s",
        (invalid_id, user_id)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        return None
    err_msg = row.get('error_message') or ''
    if 'Duplicate passport number' not in err_msg:
        return None

    m = re.search(r'Duplicate passport number:\s*([A-Za-z0-9<]+)', err_msg)
    g = re.search(r'\(Group:\s*(.+?)\)', err_msg)
    if not m or not g:
        # Message doesn't match the expected pattern — can't safely verify,
        # let the caller decide (defaults to allowing restore).
        return None

    passport_number = m.group(1).strip()
    group_name = g.group(1).strip()
    visa_type = row.get('upload_visa_type') or 'nusuk'

    dup = is_passport_number_exists_in_group(passport_number, user_id, group_name, visa_type)
    if not dup:
        return None
    return {
        'invalid_id': invalid_id,
        'passport_number': passport_number,
        'group_name': dup.get('group_name'),
        'existing_passport_id': dup.get('existing_passport_id'),
        'rule': dup.get('rule'),
        'matched_visa_type': dup.get('matched_visa_type'),
    }


def restore_invalid_passport(invalid_id, user_id, force=False):
    """
    Restores an invalid/duplicate record from the recycle bin. Unless
    force=True, refuses if the original duplicate-triggering passport
    number is still active in that group, raising DuplicateRestoreConflict.
    """
    if not force:
        conflict = get_restore_conflict_for_invalid(invalid_id, user_id)
        if conflict:
            raise DuplicateRestoreConflict(conflict)

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    cursor.execute("UPDATE invalid_passports SET is_recycled = FALSE WHERE id = %s AND user_id = %s", (invalid_id, user_id))
    conn.commit()
    cursor.close()
    conn.close()

def hard_delete_invalid_passport(invalid_id, user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    cursor.execute("DELETE FROM invalid_passports WHERE id = %s AND user_id = %s", (invalid_id, user_id))
    conn.commit()
    cursor.close()
    conn.close()

def get_filename_group_map():
    """
    Returns a dict {filename: (group_name, visa_type, passport_number)} for all
    (non-recycled) passports across all users, used by the backup routines to
    organize the passport/face image folders into <visa_type>/<GROUP_NAME>/
    subfolders and rename the copies to <passport_number>.<ext> inside the zip.
    Falls back to 'GROUP 1' / 'nusuk' if a passport has no general_data row,
    and to the original filename if passport_number is blank.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    cursor.execute("""
        SELECT p.filename, COALESCE(g.group_name, 'GROUP 1'), COALESCE(g.visa_type, 'nusuk'),
               p.passport_number
        FROM passports p
        LEFT JOIN general_data g ON p.id = g.passport_id
        WHERE p.filename IS NOT NULL AND p.filename != ''
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return {
        filename: (group_name, visa_type, passport_number)
        for filename, group_name, visa_type, passport_number in rows
    }


# =====================================================
# BACKGROUND AUTO-CLEANUP TASKS
# =====================================================

def auto_empty_recycle_bin():
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("USE passport_db")
        cursor.execute("""
            DELETE FROM invalid_passports 
            WHERE is_recycled = TRUE 
            AND created_at < (NOW() - INTERVAL 24 HOUR)
        """)
        invalid_deleted = cursor.rowcount

        # Get filenames before deleting passports
        cursor.execute("""
            SELECT filename FROM passports 
            WHERE is_recycled = TRUE 
            AND created_at < (NOW() - INTERVAL 24 HOUR)
        """)
        filenames = [r[0] for r in cursor.fetchall()]

        cursor.execute("""
            DELETE FROM passports 
            WHERE is_recycled = TRUE 
            AND created_at < (NOW() - INTERVAL 24 HOUR)
        """)
        passports_deleted = cursor.rowcount
        conn.commit()

        for fname in filenames:
            _delete_passport_files(fname)

        return invalid_deleted + passports_deleted
    except Exception as e:
        print(f"Recycle bin cleanup error: {e}")
        return 0
    finally:
        cursor.close()
        conn.close()

def auto_remove_old_invalid_records():
    """Permanently deletes unhandled invalid records globally for all users."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("USE passport_db")
        cursor.execute("""
            DELETE FROM invalid_passports 
            WHERE (is_recycled = FALSE OR is_recycled IS NULL)
            AND created_at < (NOW() - INTERVAL 7 DAY)   # <--- CHANGE THIS TO 7
        """)
        deleted_count = cursor.rowcount
        conn.commit()
        return deleted_count
    except Exception as e:
        print(f"Old invalid records cleanup error: {e}")
        return 0
    finally:
        cursor.close()
        conn.close()
# =====================================================
# PLAN SYSTEM CONSTANTS & HELPERS
# =====================================================

PLAN_DETAILS = {
    'subscription': {
        'name': 'Subscription',
        'price': 0,
        'limit': None,
        'rate_per_passport': 0.25,
        'extra_rate': 0,
        'label': 'Subscription — Per Passport',
        'color': '#c9a84c',
    },
}


# =====================================================
# GROUP BATCHES
# =====================================================

def touch_group_activity(user_id, group_name):
    """
    Marks a group as having just received activity (records moved into it
    via Change Group, bulk Change Group, or Merge). Upserts a
    group_batches row with last_activity_at = NOW() so the group jumps to
    the top of the /groups list — even when the moved records themselves
    are old (their own passports.created_at is left untouched; this only
    affects sort position, not the records' actual data/history).

    Safe to call for a group that has no group_batches row yet (creates
    one with defaults) or one that already exists (only bumps the
    timestamp, leaves contract_login_id/status alone).
    """
    if not group_name:
        return
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    cursor.execute("""
        INSERT INTO group_batches (user_id, group_name, last_activity_at)
        VALUES (%s, %s, NOW())
        ON DUPLICATE KEY UPDATE last_activity_at = NOW()
    """, (user_id, group_name))
    conn.commit()
    cursor.close()
    conn.close()


def get_all_group_batches(user_id):
    """
    Returns groups with passport count, nationality count, contract login ID, status,
    and a list of distinct nationality_ids present in each group.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")

    # Main group summary
    cursor.execute("""
        SELECT
            g.group_name,
            GREATEST(MAX(p.created_at), COALESCE(MAX(gb.last_activity_at), MAX(p.created_at))) AS created_at,
            COUNT(DISTINCT p.id) AS passport_count,
            COUNT(DISTINCT g.nationality_id) AS nationality_count,
            COALESCE(gb.contract_login_id, \'\') AS contract_login_id,
            COALESCE(gb.status, \'Unsent\') AS status,
            COUNT(DISTINCT CASE WHEN p.is_processed = TRUE OR p.is_visa_processed = TRUE THEN p.id END) AS sent_count,
            COUNT(DISTINCT CASE WHEN (p.is_processed IS NOT TRUE) AND (p.is_visa_processed IS NOT TRUE) THEN p.id END) AS unsent_count,
            COUNT(DISTINCT CASE WHEN COALESCE(g.visa_type, 'nusuk') = 'nusuk' THEN p.id END) AS nusuk_count,
            COUNT(DISTINCT CASE WHEN g.visa_type = 'visit_visa' THEN p.id END) AS visa_count,
            COUNT(DISTINCT CASE WHEN COALESCE(g.visa_type, 'nusuk') = 'nusuk' AND p.is_processed = TRUE THEN p.id END) AS nusuk_processed_count,
            COUNT(DISTINCT CASE WHEN g.visa_type = 'visit_visa' AND p.is_visa_processed = TRUE THEN p.id END) AS visa_processed_count,
            MAX(g.is_emergency) AS is_emergency
        FROM general_data g
        JOIN passports p ON g.passport_id = p.id
        LEFT JOIN group_batches gb ON gb.group_name = g.group_name AND gb.user_id = p.user_id
        WHERE p.user_id = %s
          AND (p.is_recycled = FALSE OR p.is_recycled IS NULL)
          AND g.group_name IS NOT NULL AND g.group_name != \'\'
        GROUP BY g.group_name
        ORDER BY created_at DESC
    """, (user_id,))
    rows = cursor.fetchall()

    # Nationality IDs per group
    cursor.execute("""
        SELECT g.group_name, g.nationality_id
        FROM general_data g
        JOIN passports p ON g.passport_id = p.id
        WHERE p.user_id = %s
          AND (p.is_recycled = FALSE OR p.is_recycled IS NULL)
          AND g.group_name IS NOT NULL AND g.group_name != \'\'
          AND g.nationality_id IS NOT NULL
        GROUP BY g.group_name, g.nationality_id
        ORDER BY g.group_name
    """, (user_id,))
    nat_rows = cursor.fetchall()

    cursor.close()
    conn.close()

    # Build dict: group_name -> [nationality_id, ...]
    nat_map = {}
    for nr in nat_rows:
        nat_map.setdefault(nr['group_name'], []).append(nr['nationality_id'])

    for row in rows:
        row['nationality_ids'] = nat_map.get(row['group_name'], [])

    return rows
def delete_group_and_records(user_id, group_name):
    """Delete all passport records belonging to a group and their local image files."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    # Fetch filenames BEFORE deleting so we can remove local files
    cursor.execute("""
        SELECT p.filename FROM passports p
        JOIN general_data g ON g.passport_id = p.id
        WHERE p.user_id = %s AND g.group_name = %s
    """, (user_id, group_name))
    filenames = [r[0] for r in cursor.fetchall()]
    # Hard-delete passports that belong to this group for this user
    cursor.execute("""
        DELETE p FROM passports p
        JOIN general_data g ON g.passport_id = p.id
        WHERE p.user_id = %s AND g.group_name = %s
    """, (user_id, group_name))
    # Also remove group_batches entry if any
    cursor.execute("""
        DELETE FROM group_batches WHERE user_id = %s AND group_name = %s
    """, (user_id, group_name))
    conn.commit()
    cursor.close()
    conn.close()
    # Delete local image and face files for each passport
    for fname in filenames:
        _delete_passport_files(fname)
    # Remove the now-empty group folders themselves (both live and recycle-bin copies)
    for base in (
        UPLOAD_FOLDER, FACE_FOLDER,
        os.path.join(UPLOAD_FOLDER, _RECYCLE_DIRNAME),
        os.path.join(FACE_FOLDER, _RECYCLE_DIRNAME),
    ):
        group_dir = os.path.join(base, _sanitize_group(group_name))
        if os.path.isdir(group_dir):
            try:
                shutil.rmtree(group_dir)
            except Exception as e:
                print(f"Warning: could not remove group folder {group_dir}: {e}")


def rename_group(user_id, old_group_name, new_group_name, new_date):
    """Rename a group and optionally update its created date (updates all passport created_at)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    # Rename group_name in general_data
    cursor.execute("""
        UPDATE general_data g
        JOIN passports p ON g.passport_id = p.id
        SET g.group_name = %s
        WHERE g.group_name = %s AND p.user_id = %s
    """, (new_group_name, old_group_name, user_id))
    # Update created_at on all passports in the group if date provided
    # Preserves the existing TIME, only changes the DATE part
    if new_date:
        cursor.execute("""
            UPDATE passports p
            JOIN general_data g ON g.passport_id = p.id
            SET p.created_at = CONCAT(%s, ' ', TIME(p.created_at))
            WHERE g.group_name = %s AND p.user_id = %s
        """, (new_date, new_group_name, user_id))
    # Rename in group_batches too
    cursor.execute("""
        UPDATE group_batches SET group_name = %s
        WHERE group_name = %s AND user_id = %s
    """, (new_group_name, old_group_name, user_id))
    conn.commit()
    cursor.close()
    conn.close()

    # Physically move the group's folders on disk (originals + faces,
    # live and recycle-bin copies) to match the new name.
    move_group_folder(old_group_name, new_group_name)
    for base in (UPLOAD_FOLDER, FACE_FOLDER):
        old_recycle = os.path.join(base, _RECYCLE_DIRNAME, _sanitize_group(old_group_name))
        new_recycle = os.path.join(base, _RECYCLE_DIRNAME, _sanitize_group(new_group_name))
        if os.path.isdir(old_recycle):
            os.makedirs(new_recycle, exist_ok=True)
            for fname in os.listdir(old_recycle):
                try:
                    shutil.move(os.path.join(old_recycle, fname), os.path.join(new_recycle, fname))
                except Exception as e:
                    print(f"Warning: could not move {fname}: {e}")
            try:
                os.rmdir(old_recycle)
            except OSError:
                pass


def move_passport_to_group(passport_id, user_id, new_group_name):
    """Move a single passport record to a different group: updates
    general_data.group_name, auto-aligns visa_type (and its associated
    general-data fields) to the TARGET group's own visa_type — same
    behavior as /change_group and /merge_groups_into — and physically
    moves its original + face image from the old group's folder to the
    new group's folder."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")

    cursor.execute("""
        SELECT p.filename, g.group_name
        FROM passports p
        JOIN general_data g ON g.passport_id = p.id
        WHERE p.id = %s AND p.user_id = %s
    """, (passport_id, user_id))
    row = cursor.fetchone()
    if not row:
        cursor.close()
        conn.close()
        raise Exception("Passport not found.")

    filename, old_group_name = row

    cursor.execute("""
        UPDATE general_data g
        JOIN passports p ON g.passport_id = p.id
        SET g.group_name = %s
        WHERE p.id = %s AND p.user_id = %s
    """, (new_group_name, passport_id, user_id))

    # ── Auto-align to the TARGET group's own visa_type, same as
    # /change_group and /merge_groups_into ── look at a record other than
    # the one we just moved in, so probe excludes passport_id.
    cursor.execute("""
        SELECT g.visa_type
        FROM general_data g
        JOIN passports p ON g.passport_id = p.id
        WHERE p.user_id = %s AND g.group_name = %s
          AND g.visa_type IN ('nusuk', 'visit_visa')
          AND p.id != %s
        LIMIT 1
    """, (user_id, new_group_name, passport_id))
    target_row = cursor.fetchone()
    target_visa_type = target_row[0] if target_row else None

    if target_visa_type == 'visit_visa':
        from app_core import _resolve_default_arrival_departure
        db_defaults = get_user_settings(user_id) or {}
        now = ist_now()
        one_year_later = now + timedelta(days=365)
        arr_date, dep_date = _resolve_default_arrival_departure(db_defaults, now, one_year_later)
        cursor.execute("""
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
            WHERE passport_id = %s
        """, (
            safe_int(db_defaults.get('marital_status'), 5),
            db_defaults.get('city_of_birth', 'MAIN STREET'),
            db_defaults.get('profession', 'TOURISM'),
            db_defaults.get('passport_issue_place', 'PLACE'),
            db_defaults.get('hotel_name', 'Hayat Mall Gate 6, Riyadh'),
            db_defaults.get('address', 'ADDRESS'),
            db_defaults.get('city', 'MAIN STREET'),
            db_defaults.get('zip_postal_code', '676542'),
            arr_date, dep_date,
            passport_id
        ))
    elif target_visa_type == 'nusuk':
        cursor.execute("""
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
            WHERE passport_id = %s
        """, (passport_id,))

    conn.commit()
    cursor.close()
    conn.close()

    move_passport_files_to_group(filename, old_group_name, new_group_name)


def move_passports_to_group(passport_ids, user_id, new_group_name):
    """Bulk version of move_passport_to_group for moving several
    passports into a (new or existing) group at once."""
    for pid in passport_ids:
        move_passport_to_group(pid, user_id, new_group_name)


def upsert_group_batch(user_id, group_name, contract_login_id):
    """Mark a group as Sent and save the contract login email."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    cursor.execute("""
        INSERT INTO group_batches (user_id, group_name, contract_login_id, status, sent_at)
        VALUES (%s, %s, %s, 'Sent', NOW())
        ON DUPLICATE KEY UPDATE
            contract_login_id = VALUES(contract_login_id),
            status = 'Sent',
            sent_at = NOW()
    """, (user_id, group_name, contract_login_id))
    conn.commit()
    cursor.close()
    conn.close()


# =====================================================
# VISIT VISA EXE REGISTRY (lets passposys.exe run on a different PC than Flask)
# =====================================================

def register_visit_visa_exe(user_id, exe_host, exe_port, exe_secret):
    """
    Called by passposys.exe on startup (POST /api/register_visa_exe) to
    announce where it can be reached. Overwrites any prior registration for
    this user_id — only the most recently started exe instance is targeted,
    so restarting the exe (or starting it on a new PC) automatically
    supersedes an older/stale registration.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    cursor.execute("""
        INSERT INTO visit_visa_exe_registry (user_id, exe_host, exe_port, exe_secret)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            exe_host = VALUES(exe_host),
            exe_port = VALUES(exe_port),
            exe_secret = VALUES(exe_secret),
            last_seen_at = CURRENT_TIMESTAMP
    """, (user_id, exe_host, exe_port, exe_secret))
    conn.commit()
    cursor.close()
    conn.close()


def get_visit_visa_exe_registration(user_id):
    """Returns {exe_host, exe_port, exe_secret} for this user_id, or None if
    no exe has registered (falls back to the local same-machine exe)."""
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")
    cursor.execute("""
        SELECT exe_host, exe_port, exe_secret
        FROM visit_visa_exe_registry
        WHERE user_id = %s
    """, (user_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def touch_visit_visa_exe_registration(user_id):
    """Updates last_seen_at as a lightweight heartbeat, called whenever a
    push to this user's exe succeeds. Lets a future cleanup job identify
    and prune stale registrations from exe instances that were closed
    without deregistering."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    cursor.execute(
        "UPDATE visit_visa_exe_registry SET last_seen_at = CURRENT_TIMESTAMP WHERE user_id = %s",
        (user_id,)
    )
    conn.commit()
    cursor.close()
    conn.close()


def delete_visit_visa_exe_registration(user_id):
    """Removes a registration, e.g. when the exe is closing cleanly."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    cursor.execute("DELETE FROM visit_visa_exe_registry WHERE user_id = %s", (user_id,))
    conn.commit()
    cursor.close()
    conn.close()


# =====================================================
# VISIT VISA QUEUE (one-applicant-at-a-time send to passposys.exe)
# =====================================================

def create_visit_visa_queue(user_id, batch_id, applicants, credentials):
    """
    Persist a new one-at-a-time send batch.

    applicants: list of dicts already shaped the way the exe expects
                (same shape send_visit_visa builds today), each containing
                at least an "id" key (the passport_id).
    credentials: dict, stored per-row (small payload, avoids a join).

    Row 0 is inserted as 'sent' (the caller pushes it to the exe in the
    same request); all others start 'pending'.
    """
    if not applicants:
        return
    conn   = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    credentials_json = json.dumps(credentials or {}, ensure_ascii=False)
    rows = []
    for position, applicant in enumerate(applicants):
        passport_id = applicant.get("id")
        if passport_id is None:
            continue
        status = "sent" if position == 0 else "pending"
        rows.append((
            user_id, batch_id, int(passport_id), position, status,
            json.dumps(applicant, ensure_ascii=False), credentials_json,
        ))
    if rows:
        cursor.executemany("""
            INSERT INTO visit_visa_queue
                (user_id, batch_id, passport_id, position, status, applicant_json, credentials_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, rows)
        conn.commit()
    cursor.close()
    conn.close()

# =====================================================
# NUSUK QUEUE (one-applicant-at-a-time send to Chrome Extension)
# =====================================================

def create_nusuk_queue(user_id, batch_id, applicants, credentials):
    if not applicants:
        return
    conn   = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    credentials_json = json.dumps(credentials or {}, ensure_ascii=False)
    rows = []
    for position, applicant in enumerate(applicants):
        passport_id = applicant.get("id")
        if passport_id is None:
            continue
        # Row 0 is inserted as 'sent' because it's handed to the browser immediately
        status = "sent" if position == 0 else "pending"
        rows.append((
            user_id, batch_id, int(passport_id), position, status,
            json.dumps(applicant, ensure_ascii=False), credentials_json,
        ))
    if rows:
        # Insert in small chunks so a single packet can't exceed the
        # server's max_allowed_packet limit, even with large applicant_json
        # payloads (e.g. embedded passport images).
        CHUNK_SIZE = 10
        for i in range(0, len(rows), CHUNK_SIZE):
            chunk = rows[i:i + CHUNK_SIZE]
            cursor.executemany("""
                INSERT INTO nusuk_queue
                    (user_id, batch_id, passport_id, position, status, applicant_json, credentials_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, chunk)
        conn.commit()
    cursor.close()
    conn.close()

def pop_next_nusuk_queue_item(user_id, finished_passport_id):
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("USE passport_db")
        conn.start_transaction()

        cursor.execute("""
            SELECT batch_id FROM nusuk_queue
            WHERE user_id = %s AND passport_id = %s
            ORDER BY id DESC LIMIT 1
            FOR UPDATE
        """, (user_id, finished_passport_id))
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return None, None, None
        batch_id = row["batch_id"]

        cursor.execute("""
            SELECT id, passport_id, applicant_json, credentials_json
            FROM nusuk_queue
            WHERE user_id = %s AND batch_id = %s AND status = 'pending'
            ORDER BY position ASC LIMIT 1
            FOR UPDATE
        """, (user_id, batch_id))
        next_row = cursor.fetchone()
        if not next_row:
            conn.commit()
            return None, None, None

        cursor.execute(
            "UPDATE nusuk_queue SET status = 'sent' WHERE id = %s",
            (next_row["id"],)
        )
        conn.commit()

        applicant   = json.loads(next_row["applicant_json"])
        credentials = json.loads(next_row["credentials_json"])
        return applicant, credentials, batch_id
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

def mark_nusuk_queue_item_finished(user_id, passport_id, success):
    conn   = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    status = "done" if success else "skipped"
    cursor.execute("""
        UPDATE nusuk_queue SET status = %s
        WHERE user_id = %s AND passport_id = %s AND status = 'sent'
        ORDER BY id DESC LIMIT 1
    """, (status, user_id, passport_id))
    conn.commit()
    cursor.close()
    conn.close()

def clear_nusuk_queue_for_user(user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    cursor.execute("DELETE FROM nusuk_queue WHERE user_id = %s", (user_id,))
    conn.commit()
    deleted = cursor.rowcount
    cursor.close()
    conn.close()
    return deleted

def get_active_nusuk_queue_user():
    """
    Resolve which user_id currently owns the active nusuk_queue, for
    token-authenticated calls (the local exe/extension) that have no Flask
    session and no specific passport_id to key off of — e.g. clearing the
    whole queue at the end of a batch or via the Clean button.

    This app is single-user-at-a-time on a given machine, so "whoever has
    pending/sent rows right now" is an unambiguous owner. Falls back to the
    most recently touched row of any status if nothing is pending/sent
    (e.g. clearing after everything already finished).
    """
    conn   = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    cursor.execute("""
        SELECT user_id FROM nusuk_queue
        WHERE status IN ('pending', 'sent')
        ORDER BY id DESC LIMIT 1
    """)
    row = cursor.fetchone()
    if row is None:
        cursor.execute("SELECT user_id FROM nusuk_queue ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row[0] if row else None

def get_nusuk_queue_user(passport_id):
    """
    Resolve which user_id owns the most recent in-flight nusuk_queue row
    for a passport_id. Mirrors get_visit_visa_queue_user() — needed
    because mark_processed_single can be authenticated via the trusted
    local_api_token instead of a Flask session, in which case there's no
    session['user_id'] to fall back on.
    """
    conn   = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    cursor.execute("""
        SELECT user_id FROM nusuk_queue
        WHERE passport_id = %s AND status = 'sent'
        ORDER BY id DESC LIMIT 1
    """, (passport_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row[0] if row else None

def pop_next_visit_visa_queue_item(user_id, finished_passport_id):
    """
    Called after an applicant finishes (success OR skip/failure).

    Finds the next 'pending' row in the same batch as finished_passport_id
    and marks it 'sent' so the caller can push it to the exe.

    Uses SELECT ... FOR UPDATE to serialize concurrent calls for the same
    user across gunicorn's 4 worker processes/threads, so two workers can
    never both pop the same "next" row.

    Returns (applicant_dict, credentials_dict, batch_id) or (None, None, None)
    if there is no more pending work (or no queue exists) for this user.
    """
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("USE passport_db")
        conn.start_transaction()

        # Find which batch this finished passport belongs to.
        cursor.execute("""
            SELECT batch_id FROM visit_visa_queue
            WHERE user_id = %s AND passport_id = %s
            ORDER BY id DESC LIMIT 1
            FOR UPDATE
        """, (user_id, finished_passport_id))
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return None, None, None
        batch_id = row["batch_id"]

        # Lock the next pending row in this batch.
        cursor.execute("""
            SELECT id, passport_id, applicant_json, credentials_json
            FROM visit_visa_queue
            WHERE user_id = %s AND batch_id = %s AND status = 'pending'
            ORDER BY position ASC LIMIT 1
            FOR UPDATE
        """, (user_id, batch_id))
        next_row = cursor.fetchone()
        if not next_row:
            conn.commit()
            return None, None, None

        cursor.execute(
            "UPDATE visit_visa_queue SET status = 'sent' WHERE id = %s",
            (next_row["id"],)
        )
        conn.commit()

        applicant   = json.loads(next_row["applicant_json"])
        credentials = json.loads(next_row["credentials_json"])
        return applicant, credentials, batch_id
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def mark_visit_visa_queue_item_finished(user_id, passport_id, success):
    """Mark the most recent 'sent' queue row for this passport as done or skipped."""
    conn   = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    status = "done" if success else "skipped"
    cursor.execute("""
        UPDATE visit_visa_queue SET status = %s
        WHERE user_id = %s AND passport_id = %s AND status = 'sent'
        ORDER BY id DESC LIMIT 1
    """, (status, user_id, passport_id))
    conn.commit()
    cursor.close()
    conn.close()


def get_visit_visa_queue_user(passport_id):
    """
    Resolve which user_id owns the most recent in-flight queue row for a
    passport_id. Needed because mark_processed_single is called by the
    local exe with a trusted token but no Flask session — it only knows
    the passport_id.
    """
    conn   = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    cursor.execute("""
        SELECT user_id FROM visit_visa_queue
        WHERE passport_id = %s AND status = 'sent'
        ORDER BY id DESC LIMIT 1
    """, (passport_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row[0] if row else None


def get_visit_visa_queue_user_any_status(passport_id):
    """
    Like get_visit_visa_queue_user, but matches the most recent queue row
    for this passport_id regardless of status. Used when the exe is
    closing and the last-touched applicant may already be 'done' or
    'skipped' (not 'sent') by that point.
    """
    conn   = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    cursor.execute("""
        SELECT user_id FROM visit_visa_queue
        WHERE passport_id = %s
        ORDER BY id DESC LIMIT 1
    """, (passport_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row[0] if row else None


def clear_visit_visa_queue_for_user(user_id):
    """
    Delete every visit_visa_queue row for this user (all batches,
    any status). Called when passposys.exe closes, since the queue
    table is only a hand-off buffer between Flask requests and is
    never read for tick marks/results (those live on passports).
    """
    conn   = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    cursor.execute("DELETE FROM visit_visa_queue WHERE user_id = %s", (user_id,))
    conn.commit()
    deleted = cursor.rowcount
    cursor.close()
    conn.close()
    return deleted


# =====================================================
# UPLOAD PROGRESS
# =====================================================

def set_progress(user_id, current=0, total=0, success=0, invalid=0, duplicate=0, phase=''):
    """Upsert the progress row for a user at upload start."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    # Auto-add phase column if it doesn't exist yet (safe migration)
    try:
        cursor.execute("ALTER TABLE upload_progress ADD COLUMN phase VARCHAR(60) DEFAULT ''")
        conn.commit()
    except Exception:
        pass
    cursor.execute("""
        INSERT INTO upload_progress (user_id, current_, total, success, invalid, duplicate, phase)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            current_  = VALUES(current_),
            total     = VALUES(total),
            success   = VALUES(success),
            invalid   = VALUES(invalid),
            duplicate = VALUES(duplicate),
            phase     = VALUES(phase)
    """, (user_id, current, total, success, invalid, duplicate, phase))
    conn.commit()
    cursor.close()
    conn.close()


def update_progress_field(user_id, **fields):
    """Update one or more fields on the progress row in real time."""
    if not fields:
        return
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    cursor.execute(
        f"UPDATE upload_progress SET {set_clause} WHERE user_id = %s",
        list(fields.values()) + [user_id]
    )
    conn.commit()
    cursor.close()
    conn.close()


def get_progress(user_id):
    """Read the current progress row for a user."""
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("USE passport_db")
    try:
        cursor.execute(
            "SELECT current_, total, success, invalid, duplicate, phase FROM upload_progress WHERE user_id = %s",
            (user_id,)
        )
    except Exception:
        # phase column not yet migrated — fall back to safe query
        cursor.execute(
            "SELECT current_, total, success, invalid, duplicate FROM upload_progress WHERE user_id = %s",
            (user_id,)
        )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row or {}


def clear_progress(user_id):
    """Delete the progress row once upload is complete."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("USE passport_db")
    cursor.execute("DELETE FROM upload_progress WHERE user_id = %s", (user_id,))
    conn.commit()
    cursor.close()
    conn.close()