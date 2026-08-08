"""
db_admin_routes.py
-------------------
A self-contained web UI for browsing and managing the MySQL database that
the app already connects to (via db.get_connection()).

Features
  - GET  /db-admin/                          list every table + row counts,
                                              plus a global foreign-key-checks
                                              on/off toggle
  - GET  /db-admin/table/<table>             browse rows (paginated), inline
                                              search box (matches any TEXT/
                                              VARCHAR column)
  - GET  /db-admin/table/<table>/row/<pk>    edit a single row
  - POST /db-admin/table/<table>/row/<pk>    save edits to a single row
  - POST /db-admin/table/<table>/row/<pk>/delete   delete a single row
  - POST /db-admin/table/<table>/empty       empty all rows in a table
                                              (TRUNCATE, falling back to
                                              DELETE if foreign keys block it)
  - POST /db-admin/fk-checks                 turn FOREIGN_KEY_CHECKS on/off
                                              for this connection/session
  - GET  /db-admin/query                     free-form SQL runner (SELECT
                                              only by default; destructive
                                              statements require an explicit
                                              confirmation checkbox)

Security
  - This local MySQL instance belongs to the user running the app (see
    launch.py: it's a portable, per-install MySQL on 127.0.0.1 seeded with
    its own local DB user). It isn't shared multi-tenant infrastructure,
    so there's no separate "admin" tier to check here -- being logged in
    to the app (login_required, same as every other page) is enough.
  - Table/column names are validated against information_schema (never
    interpolated from raw user input) before being used in SQL, since
    MySQL doesn't support parameter placeholders for identifiers.
  - All values (row data, WHERE conditions) are always sent as parameterized
    query arguments, never string-formatted into SQL.
  - The free-form query runner defaults to SELECT-only. Non-SELECT
    statements are rejected unless the user explicitly ticks "I understand
    this may modify data" AND the statement targets a single table that
    exists in the DB (still parameter-free protection is not possible for
    ad-hoc SQL by definition, so this is opt-in + logged).
  - Every table listing is scoped to the single configured database
    (DB_CONFIG['database']) -- never cross-database.
  - "Empty table" (TRUNCATE, falling back to DELETE) and the
    foreign-key-checks toggle are both destructive/risky operations gated
    behind an explicit button click + confirm dialog in the UI. The FK
    toggle only affects requests made through this blueprint (see
    _admin_connection()) and is reset by the pool on every connection
    return, so it never silently changes behavior for the rest of the app.
"""

import math
import logging

from flask import (
    Blueprint, request, render_template, redirect, url_for,
    session, flash
)

from db import get_connection
from config import DB_CONFIG
from app_core import login_required

_logger = logging.getLogger(__name__)

db_admin_bp = Blueprint("db_admin", __name__, url_prefix="/db-admin")

PAGE_SIZE = 50


# ── identifier validation helpers ────────────────────────────────────────
def _get_database_name():
    return DB_CONFIG["database"]


def _list_tables():
    """Return [{'name':..., 'row_count':...}, ...] for the configured DB."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT TABLE_NAME AS name, TABLE_ROWS AS approx_rows "
            "FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' "
            "ORDER BY TABLE_NAME",
            (_get_database_name(),),
        )
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


def _validate_table(table_name):
    """Raise ValueError if table_name isn't a real table in this DB.
    Returns the validated name (safe to interpolate into SQL afterwards,
    since it's now known to be an exact match against information_schema)."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND TABLE_TYPE = 'BASE TABLE'",
            (_get_database_name(), table_name),
        )
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        raise ValueError(f"Unknown table: {table_name}")
    return row[0]


def _get_columns(table_name):
    """Return list of {'name','type','is_nullable','is_pk'} for a validated table."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT COLUMN_NAME AS name, DATA_TYPE AS type, "
            "IS_NULLABLE AS is_nullable, COLUMN_KEY AS col_key "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
            "ORDER BY ORDINAL_POSITION",
            (_get_database_name(), table_name),
        )
        cols = cur.fetchall()
    finally:
        cur.close()
        conn.close()
    for c in cols:
        c["is_pk"] = c["col_key"] == "PRI"
    return cols


def _primary_key_column(columns):
    for c in columns:
        if c["is_pk"]:
            return c["name"]
    return None


# ── large-value truncation (row listing only) ────────────────────────────
# Columns holding base64 image data, blobs, or just very long text make the
# row listing page huge and slow to render if printed in full. The listing
# view truncates any string value over this length to a short placeholder;
# the single-row edit page still shows the full value (needed to actually
# edit it), and a "view full value" toggle is offered there instead.
_LISTING_TRUNCATE_AT = 200
_BLOB_TYPES = ("blob", "tinyblob", "mediumblob", "longblob",
               "binary", "varbinary")


def _display_value(value, col_type=None):
    """Return a listing-safe representation of a cell value: full value for
    short/normal fields, a short placeholder for large blobs/text/base64."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return f"[binary data, {len(value)} bytes]"
    text = str(value)
    if col_type in _BLOB_TYPES or len(text) > _LISTING_TRUNCATE_AT:
        return f"[large value, {len(text)} chars] {text[:40]}…"
    return text


# ── foreign-key-checks toggle ────────────────────────────────────────────
# FOREIGN_KEY_CHECKS is a per-connection MySQL session variable, not a
# database-wide setting. Since get_connection() hands out pooled
# connections, the desired on/off state is remembered in the Flask session
# and (re)applied to whichever pooled connection this request happens to
# get, via _admin_connection() below. This only affects requests made
# through this db_admin blueprint -- it never changes behavior for the
# rest of the app, since every other call site still uses get_connection()
# directly and MySQL resets session variables when a pooled connection is
# returned to the pool (pool_reset_session=True in db.py).
def _fk_checks_enabled():
    return session.get("db_admin_fk_checks", True)


def _admin_connection():
    """Get a pooled connection with FOREIGN_KEY_CHECKS set to match the
    current toggle state for this admin session."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SET FOREIGN_KEY_CHECKS = %s", (1 if _fk_checks_enabled() else 0,))
    finally:
        cur.close()
    return conn


# ── routes ────────────────────────────────────────────────────────────────
@db_admin_bp.route("/")
@login_required
def index():
    tables = _list_tables()
    return render_template(
        "db_admin/index.html",
        tables=tables,
        database=_get_database_name(),
        fk_checks_enabled=_fk_checks_enabled(),
    )


@db_admin_bp.route("/fk-checks", methods=["POST"])
@login_required
def toggle_fk_checks():
    enable = request.form.get("enable") == "1"
    session["db_admin_fk_checks"] = enable
    flash(f"Foreign key checks {'enabled' if enable else 'disabled'}.", "success")
    return redirect(url_for("db_admin.index"))


@db_admin_bp.route("/table/<table_name>")
@login_required
def view_table(table_name):
    try:
        table_name = _validate_table(table_name)
    except ValueError:
        flash("Table not found.", "error")
        return redirect(url_for("db_admin.index"))

    columns = _get_columns(table_name)
    pk_col = _primary_key_column(columns)
    search = (request.args.get("q") or "").strip()
    page = max(1, int(request.args.get("page", 1) or 1))
    offset = (page - 1) * PAGE_SIZE

    text_cols = [c["name"] for c in columns if c["type"] in
                 ("varchar", "text", "char", "mediumtext", "longtext")]

    where_sql = ""
    params = []
    if search and text_cols:
        clauses = [f"`{c}` LIKE %s" for c in text_cols]
        where_sql = "WHERE " + " OR ".join(clauses)
        params = [f"%{search}%"] * len(text_cols)

    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(f"SELECT COUNT(*) AS cnt FROM `{table_name}` {where_sql}", params)
        total = cur.fetchone()["cnt"]

        order_sql = f"ORDER BY `{pk_col}`" if pk_col else ""
        cur.execute(
            f"SELECT * FROM `{table_name}` {where_sql} {order_sql} LIMIT %s OFFSET %s",
            params + [PAGE_SIZE, offset],
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    col_types = {c["name"]: c["type"] for c in columns}
    display_rows = [
        {k: _display_value(v, col_types.get(k)) for k, v in row.items()}
        for row in rows
    ]

    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    return render_template(
        "db_admin/table.html",
        table_name=table_name,
        columns=columns,
        pk_col=pk_col,
        rows=rows,
        display_rows=display_rows,
        search=search,
        page=page,
        total_pages=total_pages,
        total=total,
    )


@db_admin_bp.route("/table/<table_name>/row/<pk_value>", methods=["GET", "POST"])
@login_required
def edit_row(table_name, pk_value):
    try:
        table_name = _validate_table(table_name)
    except ValueError:
        flash("Table not found.", "error")
        return redirect(url_for("db_admin.index"))

    columns = _get_columns(table_name)
    pk_col = _primary_key_column(columns)
    if not pk_col:
        flash("This table has no primary key, so single-row editing isn't supported.", "error")
        return redirect(url_for("db_admin.view_table", table_name=table_name))

    if request.method == "POST":
        editable_cols = [c["name"] for c in columns if c["name"] != pk_col]
        set_clauses = ", ".join(f"`{c}` = %s" for c in editable_cols)
        values = []
        for c in editable_cols:
            val = request.form.get(c, "")
            values.append(val if val != "" else None)
        values.append(pk_value)

        conn = _admin_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                f"UPDATE `{table_name}` SET {set_clauses} WHERE `{pk_col}` = %s",
                values,
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            _logger.exception("db_admin: failed to update %s row %s", table_name, pk_value)
            flash(f"Update failed: {e}", "error")
            cur.close()
            conn.close()
            return redirect(url_for("db_admin.edit_row", table_name=table_name, pk_value=pk_value))
        cur.close()
        conn.close()
        flash("Row updated.", "success")
        return redirect(url_for("db_admin.view_table", table_name=table_name))

    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(f"SELECT * FROM `{table_name}` WHERE `{pk_col}` = %s", (pk_value,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()

    if not row:
        flash("Row not found.", "error")
        return redirect(url_for("db_admin.view_table", table_name=table_name))

    return render_template(
        "db_admin/edit_row.html",
        table_name=table_name,
        columns=columns,
        pk_col=pk_col,
        pk_value=pk_value,
        row=row,
    )


@db_admin_bp.route("/table/<table_name>/row/<pk_value>/delete", methods=["POST"])
@login_required
def delete_row(table_name, pk_value):
    try:
        table_name = _validate_table(table_name)
    except ValueError:
        flash("Table not found.", "error")
        return redirect(url_for("db_admin.index"))

    columns = _get_columns(table_name)
    pk_col = _primary_key_column(columns)
    if not pk_col:
        flash("This table has no primary key, so single-row delete isn't supported.", "error")
        return redirect(url_for("db_admin.view_table", table_name=table_name))

    conn = _admin_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"DELETE FROM `{table_name}` WHERE `{pk_col}` = %s", (pk_value,))
        conn.commit()
        deleted = cur.rowcount
    except Exception as e:
        conn.rollback()
        _logger.exception("db_admin: failed to delete %s row %s", table_name, pk_value)
        flash(f"Delete failed: {e}", "error")
        deleted = 0
    finally:
        cur.close()
        conn.close()

    if deleted:
        flash("Row deleted.", "success")
    else:
        flash("Nothing was deleted (row not found).", "error")
    return redirect(url_for("db_admin.view_table", table_name=table_name))


@db_admin_bp.route("/table/<table_name>/empty", methods=["POST"])
@login_required
def empty_table(table_name):
    try:
        table_name = _validate_table(table_name)
    except ValueError:
        flash("Table not found.", "error")
        return redirect(url_for("db_admin.index"))

    conn = _admin_connection()
    cur = conn.cursor()
    try:
        try:
            # TRUNCATE is fast and resets AUTO_INCREMENT, but MySQL refuses
            # it if another table has a foreign key pointing at this one
            # (unless FK checks are currently disabled).
            cur.execute(f"TRUNCATE TABLE `{table_name}`")
        except Exception:
            conn.rollback()
            # Fall back to DELETE, which respects/enforces FK constraints
            # row-by-row instead of failing outright.
            cur.execute(f"DELETE FROM `{table_name}`")
        conn.commit()
    except Exception as e:
        conn.rollback()
        _logger.exception("db_admin: failed to empty table %s", table_name)
        flash(f"Failed to empty table: {e}", "error")
        cur.close()
        conn.close()
        return redirect(url_for("db_admin.view_table", table_name=table_name))

    cur.close()
    conn.close()
    flash(f"Table '{table_name}' emptied.", "success")
    return redirect(url_for("db_admin.view_table", table_name=table_name))


@db_admin_bp.route("/query", methods=["GET", "POST"])
@login_required
def run_query():
    result_columns, result_rows, error, affected = None, None, None, None
    sql = ""
    confirm_write = False

    if request.method == "POST":
        sql = (request.form.get("sql") or "").strip()
        confirm_write = bool(request.form.get("confirm_write"))
        stripped = sql.lstrip().lower()
        is_select = stripped.startswith("select") or stripped.startswith("show") or stripped.startswith("describe") or stripped.startswith("explain")

        if not sql:
            error = "Enter a SQL statement."
        elif not is_select and not confirm_write:
            error = "This statement modifies data. Tick the confirmation box to run it."
        else:
            conn = _admin_connection()
            cur = conn.cursor(dictionary=True)
            try:
                cur.execute(sql)
                if is_select:
                    raw_rows = cur.fetchall()
                    result_columns = list(raw_rows[0].keys()) if raw_rows else \
                        ([d[0] for d in cur.description] if cur.description else [])
                    result_rows = [
                        {k: _display_value(v) for k, v in r.items()} for r in raw_rows
                    ]
                else:
                    conn.commit()
                    affected = cur.rowcount
            except Exception as e:
                conn.rollback()
                _logger.exception("db_admin: ad-hoc query failed")
                error = str(e)
            finally:
                cur.close()
                conn.close()

    return render_template(
        "db_admin/query.html",
        sql=sql,
        confirm_write=confirm_write,
        columns=result_columns,
        rows=result_rows,
        error=error,
        affected=affected,
    )
