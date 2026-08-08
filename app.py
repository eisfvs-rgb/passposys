"""
app.py
------
Thin entry point that wires the modules of the application together:

  - app_core.py        Flask app creation/config, shared imports, and every
                        non-route helper function (session rollback, blob
                        compression, badge/CSV generation, group lookups,
                        the invalid-passport insert pipeline, etc.)
  - app_routes.py       Every @app.route view function except the
                         reparse/rescan ones below.
  - reparse_routes.py   The invalid-passport reparse/rescan routes (MRZ
                         re-parse, rotate, and OCR rescan on the reparse UI).
  - db_admin_routes.py  Web-based MySQL database manager (browse tables,
                        view/edit/delete rows, run ad-hoc SQL). Registered
                        as a blueprint under /db-admin. Restricted to
                        usernames listed in the DB_ADMIN_USERNAMES env var
                        -- see db_admin_routes.py for details.

Kept as `app.py` (rather than folding this into app_core.py) so existing
entry points — `from app import app` in launch.py, and any WSGI server
config pointing at `app:app` — keep working unchanged.
"""

from app_core import app
import app_routes       # noqa: F401  -- executes @app.route decorators, registering all views
import reparse_routes   # noqa: F401  -- invalid-passport reparse/rescan routes

from db_admin_routes import db_admin_bp
app.register_blueprint(db_admin_bp)
