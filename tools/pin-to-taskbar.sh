#!/usr/bin/env bash
# Add (or with --remove, drop) the GT76 Fan Control launcher in every KDE Plasma 6 task manager.
set -euo pipefail
ACTION=add
[ "${1:-}" = "--remove" ] && ACTION=remove
QDBUS=$(command -v qdbus6 || command -v qdbus) || { echo "qdbus6 not found"; exit 1; }
"$QDBUS" org.kde.plasmashell /PlasmaShell org.kde.PlasmaShell.evaluateScript "
var id = 'applications:gt76-fanctl.desktop', n = 0;
panels().forEach(function (p) {
  p.widgets().forEach(function (w) {
    if (w.type !== 'org.kde.plasma.icontasks' && w.type !== 'org.kde.plasma.taskmanager') return;
    w.currentConfigGroup = ['General'];
    var l = w.readConfig('launchers', []);
    if (typeof l === 'string') l = l ? l.split(',') : [];
    var i = l.indexOf(id);
    if ('$ACTION' === 'add' && i < 0) l.push(id);
    if ('$ACTION' === 'remove' && i >= 0) l.splice(i, 1);
    w.writeConfig('launchers', l);
    w.reloadConfig();
    n++;
  });
});
print('task managers updated: ' + n);
"
