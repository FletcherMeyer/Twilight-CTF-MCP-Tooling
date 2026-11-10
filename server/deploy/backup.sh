#!/usr/bin/env bash
# Consistent, gpg-encrypted (AES-256) backup of the CTFSheet database.
# Data inside the DB is already encrypted with CTFSHEET_DATA_KEYS; this adds a
# second layer so backups are safe to copy off-host.
#
#   sudo ctfsheet-backup            # writes /var/backups/ctfsheet/ctfsheet-YYYYmmdd-HHMMSS.db.gpg
#   KEEP_DAYS=30 sudo ctfsheet-backup
#
# NOTE: restoring also requires CTFSHEET_DATA_KEYS from /etc/ctfsheet/ctfsheet.env.
# Back that file up separately (password manager / offline media).
set -euo pipefail
ENV_FILE=/etc/ctfsheet/ctfsheet.env
PASS_FILE=/etc/ctfsheet/backup.pass
BACKUP_DIR=${BACKUP_DIR:-/var/backups/ctfsheet}
KEEP_DAYS=${KEEP_DAYS:-14}

[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
DB=$(grep '^CTFSHEET_DB=' "$ENV_FILE" | cut -d= -f2-)
STAMP=$(date -u +%Y%m%d-%H%M%S)
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
chmod 700 "$TMP"

sqlite3 "$DB" ".backup '$TMP/snapshot.db'"
sqlite3 "$TMP/snapshot.db" "PRAGMA integrity_check;" | grep -qx ok || { echo "Integrity check failed" >&2; exit 1; }

install -d -m 0700 "$BACKUP_DIR"
OUT="$BACKUP_DIR/ctfsheet-$STAMP.db.gpg"
gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$PASS_FILE" \
    --symmetric --cipher-algo AES256 --s2k-digest-algo SHA512 --s2k-count 65011712 \
    -o "$OUT" "$TMP/snapshot.db"
chmod 0600 "$OUT"
find "$BACKUP_DIR" -name 'ctfsheet-*.db.gpg' -mtime +"$KEEP_DAYS" -delete
echo "Backup written: $OUT"
