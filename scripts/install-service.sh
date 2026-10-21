#!/usr/bin/env bash
# Install (or update) Hisaab's systemd user service and start it. Safe to run again.
# Uses the user manager only; starting at boot without a login also needs linger, which takes your password:
#   sudo loginctl enable-linger "$USER"
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
sed "s#@ROOT@#${ROOT}#g" "$ROOT/scripts/hisaab.service" > "$UNIT_DIR/hisaab.service"
systemctl --user daemon-reload
systemctl --user enable hisaab.service
systemctl --user restart hisaab.service
systemctl --user --no-pager --lines=0 status hisaab.service || true

if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]]; then
  echo
  echo "Linger is off: the service starts with your first WSL session, not at boot."
  echo "To start it at boot without a login, run once: sudo loginctl enable-linger $USER"
fi
