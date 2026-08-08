import sys
import os
import re
import socket
import threading
import requests  # To talk back to Flask

# ─── 1. INITIALIZATION ───────────────────────────────────────────────────────
# PyQt6 STRICT RULE: WebEngine must be imported BEFORE QApplication is created
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication

# Initialize the app
app = QApplication(sys.argv)
app.setStyle("Fusion")

# Safely resolve paths (Handles raw .py execution AND PyInstaller .exe execution)
def get_resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath(os.path.dirname(__file__)), relative_path)

def _exe_base_dir() -> str:
    return os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__))

def _load_remote_config() -> dict | None:
    import configparser
    path = os.path.join(_exe_base_dir(), "visitvisa_config.ini")
    if not os.path.exists(path):
        return None
    try:
        cfg = configparser.ConfigParser()
        cfg.read(path)
        section = cfg["flask"]
        return {
            "host": section.get("host", "").strip(),
            "port": section.getint("port", fallback=9000),
            "token": section.get("token", "").strip(),
            "user_id": section.getint("user_id"),
        }
    except Exception as e:
        print(f"[visitvisa_config] Failed to read visitvisa_config.ini: {e}")
        return None

REMOTE_CONFIG = _load_remote_config()

FLASK_BASE_URL = (
    f"http://{REMOTE_CONFIG['host']}:{REMOTE_CONFIG['port']}"
    if REMOTE_CONFIG else
    "http://127.0.0.1:9000"
)

def _get_local_token() -> str:
    if REMOTE_CONFIG:
        return REMOTE_CONFIG["token"]
    base = _exe_base_dir()
    try:
        with open(os.path.join(base, "local_api_token.dat")) as _f:
            return _f.read().strip()
    except Exception:
        return os.environ.get("LOCAL_API_TOKEN", "")

def _get_own_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

def _register_with_flask_if_remote() -> str | None:
    if not REMOTE_CONFIG:
        return None
    try:
        resp = requests.post(
            f"{FLASK_BASE_URL}/api/register_visa_exe",
            json={
                "token": REMOTE_CONFIG["token"],
                "user_id": REMOTE_CONFIG["user_id"],
                "exe_host": _get_own_lan_ip(),
                "exe_port": VISA_SOCKET_PORT,
            },
            timeout=5,
        )
        if resp.ok and resp.json().get("success"):
            return resp.json()["secret"]
        print(f"[visitvisa] Registration failed: HTTP {resp.status_code} — {resp.text}")
    except Exception as e:
        print(f"[visitvisa] Could not reach Flask: {e}")
    return None

# ─── 2. HEAVY IMPORTS ────────────────────────────────────────────────────────
import json
from dateutil.relativedelta import relativedelta
from datetime import datetime, timedelta
from PyQt6.QtCore import QUrl, QTimer, Qt, QObject, pyqtSignal, QSize, QRect
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QStatusBar, QProgressBar, QFileDialog,
    QScrollArea, QFrame, QMessageBox, QDialog, QListWidget, QListWidgetItem,
    QStyledItemDelegate, QStyle
)
from PyQt6.QtWebEngineCore import (
    QWebEngineProfile, QWebEngineScript, QWebEngineUrlRequestInterceptor,
)
from PyQt6.QtGui import QIcon, QPixmap, QFont, QColor, QPen, QFontMetrics

# --- Fix Windows Taskbar Icon ---
if os.name == 'nt':
    import ctypes
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            'mycompany.visaautomator.local.2'
        )
    except Exception:
        pass

# ─── Configuration ────────────────────────────────────────────────────────────
VISA_URL       = "https://visa.visitsaudi.com/Login"
VISA_INDEX_URL = "https://visa.visitsaudi.com/Visa/Index"
MAX_RETRIES    = 3
STUCK_TIMEOUT     = 120   # Increased to 120s for mobile networks
PAGE_SLOW_TIMEOUT = 120   # Increased to 120s for mobile networks

# States where lag detection is active (PAGE1 and beyond, plus GROUP_CREATE —
# GROUP_CREATE can silently hang forever if the "already exists" click fails,
# since its flag is set once and never re-checked otherwise).
LAG_ACTIVE_STATES = {'GROUP_CREATE', 'PAGE1', 'PAGE2', 'INSURANCE', 'TERMS', 'REVIEW'}

# ─── Socket port — Flask pushes applicant data into the exe on this port ─────
VISA_SOCKET_PORT = 9001


class PayloadBridge(QObject):
    """Qt signal bridge — safely delivers payload from socket thread to Qt main thread."""
    payload_received = pyqtSignal(dict)


class VisaSocketServer(threading.Thread):
    def __init__(self, bridge, window_ref, registered_secret: str | None = None):
        super().__init__(daemon=True)
        self._bridge = bridge
        self._window = window_ref
        self._stop   = threading.Event()
        self._registered_secret = registered_secret

    def run(self):
        import struct
        log_path = os.path.join(os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else __file__), "socket_debug.log")

        def log(msg):
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"{datetime.now()}: {msg}\n")
            except Exception:
                pass

        bind_host = "0.0.0.0" if REMOTE_CONFIG else "127.0.0.1"
        expected_secret = self._registered_secret if REMOTE_CONFIG else None

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((bind_host, VISA_SOCKET_PORT))
        except OSError as e:
            log(f"BIND FAILED: {e}")
            print(f"[VisaSocket] Cannot bind port {VISA_SOCKET_PORT}: {e}")
            return
        srv.listen(5)
        srv.settimeout(1.0)
        log(f"Listening on port {VISA_SOCKET_PORT} (bind={bind_host})")
        print(f"[VisaSocket] Ready on {bind_host}:{VISA_SOCKET_PORT}")

        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except Exception as e:
                log(f"accept() failed: {e}")
                break

            log(f"Connection from {addr}")
            try:
                conn.settimeout(30)

                header = b""
                while len(header) < 4:
                    part = conn.recv(4 - len(header))
                    if not part:
                        break
                    header += part

                if len(header) < 4:
                    log(f"No header received (got {len(header)} bytes) — ignoring probe connection")
                    conn.close()
                    continue

                msg_len = struct.unpack("!I", header)[0]
                log(f"Header received, expecting {msg_len} bytes")

                data = b""
                while len(data) < msg_len:
                    part = conn.recv(min(65536, msg_len - len(data)))
                    if not part:
                        break
                    data += part

                log(f"Received {len(data)}/{msg_len} bytes")

                if len(data) < msg_len:
                    log(f"Incomplete payload — skipping")
                    conn.close()
                    continue

                raw     = data.decode("utf-8")
                payload = json.loads(raw)

                if expected_secret is not None:
                    if payload.get("secret") != expected_secret:
                        log("REJECTED: bad or missing secret on remote push")
                        conn.close()
                        continue

                conn.sendall(b'{"ok":true}')
                conn.close()

                count = len(payload.get("applicants", []))
                log(f"Parsed payload: {count} applicants — scheduling UI update")

                self._bridge.payload_received.emit(payload)

            except Exception as e:
                log(f"Error handling connection: {e}")
                try:
                    conn.close()
                except Exception:
                    pass

        srv.close()
        log("Server stopped")

    def stop(self):
        self._stop.set()

# ─── Anti-Bot Network Interceptor ────────────────────────────────────────────
class BlockTrackersInterceptor(QWebEngineUrlRequestInterceptor):
    BLOCKED = [
        "fullstory.com", "mfilterit.net", "appdynamics.com",
        "yandex.ru", "maze.co", "survicate.com", "adriver.ru",
        "tiktok.com", "oceanengine.com",
    ]
    def interceptRequest(self, info):
        url = info.requestUrl().toString().lower()
        if any(d in url for d in self.BLOCKED):
            info.block(True)


# ─── PAGE2 Error Dialog  (15-second auto-close, Skip only) ───────────────────
class Page2ErrorDialog(QDialog):
    """
    Shown when #divFailureMsg is visible after submitting the passport page.
    Auto-closes after 15 seconds.  Single button: Skip Applicant.
    """
    TIMER_SEC = 15

    def __init__(self, parent, error_text: str, passport: str):
        super().__init__(parent)
        self.setWindowTitle("Passport Page — Server Error")
        self.setModal(True)
        self.setMinimumWidth(480)
        self._remaining = self.TIMER_SEC

        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(22, 18, 22, 14)

        heading = QLabel("<b style='font-size:15px'>⚠️  Passport Page — Server Error</b>")
        heading.setStyleSheet("color:#c0392b;")
        # --- NEW: Make heading copyable ---
        heading.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(heading)

        body = QLabel(
            f"<b>The server rejected this application:</b><br>"
            f"<span style='color:#c0392b'>{error_text or 'Unknown server error'}</span>"
            f"<br><br><b>Passport Number:</b> {passport}"
        )
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setStyleSheet("font-size:13px; padding:4px 0;")
        # (Already exists) Make body copyable
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(body)

        self._cdown = QLabel()
        self._cdown.setStyleSheet("color:#e74c3c; font-size:11px;")
        # --- NEW: Make countdown timer copyable ---
        self._cdown.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._refresh()
        root.addWidget(self._cdown)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        skip_btn = QPushButton("⏭  Skip Applicant")
        skip_btn.setStyleSheet(
            "background:#c0392b;color:white;padding:7px 20px;"
            "border-radius:4px;font-weight:bold;"
        )
        skip_btn.setDefault(True)
        skip_btn.clicked.connect(self.accept)
        btn_row.addWidget(skip_btn)
        root.addLayout(btn_row)

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._tick)
        self._tmr.start(1000)

    def _refresh(self):
        self._cdown.setText(
            f"⏱  Auto-skipping in <b>{self._remaining}</b> second(s)…"
        )

    def _tick(self):
        self._remaining -= 1
        self._refresh()
        if self._remaining <= 0:
            self._tmr.stop()
            self.accept()


# ─── Lag / Network Dialog  (no timer, Reload Page only) ──────────────────────
# ─── Lag / Network Dialog  (no timer, Reload Page only) ──────────────────────
class LagDialog(QDialog):
    """
    Shown when automation is stuck on PAGE1+ for >= STUCK_TIMEOUT seconds.
    User can Reload Page or manually Skip Applicant.
    """
    def __init__(self, parent, stuck_state: str, reason: str, passport: str):
        super().__init__(parent)
        self.setWindowTitle("Network / Lag Issue")
        self.setModal(True)
        self.setMinimumWidth(480)

        root = QVBoxLayout(self)
        root.setSpacing(14)
        root.setContentsMargins(22, 18, 22, 16)

        heading = QLabel("<b style='font-size:15px'>🌐  Network / Lag Detected</b>")
        heading.setStyleSheet("color:#e67e22;")
        heading.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(heading)

        msg = QLabel(
            f"{reason}<br><br>"
            f"<b>Current applicant passport:</b> {passport}<br>"
            f"<b>Stuck on step:</b> <code>{stuck_state}</code><br><br>"
            f"<i>Choose <b>Reload Page</b> to retry this step, or <b>Skip Applicant</b> to bypass them and move on.</i>"
        )
        msg.setWordWrap(True)
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setStyleSheet("font-size:13px; padding:4px 0;")
        msg.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(msg)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        # --- NEW: Skip Button ---
        skip_btn = QPushButton("⏭  Skip Applicant")
        skip_btn.setStyleSheet(
            "background:#c0392b;color:white;padding:9px 20px;"
            "border-radius:4px;font-weight:bold;font-size:13px;"
        )
        skip_btn.clicked.connect(self.reject)  # Closes dialog and returns 0
        btn_row.addWidget(skip_btn)

        reload_btn = QPushButton("🔄  Reload Page")
        reload_btn.setStyleSheet(
            "background:#e67e22;color:white;padding:9px 28px;"
            "border-radius:4px;font-weight:bold;font-size:13px;"
        )
        reload_btn.setDefault(True)
        reload_btn.clicked.connect(self.accept) # Closes dialog and returns 1
        btn_row.addWidget(reload_btn)
        root.addLayout(btn_row)


# ─── Applicant Checklist Delegate ─────────────────────────────────────────────
class ApplicantItemDelegate(QStyledItemDelegate):
    """
    Renders each sidebar row as two lines with a hairline separator:
        ⏳  JOHN DOE SMITH          ← bold, coloured by status
            (401367560)             ← smaller, grey passport number
    UserRole   = passport_number
    UserRole+1 = full display name (may include 👶)
    UserRole+2 = status icon string  ('⏳' | '✅' | '❌')
    """
    ROW_H    = 54
    PAD_L    = 10
    ICON_OFF = 24   # horizontal offset for passport line (aligns under name text)

    C_BG   = QColor('#ecf0f1')
    C_SEL  = QColor('#d5e8d4')
    C_SEP  = QColor('#b2bec3')
    C_NAME = QColor('#2c3e50')
    C_DONE = QColor('#27ae60')
    C_SKIP = QColor('#e17055')
    C_PP   = QColor('#636e72')

    def paint(self, painter, option, index):
        painter.save()

        r = option.rect
        # ── Background ──────────────────────────────────────────────────────
        bg = self.C_SEL if (option.state & QStyle.StateFlag.State_Selected) else self.C_BG
        painter.fillRect(r, bg)

        # ── Retrieve data ────────────────────────────────────────────────────
        status  = index.data(Qt.ItemDataRole.UserRole + 2) or '⏳'
        name    = index.data(Qt.ItemDataRole.UserRole + 1) or ''
        pp_num  = index.data(Qt.ItemDataRole.UserRole)     or ''

        # ── Status → colour ──────────────────────────────────────────────────
        if   status == '✅': name_col = self.C_DONE
        elif status == '❌': name_col = self.C_SKIP
        else:                name_col = self.C_NAME

        px = r.left() + self.PAD_L
        # ── Line 1: status icon + full name (bold) ───────────────────────────
        # Split off a trailing " 👶" (if present) so it can be drawn with an
        # emoji-capable font — the plain QFont() used for the name text does
        # not include emoji glyphs on many systems and would render it blank.
        baby_suffix = ""
        base_name = name
        if name.endswith(" 👶"):
            base_name = name[:-2]
            baby_suffix = "👶"

        f1 = QFont()
        f1.setPointSize(11)
        f1.setBold(True)
        painter.setFont(f1)
        painter.setPen(name_col)
        name_rect = QRect(px, r.top() + 5, r.width() - self.PAD_L * 2, 22)
        main_text = f"{status}  {base_name}"
        painter.drawText(name_rect,
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         main_text)

        if baby_suffix:
            metrics = QFontMetrics(f1)
            text_width = metrics.horizontalAdvance(main_text)
            emoji_font = QFont("Segoe UI Emoji")
            emoji_font.setPointSize(11)
            painter.setFont(emoji_font)
            baby_rect = QRect(
                px + text_width + 4, r.top() + 5,
                r.width() - self.PAD_L * 2 - text_width - 4, 22
            )
            painter.drawText(baby_rect,
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             baby_suffix)

        # ── Line 2: passport number (smaller, grey) ──────────────────────────
        f2 = QFont()
        f2.setPointSize(9)
        painter.setFont(f2)
        painter.setPen(self.C_PP)
        pp_rect = QRect(px + self.ICON_OFF, r.top() + 28, r.width() - self.PAD_L * 2, 18)
        painter.drawText(pp_rect,
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         f"({pp_num})")

        # ── Bottom separator line ────────────────────────────────────────────
        painter.setPen(QPen(self.C_SEP, 1))
        painter.drawLine(r.left(), r.bottom(), r.right(), r.bottom())

        painter.restore()

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.ROW_H)


# ─── Queue-Complete Dialog  (processed count + skipped list) ─────────────────
class QueueCompleteDialog(QDialog):
    """
    Shown when the queue finishes.
    Displays: total processed count, and a scrollable card list of every
    skipped applicant with their passport number and the error message.
    """
    def __init__(self, parent, processed: int, skipped: list):
        super().__init__(parent)
        self.setWindowTitle("Complete")
        self.setModal(True)
        self.setMinimumWidth(520)
        self.setMinimumHeight(320)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setStyleSheet("background-color:#ecf0f1;")

        root = QVBoxLayout(self)
        root.setSpacing(14)
        root.setContentsMargins(26, 22, 26, 18)

        # Icon + heading
        icon_lbl = QLabel("✅")
        icon_lbl.setStyleSheet("font-size:44px;")
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(icon_lbl)

        heading = QLabel(" Complete")
        heading.setStyleSheet("font-size:19px;font-weight:bold;color:#27ae60;")
        heading.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(heading)

        # Stats row ──────────────────────────────────────────────────────────
        stats_frame = QFrame()
        stats_frame.setStyleSheet(
            "QFrame{background:#dfe6e9;border-radius:6px;}"
        )
        stats_layout = QHBoxLayout(stats_frame)
        stats_layout.setContentsMargins(16, 10, 16, 10)

        proc_lbl = QLabel(
            f"<div align='center'>"
            f"<span style='font-size:28px;font-weight:bold;color:#27ae60'>"
            f"{processed}</span><br>"
            f"<span style='font-size:11px;color:#636e72'>Processed</span>"
            f"</div>"
        )
        proc_lbl.setTextFormat(Qt.TextFormat.RichText)

        skip_lbl = QLabel(
            f"<div align='center'>"
            f"<span style='font-size:28px;font-weight:bold;color:#e17055'>"
            f"{len(skipped)}</span><br>"
            f"<span style='font-size:11px;color:#636e72'>Skipped</span>"
            f"</div>"
        )
        skip_lbl.setTextFormat(Qt.TextFormat.RichText)

        stats_layout.addStretch()
        stats_layout.addWidget(proc_lbl)
        stats_layout.addSpacing(60)
        stats_layout.addWidget(skip_lbl)
        stats_layout.addStretch()
        root.addWidget(stats_frame)

        # Skipped applicants list ────────────────────────────────────────────
        if skipped:
            skip_heading = QLabel(
                f"<b>Skipped Applicants  ({len(skipped)})</b>"
            )
            skip_heading.setStyleSheet("font-size:13px;color:#c0392b;")
            root.addWidget(skip_heading)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setMaximumHeight(230)
            scroll.setStyleSheet(
                "QScrollArea{"
                "  border:1px solid #b2bec3;"
                "  border-radius:4px;"
                "  background:#fff;"
                "}"
            )

            container = QWidget()
            container.setStyleSheet("background:#fff;")
            cbox = QVBoxLayout(container)
            cbox.setContentsMargins(8, 6, 8, 6)
            cbox.setSpacing(6)

            for idx, entry in enumerate(skipped, start=1):
                pp  = entry.get('passport', '—')
                err = entry.get('error',    'No error details available')

                card = QFrame()
                card.setStyleSheet(
                    "QFrame{"
                    "  background:#fff5f5;"
                    "  border-radius:4px;"
                    "  border-left:4px solid #e17055;"
                    "}"
                )
                card_layout = QVBoxLayout(card)
                card_layout.setContentsMargins(10, 6, 10, 6)
                card_layout.setSpacing(2)

                pp_lbl = QLabel(f"<b>#{idx} &nbsp; Passport:&nbsp; {pp}</b>")
                pp_lbl.setStyleSheet("font-size:12px;color:#2d3436;")
                # --- ADD THIS LINE ---
                pp_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

                err_lbl = QLabel(err)
                err_lbl.setWordWrap(True)
                err_lbl.setStyleSheet("font-size:11px;color:#636e72;")
                # --- ADD THIS LINE ---
                err_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

                card_layout.addWidget(pp_lbl)
                card_layout.addWidget(err_lbl)
                cbox.addWidget(card)

            cbox.addStretch()
            scroll.setWidget(container)
            root.addWidget(scroll)

        # OK button ──────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("   OK   ")
        ok_btn.setStyleSheet(
            "background:#27ae60;color:white;padding:9px 46px;"
            "border-radius:5px;font-size:14px;font-weight:bold;"
        )
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        btn_row.addStretch()
        root.addLayout(btn_row)


# ─── Helpers ─────────────────────────────────────────────────────────────────
def format_date(date_str, minus_days=0):
    if not date_str or str(date_str).lower() == 'none':
        return ""
    try:
        dt = datetime.strptime(str(date_str).split(" ")[0], "%Y-%m-%d")
        if minus_days:
            dt -= timedelta(days=minus_days)
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return str(date_str)

def esc(val):
    if val is None:
        return ""
    return (str(val)
            .replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace("\n", " ")
            .replace("\r", " "))

def prepare_image_data(base64_data, filename="photo.jpg"):
    if not base64_data or len(base64_data) < 50:
        return None, None
    return base64_data, filename

def calculate_age(dob_str):
    try:
        dob_dt = datetime.strptime(str(dob_str).split(" ")[0], "%Y-%m-%d")
        today  = datetime.today()
        return (today.year - dob_dt.year
                - ((today.month, today.day) < (dob_dt.month, dob_dt.day)))
    except Exception:
        return 18

def transform_applicant(raw, credentials, index):
    return {
        "index": index,
        "credentials": credentials,
        "applicant_data": {
            "id":                   raw.get("id", ""),   # ADDED DB ID
            "group_name":           raw.get("group_name", "GROUP 1"),
            "given_names":          raw.get("given_names", ""),
            "surname":              raw.get("surname", ""),
            "middle_name":          raw.get("middle_name", ""),
            "face_image":           raw.get("face_image", ""),
            "filename":             raw.get("filename", "photo.jpg"),
            "nationality_id":       raw.get("nationality_id", ""),
            "dob":                  raw.get("dob", ""),
            "sex":                  raw.get("sex", "M"),
            "marital_status":       str(raw.get("marital_status", "1")),
            "city_of_birth":        raw.get("city_of_birth", ""),
            "profession":           raw.get("profession", ""),
            "city":                 raw.get("city", ""),
            "zip_postal_code":      str(raw.get("zip_postal_code", "")),
            "address":              raw.get("address", ""),
            "passport_type":        str(raw.get("passport_type", "1")),
            "passport_number":      raw.get("passport_number", ""),
            "passport_issue_place": raw.get("passport_issue_place", ""),
            "passport_issue_date":  raw.get("passport_issue_date", ""),
            "expiry":               raw.get("expiry", ""),
            "expected_arrival":     raw.get("expected_arrival", ""),
            "expected_departure":   raw.get("expected_departure", ""),
            "hotel_name":           raw.get("hotel_name", ""),
            "contact_number":       raw.get("contact_number", ""),
            "email":                raw.get("email", ""),
        }
    }

# ─── JavaScript Helpers ───────────────────────────────────────────────────────
JS_HELPERS = """
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function waitForEl(sel, timeoutMs = 30000) {
    let start = Date.now();
    while (Date.now() - start < timeoutMs) {
        let el = document.querySelector(sel);
        // Check if element exists AND is visible on screen
        if (el && el.offsetParent !== null && window.getComputedStyle(el).display !== 'none') {
            await sleep(100); // Tiny buffer for safety
            return el; 
        }
        await sleep(200); // Check again in 200ms
    }
    return null; // Timed out
}

async function fill(sel, val) {
    let el = document.querySelector(sel);
    if (!el || val === undefined || val === null || String(val).trim() === '') return;

    val = String(val);
    el.focus();

    // Use native setter so React/Angular/Vue frameworks acknowledge the change
    let nativeInputValueSetter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    ).set;

    // Clear properly — trigger framework clear
    nativeInputValueSetter.call(el, '');
    el.dispatchEvent(new Event('input', { bubbles: true }));
    await sleep(80);

    for (let i = 0; i < val.length; i++) {
        let expected = val.slice(0, i + 1);

        nativeInputValueSetter.call(el, expected);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        // INCREASED SLEEP TIME to prevent framework overwrite lag (Fix 3)
        await sleep(Math.random() * 50 + 50); 

        // If framework rewrote the value — correct it before next char
        if (el.value !== expected) {
            nativeInputValueSetter.call(el, expected);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            await sleep(50);
        }
    }

    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur',   { bubbles: true }));
}

async function fillCritical(sel, val) {
    // High-reliability fill specifically for important fields (FirstName, LastName).
    // Retries up to 5 times, verifies after every character, and confirms
    // the final value before returning. Use this instead of fill() for name fields.
    if (!val || String(val).trim() === '') return;
    val = String(val).trim();

    let nativeSetter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    ).set;

    for (let attempt = 0; attempt < 5; attempt++) {
        let el = document.querySelector(sel);
        if (!el) return;

        // ── Phase 1: Aggressive multi-strategy clear ──────────────────────
        el.focus();
        await sleep(120);
        try { el.setSelectionRange(0, 9999); } catch(e) {}
        try { document.execCommand('selectAll'); document.execCommand('delete'); } catch(e) {}
        nativeSetter.call(el, '');
        el.dispatchEvent(new Event('input', { bubbles: true }));
        await sleep(220);

        // ── Phase 2: Type char-by-char with per-character verification ────
        let charOk = true;
        for (let i = 0; i < val.length; i++) {
            let el2 = document.querySelector(sel); // re-query — DOM may have reloaded
            if (!el2) { charOk = false; break; }
            let target = val.slice(0, i + 1);
            nativeSetter.call(el2, target);
            el2.dispatchEvent(new Event('input', { bubbles: true }));
            await sleep(90 + Math.random() * 60);

            // Immediate correction if framework reset it
            if (el2.value !== target) {
                nativeSetter.call(el2, target);
                el2.dispatchEvent(new Event('input', { bubbles: true }));
                await sleep(110);
                if (el2.value !== target) { charOk = false; break; }
            }
        }

        let elFinal = document.querySelector(sel);
        if (elFinal) {
            elFinal.dispatchEvent(new Event('change', { bubbles: true }));
            elFinal.dispatchEvent(new Event('blur',   { bubbles: true }));
        }
        await sleep(180);

        // ── Phase 3: Final value check — exit on success ──────────────────
        let elCheck = document.querySelector(sel);
        if (elCheck && elCheck.value === val) return; // ✅ SUCCESS

        // Value wrong — wait and retry
        await sleep(350 + attempt * 100);
    }
}

async function clickEl(sel) {
    let el = document.querySelector(sel);
    if (el) {
        el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
        await sleep(50);
        el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
        await sleep(50);
        el.click();
        el.dispatchEvent(new MouseEvent('mouseup',  { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    }
}

async function selectOption(sel, val) {
    let el = document.querySelector(sel);
    if (el) {
        el.focus(); await sleep(100);
        for (let i = 0; i < el.options.length; i++) {
            if (el.options[i].value == val) {
                el.selectedIndex = i;
                el.dispatchEvent(new Event('change', { bubbles: true }));
                break;
            }
        }
        el.blur();
    }
}

setInterval(async () => {
    let btn = document.querySelector('#btnExtend');
    if (btn && btn.offsetParent !== null) {
        await sleep(Math.random() * 500 + 200);
        btn.click();
    }
    let modal = document.querySelector('#divExecutiontimeout');
    if (modal && modal.style.display !== 'none') {
        modal.style.display = 'none';
        modal.classList.remove('show');
    }
}, 3000);
"""

# ─── Page-state Detector ─────────────────────────────────────────────────────
JS_DETECT_STATE = """
(function () {
    var ld = document.querySelector('#resultLoading');
    if (ld && ld.style.display !== 'none') return 'LOADING';

    // ── Site-wide blocking error check ───────────────────────────────
    // ── Site-wide blocking error check ───────────────────────────────
    var blockingPhrases = [
        'cannot create new visa request',
        'still valid for the same passport',
        'already has a valid visa',
        'duplicate application',
    ];

    var bodyText = (document.body.innerText || '').toLowerCase();
    for (var pi = 0; pi < blockingPhrases.length; pi++) {
        if (bodyText.indexOf(blockingPhrases[pi]) !== -1) {
            
            var bestText = "Error: " + blockingPhrases[pi];
            var minLen = 999999; // Start with an impossibly large number
            
            // Check all text-holding elements
            var allEls = document.querySelectorAll('div, p, span, h1, h2, h3, h4, h5, h6, b, strong, li');
            
            for (var i = 0; i < allEls.length; i++) {
                var el = allEls[i];
                if (window.getComputedStyle(el).display === 'none') continue;
                
                var txt = (el.innerText || el.textContent || '').trim();
                
                // If the element contains the phrase, check if it's the smallest one we've found so far
                if (txt.toLowerCase().indexOf(blockingPhrases[pi]) !== -1) {
                    if (txt.length < minLen) {
                        minLen = txt.length;
                        bestText = txt;
                    }
                }
            }
            
            window.__siteBlockingErrorText = bestText;
            return 'SITE_BLOCKING_ERROR';
        }
    }



    var emailEl = document.querySelector('#EmailId');
    if (emailEl && emailEl.offsetParent !== null) return 'LOGIN';

    var gn = document.querySelector('#txtGroupName');
    if (gn && gn.offsetParent !== null && gn.value === '') return 'GROUP_CREATE';

    if (document.querySelector('#btnApplyGroupVisa') &&
        document.querySelector('#btnApplyGroupVisa').offsetParent !== null)
        return 'DASHBOARD';

    if (document.querySelector('#FirstNameEnglish') &&
        document.querySelector('#FirstNameEnglish').offsetParent !== null)
        return 'PAGE1';

    if (document.querySelector('#PassportNumber') &&
        document.querySelector('#PassportNumber').offsetParent !== null) {

        // Collect every error surface on this page
        var hasErr = false;

        // 1. Main red failure banner
        var failDiv = document.querySelector('#divFailureMsg');
        if (failDiv && window.getComputedStyle(failDiv).display !== 'none' &&
                failDiv.textContent.trim().length > 0) hasErr = true;

        // 2. Warning banner
        if (!hasErr) {
            var warnDiv = document.querySelector('#divWarningMsg');
            if (warnDiv && window.getComputedStyle(warnDiv).display !== 'none' &&
                    warnDiv.textContent.trim().length > 0) hasErr = true;
        }

        // 3. Six-month / date-range / purpose-of-visit danger callout
        if (!hasErr) {
            var sixDiv = document.querySelector('#divSixMonthMsg');
            if (sixDiv && window.getComputedStyle(sixDiv).display !== 'none') {
                var subIds = ['#idMaxSix', '#idMaxTen', '#idPupseOfVisitMsg'];
                for (var si = 0; si < subIds.length; si++) {
                    var subEl = document.querySelector(subIds[si]);
                    if (subEl && window.getComputedStyle(subEl).display !== 'none' &&
                            subEl.textContent.trim().length > 0) {
                        hasErr = true; break;
                    }
                }
            }
        }

        // 4. Per-field inline validation errors (jQuery unobtrusive renders these)
        if (!hasErr) {
            var fieldErrs = document.querySelectorAll(
                'span.field-validation-error, ' +
                'span[data-valmsg-replace]'
            );
            for (var fi = 0; fi < fieldErrs.length; fi++) {
                var fe = fieldErrs[fi];
                if (fe.textContent.trim().length > 0 &&
                        window.getComputedStyle(fe).display !== 'none') {
                    hasErr = true; break;
                }
            }
        }

        // 5. Standalone error spans
        if (!hasErr) {
            var spanIds = [
                '#spnSelectedPurposeOfVisitMsg',
                '#spnMobileOrPhoneNumber',
                '#errMsgWhatsappMobileCountryCode'
            ];
            for (var sp = 0; sp < spanIds.length; sp++) {
                var spEl = document.querySelector(spanIds[sp]);
                if (spEl && window.getComputedStyle(spEl).display !== 'none' &&
                        spEl.textContent.trim().length > 0) {
                    hasErr = true; break;
                }
            }
        }

        return hasErr ? 'PAGE2_ERROR' : 'PAGE2';
    }

    if (document.querySelector('#chkInsurance') &&
        document.querySelector('#chkInsurance').offsetParent !== null)
        return 'INSURANCE';

    // NEW (Fix 2): Detect the Terms and Conditions page specifically
    if (document.body.innerText.includes('I HAVE READ AND AGREE ALL THE ABOVE TERMS') && document.querySelector('#btnNext'))
        return 'TERMS';

    if (document.querySelector('#chkSelectDeselectAll') &&
        document.querySelector('#chkSelectDeselectAll').offsetParent !== null &&
        !document.querySelector('#PassportNumber'))
        return 'REVIEW';

    if (document.querySelector('#btnAddMoreToGroup') &&
        document.querySelector('#btnAddMoreToGroup').offsetParent !== null)
        return 'DONE';

    return 'UNKNOWN';
})();
"""

# ─── Stuck-state reason strings ──────────────────────────────────────────────
STUCK_REASONS = {
    'GROUP_CREATE': ('The <b>Group Application</b> page has not progressed. '
                      'The group may already exist and the "Add New Application" '
                      'link could not be found/clicked in time — reloading will '
                      'retry this step.'),
    'PAGE1':     ('The <b>Personal Information</b> page has not progressed. '
                  'This may be a slow server response or network issue.'),
    'PAGE2':     ('The <b>Passport / Traveller</b> page has not advanced. '
                  'The server may be slow or there is a network disruption.'),
    'INSURANCE': 'The <b>Medical Insurance</b> page is unresponsive.',
    'TERMS':     'The <b>Terms and Conditions</b> page is unresponsive.',
    'REVIEW':    'The <b>Review Application</b> page has stalled.',
}


# ─── Main Window ─────────────────────────────────────────────────────────────
class LocalVisaAutomator(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Passposys Automator v2.3")
        
        self.setWindowIcon(QIcon(get_resource_path("icon.png")))
        self.setMinimumSize(1000, 600)
        self.resize(1400, 800)

        # ── Browser profile ──────────────────────────────────────────────────
        profile = QWebEngineProfile.defaultProfile()
        profile.setHttpUserAgent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        self.interceptor = BlockTrackersInterceptor()
        profile.setUrlRequestInterceptor(self.interceptor)

        hide_wd = QWebEngineScript()
        hide_wd.setSourceCode(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        hide_wd.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        hide_wd.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        profile.scripts().insert(hide_wd)

        # ── Network-failure tracker ────────────────────────────────────────
        # Wraps fetch() and XMLHttpRequest so we can tell whether a request
        # made during an applicant's submission actually failed server-side
        # (timeout, dropped connection, 4xx/5xx) even though the page may
        # still locally render a "success" / Add-More screen. Read via
        # window.__visaNetLog from Python right before trusting a DONE state.
        net_tracker = QWebEngineScript()
        net_tracker.setSourceCode("""
(function() {
    if (window.__visaNetLog) return; // avoid double-patching
    window.__visaNetLog = [];

    var origFetch = window.fetch;
    if (origFetch) {
        window.fetch = function() {
            var args = arguments;
            var urlArg = args[0];
            var url = (urlArg && urlArg.url) ? urlArg.url : String(urlArg);
            return origFetch.apply(this, args).then(function(resp) {
                if (!resp.ok) {
                    window.__visaNetLog.push({
                        type: 'fetch', url: url, status: resp.status, time: Date.now()
                    });
                }
                return resp;
            }).catch(function(err) {
                window.__visaNetLog.push({
                    type: 'fetch-error', url: url, error: String(err), time: Date.now()
                });
                throw err;
            });
        };
    }

    var origOpen = XMLHttpRequest.prototype.open;
    var origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url) {
        this.__visaUrl = url;
        return origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function() {
        var xhr = this;
        xhr.addEventListener('loadend', function() {
            if (xhr.status === 0 || xhr.status >= 400) {
                window.__visaNetLog.push({
                    type: 'xhr', url: String(xhr.__visaUrl),
                    status: xhr.status, time: Date.now()
                });
            }
        });
        return origSend.apply(this, arguments);
    };
})();
""")
        net_tracker.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        net_tracker.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        net_tracker.setRunsOnSubFrames(True)
        profile.scripts().insert(net_tracker)

        # ── Layout ───────────────────────────────────────────────────────────
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        ctrl = QWidget()
        ctrl.setStyleSheet("background-color:#2c3e50;color:white;")
        ctrl.setFixedHeight(50)
        cl = QHBoxLayout(ctrl)

        logo_lbl = QLabel()
        logo_path = get_resource_path("logo.png")
        if os.path.exists(logo_path):
            pix = QPixmap(logo_path)
            logo_lbl.setPixmap(
                pix.scaledToHeight(30, Qt.TransformationMode.SmoothTransformation)
            )
        logo_lbl.setContentsMargins(0, 0, 10, 0)
        cl.addWidget(logo_lbl)

        title_lbl = QLabel("PasspoSys")
        title_lbl.setStyleSheet("font-size:18px;font-weight:bold;")
        cl.addWidget(title_lbl)

        self.btn_load = QPushButton("📂 Load JSON File")
        self.btn_load.setStyleSheet(
            "background-color:#2980b9;padding:8px 15px;"
            "border-radius:4px;font-weight:bold;"
        )
        self.btn_load.clicked.connect(self.load_json_file)
        self.btn_load.setVisible(False)  # Hidden as requested
        cl.addWidget(self.btn_load)

        cl.addStretch()

        self.btn_start = QPushButton("▶ Start Sending")
        self.btn_start.setStyleSheet(
            "background-color:#27ae60;padding:8px 15px;"
            "border-radius:4px;font-weight:bold;"
        )
        self.btn_start.clicked.connect(self.toggle_automation)
        self.btn_start.setEnabled(False)
        cl.addWidget(self.btn_start)

        btn_reload = QPushButton("⟳ Reload Page")
        btn_reload.setStyleSheet(
            "background-color:#e67e22;padding:8px 15px;"
            "border-radius:4px;font-weight:bold;"
        )
        btn_reload.clicked.connect(self.reload_browser)
        cl.addWidget(btn_reload)

        # --- NEW: Main UI Skip Button ---
        self.btn_skip = QPushButton("⏭ Skip Applicant")
        self.btn_skip.setStyleSheet(
            "background-color:#c0392b;padding:8px 15px;"
            "border-radius:4px;font-weight:bold;"
        )
        self.btn_skip.clicked.connect(self.manual_skip_applicant)
        cl.addWidget(self.btn_skip)
        # --------------------------------

        layout.addWidget(ctrl)

        sbar = QWidget()
        sbar.setFixedHeight(30)
        sbar.setStyleSheet("background-color:#34495e;color:#ecf0f1;")
        sl = QHBoxLayout(sbar)
        sl.setContentsMargins(10, 0, 10, 0)
        self.lbl_status   = QLabel("Status: Please load Applicant…")
        self.lbl_progress = QLabel("Progress: 0/0")
        sl.addWidget(self.lbl_status)
        sl.addWidget(self.lbl_progress)
        sl.addStretch()
        self.lbl_passport = QLabel("Passport: — idle —")
        sl.addWidget(self.lbl_passport)
        layout.addWidget(sbar)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(5)
        self.progress_bar.setStyleSheet(
            "QProgressBar{border:none;background-color:#34495e;}"
            "QProgressBar::chunk{background-color:#27ae60;}"
        )
        layout.addWidget(self.progress_bar)

        # ── Browser and Sidebar Checklist ────────────────────────────────────
        main_body = QWidget()
        h_layout = QHBoxLayout(main_body)
        h_layout.setContentsMargins(0, 0, 0, 0)

        # Create the sidebar for the checklist
        # Create the sidebar for the checklist
        self.checklist = QListWidget()
        self.checklist.setFixedWidth(265)
        self.checklist.setStyleSheet(
            "QListWidget { background-color: #ecf0f1; border: none; outline: none; }"
            "QListWidget::item { border: none; padding: 0px; }"
            "QListWidget::item:selected { background-color: #d5e8d4; }"
        )
        self.checklist.setItemDelegate(ApplicantItemDelegate(self.checklist))

        # --- NEW: Enable Copying (Right-click menu + Ctrl+C) ---
        self.checklist.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.checklist.customContextMenuRequested.connect(self._show_checklist_context_menu)
        
        from PyQt6.QtGui import QAction, QKeySequence
        copy_action = QAction("Copy", self)
        copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        copy_action.triggered.connect(self._copy_checklist_item)
        self.checklist.addAction(copy_action)
        # -------------------------------------------------------

        h_layout.addWidget(self.checklist)

        self.browser = QWebEngineView()
        h_layout.addWidget(self.browser)
        
        layout.addWidget(main_body)

        self.statusBar_ = QStatusBar()
        self.setStatusBar(self.statusBar_)
        self.statusBar_.setStyleSheet("color:#ffffff;")
        self.statusBar_.showMessage("Ready. Please load applicant data.")

        # Connect browser load signals to update status bar
        self.browser.loadStarted.connect(self._on_load_started)
        self.browser.loadFinished.connect(self._on_load_finished)

        # ── Runtime state ────────────────────────────────────────────────────
        self.queue                = []
        self.total_count          = 0
        self.adult_names_in_group = []
        self.adult_data_in_group  = []   # [{name: str, nat: str}, ...] for guardian nationality matching
        self.current_applicant    = None
        self.is_processing_step   = False
        self.is_running           = False
        self.step_flags           = self._fresh_flags()
        self.retry_count          = 0
        self._json_file_path      = None
        self._load_error_count    = 0    # consecutive page-load failures during automation

        # Completion stats
        self._processed_count = 0   # incremented in DONE
        self._skipped_list    = []  # {'passport': ..., 'error': ...}

        # Stuck-state tracking
        self._stuck_state      = None
        self._stuck_since      = None
        self._step_start_time  = None   # when we first started filling this step
        self._pause_until      = None   # Python-side lock for safe page transitions

        # ── Field-by-field sub-step engine ────────────────────────────────────
        # Lets pause/resume stop and continue between individual field fills
        # instead of only between whole pages. Built once per page-state via
        # _build_sub_steps(state, user, creds); consumed one at a time by _tick().
        self._sub_steps       = []    # [(field_label:str, js:str), ...] for current page
        self._sub_step_idx    = 0     # index of the NEXT sub-step to run
        self._sub_steps_state = None  # which page state this queue belongs to

        # ── Diagnostic log (shared with socket_debug.log) ─────────────────────
        self._diag_log_path = os.path.join(
            os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else __file__),
            "socket_debug.log"
        )
        self._diag_seq = 0  # running counter so log lines are easy to order/grep

        self.timer = QTimer()
        self.timer.timeout.connect(self._tick)
        self.browser.setUrl(QUrl(VISA_URL))

# ── Start socket server so Flask can push data directly ──────────────
# ── Start socket server so Flask can push data directly ──────────────
        self._payload_bridge = PayloadBridge()
        self._payload_bridge.payload_received.connect(self.load_from_payload)
        registered_secret = _register_with_flask_if_remote()
        self._socket_server = VisaSocketServer(self._payload_bridge, self, registered_secret)
        self._socket_server.start()

        # Auto-load data if passed via command line (legacy mode)
        if len(sys.argv) > 1 and sys.argv[1].endswith('.json'):
            self._auto_load(sys.argv[1])

    # ─── Helpers ─────────────────────────────────────────────────────────────

    # ─── Checklist Copying Helpers ────────────────────────────────────────

    def _show_checklist_context_menu(self, pos):
        item = self.checklist.itemAt(pos)
        if not item:
            return
        
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self.checklist)
        menu.setStyleSheet(
            "QMenu { background-color: #fff; border: 1px solid #bdc3c7; }"
            "QMenu::item { padding: 6px 24px; }"
            "QMenu::item:selected { background-color: #3498db; color: white; }"
        )
        
        pp_num = item.data(Qt.ItemDataRole.UserRole)
        raw_name = item.data(Qt.ItemDataRole.UserRole + 1) or ""
        clean_name = str(raw_name).replace(" 👶", "").strip()
        
        action_copy_all = menu.addAction("📋 Copy Name & Passport")
        action_copy_pp = menu.addAction("📄 Copy Passport Only")
        action_copy_name = menu.addAction("📝 Copy Name Only")
        
        action = menu.exec(self.checklist.mapToGlobal(pos))
        
        clipboard = QApplication.clipboard()
        if action == action_copy_all:
            clipboard.setText(f"{clean_name} ({pp_num})")
            self.statusBar_.showMessage(f"Copied: {clean_name} ({pp_num})")
        elif action == action_copy_pp:
            clipboard.setText(str(pp_num))
            self.statusBar_.showMessage(f"Copied: {pp_num}")
        elif action == action_copy_name:
            clipboard.setText(clean_name)
            self.statusBar_.showMessage(f"Copied: {clean_name}")

    def _copy_checklist_item(self):
        """Triggered via Ctrl+C shortcut on the list"""
        item = self.checklist.currentItem()
        if item:
            pp_num = item.data(Qt.ItemDataRole.UserRole)
            raw_name = item.data(Qt.ItemDataRole.UserRole + 1) or ""
            clean_name = str(raw_name).replace(" 👶", "").strip()
            QApplication.clipboard().setText(f"{clean_name} ({pp_num})")
            self.statusBar_.showMessage(f"Copied: {clean_name} ({pp_num})")

    def closeEvent(self, event):
        try:
            app_id = None
            if self.current_applicant:
                app_id = self.current_applicant['applicant_data'].get('id')
            if not app_id and self.queue:
                app_id = self.queue[-1]['applicant_data'].get('id')

            if app_id:
                requests.post(
                    f"{FLASK_BASE_URL}/api/clear_visit_visa_queue",
                    json={"id": app_id, "token": _get_local_token()},
                    timeout=3,
                )
        except Exception as e:
            self._diag(f"[closeEvent] Failed to clear visit_visa_queue: {e}")

        if REMOTE_CONFIG:
            try:
                requests.post(
                    f"{FLASK_BASE_URL}/api/unregister_visa_exe",
                    json={"token": REMOTE_CONFIG["token"], "user_id": REMOTE_CONFIG["user_id"]},
                    timeout=3,
                )
            except Exception as e:
                self._diag(f"[closeEvent] Failed to unregister exe: {e}")

        super().closeEvent(event)

    def _diag(self, msg: str):
        """Append a timestamped diagnostic line to socket_debug.log.
        Used to compare what the EXE thinks happened vs. what the site
        actually shows, so mismatches (fewer entries on site than ticks
        in the app) can be pinpointed from the log instead of guessed at."""
        self._diag_seq += 1
        try:
            with open(self._diag_log_path, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now()} [#{self._diag_seq:04d}] {msg}\n")
        except Exception:
            pass

    def _fresh_flags(self):
        return {k: False for k in [
            'LOGIN', 'DASHBOARD', 'GROUP_CREATE',
            'PAGE1', 'PAGE2', 'PAGE2_ERROR', 'SITE_BLOCKING_ERROR',
            'INSURANCE', 'TERMS', 'REVIEW', 'DONE',
        ]}

    def _on_load_started(self):
        self.statusBar_.showMessage("⏳  Page loading…")

    def _on_load_finished(self, ok: bool):
        if not ok:
            self.statusBar_.showMessage("⚠️  Page load reported an error.")
            # Auto-reload when automation is active and we have a current applicant
            if self.is_running and self.current_applicant:
                self._load_error_count += 1
                if self._load_error_count <= MAX_RETRIES:
                    # Reset step flags so the page is refilled from scratch on reload
                    self.step_flags       = self._fresh_flags()
                    self.is_processing_step = False
                    self._stuck_state     = None
                    self._stuck_since     = None
                    self._step_start_time = None
                    pp = self.current_applicant['applicant_data'].get('passport_number', '—')
                    self.statusBar_.showMessage(
                        f"⚠️ Page load error for passport {pp} — "
                        f"auto-reloading (attempt {self._load_error_count}/{MAX_RETRIES})…"
                    )
                    QTimer.singleShot(3000, self.browser.reload)
                else:
                    # Too many consecutive load errors — pause the queue
                    self._load_error_count = 0
                    self.is_running = False
                    self.timer.stop()
                    self.btn_start.setText("▶ Resume ")
                    self.btn_start.setStyleSheet(
                        "background-color:#27ae60;padding:8px 15px;"
                        "border-radius:4px;font-weight:bold;"
                    )
                    self.lbl_status.setText(
                        "Status: Paused — repeated page-load errors, resume when ready."
                    )
                    pp = self.current_applicant['applicant_data'].get('passport_number', '—')
                    self.statusBar_.showMessage(
                        f"⏸ paused after {MAX_RETRIES} load errors for passport {pp}. "
                        f"Fix network then click Resume."
                    )
                    self.is_processing_step = False
        else:
            self._load_error_count = 0   # Reset counter on clean load
            self.statusBar_.showMessage("✅  Page loaded.")

    def _set_status(self, msg: str):
        self.statusBar_.showMessage(msg)
        self.lbl_status.setText(f"Status: {msg}")

    def _update_progress(self):
        self.lbl_progress.setText(
            f"Progress: {self._processed_count}/{self.total_count}"
        )
        self.progress_bar.setValue(self._processed_count)

    # ─── JSON file handling ───────────────────────────────────────────────────

    def load_json_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, ""
        )
        if not path:
            return
        self._auto_load(path)

    def _auto_load(self, filepath):
        try:
            self._json_file_path = filepath
            with open(filepath, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            
            creds = raw.get("credentials", {})
            applicants = raw.get("applicants", [])

            # Adults first, minors after — matches load_from_payload behaviour.
            # Ensures adult_names_in_group is populated before any minor's PAGE1 runs.
            applicants = sorted(
                applicants,
                key=lambda a: 0 if calculate_age(a.get("dob", "")) >= 18 else 1
            )

            self.queue = []
            self.adult_names_in_group = []
            self.adult_data_in_group  = []
            self._processed_count = 0
            self._skipped_list    = []
            self.checklist.clear()  # Clear the UI list

            for i, app_data in enumerate(applicants, start=1):
                t = transform_applicant(app_data, creds, i)
                self.queue.append(t)
                
                if calculate_age(t['applicant_data'].get('dob', '')) >= 18:
                    name = (
                        f"{t['applicant_data'].get('given_names','')} "
                        f"{t['applicant_data'].get('surname','')}".strip()
                    )
                    self.adult_names_in_group.append(name)
                    self.adult_data_in_group.append({
                        "name": name,
                        "nat":  str(t['applicant_data'].get('nationality_id', '')),
                        "sex":  str(t['applicant_data'].get('sex', '')).upper(),
                    })
                
                # Add to UI Checklist as unticked (⏳)
                pp_num     = t['applicant_data'].get('passport_number', 'Unknown')
                given      = t['applicant_data'].get('given_names', '')
                sur        = t['applicant_data'].get('surname', '')
                full_name  = f"{given} {sur}".strip()
                age_ch     = calculate_age(t['applicant_data'].get('dob', ''))
                age_tag    = " 👶" if age_ch < 18 else ""
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole,     pp_num)
                item.setData(Qt.ItemDataRole.UserRole + 1, f"{full_name}{age_tag}")
                item.setData(Qt.ItemDataRole.UserRole + 2, '⏳')
                item.setSizeHint(QSize(265, 54))
                self.checklist.addItem(item)

            self.total_count = len(self.queue)
            if self.total_count:
                self.progress_bar.setMaximum(self.total_count)
                self.progress_bar.setValue(0)
                self.lbl_progress.setText(f"Progress: 0/{self.total_count}")
                self.btn_start.setEnabled(True)
                self.current_applicant = None
                self.step_flags = self._fresh_flags()
                self._set_status(f"Records loaded automatically. Ready to start.")
            else:
                QMessageBox.warning(self, "Empty File",
                                    "The JSON file contains no applicants.")
        except Exception as ex:
            QMessageBox.critical(self, "Load Error", f"Cannot parse JSON:\n{ex}")
            self._set_status("Failed to load JSON.")

    def load_from_payload(self, payload: dict):
        """
        Called by VisaSocketServer when Flask pushes data directly into the exe.
        Sorts adults first, loads the queue and checklist, brings window to front.
        """
        try:
            creds      = payload.get("credentials", {})
            applicants = payload.get("applicants", [])
            total_hint = payload.get("total", len(applicants))

            # Adults (>=18) first, minors after
            applicants = sorted(
                applicants,
                key=lambda a: 0 if calculate_age(a.get("dob", "")) >= 18 else 1
            )

            # total_hint == 1 marks a continuation push (one applicant added
            # to an in-progress batch); anything else is the start of a new
            # batch, so reset the queue/checklist only in that case.
            is_new_batch = payload.get("is_new_batch", total_hint != 1)
            if is_new_batch:
                self.queue                = []
                self.adult_names_in_group = []
                self.adult_data_in_group  = []
                self._processed_count     = 0
                self._skipped_list        = []
                self._json_file_path      = None  # socket mode — no file
                self.checklist.clear()

            for i, app_data in enumerate(applicants, start=len(self.queue) + 1):
                t   = transform_applicant(app_data, creds, i)
                age = calculate_age(t['applicant_data'].get('dob', ''))
                self.queue.append(t)

                if age >= 18:
                    name = (
                        f"{t['applicant_data'].get('given_names', '')} "
                        f"{t['applicant_data'].get('surname', '')}".strip()
                    )
                    self.adult_names_in_group.append(name)
                    self.adult_data_in_group.append({
                        "name": name,
                        "nat":  str(t['applicant_data'].get('nationality_id', '')),
                        "sex":  str(t['applicant_data'].get('sex', '')).upper(),
                    })

                pp_num    = t['applicant_data'].get('passport_number', 'Unknown')
                given     = t['applicant_data'].get('given_names', '')
                sur       = t['applicant_data'].get('surname', '')
                full_name = f"{given} {sur}".strip()
                age_tag   = " 👶" if age < 18 else ""
                item      = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole,     pp_num)
                item.setData(Qt.ItemDataRole.UserRole + 1, f"{full_name}{age_tag}")
                item.setData(Qt.ItemDataRole.UserRole + 2, '⏳')
                item.setSizeHint(QSize(265, 54))
                self.checklist.addItem(item)

            if is_new_batch:
                self.total_count = total_hint if total_hint else len(self.queue)

            if self.total_count:
                self.progress_bar.setMaximum(self.total_count)
                if is_new_batch:
                    self.progress_bar.setValue(0)
                    self.lbl_progress.setText(f"Progress: 0/{self.total_count}")
                    self.btn_start.setEnabled(True)
                    self.current_applicant = None
                    self.step_flags        = self._fresh_flags()
                    self._set_status(
                        f"✅ {self.total_count} applicant(s) loaded — click ▶ Start Sending."
                    )
                    self.showNormal()
                    self.raise_()
                    self.activateWindow()
                else:
                    self._update_progress()
            else:
                QMessageBox.warning(self, "Empty Payload", "No applicants were received.")
        except Exception as ex:
            QMessageBox.critical(self, "Load Error",
                                 f"Could not load received data:\n{ex}")

    def _remove_processed_applicant(self):
        if not self.current_applicant or not self._json_file_path:
            # Socket mode — no file to update
            return
        try:
            with open(self._json_file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            pp  = self.current_applicant['applicant_data'].get('passport_number')
            aid = self.current_applicant['applicant_data'].get('id')
            data['applicants'] = [
                a for a in data.get('applicants', [])
                if a.get('passport_number') != pp and a.get('id') != aid
            ]
            data['total'] = len(data['applicants'])
            backup = self._json_file_path + '.backup'
            if not os.path.exists(backup):
                import shutil
                shutil.copy2(self._json_file_path, backup)
            with open(self._json_file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=1, ensure_ascii=False)
            self.statusBar_.showMessage(
                f"Removed applicant from JSON. Remaining: {data['total']}"
            )
        except Exception as ex:
            self.statusBar_.showMessage(f"⚠️ Could not update JSON: {ex}")

    # ─── Browser controls ─────────────────────────────────────────────────────

    def reload_browser(self):
        self.step_flags = self._fresh_flags()
        self.is_processing_step = False
        self.retry_count      = 0
        self._stuck_state     = None
        self._stuck_since     = None
        self._step_start_time = None
        self._pause_until     = None
        self._load_error_count = 0   # Reset page-load error counter on manual reload
        self.browser.reload()
        self.statusBar_.showMessage("Page reloaded — state reset.")
    def manual_skip_applicant(self):
        """Triggered by the top-bar Skip button. Instantly skips the current applicant."""
        if not self.current_applicant:
            self.statusBar_.showMessage("⚠️ No active applicant to skip.")
            return
            
        self.statusBar_.showMessage("Skipping applicant manually...")
        
        # We reuse your existing skip logic, but pass show_dialog=False 
        # so it skips instantly without making you wait for a 15-second timer!
        self._on_page2_error("Manually skipped by operator.", show_dialog=False)
    def toggle_automation(self):
        if not self.queue and not self.current_applicant:
            self.statusBar_.showMessage(" Load first Applicant.")
            return
        if not self.is_running:
            self.is_running = True
            self.btn_start.setText("⏸ Pause")
            self.btn_start.setStyleSheet(
                "background-color:#c0392b;padding:8px 15px;"
                "border-radius:4px;font-weight:bold;"
            )
            self.lbl_status.setText("Status: Running…")
            self.timer.start(500)
            self._tick()
            self.statusBar_.showMessage("✅ Automation started.")
        else:
            self.is_running = False
            self.btn_start.setText("▶ Resume")
            self.btn_start.setStyleSheet(
                "background-color:#27ae60;padding:8px 15px;"
                "border-radius:4px;font-weight:bold;"
            )
            self.lbl_status.setText("Status: Paused.")
            self.timer.stop()
            self.statusBar_.showMessage("⏸ Automation paused.")

    # ─── Main tick ───────────────────────────────────────────────────────────

    def _tick(self):
        if self.is_processing_step or not self.is_running:
            return
            
        # Check if we are enforcing a Python-side pause (e.g. waiting for page transitions)
        if self._pause_until and datetime.now() < self._pause_until:
            return

        self.is_processing_step = True

        if not self.current_applicant:
            if self.queue:
                self.current_applicant = self.queue.pop(0)
                self.step_flags   = self._fresh_flags()
                self.retry_count  = 0
                self._stuck_state     = None
                self._stuck_since     = None
                self._step_start_time = None
                pp = self.current_applicant['applicant_data'].get('passport_number', '—')
                self.lbl_passport.setText(f"Passport: {pp}")
                self._diag(
                    f"POP queue → passport={pp} "
                    f"name={self.current_applicant['applicant_data'].get('given_names','')} "
                    f"remaining_in_queue={len(self.queue)} "
                    f"browser_url={self.browser.url().toString()}"
                )
            else:
                # --- PREVENT PREMATURE COMPLETION ---
                total_handled = self._processed_count + len(self._skipped_list)
                if self.total_count > 0 and total_handled < self.total_count:
                    # Still waiting for Flask to push the next applicant over the socket
                    self.statusBar_.showMessage(f"⏳ Waiting for applicant {total_handled + 1} of {self.total_count} from server...")
                    self.is_processing_step = False
                    return
                # ----------------------------------------------

                # ── All done ─────────────────────────────────────────────────
                self.lbl_status.setText("Status: All done!.")
                self.lbl_progress.setText(
                    f"Progress: {self._processed_count}/{self.total_count}"
                )
                self.progress_bar.setValue(self._processed_count)
                self.is_running = False
                self.btn_start.setText("▶ Start Sending")
                self.btn_start.setStyleSheet(
                    "background-color:#27ae60;padding:8px 15px;"
                    "border-radius:4px;font-weight:bold;"
                )
                self.timer.stop()
                self.is_processing_step = False
                self.statusBar_.showMessage("✅ All applicants processed!")

                # Bring the main window back if it was minimized, so the
                # completion popup is actually visible to the operator.
                if self.isMinimized():
                    self.showNormal()
                self.raise_()
                self.activateWindow()

                QueueCompleteDialog(
                    self, self._processed_count, self._skipped_list
                ).exec()
                # Navigate back to the visa index page after the operator clicks OK
                self.browser.setUrl(QUrl(VISA_INDEX_URL))
                # Delete the source JSON file now that the batch is complete
                if self._json_file_path and os.path.exists(self._json_file_path):
                    try:
                        os.remove(self._json_file_path)
                        backup_path = self._json_file_path + '.backup'
                        if os.path.exists(backup_path):
                            os.remove(backup_path)
                        self.statusBar_.showMessage(
                            f"🗑️  Deleted batch file: "
                            f"{os.path.basename(self._json_file_path)}"
                        )
                    except Exception as del_err:
                        self.statusBar_.showMessage(
                            f"⚠️  Could not delete batch file: {del_err}"
                        )
                    finally:
                        self._json_file_path = None
                return

        self.browser.page().runJavaScript(JS_DETECT_STATE, self._handle_state)

    # ─── State handler ───────────────────────────────────────────────────────

    def _handle_state(self, state: str):
        try:
            if not self.is_running or not self.current_applicant:
                self.is_processing_step = False
                return

            if state in ('LOADING', 'UNKNOWN'):
                self.is_processing_step = False
                return

            # ── Lag / slow-network detection (PAGE1+ only) ───────────────────
            if self.step_flags.get(state) and state in LAG_ACTIVE_STATES:
                now = datetime.now()

                # ── Trigger 1: Total-page slow timeout ──────────────
                if (self._step_start_time is not None and
                        (now - self._step_start_time).total_seconds()
                        >= PAGE_SLOW_TIMEOUT):
                    self._step_start_time = None
                    self._stuck_state     = None
                    self._stuck_since     = None
                    self._show_lag_dialog(state, slow=True)
                    return

                # ── Trigger 2: Post-fill stuck window ───────────────
                if self._stuck_state != state:
                    self._stuck_state = state
                    self._stuck_since = now
                elif (now - self._stuck_since).total_seconds() >= STUCK_TIMEOUT:
                    self._stuck_state = None
                    self._stuck_since = None
                    self._show_lag_dialog(state, slow=False)
                    return

                self.is_processing_step = False
                return

            # For early states (LOGIN / DASHBOARD / GROUP_CREATE) whose flag is
            # already set: wait silently — no lag dialog for these steps.
            if self.step_flags.get(state) and state not in ('PAGE2_ERROR', 'SITE_BLOCKING_ERROR'):
                self.is_processing_step = False
                return

            # State progressed → reset stuck tracker and step timer
            if state != self._stuck_state:
                self._stuck_state     = None
                self._stuck_since     = None
                self._step_start_time = None

            creds = self.current_applicant['credentials']
            user  = self.current_applicant['applicant_data']

            # ── LOGIN ────────────────────────────────────────────────────────
            if state == 'LOGIN':
                js = f"""(async function() {{ {JS_HELPERS}
                await waitForEl('#EmailId');
                await fill('#EmailId', '{esc(creds["username"])}');
                await sleep(500);
                await fill('#Password', '{esc(creds["password"])}');
                await sleep(500);
                var cap = document.querySelector('#CaptchaCode');
                if (cap) {{
                cap.focus(); cap.style.border = '3px solid red';
                }}
                }})();"""
                self.browser.page().runJavaScript(js)
                self.step_flags['LOGIN'] = True
                self.statusBar_.showMessage("Credentials typed — enter CAPTCHA.")

            # ── DASHBOARD ────────────────────────────────────────────────────
            elif state == 'DASHBOARD':
                self.browser.page().runJavaScript(
                    f"(async function(){{ {JS_HELPERS} "
                    f"await sleep(300); await clickEl('#btnApplyGroupVisa'); }})();"
                )
                self.step_flags['DASHBOARD'] = True
                self.statusBar_.showMessage("Navigating to group application…")

            # ── GROUP CREATE ─────────────────────────────────────────────────
            elif state == 'GROUP_CREATE':
                grp = esc(user.get("group_name", "GROUP 1"))
                js = f"""(async function(){{ {JS_HELPERS}
                    await sleep(500);
                    await fill('#txtGroupName', '{grp}');
                    await sleep(200);
                    await clickEl('#btnCreateGroup');

                    // FIX: poll for the error span / group labels instead of a
                    // single fixed 600ms sleep — on a slow connection the
                    // "already exists" message or the group-label list can take
                    // longer than that to render, which previously caused the
                    // click-match logic below to run against a page that hadn't
                    // finished updating yet (hasError/labels both empty), so
                    // nothing got clicked and the automation silently hung here
                    // forever (GROUP_CREATE has no re-run and, before this fix,
                    // no lag-timeout either).
                    var errSpan  = null;
                    var hasError = false;
                    for (var t = 0; t < 10; t++) {{   // up to ~3s total
                        errSpan  = document.querySelector('#spnErrMsg');
                        hasError = errSpan &&
                            errSpan.textContent.toLowerCase().includes('already exists');
                        if (hasError) break;
                        await sleep(300);
                    }}

                    var diag = {{
                        target_group: '{grp}',
                        already_exists: hasError,
                        all_group_labels: [],
                        matched_label: false,
                        clicked: false
                    }};

                    if (hasError) {{
                        var labels = [];
                        for (var t2 = 0; t2 < 6; t2++) {{   // up to ~1.8s total
                            labels = document.querySelectorAll('[id^="lblGroupName"]');
                            if (labels.length > 0) break;
                            await sleep(300);
                        }}
                        diag.all_group_labels = Array.prototype.map.call(
                            labels, function(l) {{ return l.textContent.trim(); }}
                        );
                        var clicked = false;
                        for (var i = 0; i < labels.length; i++) {{
                            var lbl = labels[i];
                            if (lbl.textContent.trim().toLowerCase() === '{grp}'.toLowerCase()) {{
                                diag.matched_label = true;
                                var row = lbl.closest('.d-flex.flex-wrap');
                                if (row) {{
                                    var addLink = row.querySelector(
                                        '.tools_btn a.px-2[title="Add New Application"]'
                                    );
                                    if (addLink) {{
                                        addLink.dispatchEvent(
                                            new MouseEvent('mouseover', {{bubbles:true}})
                                        );
                                        await sleep(100);
                                        addLink.click();
                                        clicked = true; break;
                                    }}
                                }}
                            }}
                        }}
                        if (!clicked) {{
                            for (var j = 0; j < labels.length; j++) {{
                                var lbl2 = labels[j];
                                if (lbl2.textContent.trim().replace(/\\s+/g,' ').toLowerCase()
                                        === '{grp}'.toLowerCase()) {{
                                    diag.matched_label = true;
                                    var row2 = lbl2.closest('.d-flex.flex-wrap');
                                    if (row2) {{
                                        var al2 = row2.querySelector(
                                            '.tools_btn a.px-2[title="Add New Application"]'
                                        );
                                        if (al2) {{ al2.click(); clicked = true; break; }}
                                    }}
                                }}
                            }}
                        }}
                        diag.clicked = clicked;
                    }}
                    return diag;
                }})();"""
                self.browser.page().runJavaScript(js, self._on_group_create_diag)
                self.step_flags['GROUP_CREATE'] = True
                self._step_start_time = datetime.now()
                self.statusBar_.showMessage(f"Creating/joining group '{grp}'…")

            # ── PAGE 1 ───────────────────────────────────────────────────────
            elif state == 'PAGE1':
                raw_b64, raw_fname = prepare_image_data(
                    user.get("face_image", ""), user.get("filename", "photo.jpg")
                )
                b64_lit   = json.dumps(raw_b64  if raw_b64  else "")
                fname_lit = json.dumps(raw_fname if raw_fname else "photo.jpg")
                nat       = user.get("nationality_id", "")
                mid       = user.get("middle_name", "")
                js_mid    = (
                    f"await fill('#FatherNameEnglish','{esc(mid)}'); await sleep(100);"
                    if mid and str(mid).strip() else ""
                )
                is_minor        = calculate_age(user.get("dob", "")) < 18
                js_minor        = "true" if is_minor else "false"
                adults_json     = json.dumps(self.adult_names_in_group)
                adults_data_json = json.dumps(self.adult_data_in_group)  # [{name, nat}]
                minor_nat        = str(user.get("nationality_id", ""))

                js = f"""(async function() {{ {JS_HELPERS}
                    /* Reduced from 4000ms — server session-overwrite guard, 1500ms is minimum safe value */
                    await sleep(1500);
                    
                    await waitForEl('#ApplyingVisaForSomeoneElseYes');
                    document.querySelectorAll('div.info').forEach(e => e.remove());
                    var yesRadio = document.querySelector('#ApplyingVisaForSomeoneElseYes');
                    if (yesRadio && !yesRadio.checked) {{ yesRadio.click(); await sleep(150); }}

                    var b64 = {b64_lit}; var fname = {fname_lit};
                    if (b64 && b64.length >= 50) {{
                        var cleanB64 = b64.includes(',') ? b64.split(',')[1] : b64;
                        let img = new Image(); img.src = 'data:image/jpeg;base64,' + cleanB64;
                        img.onload = function () {{
                            let c = document.createElement('canvas');
                            c.width = 200; c.height = 200;
                            let ctx = c.getContext('2d');
                            ctx.fillStyle = '#FFF'; ctx.fillRect(0,0,200,200);
                            let s = Math.min(200/img.width, 200/img.height);
                            let w = img.width*s, h = img.height*s;
                            ctx.drawImage(img,(200-w)/2,(200-h)/2,w,h);
                            let fb64 = c.toDataURL('image/jpeg',0.95).split(',')[1];
                            document.querySelector('#ProfileImageBytes').value = fb64;
                            document.querySelector('#OriginalUploadedProfileImageBytes').value = fb64;
                            document.querySelector('#AttachmentOriginalName').value = fname;
                            document.querySelector('#PictureDetOriginalFileName').value = fname;
                            var disp = document.getElementById('croppedImageId');
                            if (disp) {{ disp.src='data:image/jpeg;base64,'+fb64; disp.style.display='block'; }}
                            var lbl = document.querySelector('label[for="AttachmentPersonalPicture"]');
                            if (lbl) {{ lbl.innerHTML='✅ '+fname; lbl.style.backgroundColor='#27ae60'; lbl.style.color='#fff'; }}
                        }};
                    }}
                     await sleep(200);

                    var natVal = String({nat});
                    if (natVal && natVal !== 'undefined' && natVal !== '0') {{
                        await selectOption('#Nationality',    natVal); await sleep(100);
                        await selectOption('#Country',        natVal); await sleep(100);
                        await selectOption('#CountryOfBirth', natVal); await sleep(100);
                    }}

                    // ── Fill everything EXCEPT names first ────────────────────────────
                    // (Nationality dropdowns can trigger React re-renders that wipe name
                    //  fields — so we fill names LAST after all framework updates settle.)
                    var gender = ('{esc(user.get("sex","X"))}').toUpperCase().includes('M') ? '1' : '2';
                    await selectOption('#Gender',       gender);                                   await sleep(50);
                    await selectOption('#SocialStatus', '{esc(user.get("marital_status"))}');      await sleep(50);
                    await fill('input#DateOfBirth', '{format_date(user.get("dob"))}');             await sleep(50);
                    await fill('#CityOfBirth',      '{esc(user.get("city_of_birth"))}');           await sleep(50);
                    await fill('#Profession',       '{esc(user.get("profession"))}');              await sleep(50);

                    if ({js_minor}) {{
                        var gSel = document.querySelector('#GuardianList');
                        var matchedSex = '';   
                        if (gSel && gSel.options.length > 1) {{
                            var adultsData  = {adults_data_json};   
                            var minorNat    = String('{esc(minor_nat)}');
                            var matched     = false;

                            // STRICT PASS: same-nationality adult ONLY
                            for (var i=1; i<gSel.options.length && !matched; i++) {{
                                var optText = gSel.options[i].text.toUpperCase();
                                for (var j=0; j<adultsData.length; j++) {{
                                    if (String(adultsData[j].nat) === minorNat &&
                                            optText.includes(adultsData[j].name.toUpperCase())) {{
                                        gSel.selectedIndex = i; matched = true;
                                        matchedSex = String(adultsData[j].sex || '').toUpperCase();
                                        break;
                                    }}
                                }}
                            }}
                            
                            // NEW FIX: If no exact match is found, FORCE reset to 0
                            // This stops the website from auto-selecting the wrong nationality!
                            if (!matched) {{
                                gSel.selectedIndex = 0;
                            }}
                            
                            gSel.dispatchEvent(new Event('change', {{bubbles:true}}));
                        }}
                        
                        await sleep(1000); 

                        var rSel = document.querySelector('#GuardianRelation');
                        if (rSel && matchedSex !== '') {{
                            var relVal = matchedSex.includes('M') ? '5' : '6';
                            if (rSel.querySelector('option[value="' + relVal + '"]')) {{
                                await selectOption('#GuardianRelation', relVal);
                            }} else if (rSel.querySelector('option[value="5"]')) {{
                                await selectOption('#GuardianRelation', '5');
                            }} else if (rSel.querySelector('option[value="6"]')) {{
                                await selectOption('#GuardianRelation', '6');
                            }}
                        }}
                        await sleep(200);
                    }}

                    await fill('#City',       '{esc(user.get("city"))}');            await sleep(50);
                    await fill('#PostalCode', '{esc(user.get("zip_postal_code"))}'); await sleep(50);
                    await fill('#Address',    '{esc(user.get("address"))}');         await sleep(50);

                    // ── FILL NAMES LAST — after all dropdowns/selects have settled ───
                    // All nationality/gender/marital dropdowns are done, React has had
                    // its re-renders; filling names now prevents any framework reset.
                    await sleep(150);
                    await fillCritical('#FirstNameEnglish', '{esc(user.get("given_names"))}');
                    await sleep(100);
                    await fillCritical('#LastNameEnglish',  '{esc(user.get("surname"))}');
                    await sleep(100);
                    {js_mid}

                    // FIX: for minors, refuse to submit if no real guardian ended up
                    // selected (index 0 is normally the placeholder/"-- select --"
                    // option). Submitting here previously caused the guardian field
                    // to go through empty/invalid and the server to reject the form.
                    // FIX: refuse to submit if no real guardian ended up selected
                    if ({js_minor}) {{
                        var gSelFinal = document.querySelector('#GuardianList');
                        if (!gSelFinal || gSelFinal.selectedIndex <= 0) {{
                            window.__guardianMissing = true;
                            return 'MINOR_NATIONALITY_MISMATCH';
                        }}
                    }}

                    await clickEl('#btnNext');
                }})();"""
                self.browser.page().runJavaScript(js, self._on_page1_fill_result)
                self.step_flags['PAGE1'] = True
                self._step_start_time = datetime.now()
                self.statusBar_.showMessage("Filling personal information…")

            # ── PAGE 2 ───────────────────────────────────────────────────────
            elif state == 'PAGE2':
                arr = datetime.today().strftime("%d/%m/%Y")
                dep = (datetime.today() + relativedelta(years=1, months=-1)).strftime("%d/%m/%Y")
                # Prefer the global mobile number entered at login (credentials),
                # fall back to this applicant's own contact_number if not set.
                # NOTE: the Visit Visa portal shows a fixed "+966" country-code
                # selector next to this field, so we must strip a leading 966
                # off the entered number before filling it — only the local
                # number (without country code) goes into the input itself.
                raw_mobile = (creds.get("mobile") or "").strip()
                if raw_mobile:
                    digits = re.sub(r"\D", "", raw_mobile)
                    mobile_val = digits[3:] if digits.startswith("966") else digits
                else:
                    mobile_val = user.get("contact_number")
                js = f"""(async function() {{ {JS_HELPERS}
                    await waitForEl('#PassportType');
                    await selectOption('#PassportType', '{esc(user.get("passport_type",1))}'); await sleep(50);
                    await fill('#PassportNumber',      '{esc(user.get("passport_number"))}');   await sleep(50);
                    await fill('#PassportIssuePlace',  '{esc(user.get("passport_issue_place"))}'); await sleep(50);
                    await fill('input#PassportIssueDate',  '{format_date(user.get("passport_issue_date"))}'); await sleep(50);
                    await fill('input#PassportExpiryDate', '{format_date(user.get("expiry"))}');              await sleep(50);
                    await fill('input#ExpectedDateOfEntry','{arr}');                              await sleep(50);
                    await fill('input#ExpectedDateOfLeave','{dep}');                              await sleep(50);
                    await clickEl('#AccomodationHotel');                                        await sleep(150);
                    await fill('#pac-input',           '{esc(user.get("hotel_name"))}');        await sleep(50);
                    await fill('#MobileOrPhoneNumber', '{esc(mobile_val)}');    await sleep(50);
                    await fill('#Email',               '{esc(user.get("email"))}');             await sleep(50);
                    var chk = document.querySelector('#chkSelectDeselectAll');
                    if (chk && !chk.checked) await clickEl('#chkSelectDeselectAll');
                    await sleep(100);
                    await clickEl('#btnNext');
                }})();"""
                self.browser.page().runJavaScript(js)
                self.step_flags['PAGE2'] = True
                self._step_start_time = datetime.now()
                self.statusBar_.showMessage("Filling passport information…")

            # ── PAGE2_ERROR ──────────────────────────────────────────────────
            elif state == 'PAGE2_ERROR':
                if not self.step_flags.get('PAGE2_ERROR'):
                    self.browser.page().runJavaScript(
                        """(function () {
    var parts = [];

    function addText(el, label) {
        if (!el) return;
        var txt = el.textContent.trim();
        if (txt.length === 0) return;
        if (window.getComputedStyle(el).display === 'none') return;
        parts.push(label ? label + ': ' + txt : txt);
    }

    // 1. Main failure banner
    addText(document.querySelector('#divFailureMsg'), '');

    // 2. Warning banner
    addText(document.querySelector('#divWarningMsg'), 'Warning');

    // 3. Six-month / date-range / purpose-of-visit danger callout sub-labels
    var sixDiv = document.querySelector('#divSixMonthMsg');
    if (sixDiv && window.getComputedStyle(sixDiv).display !== 'none') {
        ['#idMaxSix', '#idMaxTen', '#idPupseOfVisitMsg'].forEach(function (id) {
            var el = document.querySelector(id);
            if (el && window.getComputedStyle(el).display !== 'none')
                addText(el, '');
        });
    }

    // 4. Per-field inline validation errors
    document.querySelectorAll('span.field-validation-error').forEach(function (el) {
        var txt = el.textContent.trim();
        if (txt.length > 0 && window.getComputedStyle(el).display !== 'none') {
            // Try to find the associated label
            var container = el.closest('[data-component]') || el.closest('.input') || el.parentElement;
            var lbl = container ? container.querySelector('label') : null;
            var lblText = lbl ? lbl.textContent.trim().replace(/\\*$/, '').trim() : '';
            parts.push(lblText ? lblText + ': ' + txt : txt);
        }
    });

    // 5. Standalone named error spans
    [
        { id: '#spnSelectedPurposeOfVisitMsg', lbl: 'Purpose of Visit' },
        { id: '#spnMobileOrPhoneNumber',       lbl: 'Contact Number'   },
        { id: '#errMsgWhatsappMobileCountryCode', lbl: 'WhatsApp Country Code' }
    ].forEach(function (item) {
        var el = document.querySelector(item.id);
        if (el && window.getComputedStyle(el).display !== 'none' &&
                el.textContent.trim().length > 0) {
            parts.push(item.lbl + ': ' + el.textContent.trim());
        }
    });

    // Deduplicate and join
    var seen = {};
    var unique = parts.filter(function (p) {
        p = p.trim();
        if (!p || seen[p]) return false;
        seen[p] = true;
        return true;
    });

    return unique.length > 0 ? unique.join(' | ') : 'Unknown error on Passport page';
})();""",
                        self._on_page2_error
                    )
                    self.step_flags['PAGE2_ERROR'] = True
                    return   # _on_page2_error releases is_processing_step

            # ── SITE_BLOCKING_ERROR ─────────────────────────────────────────
            elif state == 'SITE_BLOCKING_ERROR':
                if not self.step_flags.get('SITE_BLOCKING_ERROR'):
                    self.browser.page().runJavaScript(
                        "window.__siteBlockingErrorText || 'Unknown blocking error';",
                        self._on_page2_error
                    )
                    self.step_flags['SITE_BLOCKING_ERROR'] = True
                    return   # _on_page2_error releases is_processing_step

            # ── INSURANCE ────────────────────────────────────────────────────
            elif state == 'INSURANCE':
                self.browser.page().runJavaScript(
                    f"(async function(){{ {JS_HELPERS} "
                    f"  await waitForEl('#chkInsurance');"
                    f"  var ins = document.querySelector('#chkInsurance');"
                    f"  if (ins && !ins.checked) await clickEl('#chkInsurance');"
                     f"  await sleep(100); await clickEl('#btnNext');"
                    f"}})();"
                )
                self.step_flags['INSURANCE'] = True
                self._step_start_time = datetime.now()
                self.statusBar_.showMessage("Selecting medical insurance…")

            # ── TERMS ────────────────────────────────────────────────────────
            elif state == 'TERMS':
                self.browser.page().runJavaScript(
                    f"(async function(){{ {JS_HELPERS} "
                    f"  var chks = document.querySelectorAll('input[type=\"checkbox\"]');"
                    f"  for(var i=0; i<chks.length; i++){{"
                    f"      if(!chks[i].checked) {{"
                    f"          chks[i].click();"
                     f"          await sleep(100);"
                    f"      }}"
                    f"  }}"
                    f"  var nxt = document.querySelector('#btnNext');"
                    f"  if(nxt) await clickEl('#btnNext');"
                    f"}})();"
                )
                self.step_flags['TERMS'] = True
                self._step_start_time = datetime.now()
                self.statusBar_.showMessage("Accepting Terms and Conditions…")

            # ── REVIEW ───────────────────────────────────────────────────────
            elif state == 'REVIEW':
                queue_length = len(self.queue)
                self.browser.page().runJavaScript(
                    f"(async function(){{ {JS_HELPERS} "
                    f"  await waitForEl('#chkSelectDeselectAll');"
                    f"  var sa = document.querySelector('#chkSelectDeselectAll');"
                    f"  if (sa && !sa.checked) await clickEl('#chkSelectDeselectAll');"
                     f"  await sleep(100);"
                    f"  var pay = document.querySelector('#chkPay');"
                    f"  if (pay && !pay.checked) await clickEl('#chkPay');"
                     f"  await sleep(100);"
                    f"  var rowCheckboxes = document.querySelectorAll("
                    f"      'input[type=\"checkbox\"]:not(#chkSelectDeselectAll):not(#chkPay)');"
                    f"  var diag = {{"
                    f"      row_checkbox_count: rowCheckboxes.length,"
                    f"      page_url: window.location.href,"
                    f"      page_title: document.title,"
                    f"      button_clicked: null"
                    f"  }};"
                    f"  var qLen = {queue_length};"
                    f"  var nxt = document.querySelector('#btnNext');"
                    f"  var sub = document.querySelector('#btnSubmit');"
                    f"  var addMore = document.querySelector('#btnAddMoreToGroup');"
                    f"  "
                    f"  if (qLen > 0 && sub && !nxt) {{"
                    f"      /* Prevent closing the group if we still have passports in the queue! */"
                    f"      if (addMore) {{"
                    f"          diag.button_clicked = 'btnAddMoreToGroup';"
                    f"          await clickEl('#btnAddMoreToGroup');"
                    f"      }} else {{"
                    f"          diag.button_clicked = 'BLOCKED_SUBMIT_TO_PREVENT_SPLIT';"
                    f"      }}"
                    f"  }} else if (nxt) {{"
                    f"      diag.button_clicked = nxt.id; await clickEl('#'+nxt.id);"
                    f"  }} else if (sub) {{"
                    f"      diag.button_clicked = sub.id; await clickEl('#'+sub.id);"
                    f"  }}"
                    f"  return diag;"
                    f"}})();",
                    self._on_review_diag
                )
                self.step_flags['REVIEW'] = True
                self._step_start_time = datetime.now()
                self.statusBar_.showMessage("Reviewing and submitting…")

            # ── DONE ─────────────────────────────────────────────────────────
            elif state == 'DONE':
                pp_num = user.get('passport_number', '')

                self._diag(
                    f"DONE state detected for passport={pp_num} "
                    f"(this applicant's step_flags before this tick: "
                    f"DONE_was_already_True={self.step_flags.get('DONE')}) "
                    f"browser_url={self.browser.url().toString()} "
                    f"processed_count_before={self._processed_count} "
                    f"— checking for silent network failures before trusting it…"
                )
                self.step_flags['DONE'] = True

                # Read + clear this applicant's network-failure log in one
                # synchronous JS call (NOT a Promise, so the callback actually
                # receives the real value — unlike the earlier async diag calls).
                self.browser.page().runJavaScript(
                    "(function(){"
                    "  var l = window.__visaNetLog || [];"
                    "  var copy = l.slice();"
                    "  window.__visaNetLog = [];"
                    "  return copy;"
                    "})();",
                    self._on_done_network_check
                )
                return   # _on_done_network_check releases is_processing_step

            self.is_processing_step = False

        except Exception as ex:
            self.statusBar_.showMessage(f"❌ Error in state handler: {ex}")
            self.retry_count += 1
            if self.retry_count >= MAX_RETRIES:
                self.current_applicant = None
                self.retry_count = 0
            self.is_processing_step = False

    # ─── REVIEW diagnostic callback ───────────────────────────────────────────

    def _on_review_diag(self, result):
        """
        Logs the applicant-row count and which button fired on the REVIEW
        page for every applicant. If 'button_clicked' is btnSubmit (not
        btnNext) partway through the batch, that submit is very likely
        FINALIZING/paying for the group at that point — explaining why
        later 'Add More' entries end up in a second, separate group that
        the open entry list doesn't show.
        """
        try:
            pp = (self.current_applicant['applicant_data'].get('passport_number', '—')
                  if self.current_applicant else '—')
            if not isinstance(result, dict):
                self._diag(f"REVIEW diag (passport={pp}): unexpected result={result!r}")
                return
            self._diag(
                f"REVIEW (passport={pp}) processed_count_before_this={self._processed_count} "
                f"row_checkbox_count={result.get('row_checkbox_count')} "
                f"button_clicked={result.get('button_clicked')!r} "
                f"page_title={result.get('page_title')!r} "
                f"page_url={result.get('page_url')!r}"
            )
            if result.get('button_clicked') == 'btnSubmit':
                self._diag(
                    "WARNING: 'btnSubmit' fired on REVIEW for an applicant "
                    "that is not necessarily the last one in the batch — if "
                    "this button finalizes/pays for the group, everything "
                    "after this point may be creating a SEPARATE group."
                )
        except Exception as ex:
            self._diag(f"REVIEW diag error: {ex}")

    # ─── GROUP_CREATE diagnostic callback ─────────────────────────────────────

    def _on_group_create_diag(self, result):
        """
        Logs what actually happened when trying to create/join the group, and
        — critically — RECOVERS when the "already exists" click didn't fire.

        GROUP_CREATE's step_flag is set unconditionally right after the JS is
        fired (see caller), so if the click never happened, the state machine
        would otherwise just see the flag is already True on every subsequent
        poll and go silent forever (this state doesn't re-run on its own).
        We now clear the flag here whenever nothing was clicked, so the next
        poll re-fires the GROUP_CREATE JS and retries the click. If it keeps
        failing, GROUP_CREATE is in LAG_ACTIVE_STATES, so the normal stuck-page
        timeout/reload dialog will eventually catch and recover it too.
        """
        try:
            pp = (self.current_applicant['applicant_data'].get('passport_number', '—')
                  if self.current_applicant else '—')
            if not isinstance(result, dict):
                self._diag(f"GROUP_CREATE diag (passport={pp}): unexpected result={result!r}")
                return
            already_exists = result.get('already_exists')
            matched_label  = result.get('matched_label')
            clicked        = result.get('clicked')

            self._diag(
                f"GROUP_CREATE (passport={pp}) target_group="
                f"{result.get('target_group')!r} already_exists="
                f"{already_exists} matched_label="
                f"{matched_label} clicked={clicked} "
                f"all_group_labels_on_page={result.get('all_group_labels')}"
            )

            if already_exists and not matched_label:
                self._diag(
                    f"WARNING: group '{result.get('target_group')}' reported as "
                    f"already existing but NO matching label was found on the "
                    f"page — this likely means a NEW second group was created "
                    f"instead of joining the existing one (split-group risk)."
                )

            # FIX: if the group already existed but we never actually clicked
            # into it, retry instead of hanging silently.
            if already_exists and not clicked:
                self.step_flags['GROUP_CREATE'] = False
                self.statusBar_.showMessage(
                    f"⚠️  Group '{result.get('target_group')}' exists but "
                    f"'Add New Application' wasn't found yet — retrying…"
                )
        except Exception as ex:
            self._diag(f"GROUP_CREATE diag error: {ex}")

    # ─── DONE network-failure check ───────────────────────────────────────────

    def _on_done_network_check(self, failures):
        """
        Called right after detecting a DONE page.
        """
        try:
            if not self.current_applicant:
                self.is_processing_step = False
                return

            user   = self.current_applicant['applicant_data']
            name   = user.get('given_names', '—')
            pp_num = user.get('passport_number', '')
            app_id = user.get('id')

            if failures:
                detail = "; ".join(
                    f"{f.get('type','?')} {f.get('url','?')} "
                    f"status={f.get('status', f.get('error','?'))}"
                    for f in failures[:5]
                )
                self._diag(
                    f"WARNING for passport={pp_num} name={name!r}: "
                    f"Background network errors detected, but trusting the DONE state. "
                    f"Details: {detail}"
                )

            # ── Mark as Successful ──
            self._diag(f"DONE confirmed clean for passport={pp_num} name={name!r}.")
            self._remove_processed_applicant()
            self._processed_count += 1        # ← success counter

            # --- UPDATE THE SIDEBAR WITH A GREEN TICK ---
            for i in range(self.checklist.count()):
                item = self.checklist.item(i)
                if item.data(Qt.ItemDataRole.UserRole) == pp_num:
                    item.setData(Qt.ItemDataRole.UserRole + 2, '✅')
                    self.checklist.update(self.checklist.indexFromItem(item))
                    break

            # --- TELL FLASK BACKEND IT IS DONE (also advances the send queue) ---
            if app_id:
                try:
                    r = requests.post(
                        "http://127.0.0.1:9000/api/mark_processed_single",
                        json={"id": app_id, "platform": "visa", "token": _get_local_token(), "success": True},
                        timeout=3,
                    )
                    if not r.ok or not r.json().get("success"):
                        self._diag(f"DB update failed for id={app_id}: HTTP {r.status_code} — {r.text}")
                except Exception as e:
                    self._diag(f"Failed to update web DB: {e}")

            self.current_applicant = None
            self.lbl_passport.setText("Passport: — idle —")
            self._stuck_state     = None
            self._stuck_since     = None
            self._step_start_time = None
            self.retry_count  = 0
            self._update_progress()
            
            # --- Click 'Add More' with a short delay ---
            self.browser.page().runJavaScript(
                f"(async function(){{ {JS_HELPERS} "
                f"await sleep(500); "
                f"await clickEl('#btnAddMoreToGroup'); "
                f"return 'clicked'; }})();",
                lambda r, pp=pp_num: self._diag(
                    f"DONE (passport={pp}) clicked #btnAddMoreToGroup result={r!r}"
                )
            )
            
            # FIX: HARD PYTHON LOCK.
            # We pause Python from running ANY automation or popping the next applicant.
            # 2 seconds is the minimum safe value: the JS Add More click fires at ~500ms,
            # giving the server 1.5 s to start navigating away before Python polls again.
            self._pause_until = datetime.now() + timedelta(seconds=2)
            
            self.statusBar_.showMessage(f"✅ {name} completed successfully!")
            self.is_processing_step = False

        except Exception as ex:
            self._diag(f"_on_done_network_check error: {ex}")
            self.statusBar_.showMessage(f"❌ Error finishing applicant: {ex}")
            self.is_processing_step = False

    # ─── PAGE1 fill result callback ────────────────────────────────────────────

    def _on_page1_fill_result(self, result):
        """
        Guards against a minor's PAGE1 being submitted without a real guardian.
        If strict nationality matching fails, immediately triggers the skip sequence.
        """
        if result not in ('MINOR_NATIONALITY_MISMATCH', 'GUARDIAN_NOT_SELECTED'):
            return

        # Route directly into the existing error/skip handler.
        # This will show the 15-second skip dialog, mark the applicant with ❌,
        # remove them from the JSON queue, and reload the browser for the next entry.
        self._on_page2_error(
            "Skipped: No adult guardian of the exact same nationality was found in the group."
        )

    # ─── PAGE2 error callback ─────────────────────────────────────────────────

    # ─── PAGE2 error callback ─────────────────────────────────────────────────

    def _on_page2_error(self, error_text: str, show_dialog: bool = True):
        """
        Called with #divFailureMsg text or manual skip.
        Records the skip, removes from JSON, navigates to Index for next.
        """
        pp     = (self.current_applicant['applicant_data'].get('passport_number', '—')
                  if self.current_applicant else '—')
        app_id = (self.current_applicant['applicant_data'].get('id')
                  if self.current_applicant else None)

        # FIX: this applicant never actually completed submission, so they do
        # NOT exist as a real option in the server-side group.
        if self.current_applicant:
            skipped_name = (
                f"{self.current_applicant['applicant_data'].get('given_names','')} "
                f"{self.current_applicant['applicant_data'].get('surname','')}".strip()
            )
            if skipped_name in self.adult_names_in_group:
                self.adult_names_in_group.remove(skipped_name)
            self.adult_data_in_group = [
                a for a in self.adult_data_in_group if a.get('name') != skipped_name
            ]

        # Mark as skipped in sidebar before dialog closes
        for _ci in range(self.checklist.count()):
            _ci_item = self.checklist.item(_ci)
            if _ci_item.data(Qt.ItemDataRole.UserRole) == pp:
                _ci_item.setData(Qt.ItemDataRole.UserRole + 2, '❌')
                self.checklist.update(self.checklist.indexFromItem(_ci_item))
                break

        # --- NEW: Only show the 15-second timer if it's an automated server error ---
        if show_dialog:
            Page2ErrorDialog(self, error_text or 'Unknown server error', pp).exec()

       

        # --- TELL FLASK BACKEND THIS ONE WAS SKIPPED (advances the send queue) ---
        if app_id:
            try:
                r = requests.post(
                    "http://127.0.0.1:9000/api/mark_processed_single",
                    json={"id": app_id, "platform": "visa", "token": _get_local_token(), "success": False},
                    timeout=3,
                )
                if not r.ok or not r.json().get("success"):
                    self._diag(f"DB update failed for skipped id={app_id}: HTTP {r.status_code} — {r.text}")
            except Exception as e:
                self._diag(f"Failed to notify web DB of skip: {e}")

        # Remove from JSON (permanent server-side failure)
        self._remove_processed_applicant()

        self.current_applicant = None
        self.step_flags       = self._fresh_flags()
        self.retry_count      = 0
        self._stuck_state     = None
        self._stuck_since     = None
        self._step_start_time = None
        
        # FIX 1: Lock the queue so we don't accidentally skip the NEXT applicant 
        # while the browser is redirecting to the index URL.
        self._pause_until     = datetime.now() + timedelta(seconds=5)
        
        self.lbl_passport.setText("Passport: — idle —")
        self._update_progress()
        self.browser.setUrl(QUrl(VISA_INDEX_URL))
        self.statusBar_.showMessage(f"⏭  Skipped — passport: {pp}.")
        self.is_processing_step = False

    # ─── Lag / network dialog ─────────────────────────────────────────────────

    # ─── Lag / network dialog ─────────────────────────────────────────────────

    def _show_lag_dialog(self, stuck_state: str, slow: bool = False):
        if slow:
            reason = (
                f'The <b>{stuck_state}</b> step has been running for over '
                f'<b>{PAGE_SLOW_TIMEOUT} seconds</b>.<br>'
                f'The network appears to be very slow or the autofill is '
                f'taking unusually long to complete.'
            )
        else:
            reason = STUCK_REASONS.get(
                stuck_state,
                f'The page <b>{stuck_state}</b> has not responded for over '
                f'{STUCK_TIMEOUT} seconds after the form was submitted.'
            )

        pp = (self.current_applicant['applicant_data'].get('passport_number', '—')
              if self.current_applicant else '—')

        # Run dialog and capture the result
        result = LagDialog(self, stuck_state, reason, pp).exec()

        # --- NEW: Handle the Skip Button ---
        # 0 means self.reject() was called (Skip Button)
        if result == 0:
            self._on_page2_error(
                f"Manually skipped during {stuck_state} due to network/lag timeout.", 
                show_dialog=False
            )
            return
        # -----------------------------------

        # User clicked reload (returns 1)
        self.retry_count += 1
        
        
        if self.retry_count >= MAX_RETRIES:
            # Do NOT skip the applicant — pause the queue so the operator
            # can investigate and resume manually when connectivity improves.
            self.is_running = False
            self.timer.stop()
            self.btn_start.setText("▶ Resume")
            self.btn_start.setStyleSheet(
                "background-color:#27ae60;padding:8px 15px;"
                "border-radius:4px;font-weight:bold;"
            )
            self.lbl_status.setText("Status: Paused — network issue, resume when ready.")
            self.statusBar_.showMessage(
                f"⏸  paused after {MAX_RETRIES} reload attempts "
                f"for passport {pp}. Fix network then click Resume."
            )
            # Reset retry counter so resuming starts fresh reload attempts
            self.retry_count      = 0
            self._step_start_time = None
            self._stuck_state     = None
            self._stuck_since     = None
            self._pause_until     = None
            # Keep current_applicant intact so it retries on resume
        else:
            # Clear all flags so the applicant is refilled from scratch
            self.step_flags       = self._fresh_flags()
            self._stuck_state     = None
            self._stuck_since     = None
            self._step_start_time = None
            self._pause_until     = None
            self.browser.reload()
            self.statusBar_.showMessage(
                f"🔄  Reloading for passport {pp} "
                f"(attempt {self.retry_count}/{MAX_RETRIES})…"
            )

        self.is_processing_step = False


# ─── 3. START EVENT LOOP ─────────────────────────────────────────────────────
if __name__ == "__main__":
    app.setStyleSheet(
        "QMainWindow{background-color:#2c3e50;}"
        "QLabel{color:white;}"
        "QDialog{background-color:#ecf0f1;}"
        "QDialog QLabel{color:#2c3e50;}"
    )

    # Initialize heavy main window
    window = LocalVisaAutomator()
    window.showMaximized()

    # Close the PyInstaller native C-level splash screen
    try:
        import pyi_splash
        pyi_splash.close()
    except ImportError:
        pass

    sys.exit(app.exec())