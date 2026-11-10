"""Thin HTTPS client for the CTFSheet REST API."""
from __future__ import annotations

from typing import Any

import httpx

from . import __version__
from .config import Config


class CTFSheetError(RuntimeError):
    pass


class Client:
    def __init__(self, cfg: Config):
        if not cfg.url or not cfg.token:
            raise CTFSheetError("Not logged in. Run `ctfsheet-mcp login --url https://your-server` first.")
        self.cfg = cfg
        self.http = httpx.Client(
            base_url=cfg.url,
            headers={"Authorization": f"Bearer {cfg.token}", "User-Agent": f"ctfsheet-mcp/{__version__}"},
            verify=cfg.verify,
            timeout=60,
            follow_redirects=False,
        )

    def _req(self, method: str, path: str, **kw) -> Any:
        try:
            r = self.http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise CTFSheetError(f"Cannot reach CTFSheet server: {exc}") from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            if r.status_code == 401:
                detail += " — your token may be expired or revoked; run `ctfsheet-mcp login` again."
            raise CTFSheetError(f"{r.status_code}: {detail}")
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return r.content

    def get(self, path: str, **params) -> Any:
        return self._req("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, body: dict | None = None) -> Any:
        return self._req("POST", path, json=body or {})

    def patch(self, path: str, body: dict) -> Any:
        return self._req("PATCH", path, json=body)

    def put(self, path: str, body: dict) -> Any:
        return self._req("PUT", path, json=body)

    def delete(self, path: str, **params) -> Any:
        return self._req("DELETE", path, params={k: v for k, v in params.items() if v is not None})


def token_login(url: str, username: str, password: str, label: str, verify: str | bool) -> dict:
    with httpx.Client(base_url=url, verify=verify, timeout=30) as http:
        r = http.post("/api/auth/token", json={"username": username, "password": password, "label": label})
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise CTFSheetError(f"Login failed ({r.status_code}): {detail}")
        return r.json()
