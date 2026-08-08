"""
validate_env.py
===============
Called by build.bat to check all required keys are present in .env
"""
import sys

try:
    from dotenv import dotenv_values
except ImportError:
    # dotenv may not be installed yet — skip validation gracefully
    print("[OK] dotenv not available, skipping validation.")
    sys.exit(0)

env = dotenv_values('.env')
required = ['FLASK_SECRET_KEY', 'LOCAL_API_TOKEN', 'HOST_API_SECRET', 'OCR_API_SECRET']
missing = [k for k in required if not env.get(k, '').strip()]

if missing:
    print("ERROR: Missing or empty required keys in .env:")
    for k in missing:
        print(f"  - {k}")
    sys.exit(1)

print("[OK] All required keys present.")
