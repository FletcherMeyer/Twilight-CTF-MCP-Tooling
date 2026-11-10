#!/usr/bin/env bash
# Update an existing install with new code. Takes a backup first.
#   sudo ./deploy/update.sh            # run from the new server/ directory
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
SRC_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
APP_DIR=/opt/ctfsheet
[[ -d "$SRC_DIR/ctfsheet_server" ]] || { echo "No ctfsheet_server/ in $SRC_DIR" >&2; exit 1; }

/usr/local/sbin/ctfsheet-backup || { echo "Backup failed; aborting update." >&2; exit 1; }

STAGE=$(mktemp -d)
cp -r "$SRC_DIR/ctfsheet_server" "$SRC_DIR/requirements.txt" "$SRC_DIR/deploy" "$STAGE/"
find "$STAGE" -name '__pycache__' -prune -exec rm -rf {} +
rm -rf "$APP_DIR/app.prev"; mv "$APP_DIR/app" "$APP_DIR/app.prev"; mv "$STAGE" "$APP_DIR/app"
chown -R root:root "$APP_DIR/app"; chmod -R go-w "$APP_DIR/app"; chmod 755 "$APP_DIR/app"

"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/app/requirements.txt"
install -m 0644 "$APP_DIR/app/deploy/ctfsheet.service" /etc/systemd/system/ctfsheet.service
install -m 0750 "$APP_DIR/app/deploy/backup.sh" /usr/local/sbin/ctfsheet-backup
systemctl daemon-reload
runuser -u ctfsheet -- bash -c "set -a; . /etc/ctfsheet/ctfsheet.env; set +a; cd $APP_DIR/app && exec $APP_DIR/venv/bin/python -m ctfsheet_server.manage init-db"
systemctl restart ctfsheet
sleep 2
if curl -fs http://127.0.0.1:8088/healthz >/dev/null; then
  echo "Updated. Previous version kept in $APP_DIR/app.prev"
else
  echo "Health check failed; rolling back." >&2
  rm -rf "$APP_DIR/app"; mv "$APP_DIR/app.prev" "$APP_DIR/app"
  systemctl restart ctfsheet
  exit 1
fi
