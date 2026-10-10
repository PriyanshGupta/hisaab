#!/usr/bin/env bash
# Start Hisaab: build the frontend if needed, migrate, serve (tech-design §9.4).
#
# Settings come from <project root>/.env, read by the app itself. This script
# never sources or exports .env. In particular it must never export an
# ANTHROPIC_API_KEY (see src/hisaab/config.py for why).
#
# Any failing step aborts (set -e). A failed migration means the server never
# starts on a half-migrated schema (§9.3).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/python ]]; then
  echo "No virtualenv. Run: python3.14 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi
[[ -f .env ]] || echo "note: no .env found; using defaults (copy .env.example to .env)" >&2

# Frontend arrives in slice 4. Rebuild only when dist/ is missing or older than its sources.
if [[ -d frontend ]]; then
  if [[ ! -d frontend/dist ]] || [[ -n "$(find frontend/src frontend/index.html -newer frontend/dist -print -quit 2>/dev/null)" ]]; then
    echo "Building frontend..." >&2
    (cd frontend && npm run build)
  fi
fi

.venv/bin/alembic upgrade head
exec .venv/bin/python -m hisaab
