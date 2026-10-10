# Hisaab

Personal finance tracker: ingests bank alert emails and statements, reports where the money went.
Runs as a local server (FastAPI + SQLite) inside WSL2 and serves a web UI on the LAN.

Design documents and bank fixtures are intentionally kept out of this repository.

## Setup

```bash
python3.14 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env        # then fill in secrets
```

## Run

```bash
scripts/run.sh              # build frontend if present → alembic upgrade head → serve on :8765
curl http://localhost:8765/health
```

## Test

```bash
.venv/bin/pytest
```

## Two rules that prevent real damage

1. **The database must live on the Linux filesystem** (`/home/...`), never under `/mnt/c`.
   SQLite locking is broken over the Windows mount; the app refuses to start there.
2. **Secrets live only in `.env`.** Never `export` an API key from a shell profile, rc file or
   setup script. Claude Code would pick up `ANTHROPIC_API_KEY` and bill development work to the
   app's API credits, which is why the app's own variable is `HISAAB_ANTHROPIC_API_KEY`.
