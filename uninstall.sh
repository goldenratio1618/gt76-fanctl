#!/usr/bin/env bash
# Remove gt76-fanctl. Restores factory fan/clock settings first; leaves the msi-ec driver installed.
set -uo pipefail
SRC=$(dirname "$(readlink -f "$0")")
"$SRC/tools/pin-to-taskbar.sh" --remove || true
python3 -I -c '
import json, socket
s = socket.socket(socket.AF_UNIX); s.settimeout(5); s.connect("/run/gt76-fanctl.sock")
s.sendall(b"{\"cmd\": \"restore\", \"which\": \"factory\"}\n"); print(json.loads(s.recv(65536))["ok"] and "Factory settings restored.")
' 2>/dev/null || echo "Service not reachable; settings return to factory at the next boot."
pkexec bash -c '
systemctl disable --now gt76-fanctld.service
rm -rf /opt/gt76-fanctl /etc/systemd/system/gt76-fanctld.service /usr/share/applications/gt76-fanctl.desktop \
       /usr/share/icons/hicolor/scalable/apps/gt76-fanctl.svg
systemctl daemon-reload
echo "Removed. Saved defaults kept in /etc/gt76-fanctl (delete it to discard them)."
'
