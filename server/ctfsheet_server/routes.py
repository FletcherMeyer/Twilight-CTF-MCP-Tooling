"""REST API. Every data route is scoped to the caller's team."""
from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
import os
import re
import sqlite3
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .auth import (
    Principal,
    auth_limiter,
    client_ip,
    create_session,
    current_user,
    destroy_session,
    issue_api_token,
    require_editor,
    require_owner,
)
from .config import ARTIFACT_KINDS, DIFFICULTIES, STATUSES, settings
from .crypto import box, burn_time, hash_secret, new_join_code, verify_secret
from .db import audit, bump_revision, get_db, now

router = APIRouter(prefix="/api")

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
INITIALS_RE = re.compile(r"^[A-Z0-9]{1,4}$")
SAFE_INLINE_IMAGES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
TEXT_TYPES = {"application/json", "application/xml", "application/x-sh", "application/javascript",
              "application/x-python", "application/x-yaml", "application/toml"}
MAX_AGENT_MD = 256 * 1024


# --------------------------------------------------------------------------- helpers
def _initials(raw: str) -> str:
    val = re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()
    if not INITIALS_RE.match(val):
        raise HTTPException(422, "Initials must be 1-4 letters/digits")
    return val


def _check_password(pw: str) -> None:
    if len(pw) < settings.min_password_len:
        raise HTTPException(422, f"Password must be at least {settings.min_password_len} characters")
    if len(pw) > 256:
        raise HTTPException(422, "Password too long")


def _check_username(name: str) -> str:
    if not USERNAME_RE.match(name or ""):
        raise HTTPException(422, "Username must be 3-32 chars: letters, digits, _ . -")
    return name


def _clean_name(raw: str, what: str, max_len: int = 120) -> str:
    val = " ".join((raw or "").split())
    if not val or len(val) > max_len:
        raise HTTPException(422, f"{what} must be 1-{max_len} characters")
    return val


def _clean_filename(raw: str, default: str) -> str:
    name = os.path.basename((raw or "").replace("\\", "/")).strip()
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name)[:120].strip(" .")
    return name or default


def _difficulty(raw: str) -> str:
    val = (raw or "").strip().lower()
    if val not in DIFFICULTIES:
        raise HTTPException(422, f"difficulty must be one of {DIFFICULTIES}")
    return val


def _is_text(content_type: str) -> bool:
    return content_type.startswith("text/") or content_type in TEXT_TYPES


def _guess_type(filename: str, data: bytes) -> str:
    ctype = mimetypes.guess_type(filename)[0]
    if filename.lower().endswith(".md"):
        ctype = "text/markdown"
    if not ctype:
        try:
            data[:65536].decode("utf-8")
            ctype = "text/plain"
        except UnicodeDecodeError:
            ctype = "application/octet-stream"
    return ctype


def _b64(data: str, what: str) -> bytes:
    if data.startswith("data:"):
        data = data.split(",", 1)[-1]
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, f"{what} is not valid base64")


def _challenge(conn: sqlite3.Connection, p: Principal, cid: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT c.*, cat.name AS category FROM challenges c JOIN categories cat ON cat.id = c.category_id "
        "WHERE c.id = ? AND c.team_id = ?",
        (cid, p.team_id),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Challenge not found")
    return row


def _category(conn: sqlite3.Connection, p: Principal, ref: str | int, create: bool = False) -> sqlite3.Row:
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        row = conn.execute("SELECT * FROM categories WHERE id = ? AND team_id = ?", (int(ref), p.team_id)).fetchone()
    else:
        name = _clean_name(str(ref), "Category", 60)
        row = conn.execute("SELECT * FROM categories WHERE name = ? AND team_id = ?", (name, p.team_id)).fetchone()
        if not row and create:
            pos = conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM categories WHERE team_id = ?",
                               (p.team_id,)).fetchone()[0]
            cur = conn.execute("INSERT INTO categories(team_id, name, position) VALUES (?,?,?)", (p.team_id, name, pos))
            row = conn.execute("SELECT * FROM categories WHERE id = ?", (cur.lastrowid,)).fetchone()
    if not row:
        raise HTTPException(404, f"Category '{ref}' not found")
    return row


def _artifact_meta(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"], "challenge_id": r["challenge_id"], "parent_id": r["parent_id"], "kind": r["kind"],
        "flag_id": r["flag_id"], "title": r["title"], "filename": r["filename"],
        "content_type": r["content_type"], "size": r["size"], "sha256": r["sha256"],
        "source": r["source"], "agent_name": r["agent_name"], "author_initials": r["author_initials"],
        "author_id": r["author_id"], "created_at": r["created_at"],
    }


def _store_artifact(conn: sqlite3.Connection, p: Principal, cid: int, *, kind: str, filename: str,
                    data: bytes, content_type: str, source: str, title: str | None = None,
                    flag_id: int | None = None, agent_name: str | None = None,
                    parent_id: int | None = None) -> int:
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(413, f"File exceeds {settings.max_upload_mb} MB")
    if flag_id is not None:
        ok = conn.execute("SELECT 1 FROM flags WHERE id = ? AND challenge_id = ?", (flag_id, cid)).fetchone()
        if not ok:
            raise HTTPException(422, "flag_id does not belong to this challenge")
    cur = conn.execute(
        "INSERT INTO artifacts(challenge_id, parent_id, kind, flag_id, title, filename, content_type, size, sha256,"
        " content, source, agent_name, author_id, author_initials, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, parent_id, kind, flag_id, (title or "")[:200] or None, filename, content_type[:100], len(data),
         hashlib.sha256(data).hexdigest(), box().enc(data), source, (agent_name or "")[:60] or None,
         p.user_id, p.initials, now()),
    )
    return cur.lastrowid


def _touch(conn: sqlite3.Connection, p: Principal, cid: int | None = None) -> None:
    if cid is not None:
        conn.execute("UPDATE challenges SET updated_at = ? WHERE id = ?", (now(), cid))
    bump_revision(conn, p.team_id)


def _challenge_bundle(conn: sqlite3.Connection, team_id: int, ids: list[int] | None = None) -> dict[int, dict]:
    """Flags, completions and artifact metadata for many challenges at once."""
    where, args = "c.team_id = ?", [team_id]
    if ids is not None:
        if not ids:
            return {}
        where += f" AND c.id IN ({','.join('?' * len(ids))})"
        args += ids
    out: dict[int, dict] = {}
    for r in conn.execute(f"SELECT c.id FROM challenges c WHERE {where}", args):
        out[r["id"]] = {"flags": [], "completions": [], "artifacts": []}
    for r in conn.execute(
        f"SELECT f.* FROM flags f JOIN challenges c ON c.id = f.challenge_id WHERE {where} ORDER BY f.id", args
    ):
        out[r["challenge_id"]]["flags"].append({
            "id": r["id"], "label": r["label"], "value": box().dec_str(r["value"]),
            "submitted_by": r["submitted_by"], "created_at": r["created_at"],
        })
    for r in conn.execute(
        f"SELECT m.* FROM completions m JOIN challenges c ON c.id = m.challenge_id WHERE {where} ORDER BY m.created_at, m.id",
        args,
    ):
        lst = out[r["challenge_id"]]["completions"]
        lst.append({"initials": r["initials"], "first": not lst, "created_at": r["created_at"]})
    for r in conn.execute(
        f"SELECT a.id, a.challenge_id, a.parent_id, a.kind, a.flag_id, a.title, a.filename, a.content_type, a.size,"
        f" a.sha256, a.source, a.agent_name, a.author_initials, a.author_id, a.created_at"
        f" FROM artifacts a JOIN challenges c ON c.id = a.challenge_id WHERE {where} ORDER BY a.id",
        args,
    ):
        out[r["challenge_id"]]["artifacts"].append(_artifact_meta(r))
    # nest pasted images under their human solution
    for bundle in out.values():
        parents = {a["id"]: a for a in bundle["artifacts"]}
        top = []
        for a in bundle["artifacts"]:
            if a["parent_id"] and a["parent_id"] in parents:
                parents[a["parent_id"]].setdefault("images", []).append(a)
            else:
                top.append(a)
        bundle["artifacts"] = top
    return out


def _challenge_dict(r: sqlite3.Row, bundle: dict, full: bool = True) -> dict:
    desc = box().dec_str(r["description"]) or ""
    d = {
        "id": r["id"], "category_id": r["category_id"], "category": r["category"], "name": r["name"],
        "difficulty": r["difficulty"], "points": r["points"], "status": r["status"],
        "created_by": r["created_by"], "created_at": r["created_at"], "updated_at": r["updated_at"],
        "solved_by": [c["initials"] for c in bundle["completions"]],
        "completions": bundle["completions"],
    }
    if full:
        d.update(description=desc, notes=box().dec_str(r["notes"]) or "", flags=bundle["flags"],
                 artifacts=bundle["artifacts"])
    else:
        counts: dict[str, int] = {}
        for a in bundle["artifacts"]:
            counts[a["kind"]] = counts.get(a["kind"], 0) + 1
        d.update(description_preview=desc[:240], flag_count=len(bundle["flags"]), artifact_counts=counts)
    return d


# --------------------------------------------------------------------------- auth / onboarding
class CreateTeamIn(BaseModel):
    team_name: str = Field(min_length=2, max_length=64)
    username: str
    password: str
    initials: str
    creation_code: str = ""


class JoinTeamIn(BaseModel):
    team_name: str = Field(max_length=64)
    join_code: str = Field(max_length=64)
    username: str
    password: str
    initials: str


class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class TokenLoginIn(LoginIn):
    label: str = Field(default="mcp", max_length=80)


def _new_user(conn, team_id: int, body, role: str) -> int:
    username = _check_username(body.username)
    _check_password(body.password)
    initials = _initials(body.initials)
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise HTTPException(409, "Username already taken")
    cur = conn.execute(
        "INSERT INTO users(team_id, username, pw_hash, initials, role, created_at) VALUES (?,?,?,?,?,?)",
        (team_id, username, hash_secret(body.password), initials, role, now()),
    )
    return cur.lastrowid


@router.get("/auth/config")
def auth_config():
    return {
        "allow_team_creation": settings.allow_team_creation,
        "creation_code_required": bool(settings.team_creation_code),
        "min_password_len": settings.min_password_len,
    }


@router.post("/auth/create-team")
def create_team(body: CreateTeamIn, request: Request, response: Response, conn=Depends(get_db)):
    auth_limiter.check(f"ip:{client_ip(request)}")
    if not settings.allow_team_creation:
        raise HTTPException(403, "Team creation is disabled on this server")
    if settings.team_creation_code and not _consteq(body.creation_code, settings.team_creation_code):
        raise HTTPException(403, "Invalid team creation code")
    name = _clean_name(body.team_name, "Team name", 64)
    if conn.execute("SELECT 1 FROM teams WHERE name = ?", (name,)).fetchone():
        raise HTTPException(409, "Team name already taken")
    editor_code, viewer_code = new_join_code(), new_join_code()
    cur = conn.execute(
        "INSERT INTO teams(name, editor_code_hash, viewer_code_hash, created_at) VALUES (?,?,?,?)",
        (name, hash_secret(editor_code), hash_secret(viewer_code), now()),
    )
    team_id = cur.lastrowid
    uid = _new_user(conn, team_id, body, "owner")
    for pos, cat in enumerate(["Web", "Crypto", "Pwn", "Reverse", "Forensics", "OSINT", "Misc"], 1):
        conn.execute("INSERT INTO categories(team_id, name, position) VALUES (?,?,?)", (team_id, cat, pos))
    csrf = create_session(conn, response, uid)
    audit(conn, team_id, uid, "team.create", name, client_ip(request))
    conn.commit()
    return {"team": name, "editor_join_code": editor_code, "viewer_join_code": viewer_code, "csrf": csrf,
            "note": "Join codes are shown ONCE. Store them in a password manager; the owner can rotate them."}


def _consteq(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())


@router.post("/auth/join")
def join_team(body: JoinTeamIn, request: Request, response: Response, conn=Depends(get_db)):
    auth_limiter.check(f"ip:{client_ip(request)}", f"team:{body.team_name.lower()}")
    team = conn.execute("SELECT * FROM teams WHERE name = ?", (" ".join(body.team_name.split()),)).fetchone()
    code = body.join_code.strip().upper()
    role = None
    if team:
        if verify_secret(team["editor_code_hash"], code):
            role = "editor"
        elif verify_secret(team["viewer_code_hash"], code):
            role = "viewer"
    else:
        burn_time()
    if not role:
        raise HTTPException(403, "Unknown team or invalid join code")
    uid = _new_user(conn, team["id"], body, role)
    csrf = create_session(conn, response, uid)
    audit(conn, team["id"], uid, "team.join", role, client_ip(request))
    bump_revision(conn, team["id"])
    conn.commit()
    return {"team": team["name"], "role": role, "csrf": csrf}


def _verify_login(conn, body: LoginIn, request: Request) -> sqlite3.Row:
    auth_limiter.check(f"ip:{client_ip(request)}", f"user:{body.username.lower()}")
    user = conn.execute("SELECT * FROM users WHERE username = ? AND disabled = 0", (body.username,)).fetchone()
    if not user:
        burn_time()
        raise HTTPException(401, "Invalid username or password")
    if not verify_secret(user["pw_hash"], body.password):
        audit(conn, user["team_id"], user["id"], "auth.fail", "", client_ip(request))
        conn.commit()
        raise HTTPException(401, "Invalid username or password")
    return user


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response, conn=Depends(get_db)):
    user = _verify_login(conn, body, request)
    csrf = create_session(conn, response, user["id"])
    audit(conn, user["team_id"], user["id"], "auth.login", "web", client_ip(request))
    conn.commit()
    return {"ok": True, "csrf": csrf}


@router.post("/auth/token")
def login_token(body: TokenLoginIn, request: Request, conn=Depends(get_db)):
    """Exchange username/password for an API token (used by `ctfsheet-mcp login`)."""
    user = _verify_login(conn, body, request)
    tok = issue_api_token(conn, user["id"], body.label)
    audit(conn, user["team_id"], user["id"], "token.create", body.label, client_ip(request))
    conn.commit()
    return tok


@router.post("/auth/logout")
def logout(request: Request, response: Response, conn=Depends(get_db)):
    destroy_session(conn, request, response)
    conn.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- me / tokens
class MeUpdateIn(BaseModel):
    initials: Optional[str] = None
    current_password: Optional[str] = None
    new_password: Optional[str] = None


@router.get("/me")
def me(p: Principal = Depends(current_user)):
    return {"user_id": p.user_id, "username": p.username, "initials": p.initials, "role": p.role,
            "team": p.team_name, "team_id": p.team_id, "via": p.via, "csrf": p.csrf,
            "difficulties": DIFFICULTIES, "statuses": STATUSES, "max_upload_mb": settings.max_upload_mb}


@router.patch("/me")
def update_me(body: MeUpdateIn, request: Request, p: Principal = Depends(current_user), conn=Depends(get_db)):
    if body.initials is not None:
        conn.execute("UPDATE users SET initials = ? WHERE id = ?", (_initials(body.initials), p.user_id))
    if body.new_password is not None:
        if p.via != "session":
            raise HTTPException(403, "Change passwords from the web UI")
        row = conn.execute("SELECT pw_hash FROM users WHERE id = ?", (p.user_id,)).fetchone()
        if not verify_secret(row["pw_hash"], body.current_password or ""):
            raise HTTPException(403, "Current password is incorrect")
        _check_password(body.new_password)
        conn.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (hash_secret(body.new_password), p.user_id))
        audit(conn, p.team_id, p.user_id, "auth.password_change", "", client_ip(request))
    conn.commit()
    return {"ok": True}


class TokenIn(BaseModel):
    label: str = Field(default="mcp", max_length=80)


@router.get("/tokens")
def list_tokens(p: Principal = Depends(current_user), conn=Depends(get_db)):
    rows = conn.execute(
        "SELECT id, label, created_at, last_used_at, expires_at FROM api_tokens WHERE user_id = ? ORDER BY id DESC",
        (p.user_id,),
    ).fetchall()
    return [dict(r) | {"current": r["id"] == p.token_id} for r in rows]


@router.post("/tokens")
def create_token(body: TokenIn, request: Request, p: Principal = Depends(current_user), conn=Depends(get_db)):
    if p.via != "session":
        raise HTTPException(403, "Create tokens from the web UI or with `ctfsheet-mcp login`")
    tok = issue_api_token(conn, p.user_id, body.label)
    audit(conn, p.team_id, p.user_id, "token.create", body.label, client_ip(request))
    conn.commit()
    return tok


@router.delete("/tokens/current")
def revoke_current_token(p: Principal = Depends(current_user), conn=Depends(get_db)):
    if p.token_id is None:
        raise HTTPException(400, "Not authenticated with a token")
    conn.execute("DELETE FROM api_tokens WHERE id = ?", (p.token_id,))
    conn.commit()
    return {"ok": True}


@router.delete("/tokens/{token_id}")
def revoke_token(token_id: int, p: Principal = Depends(current_user), conn=Depends(get_db)):
    conn.execute("DELETE FROM api_tokens WHERE id = ? AND user_id = ?", (token_id, p.user_id))
    conn.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- team
@router.get("/team")
def get_team(p: Principal = Depends(current_user), conn=Depends(get_db)):
    team = conn.execute("SELECT name, revision, created_at, agent_md_updated_at FROM teams WHERE id = ?",
                        (p.team_id,)).fetchone()
    members = conn.execute(
        "SELECT id, username, initials, role, disabled, created_at FROM users WHERE team_id = ? ORDER BY id",
        (p.team_id,),
    ).fetchall()
    return {**dict(team), "my_role": p.role, "members": [dict(m) for m in members]}


@router.get("/team/revision")
def team_revision(p: Principal = Depends(current_user), conn=Depends(get_db)):
    return {"revision": conn.execute("SELECT revision FROM teams WHERE id = ?", (p.team_id,)).fetchone()[0]}


@router.post("/team/rotate-codes")
def rotate_codes(request: Request, p: Principal = Depends(require_owner), conn=Depends(get_db)):
    editor_code, viewer_code = new_join_code(), new_join_code()
    conn.execute("UPDATE teams SET editor_code_hash = ?, viewer_code_hash = ? WHERE id = ?",
                 (hash_secret(editor_code), hash_secret(viewer_code), p.team_id))
    audit(conn, p.team_id, p.user_id, "team.rotate_codes", "", client_ip(request))
    conn.commit()
    return {"editor_join_code": editor_code, "viewer_join_code": viewer_code}


class MemberIn(BaseModel):
    role: Optional[Literal["owner", "editor", "viewer"]] = None
    disabled: Optional[bool] = None


@router.patch("/team/members/{uid}")
def update_member(uid: int, body: MemberIn, request: Request, p: Principal = Depends(require_owner),
                  conn=Depends(get_db)):
    target = conn.execute("SELECT * FROM users WHERE id = ? AND team_id = ?", (uid, p.team_id)).fetchone()
    if not target:
        raise HTTPException(404, "Member not found")
    if uid == p.user_id:
        owners = conn.execute("SELECT COUNT(*) FROM users WHERE team_id = ? AND role = 'owner' AND disabled = 0",
                              (p.team_id,)).fetchone()[0]
        if owners <= 1 and (body.role not in (None, "owner") or body.disabled):
            raise HTTPException(400, "Promote another owner first")
    if body.role:
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (body.role, uid))
    if body.disabled is not None:
        conn.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(body.disabled), uid))
        if body.disabled:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM api_tokens WHERE user_id = ?", (uid,))
    audit(conn, p.team_id, p.user_id, "team.member_update", f"{target['username']} {body.model_dump()}",
          client_ip(request))
    bump_revision(conn, p.team_id)
    conn.commit()
    return {"ok": True}


@router.get("/team/audit")
def team_audit(p: Principal = Depends(require_owner), conn=Depends(get_db)):
    rows = conn.execute(
        "SELECT a.created_at, a.action, a.detail, a.ip, u.username FROM audit a LEFT JOIN users u ON u.id = a.user_id"
        " WHERE a.team_id = ? ORDER BY a.id DESC LIMIT 300", (p.team_id,)).fetchall()
    return [dict(r) for r in rows]


class AgentMdIn(BaseModel):
    content: str


@router.get("/team/agent-md")
def get_agent_md(p: Principal = Depends(current_user), conn=Depends(get_db)):
    r = conn.execute("SELECT agent_md, agent_md_updated_at, agent_md_updated_by FROM teams WHERE id = ?",
                     (p.team_id,)).fetchone()
    return {"content": box().dec_str(r["agent_md"]) or "", "updated_at": r["agent_md_updated_at"],
            "updated_by": r["agent_md_updated_by"]}


@router.put("/team/agent-md")
def put_agent_md(body: AgentMdIn, request: Request, p: Principal = Depends(require_editor), conn=Depends(get_db)):
    if len(body.content.encode()) > MAX_AGENT_MD:
        raise HTTPException(413, "agent.md is limited to 256 KB")
    conn.execute("UPDATE teams SET agent_md = ?, agent_md_updated_at = ?, agent_md_updated_by = ? WHERE id = ?",
                 (box().enc(body.content), now(), p.initials, p.team_id))
    audit(conn, p.team_id, p.user_id, "team.agent_md", f"{len(body.content)} chars", client_ip(request))
    bump_revision(conn, p.team_id)
    conn.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- categories
class CategoryIn(BaseModel):
    name: Optional[str] = None
    position: Optional[int] = None


@router.get("/categories")
def list_categories(p: Principal = Depends(current_user), conn=Depends(get_db)):
    rows = conn.execute(
        "SELECT cat.id, cat.name, cat.position, COUNT(c.id) AS challenges,"
        " SUM(CASE WHEN c.status = 'solved' THEN 1 ELSE 0 END) AS solved"
        " FROM categories cat LEFT JOIN challenges c ON c.category_id = cat.id"
        " WHERE cat.team_id = ? GROUP BY cat.id ORDER BY cat.position, cat.name", (p.team_id,)).fetchall()
    return [dict(r) | {"solved": r["solved"] or 0} for r in rows]


@router.post("/categories")
def create_category(body: CategoryIn, p: Principal = Depends(require_editor), conn=Depends(get_db)):
    name = _clean_name(body.name or "", "Category", 60)
    if conn.execute("SELECT 1 FROM categories WHERE team_id = ? AND name = ?", (p.team_id, name)).fetchone():
        raise HTTPException(409, "Category already exists")
    row = _category(conn, p, name, create=True)
    _touch(conn, p)
    conn.commit()
    return dict(row)


@router.patch("/categories/{cat_id}")
def update_category(cat_id: int, body: CategoryIn, p: Principal = Depends(require_editor), conn=Depends(get_db)):
    _category(conn, p, cat_id)
    if body.name is not None:
        conn.execute("UPDATE categories SET name = ? WHERE id = ?", (_clean_name(body.name, "Category", 60), cat_id))
    if body.position is not None:
        conn.execute("UPDATE categories SET position = ? WHERE id = ?", (body.position, cat_id))
    _touch(conn, p)
    conn.commit()
    return {"ok": True}


@router.delete("/categories/{cat_id}")
def delete_category(cat_id: int, force: bool = False, p: Principal = Depends(require_editor), conn=Depends(get_db)):
    _category(conn, p, cat_id)
    n = conn.execute("SELECT COUNT(*) FROM challenges WHERE category_id = ?", (cat_id,)).fetchone()[0]
    if n and not (force and p.role == "owner"):
        raise HTTPException(409, f"Category has {n} challenges; only the owner can force-delete it")
    conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
    _touch(conn, p)
    conn.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- challenges
class ChallengeIn(BaseModel):
    name: str
    category: str
    difficulty: str
    points: Optional[int] = Field(default=None, ge=0, le=100000)
    description: str = Field(default="", max_length=100_000)
    create_category: bool = False


class ChallengePatch(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    difficulty: Optional[str] = None
    points: Optional[int] = Field(default=None, ge=0, le=100000)
    status: Optional[str] = None
    description: Optional[str] = Field(default=None, max_length=100_000)
    notes: Optional[str] = Field(default=None, max_length=100_000)


@router.get("/sheet")
def sheet(p: Principal = Depends(current_user), conn=Depends(get_db)):
    """Everything the spreadsheet UI needs, in one call."""
    team = conn.execute("SELECT name, revision FROM teams WHERE id = ?", (p.team_id,)).fetchone()
    cats = [dict(r) | {"challenges": []} for r in conn.execute(
        "SELECT id, name, position FROM categories WHERE team_id = ? ORDER BY position, name", (p.team_id,))]
    by_id = {c["id"]: c for c in cats}
    bundles = _challenge_bundle(conn, p.team_id)
    for r in conn.execute(
        "SELECT c.*, cat.name AS category FROM challenges c JOIN categories cat ON cat.id = c.category_id"
        " WHERE c.team_id = ? ORDER BY c.points IS NULL, c.points, c.name", (p.team_id,)):
        by_id[r["category_id"]]["challenges"].append(_challenge_dict(r, bundles[r["id"]]))
    return {"team": team["name"], "revision": team["revision"], "difficulties": DIFFICULTIES, "categories": cats}


@router.get("/challenges")
def list_challenges(category: Optional[str] = None, difficulty: Optional[str] = None, status: Optional[str] = None,
                    q: Optional[str] = None, p: Principal = Depends(current_user), conn=Depends(get_db)):
    sql = ("SELECT c.*, cat.name AS category FROM challenges c JOIN categories cat ON cat.id = c.category_id"
           " WHERE c.team_id = ?")
    args: list = [p.team_id]
    if category:
        sql += " AND (cat.name = ? OR cat.id = ?)"
        args += [category, category]
    if difficulty:
        sql += " AND c.difficulty = ?"
        args.append(difficulty.lower())
    if status:
        sql += " AND c.status = ?"
        args.append(status.lower())
    rows = conn.execute(sql + " ORDER BY cat.position, c.difficulty, c.name", args).fetchall()
    bundles = _challenge_bundle(conn, p.team_id, [r["id"] for r in rows])
    out = [_challenge_dict(r, bundles[r["id"]], full=False) for r in rows]
    if q:
        terms = [t for t in q.lower().split() if t]
        out = [c for c in out if all(
            t in f"{c['name']} {c['category']} {c['description_preview']}".lower() for t in terms)]
    return out


@router.post("/challenges")
def create_challenge(body: ChallengeIn, request: Request, p: Principal = Depends(require_editor),
                     conn=Depends(get_db)):
    name = _clean_name(body.name, "Challenge name")
    cat = _category(conn, p, body.category, create=body.create_category)
    if conn.execute("SELECT 1 FROM challenges WHERE category_id = ? AND name = ?", (cat["id"], name)).fetchone():
        raise HTTPException(409, f"'{name}' already exists in {cat['name']}")
    cur = conn.execute(
        "INSERT INTO challenges(team_id, category_id, name, difficulty, points, status, description, created_by,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (p.team_id, cat["id"], name, _difficulty(body.difficulty), body.points, "open",
         box().enc(body.description), p.initials, now(), now()))
    audit(conn, p.team_id, p.user_id, "challenge.create", f"{cat['name']}/{name}", client_ip(request))
    _touch(conn, p)
    conn.commit()
    return get_challenge(cur.lastrowid, p, conn)


@router.get("/challenges/{cid}")
def get_challenge(cid: int, p: Principal = Depends(current_user), conn=Depends(get_db)):
    r = _challenge(conn, p, cid)
    return _challenge_dict(r, _challenge_bundle(conn, p.team_id, [cid])[cid])


@router.patch("/challenges/{cid}")
def update_challenge(cid: int, body: ChallengePatch, p: Principal = Depends(require_editor), conn=Depends(get_db)):
    r = _challenge(conn, p, cid)
    sets: dict = {}
    if body.name is not None:
        sets["name"] = _clean_name(body.name, "Challenge name")
    if body.category is not None:
        sets["category_id"] = _category(conn, p, body.category)["id"]
    if body.difficulty is not None:
        sets["difficulty"] = _difficulty(body.difficulty)
    if body.points is not None:
        sets["points"] = body.points
    if body.status is not None:
        if body.status not in STATUSES:
            raise HTTPException(422, f"status must be one of {STATUSES}")
        sets["status"] = body.status
    if body.description is not None:
        sets["description"] = box().enc(body.description)
    if body.notes is not None:
        sets["notes"] = box().enc(body.notes)
    if sets:
        try:
            conn.execute(f"UPDATE challenges SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?",
                         (*sets.values(), r["id"]))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "A challenge with that name already exists in that category")
        _touch(conn, p, cid)
        conn.commit()
    return get_challenge(cid, p, conn)


@router.delete("/challenges/{cid}")
def delete_challenge(cid: int, request: Request, p: Principal = Depends(require_editor), conn=Depends(get_db)):
    r = _challenge(conn, p, cid)
    if p.role != "owner" and r["created_by"] != p.initials:
        raise HTTPException(403, "Only the creator or the team owner can delete a challenge")
    conn.execute("DELETE FROM challenges WHERE id = ?", (cid,))
    audit(conn, p.team_id, p.user_id, "challenge.delete", r["name"], client_ip(request))
    _touch(conn, p)
    conn.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- flags & completion
class FlagIn(BaseModel):
    label: str = Field(default="flag", max_length=80)
    value: str = Field(min_length=1, max_length=4000)


@router.post("/challenges/{cid}/flags")
def add_flag(cid: int, body: FlagIn, request: Request, p: Principal = Depends(require_editor), conn=Depends(get_db)):
    _challenge(conn, p, cid)
    cur = conn.execute(
        "INSERT INTO flags(challenge_id, label, value, submitted_by, user_id, created_at) VALUES (?,?,?,?,?,?)",
        (cid, _clean_name(body.label, "Flag label", 80), box().enc(body.value.strip()), p.initials, p.user_id, now()))
    audit(conn, p.team_id, p.user_id, "flag.add", f"challenge {cid}", client_ip(request))
    _touch(conn, p, cid)
    conn.commit()
    return {"id": cur.lastrowid}


@router.delete("/flags/{flag_id}")
def delete_flag(flag_id: int, p: Principal = Depends(require_editor), conn=Depends(get_db)):
    row = conn.execute("SELECT f.*, c.team_id FROM flags f JOIN challenges c ON c.id = f.challenge_id WHERE f.id = ?",
                       (flag_id,)).fetchone()
    if not row or row["team_id"] != p.team_id:
        raise HTTPException(404, "Flag not found")
    if p.role != "owner" and row["user_id"] != p.user_id:
        raise HTTPException(403, "Only the submitter or the owner can delete a flag")
    conn.execute("DELETE FROM flags WHERE id = ?", (flag_id,))
    _touch(conn, p, row["challenge_id"])
    conn.commit()
    return {"ok": True}


class CompleteIn(BaseModel):
    initials: Optional[str] = None


@router.post("/challenges/{cid}/complete")
def mark_complete(cid: int, body: CompleteIn, request: Request, p: Principal = Depends(require_editor),
                  conn=Depends(get_db)):
    _challenge(conn, p, cid)
    initials = _initials(body.initials) if body.initials else p.initials
    existing = conn.execute("SELECT COUNT(*) FROM completions WHERE challenge_id = ?", (cid,)).fetchone()[0]
    try:
        conn.execute("INSERT INTO completions(challenge_id, user_id, initials, created_at) VALUES (?,?,?,?)",
                     (cid, p.user_id, initials, now()))
    except sqlite3.IntegrityError:
        return {"ok": True, "already": True, "first": False, "initials": initials}
    conn.execute("UPDATE challenges SET status = 'solved' WHERE id = ?", (cid,))
    audit(conn, p.team_id, p.user_id, "challenge.complete", f"{cid} {initials}", client_ip(request))
    _touch(conn, p, cid)
    conn.commit()
    return {"ok": True, "first": existing == 0, "initials": initials}


@router.delete("/challenges/{cid}/complete")
def unmark_complete(cid: int, initials: Optional[str] = None, p: Principal = Depends(require_editor),
                    conn=Depends(get_db)):
    _challenge(conn, p, cid)
    ini = _initials(initials) if initials else p.initials
    row = conn.execute("SELECT * FROM completions WHERE challenge_id = ? AND initials = ?", (cid, ini)).fetchone()
    if not row:
        raise HTTPException(404, "No completion with those initials")
    if p.role != "owner" and row["user_id"] != p.user_id and ini != p.initials:
        raise HTTPException(403, "You can only remove your own completion")
    conn.execute("DELETE FROM completions WHERE id = ?", (row["id"],))
    if not conn.execute("SELECT 1 FROM completions WHERE challenge_id = ?", (cid,)).fetchone():
        conn.execute("UPDATE challenges SET status = 'open' WHERE id = ?", (cid,))
    _touch(conn, p, cid)
    conn.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- artifacts
class ArtifactIn(BaseModel):
    kind: Literal["context", "solution", "attachment"]
    filename: str = ""
    text: Optional[str] = None
    content_b64: Optional[str] = None
    content_type: Optional[str] = None
    title: Optional[str] = Field(default=None, max_length=200)
    flag_id: Optional[int] = None
    agent_name: Optional[str] = Field(default=None, max_length=60)
    source: Optional[Literal["agent", "human"]] = None


@router.post("/challenges/{cid}/artifacts")
def upload_artifact(cid: int, body: ArtifactIn, request: Request, p: Principal = Depends(require_editor),
                    conn=Depends(get_db)):
    _challenge(conn, p, cid)
    if (body.text is None) == (body.content_b64 is None):
        raise HTTPException(422, "Provide exactly one of text or content_b64")
    data = body.text.encode("utf-8") if body.text is not None else _b64(body.content_b64, "content_b64")
    default = {"context": "context.md", "solution": "solution.md"}.get(body.kind, "file.bin")
    filename = _clean_filename(body.filename, default)
    ctype = (body.content_type or "").split(";")[0].strip().lower() or _guess_type(filename, data)
    source = body.source or ("agent" if p.via == "token" else "human")
    aid = _store_artifact(conn, p, cid, kind=body.kind, filename=filename, data=data, content_type=ctype,
                          source=source, title=body.title, flag_id=body.flag_id, agent_name=body.agent_name)
    audit(conn, p.team_id, p.user_id, f"artifact.{body.kind}", f"{cid}/{filename}", client_ip(request))
    _touch(conn, p, cid)
    conn.commit()
    return {"id": aid, "filename": filename, "content_type": ctype, "size": len(data)}


class PastedImage(BaseModel):
    data_b64: str
    filename: str = "pasted.png"
    content_type: Optional[str] = None


class HumanSolutionIn(BaseModel):
    title: str = Field(default="Human solution", max_length=200)
    text: str = Field(default="", max_length=500_000)
    images: list[PastedImage] = Field(default_factory=list, max_length=20)
    flag_id: Optional[int] = None


@router.post("/challenges/{cid}/human-solution")
def human_solution(cid: int, body: HumanSolutionIn, request: Request, p: Principal = Depends(require_editor),
                   conn=Depends(get_db)):
    _challenge(conn, p, cid)
    if not body.text.strip() and not body.images:
        raise HTTPException(422, "Paste some text or at least one image")
    title = _clean_name(body.title or "Human solution", "Title", 200)
    parent = _store_artifact(conn, p, cid, kind="human_solution", filename="human-solution.md",
                             data=body.text.encode("utf-8"), content_type="text/markdown", source="human",
                             title=title, flag_id=body.flag_id)
    for i, img in enumerate(body.images, 1):
        data = _b64(img.data_b64, f"image {i}")
        ctype = (img.content_type or _guess_type(img.filename, data)).lower()
        if ctype not in SAFE_INLINE_IMAGES:
            raise HTTPException(415, f"image {i}: only PNG, JPEG, GIF and WebP are accepted")
        _store_artifact(conn, p, cid, kind="image", filename=_clean_filename(img.filename, f"image-{i}.png"),
                        data=data, content_type=ctype, source="human", parent_id=parent)
    audit(conn, p.team_id, p.user_id, "artifact.human_solution", f"{cid} ({len(body.images)} images)",
          client_ip(request))
    _touch(conn, p, cid)
    conn.commit()
    return {"id": parent}


def _artifact_row(conn, p: Principal, aid: int) -> sqlite3.Row:
    row = conn.execute("SELECT a.*, c.team_id FROM artifacts a JOIN challenges c ON c.id = a.challenge_id"
                       " WHERE a.id = ?", (aid,)).fetchone()
    if not row or row["team_id"] != p.team_id:
        raise HTTPException(404, "Artifact not found")
    return row


@router.get("/artifacts/{aid}")
def get_artifact(aid: int, include_binary: bool = False, p: Principal = Depends(current_user),
                 conn=Depends(get_db)):
    row = _artifact_row(conn, p, aid)
    meta = _artifact_meta(row)
    data = box().dec(row["content"])
    if _is_text(row["content_type"]):
        meta["text"] = data.decode("utf-8", errors="replace")
    elif include_binary:
        meta["content_b64"] = base64.b64encode(data).decode()
    children = conn.execute("SELECT * FROM artifacts WHERE parent_id = ? ORDER BY id", (aid,)).fetchall()
    if children:
        meta["images"] = []
        for c in children:
            cm = _artifact_meta(c)
            if include_binary:
                cm["content_b64"] = base64.b64encode(box().dec(c["content"])).decode()
            meta["images"].append(cm)
    return meta


@router.get("/artifacts/{aid}/raw")
def raw_artifact(aid: int, download: bool = False, p: Principal = Depends(current_user), conn=Depends(get_db)):
    row = _artifact_row(conn, p, aid)
    data = box().dec(row["content"])
    ctype = row["content_type"]
    inline = not download
    if ctype in SAFE_INLINE_IMAGES:
        served = ctype
    elif _is_text(ctype):
        served = "text/plain; charset=utf-8"  # never render uploaded HTML/SVG/JS
    else:
        served, inline = "application/octet-stream", False
    disp = "inline" if inline else "attachment"
    safe_name = row["filename"].replace('"', "")
    return Response(content=data, media_type=served, headers={
        "Content-Disposition": f'{disp}; filename="{safe_name}"',
        "Cache-Control": "private, no-store",
        "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
    })


@router.delete("/artifacts/{aid}")
def delete_artifact(aid: int, request: Request, p: Principal = Depends(require_editor), conn=Depends(get_db)):
    row = _artifact_row(conn, p, aid)
    if p.role != "owner" and row["author_id"] != p.user_id:
        raise HTTPException(403, "Only the author or the team owner can delete this")
    conn.execute("DELETE FROM artifacts WHERE id = ?", (aid,))
    audit(conn, p.team_id, p.user_id, "artifact.delete", f"{row['challenge_id']}/{row['filename']}",
          client_ip(request))
    _touch(conn, p, row["challenge_id"])
    conn.commit()
    return {"ok": True}
