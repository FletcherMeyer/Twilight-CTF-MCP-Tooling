"""ctfsheet-mcp command line.

  ctfsheet-mcp login  --url https://ctf.example.com [--username alice] [--ca-cert server.crt]
  ctfsheet-mcp login  --url https://ctf.example.com --token-stdin      # paste a token made in the web UI
  ctfsheet-mcp whoami
  ctfsheet-mcp serve                                                     # stdio (Claude, Gemini, Codex)
  ctfsheet-mcp serve --transport http --port 8765                        # remote MCP (ChatGPT connectors)
  ctfsheet-mcp logout                                                    # revokes the token server-side
"""
from __future__ import annotations

import argparse
import getpass
import json
import secrets
import socket
import sys

from . import __version__
from .client import Client, CTFSheetError, token_login
from .config import Config, check_url, config_path, load, save


def cmd_login(a: argparse.Namespace) -> int:
    url = check_url(a.url, a.allow_insecure_http)
    cfg = load()
    cfg.url, cfg.ca_cert = url, a.ca_cert or ""
    if a.token_stdin:
        print("Paste API token, then press Enter:", file=sys.stderr)
        cfg.token = sys.stdin.readline().strip()
    else:
        username = a.username or input("Username: ").strip()
        password = getpass.getpass("Password: ")
        label = a.label or f"mcp@{socket.gethostname()}"[:80]
        try:
            tok = token_login(url, username, password, label, cfg.verify)
        except CTFSheetError as exc:
            print(exc, file=sys.stderr)
            return 1
        cfg.token, cfg.username = tok["token"], username
    try:
        me = Client(cfg).get("/api/me")
    except CTFSheetError as exc:
        print(f"Token check failed: {exc}", file=sys.stderr)
        return 1
    path = save(cfg)
    print(f"Logged in as {me['username']} ({me['initials']}, {me['role']}) on team '{me['team']}'.")
    print(f"Token saved to {path} (owner-only permissions).")
    return 0


def cmd_whoami(_: argparse.Namespace) -> int:
    try:
        me = Client(load()).get("/api/me")
    except CTFSheetError as exc:
        print(exc, file=sys.stderr)
        return 1
    me.pop("csrf", None)
    print(json.dumps(me, indent=2))
    return 0


def cmd_logout(_: argparse.Namespace) -> int:
    cfg = load()
    try:
        Client(cfg).delete("/api/tokens/current")
        print("Token revoked on the server.")
    except CTFSheetError as exc:
        print(f"Could not revoke token remotely: {exc}", file=sys.stderr)
    cfg.token = ""
    save(cfg)
    print(f"Token removed from {config_path()}.")
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    from .server import build_server

    cfg = load()
    if not cfg.url or not cfg.token:
        print("Not logged in. Run: ctfsheet-mcp login --url https://your-server", file=sys.stderr)
        return 1
    check_url(cfg.url, a.allow_insecure_http)

    if a.transport == "stdio":
        mcp = build_server(cfg, agent_md=a.agent_md, prior_work=a.prior_work)
        mcp.run("stdio")
        return 0

    # Remote (streamable HTTP) mode, e.g. for ChatGPT connectors. The endpoint path contains
    # a long random secret so the URL itself acts as a bearer credential.
    secret = a.secret or cfg.http_secret
    if not secret:
        secret = secrets.token_urlsafe(32)
        cfg.http_secret = secret
        save(cfg)
    path = f"/mcp/{secret}"
    allowed = [h.strip() for h in (a.allowed_host or []) if h.strip()]
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: binding to a non-loopback address. Put a TLS reverse proxy or tunnel in front.",
              file=sys.stderr)
    if not allowed:
        print("NOTE: no --allowed-host given; only requests with Host: localhost/127.0.0.1 are accepted. "
              "Behind a tunnel or reverse proxy pass --allowed-host <public hostname>.", file=sys.stderr)
    hosts = [*allowed, *(f"{h}:*" for h in allowed), "127.0.0.1:*", "localhost:*"] if allowed else None
    mcp = build_server(cfg, agent_md=a.agent_md, prior_work=a.prior_work, host=a.host, port=a.port,
                       path=path, allowed_hosts=hosts)
    print(f"Serving MCP over streamable HTTP on http://{a.host}:{a.port}{path}", file=sys.stderr)
    print("Expose it via HTTPS and give ChatGPT: https://<your-public-host>" + path, file=sys.stderr)
    print("Treat that URL like a password. Rotate with: ctfsheet-mcp serve --transport http --rotate-secret",
          file=sys.stderr)
    mcp.run("streamable-http")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ctfsheet-mcp", description="CTFSheet MCP tool")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("login", help="authenticate and store an API token")
    lp.add_argument("--url", required=True, help="server URL, e.g. https://ctf.example.com")
    lp.add_argument("--username")
    lp.add_argument("--label", help="token label shown in the web UI")
    lp.add_argument("--ca-cert", help="PEM file to trust (for self-signed servers)")
    lp.add_argument("--token-stdin", action="store_true", help="read an existing API token from stdin")
    lp.add_argument("--allow-insecure-http", action="store_true")
    lp.set_defaults(fn=cmd_login)

    sub.add_parser("whoami").set_defaults(fn=cmd_whoami)
    sub.add_parser("logout", help="revoke and forget the stored token").set_defaults(fn=cmd_logout)

    sp = sub.add_parser("serve", help="run the MCP server")
    sp.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--secret", help="secret URL path segment for http mode (default: generated & saved)")
    sp.add_argument("--rotate-secret", action="store_true", help="generate a new http secret path")
    sp.add_argument("--allowed-host", action="append",
                    help="public Host header(s) accepted in http mode, e.g. mcp.example.com (DNS-rebinding guard)")
    sp.add_argument("--agent-md", choices=["ask", "allow", "deny"], default="ask",
                    help="team agent.md loading: ask the user once per session (default), always, or never")
    sp.add_argument("--prior-work", choices=["ask", "allow", "deny"], default="ask",
                    help="reading other agents' context/solutions: ask once per session (default), always, never")
    sp.add_argument("--allow-insecure-http", action="store_true")
    sp.set_defaults(fn=cmd_serve)

    a = ap.parse_args(argv)
    if getattr(a, "rotate_secret", False):
        a.secret = secrets.token_urlsafe(32)
        cfg = load()
        cfg.http_secret = a.secret
        save(cfg)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
