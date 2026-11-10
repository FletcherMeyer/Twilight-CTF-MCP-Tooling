"""Local credential storage for the MCP tool.

Credentials are read from (highest priority first):
  1. environment: CTFSHEET_URL, CTFSHEET_TOKEN, CTFSHEET_CA_CERT
  2. the config file written by `ctfsheet-mcp login`
       Linux/macOS: $XDG_CONFIG_HOME/ctfsheet/config.json (default ~/.config/ctfsheet/config.json)
       Windows:     %APPDATA%\\ctfsheet\\config.json
The file is created with owner-only permissions and never contains your password,
only a revocable API token.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse


def config_path() -> Path:
    override = os.environ.get("CTFSHEET_CONFIG")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "ctfsheet" / "config.json"


@dataclass
class Config:
    url: str = ""
    token: str = ""
    ca_cert: str = ""
    username: str = ""
    http_secret: str = ""

    @property
    def verify(self) -> str | bool:
        return self.ca_cert or True


def load() -> Config:
    cfg = Config()
    p = config_path()
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        cfg = Config(**{k: v for k, v in data.items() if k in Config.__annotations__})
    cfg.url = os.environ.get("CTFSHEET_URL", cfg.url).rstrip("/")
    cfg.token = os.environ.get("CTFSHEET_TOKEN", cfg.token)
    cfg.ca_cert = os.environ.get("CTFSHEET_CA_CERT", cfg.ca_cert)
    return cfg


def save(cfg: Config) -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p.parent, 0o700)
    except OSError:
        pass
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(asdict(cfg), fh, indent=2)
    os.replace(tmp, p)
    return p


def check_url(url: str, allow_http: bool = False) -> str:
    u = urlparse(url)
    if u.scheme not in ("https", "http") or not u.netloc:
        raise SystemExit(f"Invalid server URL: {url!r} (expected https://host)")
    local = u.hostname in ("localhost", "127.0.0.1", "::1")
    if u.scheme == "http" and not (local or allow_http):
        raise SystemExit("Refusing to send credentials over plain HTTP. Use https:// "
                         "(or --allow-insecure-http for a lab network you trust).")
    return url.rstrip("/")
