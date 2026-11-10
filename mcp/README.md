# ctfsheet-mcp

The MCP tool that lets an AI agent work on your team's CTFSheet. It runs **on your machine**
(separately from the server), holds only a revocable API token, and talks to the server over HTTPS.

## 1. Install & log in (all models)

Python 3.10+ required.

```bash
pipx install /path/to/ctfsheet/mcp          # recommended (isolated); or: pip install /path/to/ctfsheet/mcp
ctfsheet-mcp login --url https://ctf.example.com
#   Username: alice
#   Password: ********
# -> Logged in as alice (AB, editor) on team 'Red Rockets'.
ctfsheet-mcp whoami
```

* Self-signed server: get `selfsigned.crt` from the admin and add `--ca-cert /path/selfsigned.crt`.
* Already made a token in the web UI (**API tokens** button)? `ctfsheet-mcp login --url … --token-stdin`.
* The token is stored in `~/.config/ctfsheet/config.json` (Windows: `%APPDATA%\ctfsheet\config.json`)
  with owner-only permissions. Your password is never stored. `ctfsheet-mcp logout` revokes it.
* Alternatively pass `CTFSHEET_URL`, `CTFSHEET_TOKEN` (and `CTFSHEET_CA_CERT`) as environment variables.

Find the full path of the command for configs that need it:
`which ctfsheet-mcp` (macOS/Linux) or `where ctfsheet-mcp` (Windows, usually
`%USERPROFILE%\.local\bin\ctfsheet-mcp.exe` with pipx).

### Consent options for `serve`

| Flag | Default | Effect |
|---|---|---|
| `--agent-md ask\|allow\|deny` | `ask` | Agent asks you **once per session** before loading the team agent.md |
| `--prior-work ask\|allow\|deny` | `ask` | Agent asks you **once per session** before reading other agents'/teammates' context and solution files |

---

## 2a. Claude

### Claude Code (CLI / desktop Code tab / IDE)

```bash
claude mcp add --scope user ctfsheet -- ctfsheet-mcp serve
claude mcp list            # ctfsheet should show "connected"
```

### Claude Desktop (chat app)

Settings → Developer → **Edit Config** opens `claude_desktop_config.json`
(macOS `~/Library/Application Support/Claude/`, Windows `%APPDATA%\Claude\`). Add:

```json
{
  "mcpServers": {
    "ctfsheet": {
      "command": "/full/path/to/ctfsheet-mcp",
      "args": ["serve"]
    }
  }
}
```

Restart Claude Desktop; the tools appear under the tools (🔧) menu.

---

## 2b. ChatGPT

ChatGPT has two routes.

### Option 1 — ChatGPT app (web/desktop) via a developer-mode connector

ChatGPT only connects to **remote** MCP servers over HTTPS, so run the tool in HTTP mode and
publish it through HTTPS.

1. Start the tool in HTTP mode (keep it running, e.g. as a service or in `tmux`):

   ```bash
   ctfsheet-mcp serve --transport http --port 8765 --allowed-host mcp.example.com
   # prints:  https://<your-public-host>/mcp/<long-random-secret>
   ```

   The random path segment is the credential (ChatGPT connectors use either OAuth or no auth;
   this tool uses an unguessable URL). Keep it secret; rotate with `--rotate-secret`.

2. Expose it over HTTPS. Either:
   * **Cloudflare Tunnel** (no open ports):
     `cloudflared tunnel --url http://127.0.0.1:8765` → use the printed `*.trycloudflare.com`
     host as `--allowed-host` (restart step 1 with it), or a named tunnel for a stable hostname.
   * **nginx on a host you control**, with a certificate for `mcp.example.com`:

     ```nginx
     location /mcp/ {
         proxy_pass http://127.0.0.1:8765;
         proxy_http_version 1.1;
         proxy_set_header Host $host;
         proxy_buffering off;              # streaming responses
         proxy_read_timeout 3600s;
     }
     ```

3. In ChatGPT: **Settings → Apps & Connectors → Advanced settings → enable Developer mode**, then
   **Create** a connector: name `CTFSheet`, MCP server URL
   `https://mcp.example.com/mcp/<secret>`, authentication **No authentication**, tick
   “I trust this application”, Create. (Requires a plan that offers developer mode; menu names may
   differ slightly between ChatGPT versions.)
4. In a chat, open the **+** / tools menu → Developer mode → enable **CTFSheet**. ChatGPT asks you to
   confirm write actions; read-only tools are annotated as such.

> Anyone with the URL acts as *your* CTFSheet account, so treat it like a password and stop the
> process (or rotate the secret) when you're done.

### Option 2 — OpenAI Codex CLI (local, no tunnel)

`~/.codex/config.toml`:

```toml
[mcp_servers.ctfsheet]
command = "ctfsheet-mcp"
args = ["serve"]
```

or `codex mcp add ctfsheet -- ctfsheet-mcp serve`.

---

## 2c. Gemini (Gemini CLI)

```bash
gemini mcp add ctfsheet ctfsheet-mcp serve
```

or edit `~/.gemini/settings.json` (user) / `.gemini/settings.json` (project):

```json
{
  "mcpServers": {
    "ctfsheet": {
      "command": "ctfsheet-mcp",
      "args": ["serve"],
      "timeout": 60000
    }
  }
}
```

Run `gemini`, then `/mcp` to confirm the tools are listed. Leave `"trust"` unset so Gemini asks
before each tool call (recommended).

> Gemini in the browser (gemini.google.com) doesn't support custom MCP servers; use Gemini CLI
> (or another MCP-capable client with the same `command`/`args`).

---

## 3. Using it

Start a chat and give the agent the challenge (screenshots, prompt text, file paths), e.g.:

> *Here's a challenge from the CTF (screenshot attached, files in ~/ctf/rsa101). Use CTFSheet.*

The tool sends the agent a built-in workflow; you'll typically see it:

1. Call `ctf_whoami` and **ask you once** whether to load the team `agent.md`.
2. `ctf_search_challenges` — if the challenge exists with others' notes, **ask you once** whether it
   may read prior agents' work; otherwise **ask you for the name, category and difficulty** and
   create the row (attaching the files you gave it).
3. Work the challenge (sandbox, cracking, exploitation, scripts…), uploading its **own**
   `context.md` after important discoveries.
4. For every flag: `ctf_submit_flag` + a reproducible `solution.md` (`ctf_upload_solution`).
5. `ctf_mark_completed` with your initials (added as a verifier if someone solved it first).

Other prompts that work well: *“Work through every open Crypto challenge on CTFSheet.”*,
*“Summarise what the team has on ‘Baby SQLi’.”*, *“Update the team agent.md to say we use pwntools 4.x.”*

### Tools

| Tool | Purpose |
|---|---|
| `ctf_whoami` | identity, initials, role, consent state |
| `ctf_load_team_agent_md` | load team agent.md (one-time consent) |
| `ctf_update_team_agent_md` | replace agent.md (only when you ask) |
| `ctf_set_prior_work_permission` | record your one-time answer about reading prior work |
| `ctf_list_categories`, `ctf_list_challenges`, `ctf_search_challenges`, `ctf_get_challenge` | browse |
| `ctf_get_artifact`, `ctf_download_attachment` | read files (text, images, human solutions) / save challenge files locally |
| `ctf_create_challenge` | new row (requires your confirmed name/category/difficulty) |
| `ctf_update_challenge` | status, description, notes, points … |
| `ctf_attach_file` | upload a local challenge file to the row |
| `ctf_upload_context` | the agent's own context.md |
| `ctf_submit_flag`, `ctf_upload_solution` | answers + per-flag reproducible solution.md |
| `ctf_mark_completed` | record solver initials |
