# Slice 0: OAuth token longevity (throwaway spike)

`build-plan.md` Phase 0. Question: does a Gmail refresh token survive past 7 days when the
consent screen is **In production, unverified**? If it doesn't, ingestion falls back to IMAP
(`tech-design.md` §5).

## Google Cloud setup (done once, in the browser)

The order matters. There are reports that a client created while the app was in Testing
keeps Testing behaviour after you switch to production, so create the client **after**
step 4.

1. https://console.cloud.google.com → create a new project, e.g. `hisaab-slice0`.
2. APIs & Services → Library → enable **Gmail API**.
3. Google Auth Platform (or OAuth consent screen) → User type **External**. App name, your
   email as support and developer contact. Under Data Access add scope
   `https://www.googleapis.com/auth/gmail.readonly`.
4. Audience → Publishing status → **Publish app** → confirm **In production**. Don't submit
   for verification. Ignore the warning that verification is needed.
5. Clients (Credentials) → Create client → Application type **Desktop app**. Download the
   JSON and save it as `spikes/slice0_oauth/client_secret.json`.

## Run

```bash
cd spikes/slice0_oauth
.venv/bin/python authorize.py     # prints a URL; open it in the Windows browser
.venv/bin/python refresh.py       # day 0: must succeed
```

When you consent, expect Google's "Google hasn't verified this app" page. Click Advanced, then
Go to (app name). Seeing that page confirms the app is unverified, which is the condition
under test.

## Verify

| When | Command | Pass |
|---|---|---|
| Day 0, right after authorizing | `refresh.py` | exit 0, `Gmail profile OK` |
| Day 8 or later | `refresh.py` | exit 0, `PASS at day 8.x` |

Exit code 2 (`invalid_grant`) on day 7 or 8 means the assumption failed: stop and reconsider.
Running `refresh.py` on other days is harmless and adds evidence to `refresh_log.jsonl`.

Things that invalidate the test (if one happens, re-authorize with `--force` and restart the clock):
revoking the app at myaccount.google.com/permissions, changing your Google password, or moving
the consent screen back to Testing.

## Files

- `client_secret.json`, `token.json`: secrets, gitignored, token is mode 600
- `refresh_log.jsonl`: one line per refresh attempt, no secrets. This is the evidence.
