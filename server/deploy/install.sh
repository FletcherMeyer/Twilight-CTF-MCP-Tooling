#!/usr/bin/env bash
# CTFSheet installer for Debian/Ubuntu (apt) and RHEL/Fedora/Rocky (dnf).
#
#   sudo ./deploy/install.sh --domain ctf.example.com --email you@example.com   # Let's Encrypt TLS
#   sudo ./deploy/install.sh --self-signed                                      # self-signed TLS (LAN / IP only)
#
# Options:
#   --domain NAME          public DNS name (enables Let's Encrypt when --email is also given)
#   --email ADDR           ACME account e-mail for Let's Encrypt
#   --self-signed          generate a self-signed certificate instead of Let's Encrypt
#   --port N               HTTPS port (default 443)
#   --creation-code CODE   require this code to create teams (default: anyone may create a team)
#   --no-firewall          don't touch ufw/firewalld
#
# Layout:
#   /opt/ctfsheet/app            application code
#   /opt/ctfsheet/venv           python virtualenv
#   /var/lib/ctfsheet/           database (0700, owned by the ctfsheet user)
#   /etc/ctfsheet/ctfsheet.env   secrets + config (0600 root:ctfsheet)
#   /var/backups/ctfsheet/       encrypted nightly backups
set -euo pipefail

DOMAIN=""; EMAIL=""; SELF_SIGNED=0; HTTPS_PORT=443; CREATION_CODE=""; FIREWALL=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --email) EMAIL="$2"; shift 2 ;;
    --self-signed) SELF_SIGNED=1; shift ;;
    --port) HTTPS_PORT="$2"; shift 2 ;;
    --creation-code) CREATION_CODE="$2"; shift 2 ;;
    --no-firewall) FIREWALL=0; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Run as root (sudo)." >&2; exit 1; }
if [[ -z "$DOMAIN" && $SELF_SIGNED -eq 0 ]]; then
  echo "Give --domain NAME --email ADDR for Let's Encrypt, or --self-signed." >&2; exit 2
fi
if [[ -n "$DOMAIN" && -z "$EMAIL" && $SELF_SIGNED -eq 0 ]]; then
  echo "--email is required for Let's Encrypt (or add --self-signed)." >&2; exit 2
fi
SERVER_NAME="${DOMAIN:-_}"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/ctfsheet; DATA_DIR=/var/lib/ctfsheet; CONF_DIR=/etc/ctfsheet; BACKUP_DIR=/var/backups/ctfsheet
ENV_FILE=$CONF_DIR/ctfsheet.env

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------- packages
log "Installing system packages"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get install -yq python3 python3-venv python3-pip nginx openssl sqlite3 gnupg
  [[ $SELF_SIGNED -eq 0 ]] && apt-get install -yq certbot python3-certbot-nginx
  NGINX_SITE=/etc/nginx/sites-available/ctfsheet.conf; NGINX_LINK=/etc/nginx/sites-enabled/ctfsheet.conf
  rm -f /etc/nginx/sites-enabled/default
elif command -v dnf >/dev/null; then
  dnf install -y python3 python3-pip nginx openssl sqlite gnupg2
  [[ $SELF_SIGNED -eq 0 ]] && { dnf install -y epel-release || true; dnf install -y certbot python3-certbot-nginx; }
  NGINX_SITE=/etc/nginx/conf.d/ctfsheet.conf; NGINX_LINK=""
  if command -v setsebool >/dev/null; then setsebool -P httpd_can_network_connect 1 || true; fi
else
  echo "Unsupported distro (need apt or dnf)." >&2; exit 1
fi

# ---------------------------------------------------------------- user & dirs
log "Creating service user and directories"
id ctfsheet >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin ctfsheet
install -d -m 0755 "$APP_DIR"
install -d -m 0700 -o ctfsheet -g ctfsheet "$DATA_DIR"
install -d -m 0750 -o root -g ctfsheet "$CONF_DIR"
install -d -m 0700 -o root -g root "$BACKUP_DIR"

log "Copying application"
rm -rf "$APP_DIR/app"
mkdir -p "$APP_DIR/app"
cp -r "$SRC_DIR/ctfsheet_server" "$SRC_DIR/requirements.txt" "$APP_DIR/app/"
cp -r "$SRC_DIR/deploy" "$APP_DIR/app/"
find "$APP_DIR/app" -name '__pycache__' -prune -exec rm -rf {} +
chown -R root:root "$APP_DIR/app"; chmod -R go-w "$APP_DIR/app"

log "Creating virtualenv"
[[ -d "$APP_DIR/venv" ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/app/requirements.txt"

# ---------------------------------------------------------------- secrets
if [[ ! -f "$ENV_FILE" ]]; then
  log "Generating encryption key and config ($ENV_FILE)"
  DATA_KEY="$("$APP_DIR/venv/bin/python" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
  BACKUP_PASS="$(openssl rand -base64 36 | tr -d '\n')"
  umask 077
  cat > "$ENV_FILE" <<EOF
# CTFSheet configuration. KEEP THIS FILE SECRET AND BACKED UP SEPARATELY.
# Losing CTFSHEET_DATA_KEYS makes every encrypted flag/file unrecoverable.
CTFSHEET_DB=$DATA_DIR/ctfsheet.db
CTFSHEET_DATA_KEYS=$DATA_KEY
CTFSHEET_ALLOW_TEAM_CREATION=true
CTFSHEET_TEAM_CREATION_CODE=$CREATION_CODE
CTFSHEET_MAX_UPLOAD_MB=20
CTFSHEET_SESSION_HOURS=12
CTFSHEET_TOKEN_DAYS=90
CTFSHEET_COOKIE_SECURE=true
CTFSHEET_MIN_PASSWORD_LEN=10
EOF
  # Backup passphrase lives in a root-only file the app cannot read.
  printf '%s\n' "$BACKUP_PASS" > "$CONF_DIR/backup.pass"
  chmod 0600 "$CONF_DIR/backup.pass"
  umask 022
else
  log "Keeping existing $ENV_FILE"
fi
chown root:ctfsheet "$ENV_FILE"; chmod 0640 "$ENV_FILE"

log "Initialising database"
runuser -u ctfsheet -- bash -c "set -a; . '$ENV_FILE'; set +a; cd '$APP_DIR/app' && exec '$APP_DIR/venv/bin/python' -m ctfsheet_server.manage init-db"

# ---------------------------------------------------------------- systemd
log "Installing systemd unit"
install -m 0644 "$APP_DIR/app/deploy/ctfsheet.service" /etc/systemd/system/ctfsheet.service
systemctl daemon-reload
systemctl enable --now ctfsheet
systemctl restart ctfsheet

# ---------------------------------------------------------------- TLS + nginx
CERT_DIR=/etc/ssl/ctfsheet
install -d -m 0700 "$CERT_DIR"
if [[ ! -f "$CERT_DIR/dhparam.pem" ]]; then
  log "Generating DH parameters (one-time, ~30s)"
  openssl dhparam -out "$CERT_DIR/dhparam.pem" 2048 2>/dev/null
fi
if [[ $SELF_SIGNED -eq 1 || ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]]; then
  if [[ ! -f "$CERT_DIR/selfsigned.crt" ]]; then
    log "Creating self-signed certificate (temporary when using Let's Encrypt)"
    CN="${DOMAIN:-$(hostname -f 2>/dev/null || hostname)}"
    SAN="DNS:$CN"
    for ip in $(hostname -I 2>/dev/null); do SAN="$SAN,IP:$ip"; done
    openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 825 \
      -keyout "$CERT_DIR/selfsigned.key" -out "$CERT_DIR/selfsigned.crt" \
      -subj "/CN=$CN" -addext "subjectAltName=$SAN" 2>/dev/null
    chmod 0600 "$CERT_DIR/selfsigned.key"
  fi
  CERT="$CERT_DIR/selfsigned.crt"; KEY="$CERT_DIR/selfsigned.key"
else
  CERT="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"; KEY="/etc/letsencrypt/live/$DOMAIN/privkey.pem"
fi

write_nginx() {
  cat > /etc/nginx/proxy_params_ctfsheet <<'EOF'
proxy_pass http://127.0.0.1:8088;
proxy_http_version 1.1;
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $remote_addr;
proxy_set_header X-Forwarded-Proto https;
proxy_set_header Connection "";
EOF
  sed -e "s|__SERVER_NAME__|$SERVER_NAME|g" -e "s|__CERT__|$1|g" -e "s|__KEY__|$2|g" \
      -e "s|__HTTPS_PORT__|$HTTPS_PORT|g" -e "s|__DHPARAM__|$CERT_DIR/dhparam.pem|g" \
      "$APP_DIR/app/deploy/nginx.conf.template" > "$NGINX_SITE"
  [[ -n "$NGINX_LINK" ]] && ln -sf "$NGINX_SITE" "$NGINX_LINK"
  install -d -m 0755 /var/www/letsencrypt
  nginx -t
  systemctl enable nginx >/dev/null 2>&1 || true
  systemctl reload nginx 2>/dev/null || systemctl restart nginx
}
log "Configuring nginx"
write_nginx "$CERT" "$KEY"

if [[ $SELF_SIGNED -eq 0 ]]; then
  log "Requesting Let's Encrypt certificate for $DOMAIN"
  certbot certonly --webroot -w /var/www/letsencrypt -d "$DOMAIN" -m "$EMAIL" --agree-tos -n --keep-until-expiring
  write_nginx "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" "/etc/letsencrypt/live/$DOMAIN/privkey.pem"
  install -d /etc/letsencrypt/renewal-hooks/deploy
  printf '#!/bin/sh\nsystemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
  chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
fi

# ---------------------------------------------------------------- firewall
if [[ $FIREWALL -eq 1 ]]; then
  if command -v ufw >/dev/null; then
    log "Configuring ufw"
    ufw allow OpenSSH >/dev/null || true
    ufw allow 80/tcp >/dev/null; ufw allow "$HTTPS_PORT/tcp" >/dev/null
    ufw --force enable >/dev/null
  elif command -v firewall-cmd >/dev/null; then
    log "Configuring firewalld"
    firewall-cmd --permanent --add-service=http >/dev/null
    firewall-cmd --permanent --add-port="$HTTPS_PORT/tcp" >/dev/null
    firewall-cmd --reload >/dev/null
  fi
fi

# ---------------------------------------------------------------- backups
log "Installing nightly encrypted backup (03:15)"
install -m 0750 "$APP_DIR/app/deploy/backup.sh" /usr/local/sbin/ctfsheet-backup
cat > /etc/cron.d/ctfsheet-backup <<'EOF'
15 3 * * * root /usr/local/sbin/ctfsheet-backup >/var/log/ctfsheet-backup.log 2>&1
EOF

sleep 1
if curl -fsk "https://127.0.0.1:$HTTPS_PORT/healthz" -H "Host: ${DOMAIN:-localhost}" >/dev/null; then
  log "CTFSheet is up."
else
  echo "Health check failed — inspect: journalctl -u ctfsheet -e ; journalctl -u nginx -e" >&2
fi
URL="https://${DOMAIN:-$(hostname -I | awk '{print $1}')}"; [[ "$HTTPS_PORT" != 443 ]] && URL="$URL:$HTTPS_PORT"
cat <<EOF

  Open:            $URL  (create a team, then share its join codes)
  Config/secrets:  $ENV_FILE   <-- back up CTFSHEET_DATA_KEYS somewhere safe, offline
  Logs:            journalctl -u ctfsheet -f
  Update:          sudo $APP_DIR/app/deploy/update.sh /path/to/new/server
  Backup now:      sudo ctfsheet-backup
EOF
[[ $SELF_SIGNED -eq 1 ]] && cat <<EOF
  Self-signed cert: give MCP users $CERT_DIR/selfsigned.crt and have them run
                    ctfsheet-mcp login --url $URL --ca-cert selfsigned.crt
EOF
exit 0
