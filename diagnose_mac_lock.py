"""
diagnose_mac_lock.py

Run this on the SAME machine as the desktop client / Flask app, to see
exactly what secrets.php is receiving and returning — instead of relying
on secrets_client.py's simplified 401/403/other handling, which silently
swallows the response body on a 400.

Usage:
    python diagnose_mac_lock.py <install_token> [host_api_base]

Example:
    python diagnose_mac_lock.py abc123... https://pms.passposys.com/api
"""

import sys
import json
import requests

from mac_lock import get_local_mac


def main():
    if len(sys.argv) < 2:
        print("Usage: python diagnose_mac_lock.py <install_token> [host_api_base]")
        sys.exit(1)

    token = sys.argv[1]
    base = sys.argv[2] if len(sys.argv) > 2 else "https://pms.passposys.com/api"
    root = base.rsplit("/api", 1)[0] if base.rstrip("/").endswith("/api") else base
    url = f"{root.rstrip('/')}/api/config/secrets.php"

    mac = get_local_mac()
    print(f"Resolved MAC: {mac!r}")
    if mac is None:
        print("!! mac_lock could not determine a MAC on this machine.")
        print("!! secrets.php will return 400 mac_missing in this case.")

    params = {"mac_address": mac} if mac else {}
    print(f"Request URL:    {url}")
    print(f"Request params: {params}")

    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=10,
    )

    print(f"\nHTTP status: {resp.status_code}")
    print("Raw response body:")
    print(resp.text)

    try:
        data = resp.json()
        print("\nParsed JSON:")
        print(json.dumps(data, indent=2))
    except ValueError:
        print("\n(Response was not valid JSON)")


if __name__ == "__main__":
    main()
