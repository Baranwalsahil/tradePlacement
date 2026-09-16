#!/usr/bin/env python3
"""Mint a Kite Connect access token for today and cache it for strategy.py.

Run this once each morning before 09:15 IST:

    export KITE_API_KEY=...
    export KITE_API_SECRET=...
    python login.py

It prints a login URL, you log in with your Zerodha credentials and 2FA, and
Zerodha redirects to your registered redirect URL with ?request_token=... in the
query string. Paste that value back here.

No Zerodha password or TOTP secret is ever stored by this script. The only thing
written to disk is the resulting access token, at 0600.
"""

import json
import os
import stat
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect

IST = ZoneInfo("Asia/Kolkata")
TOKEN_FILE = Path(__file__).resolve().parent / ".kite_token.json"


def extract_request_token(pasted: str) -> str:
    """Accept either a bare request_token or the whole redirect URL.

    After login the browser lands on something like
    https://localhost/?status=success&request_token=abc123&action=login - pasting
    that wholesale is the obvious thing to do, so handle it.
    """
    pasted = pasted.strip().strip('"').strip("'")
    if "request_token=" not in pasted:
        return pasted
    query = urlparse(pasted).query or pasted.split("?", 1)[-1]
    found = parse_qs(query).get("request_token")
    return found[0] if found else pasted


def main() -> int:
    api_key = os.environ.get("KITE_API_KEY")
    api_secret = os.environ.get("KITE_API_SECRET")
    if not api_key or not api_secret:
        print("Set KITE_API_KEY and KITE_API_SECRET in the environment.", file=sys.stderr)
        return 1

    kite = KiteConnect(api_key=api_key)
    print("\n1. Open this URL and log in:\n")
    print("   " + kite.login_url())
    print("\n2. Your browser lands on your redirect URL and shows a connection")
    print("   error - that is expected, nothing is listening there. Paste either")
    print("   the request_token value or the whole URL from the address bar.\n")

    request_token = extract_request_token(input("request_token or URL: "))
    if not request_token:
        print("No request_token given.", file=sys.stderr)
        return 1

    try:
        session = kite.generate_session(request_token, api_secret=api_secret)
    except Exception as exc:  # noqa: BLE001 - surface whatever Kite says
        print(f"Token exchange failed: {exc}", file=sys.stderr)
        print(
            "\nrequest_tokens are single-use and expire within a few minutes. "
            "Open the login URL again and use a fresh one.",
            file=sys.stderr,
        )
        return 1

    access_token = session["access_token"]
    today = datetime.now(IST).date().isoformat()

    TOKEN_FILE.write_text(
        json.dumps(
            {
                "access_token": access_token,
                "api_key": api_key,
                "date": today,
                "user_id": session.get("user_id"),
            },
            indent=2,
        )
    )
    TOKEN_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)

    kite.set_access_token(access_token)
    profile = kite.profile()
    print(f"\nLogged in as {profile.get('user_name')} ({profile.get('user_id')}).")
    print(f"Token cached at {TOKEN_FILE} for {today}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
