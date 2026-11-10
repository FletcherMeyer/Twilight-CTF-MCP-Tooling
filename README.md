# CTFSheet

A team-shared CTF spreadsheet that humans **and AI agents** work on together.

```
Twilight-CTF-MCP-Tooling/
├── server/   web server + spreadsheet UI + REST API + Linux deployment scripts
└── mcp/      ctfsheet-mcp: the MCP tool your Claude / ChatGPT / Gemini uses to talk to the server
```

## What it does

* **Sheet layout** – one tab per category (Web, Crypto, Pwn, …), sections per difficulty
  (Easy / Medium / Hard), one row per challenge.
* **Every row holds all materials**: description, points, status, flags/answers (own column,
  masked until clicked), solvers' initials (first solver highlighted, later ones = verifiers),
  agent `context.md` files, per-flag `solution.md` files, human solutions, and challenge files.
* **Human solutions** – the “+ paste” button opens a modal: type/paste a write-up and paste
  screenshots straight from the clipboard (or drop files); it's stored as a human solution.
* **Teams** – anyone can create a team (optionally gated by a server-wide creation code).
  Creation yields two join codes: *editor* (read/write) and *viewer* (read-only). The owner can
  rotate codes, change roles, remove members and read the audit log.
* **Team `agent.md`** – shared instructions for every agent on the team. The MCP tool makes the
  agent ask its user **once per session** before loading it.
* **Agents** – via the MCP tool they search the sheet, ask (once) before reading prior agents'
  work, ask the user for name/category/difficulty before creating a challenge, upload their own
  `context.md` after important discoveries, submit flags with a reproducible `solution.md` per
  flag, and mark completion with their owner's initials.
* Live updates: open browsers refresh within ~5 s when anyone (human or agent) changes something.

## Security model

| Layer | Mechanism |
|---|---|
| Transport | nginx TLS 1.2/1.3 only (Let's Encrypt or self-signed), HSTS, HTTP→HTTPS redirect |
| Encryption at rest | Flags, descriptions, notes, every uploaded file and `agent.md` are encrypted with Fernet (AES-128-CBC + HMAC-SHA256). Key lives in `/etc/ctfsheet/ctfsheet.env` (0640 root:ctfsheet); supports rotation (`manage rotate-keys`). Backups are additionally GPG AES-256 encrypted. |
| Credentials | Passwords and join codes: Argon2id. Session cookies and API tokens: 256-bit random, only SHA-256 digests stored. Tokens expire (90 d default) and are revocable. |
| Web sessions | HttpOnly + Secure + SameSite=Strict cookie, per-session CSRF token on every write |
| Access control | Every query scoped to the caller's team; viewer / editor / owner roles enforced server-side |
| Abuse | Login/join/token endpoints rate-limited in the app *and* nginx; constant-time failure paths |
| Browser hardening | Strict CSP (no inline script), `X-Frame-Options: DENY`, `nosniff`; uploads are never rendered as HTML/SVG (served as `text/plain` or attachment); only PNG/JPEG/GIF/WebP shown inline |
| Process | Runs as unprivileged `ctfsheet` user on 127.0.0.1 under systemd sandboxing (`ProtectSystem=strict`, no capabilities, syscall filter) |
| Audit | Logins, failures, token creation, uploads, completions, deletions are logged per team |

## Quick start

### 1. Deploy the server (Linux)

```bash
# on the Linux host
git clone https://github.com/FletcherMeyer/Twilight-CTF-MCP-Tooling.git
cd Twilight-CTF-MCP-Tooling/server
sudo bash deploy/install.sh --domain ctf.example.com --email you@example.com   # public DNS name
# or, LAN / IP only:
sudo bash deploy/install.sh --self-signed
```

See [server/README.md](server/README.md) for options, operations, backups and key rotation.

### 2. Create a team

Open `https://ctf.example.com`, choose **Create a team**, and copy the two join codes shown once.
Teammates use **Join a team** with the team name + a code.

### 3. Connect your AI

```bash
# on your own machine
pipx install "git+https://github.com/FletcherMeyer/Twilight-CTF-MCP-Tooling.git#subdirectory=mcp"
# or from a clone: pipx install ./Twilight-CTF-MCP-Tooling/mcp
ctfsheet-mcp login --url https://ctf.example.com
```

Then follow [mcp/README.md](mcp/README.md) for **Claude** (Desktop / Code), **ChatGPT**
(developer-mode connector, or Codex CLI) and **Gemini** (Gemini CLI).

### 4. Work

Give your agent the challenge (screenshots, prompt, files) and say e.g.
*“Work this challenge using CTFSheet.”* or *“Go through every open challenge on CTFSheet.”*
