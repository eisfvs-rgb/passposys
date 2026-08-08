import os
import sys
import base64
import hashlib
from cryptography.fernet import Fernet
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import logging as _logging

def _exe_dir():
    """Returns the folder where PassPoSys.exe (or app.py) lives."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# Required permission scope
SCOPES = ['https://www.googleapis.com/auth/drive.file']

# The ID of the folder in your personal Drive
CLOUDSTORE_FOLDER_ID = '1RCFnZzs4BuOhfbojjUf0v2EfzCQ6gOFQ'

# ── ENCRYPTION KEY ──────────────────────────────────────────────────────────
# Never hardcoded. launch.py loads it from the external passposys.key file
# and sets _PASSPOSYS_ENC_KEY in the environment before any module is imported.
def _get_fernet():
    raw_key = os.environ.get('_PASSPOSYS_ENC_KEY', '').encode('utf-8')
    if not raw_key:
        raise RuntimeError(
            "_PASSPOSYS_ENC_KEY not set. "
            "Ensure launch.py loaded passposys.key before importing cloudstore_backup."
        )
    _key = base64.urlsafe_b64encode(hashlib.sha256(raw_key).digest())
    return Fernet(_key)

_fernet = _get_fernet()


def _bundled_path(filename):
    """Returns path to a file bundled inside the exe (_MEIPASS)."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, filename)


def _decrypt_token(enc_path):
    """Decrypts token.enc and returns the JSON string in memory."""
    with open(enc_path, 'rb') as f:
        encrypted = f.read()
    return _fernet.decrypt(encrypted).decode('utf-8')


def _encrypt_and_save_token(token_json_str, enc_path):
    """Encrypts a token JSON string and saves it to enc_path."""
    encrypted = _fernet.encrypt(token_json_str.encode('utf-8'))
    with open(enc_path, 'wb') as f:
        f.write(encrypted)


def get_oauth_credentials():
    """
    Load credentials from encrypted token.enc.
    Priority:
      1. token.enc next to exe (runtime, updated after refresh)
      2. token.enc bundled inside exe (initial/factory copy)
    After any refresh, saves updated token back as encrypted token.enc
    next to the exe — never as plain text.
    """
    creds = None

    runtime_enc  = os.path.join(_exe_dir(), 'token.enc')
    bundled_enc  = _bundled_path('token.enc')

    # Pick whichever token.enc exists (runtime copy takes priority)
    enc_path = runtime_enc if os.path.exists(runtime_enc) else (
        bundled_enc if os.path.exists(bundled_enc) else None
    )

    if enc_path:
        try:
            token_json = _decrypt_token(enc_path)
            # Load credentials from the decrypted JSON string (in memory only)
            import tempfile, json
            creds = Credentials.from_authorized_user_info(
                json.loads(token_json), SCOPES
            )
        except Exception as e:
            print(f"[CloudStore] ⚠️  Could not decrypt token.enc: {e}")
            return None
    else:
        print("[CloudStore] ❌ No token.enc found. Cannot authenticate.")
        return None

    # Refresh if expired, then re-encrypt and save next to exe
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Save refreshed token as encrypted file next to exe (never plain text)
            _encrypt_and_save_token(creds.to_json(), runtime_enc)
            print("[CloudStore] 🔄 Token refreshed and re-encrypted successfully.")
        except Exception as e:
            print(f"[CloudStore] ⚠️  Token refresh failed: {e}")
            return None

    return creds


def upload_zip_to_cloudstore(zip_filepath):
    """Uploads a local zip file to CloudStore using encrypted OAuth credentials."""
    try:
        creds = get_oauth_credentials()
        if not creds:
            print("[CloudStore] ❌ No credentials found.")
            return False

        service = build('drive', 'v3', credentials=creds)

        filename = os.path.basename(zip_filepath)
        file_metadata = {
            'name': filename,
            'parents': [CLOUDSTORE_FOLDER_ID]
        }

        media = MediaFileUpload(zip_filepath, mimetype='application/zip', resumable=True)

        print(f"[CloudStore] Uploading {filename} to CloudStore...")

        uploaded_file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()

        print(f"[CloudStore] ✅ Upload successful! File ID: {uploaded_file.get('id')}")

        # Keep only the latest 2 backups in the Drive folder
        _purge_old_cloudstore_backups(service, CLOUDSTORE_FOLDER_ID, keep=2)

        return True

    except Exception as e:
        print(f"[CloudStore] ❌ Failed to upload to CloudStore: {str(e)}")
        return False


def _purge_old_cloudstore_backups(service, folder_id, keep=2):
    """
    Lists all .zip files in the given Drive folder, sorted newest-first
    (by createdTime), and permanently deletes any beyond the `keep` count.
    """
    try:
        query = (
            f"'{folder_id}' in parents"
            " and mimeType = 'application/zip'"
            " and trashed = false"
        )
        results = service.files().list(
            q=query,
            fields="files(id, name, createdTime)",
            orderBy="createdTime desc",
            pageSize=100,
        ).execute()

        files = results.get("files", [])
        print(f"[CloudStore] Found {len(files)} backup(s) in Drive folder.")

        for old_file in files[keep:]:
            try:
                service.files().delete(fileId=old_file["id"]).execute()
                print(f"[CloudStore] 🗑️  Deleted old backup: {old_file['name']} (id={old_file['id']})")
            except Exception as del_err:
                print(f"[CloudStore] ⚠️  Could not delete {old_file['name']}: {del_err}")

    except Exception as e:
        print(f"[CloudStore] ⚠️  Could not purge old backups: {e}")
