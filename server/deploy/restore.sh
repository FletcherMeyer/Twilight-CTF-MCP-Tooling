#!/usr/bin/env bash
# Restore a backup produced by backup.sh.
#   sudo ./restore.sh /var/backups/ctfsheet/ctfsheet-20261109-031500.db.gpg
# The current database is kept as <db>.pre-restore-<timestamp>.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
SRC=${1:?usage: restore.sh BACKUP.db.gpg}
ENV_FILE=/etc/ctfsheet/ctfsheet.env
PASS_FILE=/etc/ctfsheet/backup.pass
DB=$(grep '^CTFSHEET_DB=' "$ENV_FILE" | cut -d= -f2-)
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

gpg --batch --quiet --pinentry-mode loopback --passphrase-file "$PASS_FILE" -o "$TMP/restore.db" -d "$SRC"
sqlite3 "$TMP/restore.db" "PRAGMA integrity_check;" | grep -qx ok || { echo "Backup is corrupt" >&2; exit 1; }

systemctl stop ctfsheet
[[ -f "$DB" ]] && mv "$DB" "$DB.pre-restore-$(date -u +%Y%m%d-%H%M%S)"
rm -f "$DB-wal" "$DB-shm"
install -m 0600 -o ctfsheet -g ctfsheet "$TMP/restore.db" "$DB"
systemctl start ctfsheet
echo "Restored $SRC -> $DB"
