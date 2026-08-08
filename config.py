import os
import sys
import tempfile
import atexit
import base64
import hashlib
from cryptography.fernet import Fernet

# 1. Define base directory FIRST
#    - When running as a PyInstaller --onefile exe, sys.executable points to the
#      exe itself; its parent folder is the persistent app directory.
#    - When running as a normal .py script, use this file's directory.
#    NOTE: do NOT use sys._MEIPASS here — that's the temporary extraction
#    folder created fresh on every launch and wiped afterwards.
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 2. Bind the JSON file to this absolute path
#    Bundled read-only resources (credentials, fonts, etc.) DO live inside the
#    PyInstaller temp bundle, so this one still uses _MEIPASS.
def _resource_path(rel):
    import sys
    base = getattr(sys, '_MEIPASS', BASE_DIR)
    return os.path.join(base, rel)


# =====================================================================
# 3. CLOUDSTORE Vision credentials — REMOVED from the local app.
#    ProvB/ProvA Vision OCR now runs entirely on the VPS
#    (see ocr_scan_service.py). The local app no longer decrypts or
#    holds cloudstore.enc / any Vision service-account JSON at all — it only
#    crops the MRZ strip and sends it to the VPS via ocr_client.py
#    (OCR_API_BASE / OCR_API_SECRET, configured below).
#    CLOUDSTORE_APPLICATION_CREDENTIALS is intentionally not set here.
# =====================================================================
CLOUDSTORE_APPLICATION_CREDENTIALS = None


# =====================================================================
# 4. Other Environment Variables
# =====================================================================
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
FACE_FOLDER = os.path.join(BASE_DIR, 'faces')
# Where mofa.py / mofa_downloader.py save downloaded visa PDFs:
#   visa_processed/<GROUP_NAME>/<passport_number>_visa.pdf
# Kept as an absolute path (anchored to BASE_DIR) so it resolves the same
# way regardless of the process's current working directory -- mirrors
# UPLOAD_FOLDER/FACE_FOLDER above. mofa.py/mofa_downloader.py are standalone
# scripts using a bare "visa_processed" relative path; run them from
# BASE_DIR (the app's own folder) so both sides agree on the same location.
VISA_PROCESSED_FOLDER = os.path.join(BASE_DIR, 'visa_processed')


# =====================================================================
# 5. Directories and Database 
# =====================================================================
def setup_directories():
    """Create directories with proper error handling"""
    try:
        # Create uploads directory
        if not os.path.exists(UPLOAD_FOLDER):
            os.makedirs(UPLOAD_FOLDER, exist_ok=True)
            print(f"✅ Created uploads directory: {UPLOAD_FOLDER}")
        else:
            print(f"✅ Uploads directory already exists: {UPLOAD_FOLDER}")
            
        # Create faces directory  
        if not os.path.exists(FACE_FOLDER):
            os.makedirs(FACE_FOLDER, exist_ok=True)
            print(f"✅ Created faces directory: {FACE_FOLDER}")
        else:
            print(f"✅ Faces directory already exists: {FACE_FOLDER}")

        # Create visa_processed directory. mofa_downloader.py also creates
        # this lazily (per-group, on first visa PDF download), so this
        # isn't strictly required for correctness -- it's here so the
        # folder is visible next to uploads/faces from first launch
        # instead of silently appearing later and looking "missing".
        if not os.path.exists(VISA_PROCESSED_FOLDER):
            os.makedirs(VISA_PROCESSED_FOLDER, exist_ok=True)
            print(f"✅ Created visa_processed directory: {VISA_PROCESSED_FOLDER}")
        else:
            print(f"✅ visa_processed directory already exists: {VISA_PROCESSED_FOLDER}")

        # Test if we can write to the directory
        test_file = os.path.join(UPLOAD_FOLDER, '.test_write')
        try:
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
            print("✅ Directory write permissions OK")
        except Exception as e:
            print(f"⚠️  Warning: Cannot write to directory: {e}")
            print("   You may need to run with sudo or fix permissions manually")
            
    except Exception as e:
        print(f"❌ Error creating directories: {e}")
        print("   Please create directories manually:")
        print(f"   mkdir -p {UPLOAD_FOLDER}")
        print(f"   mkdir -p {FACE_FOLDER}")
        print(f"   mkdir -p {VISA_PROCESSED_FOLDER}")
        sys.exit(1)

# Run setup when config is imported
setup_directories()

# MySQL
DB_CONFIG = {
    'host': '127.0.0.1',
    'port': 3307,                          # ← portable MySQL port
    'database': 'passport_db',
    'user': 'passport_user',
    'password': os.environ.get('DB_PASSWORD', 'passposys_local')
}

# Flask
SECRET_KEY = os.environ.get('FLASK_SECRET_KEY')

# =====================================================================
# 6. Host API
# =====================================================================
HOST_API_BASE   = os.environ.get("HOST_API_BASE", "https://pms.passposys.com/api")
HOST_API_SECRET = os.environ.get("HOST_API_SECRET", "")
if not HOST_API_SECRET:
    raise RuntimeError(
        "HOST_API_SECRET is not set in env.enc. "
        "Copy the value from your host's config.php and add it before distributing the app."
    )

# =====================================================================
# 7. OCR Scan Service (VPS)
# =====================================================================
# The local app no longer calls ProvA/ProvB Vision directly — it crops
# the MRZ strip locally and sends it to ocr_scan_service.py running on
# the VPS, which holds the actual Vision credentials and does the OCR.
OCR_API_BASE   = os.environ.get("OCR_API_BASE", "https://pms.passposys.com/ocr")
OCR_API_SECRET = os.environ.get("OCR_API_SECRET", "")
if not OCR_API_SECRET:
    raise RuntimeError(
        "OCR_API_SECRET is not set in env.enc. "
        "Copy the value from ocr_scan_service.py's OCR_SERVICE_SECRET on the VPS "
        "and add it here before distributing the app."
    )