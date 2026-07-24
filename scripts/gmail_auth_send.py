"""One-time OAuth authorization for the gmail.send scope (M6).

M4 sync uses gmail.readonly only. Sending application emails requires the
additional gmail.send scope, which needs a separate, explicit authorization.

This script runs the OAuth code flow locally:
  1. Builds an authorization URL requesting gmail.send (+ readonly).
  2. Opens it in ego-browser (or prints it) for you to approve.
  3. You paste back the authorization code from the redirect URL.
  4. It exchanges the code for tokens and stores them in a gitignored secret file.

The stored token is used by scripts/email_apply.py to send approved emails.

Usage:
    python3 scripts/gmail_auth_send.py

Requires CAREEROPS_GOOGLE_CLIENT_ID and CAREEROPS_GOOGLE_CLIENT_SECRET in .env.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECRET_DIR = PROJECT_ROOT / "secrets"
TOKEN_FILE = SECRET_DIR / "gmail_send_token.json"

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REDIRECT_URI = "http://127.0.0.1:8000/oauth/callback"
SCOPES = " ".join(
    [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ]
)


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env.setdefault(k.strip(), v.strip().strip("'\""))
    return env


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def open_in_browser(url: str) -> None:
    """Try to open the auth URL in ego-browser; fall back to printing it."""
    try:
        result = subprocess.run(
            [
                "ego-browser",
                "nodejs",
                "-e",
                f"const task = await useOrCreateTaskSpace('gmail-auth'); "
                f"await openOrReuseTab('{url}', {{ wait: true, timeout: 30 }}); "
                f"cliLog('opened')",
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if "opened" in result.stdout:
            print("  -> Opened authorization page in ego-browser.")
            return
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    print("\n  Open this URL in your browser to authorize:\n")
    print(f"  {url}\n")


def exchange_code(
    code: str,
    *,
    client_id: str,
    client_secret: str,
    code_verifier: str,
) -> dict:
    body = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    env = load_env()
    client_id = env.get("CAREEROPS_GOOGLE_CLIENT_ID", "")
    client_secret = env.get("CAREEROPS_GOOGLE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("Error: CAREEROPS_GOOGLE_CLIENT_ID / _CLIENT_SECRET not set in .env")
        sys.exit(1)

    state = secrets.token_urlsafe(16)
    verifier, challenge = pkce_pair()

    params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    auth_url = f"{AUTH_URL}?{params}"

    print("Gmail Send Authorization (gmail.send scope)")
    print("=" * 60)
    print("This grants CareerOps permission to SEND email from your")
    print("Gmail account. Only approve if you understand this.")
    print("=" * 60)

    open_in_browser(auth_url)

    print("\nAfter approving, Google redirects to a page that fails to load.")
    print("Copy the FULL redirect URL from your browser's address bar.")
    print("It looks like: http://127.0.0.1:8000/oauth/callback?code=...&state=...")
    redirect = input("\nPaste the redirect URL here: ").strip()

    parsed = urllib.parse.urlparse(redirect)
    qs = urllib.parse.parse_qs(parsed.query)
    code = qs.get("code", [""])[0]
    returned_state = qs.get("state", [""])[0]

    if not code:
        print("Error: no authorization code found in the URL.")
        sys.exit(1)
    if returned_state != state:
        print("Error: state mismatch (possible CSRF). Aborting.")
        sys.exit(1)

    print("\nExchanging code for tokens...")
    try:
        tokens = exchange_code(
            code,
            client_id=client_id,
            client_secret=client_secret,
            code_verifier=verifier,
        )
    except Exception as e:
        print(f"Error exchanging code: {e}")
        sys.exit(1)

    if "access_token" not in tokens:
        print(f"Error: no access token returned: {tokens}")
        sys.exit(1)

    granted = tokens.get("scope", "")
    if "gmail.send" not in granted:
        print(f"WARNING: gmail.send scope was NOT granted. Granted: {granted}")
        print("The send feature will not work until you approve the send scope.")

    SECRET_DIR.mkdir(exist_ok=True)
    record = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""),
        "scope": granted,
        "expires_in": tokens.get("expires_in", 3600),
        "client_id": client_id,
        "client_secret": client_secret,
    }
    TOKEN_FILE.write_text(json.dumps(record, indent=2))
    TOKEN_FILE.chmod(0o600)

    print(f"\nToken saved: {TOKEN_FILE}")
    print(f"Granted scopes: {granted}")
    print("\nYou can now send approved application emails via scripts/email_apply.py")


if __name__ == "__main__":
    main()
