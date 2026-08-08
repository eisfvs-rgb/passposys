"""
encrypt_env.py
==============
Run this ONCE before building the exe:
    python encrypt_env.py

It reads passposys.key for the encryption key, then reads your .env
file, encrypts it, and saves env.enc.
env.enc is bundled inside the exe via build.bat.
passposys.key is NEVER bundled — keep it external and secure.

To generate passposys.key (first time only):
    python encrypt_env.py --genkey
"""

import os, base64, hashlib, sys
from cryptography.fernet import Fernet

KEY_FILE = 'passposys.key'

def load_or_genkey():
    if '--genkey' in sys.argv:
        import secrets
        key = secrets.token_urlsafe(32)
        with open(KEY_FILE, 'w') as f:
            f.write(key)
        print(f"✅ {KEY_FILE} generated successfully.")
        print("   Keep this file SECRET — never bundle it inside the exe.")
        sys.exit(0)

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

    return raw


def get_fernet():
    raw = load_or_genkey()
    _key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    return Fernet(_key)


def encrypt_env():
    fernet = get_fernet()

    if not os.path.exists('.env'):
        print("❌ .env not found in current folder.")
        sys.exit(1)

    with open('.env', 'rb') as f:
        env_data = f.read()

    encrypted = fernet.encrypt(env_data)

    with open('env.enc', 'wb') as f:
        f.write(encrypted)

    print("✅ env.enc created successfully.")
    print("   You can now delete .env from this folder.")
    print("   Make sure env.enc is added to build.bat --add-data.")


if __name__ == '__main__':
    encrypt_env()
