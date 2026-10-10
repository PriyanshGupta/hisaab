"""Slice 0, step 1: run the Gmail OAuth consent flow once and store the refresh token.

Throwaway spike (build-plan.md Phase 0). Not application code.

Running this again overwrites the grant and restarts the 7-day clock, so it
refuses to run when token.json already exists unless --force is passed.
"""

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from google_auth_oauthlib.flow import InstalledAppFlow

HERE = Path(__file__).resolve().parent
CLIENT_SECRET = HERE / "client_secret.json"
TOKEN = HERE / "token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
PORT = 8765  # tech-design.md §5.5 loopback port
IST = ZoneInfo("Asia/Kolkata")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="overwrite an existing grant (restarts the clock)")
    args = ap.parse_args()

    if TOKEN.exists() and not args.force:
        print(f"{TOKEN.name} already exists. Re-authorizing restarts the 7-day clock.")
        print("Pass --force if that is really what you want.")
        return 1

    if not CLIENT_SECRET.exists():
        print(f"Missing {CLIENT_SECRET}. Download the OAuth client JSON from Google Cloud Console")
        print("(APIs & Services > Credentials > your Desktop client > Download JSON) and save it there.")
        return 1

    client_cfg = json.loads(CLIENT_SECRET.read_text())
    if "installed" not in client_cfg:
        kind = next(iter(client_cfg), "unknown")
        print(f"Client type is '{kind}', expected 'installed' (Desktop app). build-plan.md Slice 0 specifies Desktop.")
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), scopes=SCOPES)
    creds = flow.run_local_server(
        host="localhost",
        port=PORT,
        open_browser=False,  # WSL: open the printed URL in the Windows browser
        access_type="offline",
        prompt="consent",  # force a fresh refresh token even if previously granted
        authorization_prompt_message="\nOpen this URL in your browser to authorize:\n\n{url}\n",
        success_message="Authorized. You can close this tab and return to the terminal.",
    )

    if not creds.refresh_token:
        print("No refresh token returned. Revoke the app at https://myaccount.google.com/permissions and retry.")
        return 1

    granted = dt.datetime.now(dt.UTC)
    TOKEN.write_text(json.dumps({
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "scopes": list(creds.scopes or SCOPES),
        "granted_at": granted.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, indent=2))
    os.chmod(TOKEN, 0o600)

    day8 = (granted + dt.timedelta(days=8)).astimezone(IST)
    print(f"Refresh token stored in {TOKEN.name} (mode 600).")
    print(f"Granted:       {granted.astimezone(IST):%Y-%m-%d %H:%M %Z}")
    print(f"Scopes:        {', '.join(creds.scopes or SCOPES)}")
    print(f"Day-8 check on or after: {day8:%Y-%m-%d %H:%M %Z}")
    print("Now run: .venv/bin/python refresh.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
