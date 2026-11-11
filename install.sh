#!/usr/bin/env bash
# Install gt76-fanctl: root service + desktop app, then pin the app to the KDE Plasma taskbar.
# Run as your normal user; asks for your password once (pkexec).
set -euo pipefail
SRC=$(dirname "$(readlink -f "$0")")
[ "$(id -u)" -ne 0 ] || { echo "Run as your normal user, not root."; exit 1; }

pkexec "$SRC/install-system.sh" "$(id -gn)"

if [ "${1:-}" != "--no-pin" ]; then
  echo "== Pinning to the Plasma taskbar"
  "$SRC/tools/pin-to-taskbar.sh" || echo "Could not pin automatically; right-click the running app's taskbar entry and choose 'Pin to Task Manager'."
fi
echo "Done. Launch 'GT76 Fan Control' from the taskbar or the application menu."
