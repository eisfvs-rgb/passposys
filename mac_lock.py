"""
mac_lock.py

Cross-platform helper to get a stable MAC address identifying this machine.
Used by the PyQt desktop client to send "mac_address" alongside install_token
on every login and every scan request, so the server can wire/enforce the
one-time MAC lock in secrets.php.

Usage:
    from mac_lock import get_local_mac
    mac = get_local_mac()   # "AA:BB:CC:DD:EE:FF" or None on failure
"""

import uuid

_cached_mac = None
_cache_populated = False


def get_local_mac() -> str | None:
    """
    Return this machine's primary MAC address as "AA:BB:CC:DD:EE:FF",
    or None if it cannot be determined. Result is memoized in-process —
    the MAC address of a running machine cannot change, and this will be
    called on every request to the host, so we resolve it once.

    Uses uuid.getnode(), which on most platforms returns a real NIC MAC
    address. Per the stdlib docs, if all attempts fail it falls back to a
    random 48-bit number with the multicast bit set — we detect and reject
    that case so we never wire an install_token to a fake, unstable "MAC".
    """
    global _cached_mac, _cache_populated
    if _cache_populated:
        return _cached_mac

    node = uuid.getnode()

    # uuid.getnode() sets the least-significant bit of the first octet
    # when it had to fabricate a random value instead of reading real
    # hardware. Refuse to trust that value.
    if (node >> 40) & 0x01:
        _cached_mac = None
    else:
        mac_hex = f"{node:012X}"
        _cached_mac = ":".join(mac_hex[i:i + 2] for i in range(0, 12, 2))

    _cache_populated = True
    return _cached_mac


if __name__ == "__main__":
    mac = get_local_mac()
    print(f"Local MAC: {mac or 'UNAVAILABLE'}")
