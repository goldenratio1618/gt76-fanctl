#!/usr/bin/env bash
# Root half of the installer (install.sh runs this through pkexec).
# Usage: install-system.sh GROUP     GROUP may use the control socket (normally your primary group)
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run as root (install.sh does this for you)"; exit 1; }
GROUP=${1:?usage: install-system.sh GROUP}
getent group "$GROUP" >/dev/null || { echo "No such group: $GROUP"; exit 1; }
SRC=$(dirname "$(readlink -f "$0")")

echo "== Installing files"
install -d /opt/gt76-fanctl
install -m 755 "$SRC/fanctld.py" "$SRC/fanctl_gui.py" /opt/gt76-fanctl/
install -m 755 "$SRC/tools/cpu_stress.py" /opt/gt76-fanctl/
install -D -m 644 "$SRC/desktop/gt76-fanctl.svg" /usr/share/icons/hicolor/scalable/apps/gt76-fanctl.svg
install -m 644 "$SRC/desktop/gt76-fanctl.desktop" /usr/share/applications/gt76-fanctl.desktop
sed "s/@GROUP@/$GROUP/" "$SRC/systemd/gt76-fanctld.service" > /etc/systemd/system/gt76-fanctld.service

echo "== msi-ec in debug mode at boot (raw EC access for fan curves)"
[ -f /etc/modprobe.d/msi-ec-debug.conf ] || echo "options msi-ec debug=1" > /etc/modprobe.d/msi-ec-debug.conf
[ -f /etc/modules-load.d/msi-ec.conf ] || echo msi-ec > /etc/modules-load.d/msi-ec.conf
modprobe msi-ec 2>/dev/null || true
if [ ! -d /sys/devices/platform/msi-ec/debug ]; then
  echo "WARNING: /sys/devices/platform/msi-ec/debug is missing; fan control will be unavailable."
  echo "         See README.md (msi-ec DKMS build with debug=1, signed for Secure Boot)."
fi

if [ -f /etc/systemd/system/cpu-power-limit.service ]; then
  echo "== Replacing the earlier cpu-power-limit one-shot service (this service now owns those limits)"
  systemctl disable --now cpu-power-limit.service || true
  rm -f /etc/systemd/system/cpu-power-limit.service /usr/local/sbin/cpu-power-limit.sh
fi

echo "== Starting service"
systemctl daemon-reload
systemctl enable gt76-fanctld.service
systemctl restart gt76-fanctld.service
update-desktop-database -q /usr/share/applications 2>/dev/null || true
gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
sleep 1
systemctl --no-pager --lines=5 status gt76-fanctld.service || true
