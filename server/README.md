# CTFSheet server

FastAPI + SQLite, served by uvicorn on `127.0.0.1:8088` behind nginx (TLS).

## Install

Requirements: a Debian/Ubuntu (apt) or RHEL/Rocky/Fedora (dnf) host, root access, ports 80/443
open. For Let's Encrypt the DNS name must already point at the host.

```bash
sudo bash deploy/install.sh --domain ctf.example.com --email you@example.com
sudo bash deploy/install.sh --self-signed                 # no DNS name: self-signed cert with host IPs in SAN
sudo bash deploy/install.sh --self-signed --port 8443     # alternate HTTPS port
sudo bash deploy/install.sh --domain ctf.example.com --email you@example.com \
     --creation-code 'S3cret-For-Team-Creation'           # only people with this code may create teams
```

What the installer does:

1. Installs python3, nginx, openssl, sqlite3, gnupg (+ certbot when not self-signed).
2. Creates the `ctfsheet` system user and `/opt/ctfsheet`, `/var/lib/ctfsheet` (0700), `/etc/ctfsheet`.
3. Builds a virtualenv and installs `requirements.txt`.
4. Generates the **data-encryption key** and writes `/etc/ctfsheet/ctfsheet.env` (only on first install),
   plus a root-only backup passphrase in `/etc/ctfsheet/backup.pass`.
5. Installs and starts the hardened `ctfsheet.service` systemd unit.
6. Configures nginx (TLS 1.2/1.3, HSTS, rate limits, 64 MB body cap) and obtains/renews certificates.
7. Opens 80/443 in ufw or firewalld.
8. Installs a nightly encrypted backup cron job (03:15).

> **Back up `/etc/ctfsheet/ctfsheet.env` and `/etc/ctfsheet/backup.pass` offline.** Without
> `CTFSHEET_DATA_KEYS` the encrypted flags/files cannot be recovered — by design.

## Configuration (`/etc/ctfsheet/ctfsheet.env`)

| Variable | Default | Meaning |
|---|---|---|
| `CTFSHEET_DB` | `/var/lib/ctfsheet/ctfsheet.db` | SQLite path |
| `CTFSHEET_DATA_KEYS` | generated | Comma-separated Fernet keys; first encrypts, all decrypt |
| `CTFSHEET_ALLOW_TEAM_CREATION` | `true` | Set `false` to freeze team creation |
| `CTFSHEET_TEAM_CREATION_CODE` | empty | If set, required to create a team |
| `CTFSHEET_MAX_UPLOAD_MB` | `20` | Per-file limit |
| `CTFSHEET_SESSION_HOURS` | `12` | Web session lifetime |
| `CTFSHEET_TOKEN_DAYS` | `90` | API token lifetime |
| `CTFSHEET_COOKIE_SECURE` | `true` | Keep `true` in production (HTTPS) |
| `CTFSHEET_MIN_PASSWORD_LEN` | `10` | Minimum password length |

Apply changes with `sudo systemctl restart ctfsheet`.

## Operations

```bash
journalctl -u ctfsheet -f                       # app logs
sudo ctfsheet-backup                            # backup now -> /var/backups/ctfsheet/*.db.gpg (14 days kept)
sudo bash deploy/restore.sh /var/backups/ctfsheet/ctfsheet-YYYYmmdd-HHMMSS.db.gpg
sudo bash deploy/update.sh                      # from a newer copy of server/: backup, swap, health-check, auto-rollback
sudo bash deploy/uninstall.sh [--purge]         # --purge also deletes data, keys and backups

# admin CLI (run as the service user with its environment)
sudo -u ctfsheet bash -c 'set -a; . /etc/ctfsheet/ctfsheet.env; cd /opt/ctfsheet/app; \
     /opt/ctfsheet/venv/bin/python -m ctfsheet_server.manage list-teams'
#   ... manage reset-password USERNAME
#   ... manage rotate-keys
```

### Rotating the data-encryption key

1. `python -m ctfsheet_server.manage gen-key` → new key.
2. Prepend it: `CTFSHEET_DATA_KEYS=<new>,<old>` and restart.
3. Run `manage rotate-keys` (re-encrypts every row with the new key).
4. Remove `<old>` from the list and restart.

## Local development

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export CTFSHEET_DATA_KEYS=$(python -m ctfsheet_server.manage gen-key)
export CTFSHEET_DB=./dev.db CTFSHEET_COOKIE_SECURE=false
uvicorn ctfsheet_server.app:app --reload --port 8088
```

## REST API (summary)

All routes are under `/api`, JSON in/out. Auth: session cookie (+ `X-CSRF-Token` for writes) or
`Authorization: Bearer ctfs_…` API token.

| Method & path | Who | Purpose |
|---|---|---|
| `POST /auth/create-team` `/auth/join` `/auth/login` `/auth/logout` | public | onboarding & sessions |
| `POST /auth/token` | public (rate-limited) | username/password → API token (used by `ctfsheet-mcp login`) |
| `GET /me`, `PATCH /me` | any | profile, initials, password |
| `GET/POST /tokens`, `DELETE /tokens/{id}`, `DELETE /tokens/current` | any | API tokens |
| `GET /team`, `GET /team/revision`, `GET/PUT /team/agent-md` | any / editor | team info, live-update counter, agent.md |
| `POST /team/rotate-codes`, `PATCH /team/members/{id}`, `GET /team/audit` | owner | administration |
| `GET/POST /categories`, `PATCH/DELETE /categories/{id}` | any / editor | tabs |
| `GET /sheet` | any | whole sheet for the UI |
| `GET /challenges?q=&category=&difficulty=&status=` | any | search / list |
| `POST /challenges`, `GET/PATCH/DELETE /challenges/{id}` | editor | rows |
| `POST /challenges/{id}/flags`, `DELETE /flags/{id}` | editor | answers |
| `POST/DELETE /challenges/{id}/complete` | editor | solver initials |
| `POST /challenges/{id}/artifacts` | editor | context / solution / attachment upload |
| `POST /challenges/{id}/human-solution` | editor | pasted text + images |
| `GET /artifacts/{id}`, `GET /artifacts/{id}/raw`, `DELETE /artifacts/{id}` | any / author | files |
