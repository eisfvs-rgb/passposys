"""
encrypt_token.py
================
Run this ONCE before building the exe:
    python encrypt_token.py

It reads passposys.key for the encryption key, then reads token.json,
encrypts it, and saves token.enc.
token.enc is bundled inside the exe via build.bat.
token.json can then be deleted from the folder.
"""

import os, base64, hashlib, sys
from cryptography.fernet import Fernet

KEY_FILE = 'passposys.key'

def get_fernet():
    if not os.path.exists(KEY_FILE):
        print(f"❌ {KEY_FILE} not found.")
        print("   Generate it first by running:")
        print("     python encrypt_env.py --genkey")
        sys.exit(1)

    with open(KEY_FILE, 'rb') as f:
        raw = f.read().strip()

    if len(raw) < 16:
        print(f"❌ {KEY_FILE} is too short or corrupted.")
        sys.exit(1)

    _key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    return Fernet(_key)


def encrypt_token():
    fernet = get_fernet()

    if not os.path.exists('token.json'):
        print("❌ token.json not found in current folder.")
        sys.exit(1)

    with open('token.json', 'rb') as f:
        token_data = f.read()

    encrypted = fernet.encrypt(token_data)

    with open('token.enc', 'wb') as f:
        f.write(encrypted)

    print("✅ token.enc created successfully.")
    print("   You can now delete token.json from this folder.")
    print("   Add token.enc to your build.bat --add-data line.")

if __name__ == '__main__':
    encrypt_token()
