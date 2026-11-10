#!/usr/bin/env bash
# Remove CTFSheet. Data, config and backups are KEPT unless --purge is given.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
systemctl disable --now ctfsheet 2>/dev/null || true
rm -f /etc/systemd/system/ctfsheet.service /etc/cron.d/ctfsheet-backup /usr/local/sbin/ctfsheet-backup
rm -f /etc/nginx/sites-enabled/ctfsheet.conf /etc/nginx/sites-available/ctfsheet.conf /etc/nginx/conf.d/ctfsheet.conf /etc/nginx/proxy_params_ctfsheet
systemctl daemon-reload
systemctl reload nginx 2>/dev/null || true
rm -rf /opt/ctfsheet
if [[ "${1:-}" == "--purge" ]]; then
  read -r -p "Permanently delete the database, keys and backups? Type DELETE: " ans
  [[ "$ans" == "DELETE" ]] && rm -rf /var/lib/ctfsheet /etc/ctfsheet /var/backups/ctfsheet /etc/ssl/ctfsheet && userdel ctfsheet 2>/dev/null || true
fi
echo "CTFSheet removed."
