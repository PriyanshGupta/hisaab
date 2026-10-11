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

# Linux Node.js for the frontend build. Under WSL a Windows npm (/mnt/c/...) is
# often first on PATH via interop; it must not be used: it is slow over the
# Windows mount and builds against the wrong platform. Prefer a Linux npm on
# PATH, else nvm (bash-only setups don't load it in zsh), else stop and say so.
find_linux_npm() {
  local npm_path
  npm_path="$(command -v npm || true)"
  if [[ -n "$npm_path" && "$npm_path" != /mnt/* ]]; then
    echo "$npm_path"; return 0
  fi
  if [[ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]]; then
    # shellcheck disable=SC1091
    source "${NVM_DIR:-$HOME/.nvm}/nvm.sh" >/dev/null
    (cd frontend && nvm use --silent >/dev/null 2>&1 || nvm use --silent default >/dev/null 2>&1) || true
    npm_path="$(nvm which current 2>/dev/null | sed 's#/node$#/npm#')"
    if [[ -x "$npm_path" ]]; then echo "$npm_path"; return 0; fi
  fi
  return 1
}

# Rebuild the frontend when dist/ is missing or older than anything it is built
# from, including the backend API (the TypeScript client is generated from it).
frontend_is_stale() {
  [[ ! -f frontend/dist/index.html ]] && return 0
  [[ -n "$(find frontend/src frontend/index.html frontend/package.json src/hisaab \
            -newer frontend/dist/index.html \( -name '*.py' -o -path 'frontend/*' \) -print -quit 2>/dev/null)" ]]
}

if [[ -d frontend ]] && frontend_is_stale; then
  if ! NPM="$(find_linux_npm)"; then
    echo "Linux Node.js not found (a Windows npm under /mnt/c does not work here)." >&2
    echo "Install Node 24 in WSL (e.g. via nvm), then re-run." >&2
    exit 1
  fi
  echo "Building frontend with $NPM ..." >&2
  NODE_BIN="$(dirname "$NPM")"
  (cd frontend && export PATH="$NODE_BIN:$PATH" && { [[ -d node_modules ]] || "$NPM" ci; } && "$NPM" run build)
fi

.venv/bin/alembic upgrade head
exec .venv/bin/python -m hisaab
