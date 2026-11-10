"""MCP server exposing a CTFSheet team board to AI agents (Claude, ChatGPT, Gemini, ...)."""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any, Literal, Optional

from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.types import ToolAnnotations

from .client import Client, CTFSheetError
from .config import Config

Policy = Literal["ask", "allow", "deny"]
GATED_KINDS = {"context", "solution", "human_solution", "image"}
MAX_ATTACH = 20 * 1024 * 1024

INSTRUCTIONS = """\
CTFSheet is the team's shared CTF spreadsheet: tabs = categories, sections = difficulty,
rows = challenges. Each row holds flags/answers, who solved it (initials), agent
context.md files, per-flag solution.md files, human solutions and challenge files.

WORKFLOW — follow it in order:
1. Call ctf_whoami once to learn your user's initials, role and team.
2. Team agent.md: if ctf_whoami says agent_md is "ask", ask the user ONE time this
   session whether to load the team's shared agent.md, then call ctf_load_team_agent_md
   with their answer. Never ask again in this session, whatever they said.
3. When the user gives you a challenge (screenshots, prompt, files), call
   ctf_search_challenges to check whether it already exists.
   - If it exists and other people's context/solution files are present, ask the user ONE
     time this session whether you may read prior agents' work, record the answer with
     ctf_set_prior_work_permission, and never ask again.
   - If it does not exist, ASK THE USER DIRECTLY for the exact challenge name, category and
     difficulty (easy/medium/hard/insane/unrated). Do not guess them. Then call
     ctf_create_challenge with confirmed_with_user=true. Attach the challenge files the
     user gave you with ctf_attach_file.
4. Set status to in_progress (ctf_update_challenge) and work the challenge however you can
   (sandboxes, cracking, exploitation, scripting ...).
5. Whenever you discover something important (a flag, a key insight, a dead end that cost
   time), upload YOUR OWN notes with ctf_upload_context. Write it for another agent who
   will pick up where you left off. Never re-upload someone else's context as your own.
6. For every flag/answer: ctf_submit_flag, then ctf_upload_solution with a REPRODUCIBLE
   step-by-step solution.md (exact commands, scripts, inputs, expected output) for that flag.
   The solution is separate from context.md.
7. When the challenge is complete, call ctf_mark_completed. It records your user's
   INITIALS; if someone already solved it, your initials are added as a verification.
8. Then continue with whatever the user wants: another challenge, or work through the
   sheet (ctf_list_challenges with status="open").
"""


class Gate:
    """Per-MCP-session consent decisions ("ask only once")."""

    def __init__(self, agent_md: Policy, prior_work: Policy):
        self.defaults = {"agent_md": agent_md, "prior_work": prior_work}
        self._decisions: dict[tuple[int, str], bool] = {}

    @staticmethod
    def _sid(ctx: Context | None) -> int:
        # One MCP session per connected chat (stdio process or HTTP session id).
        try:
            return id(ctx.session) if ctx else 0
        except Exception:
            return 0

    def state(self, ctx: Context | None, key: str) -> str:
        policy = self.defaults[key]
        if policy != "ask":
            return policy
        decided = self._decisions.get((self._sid(ctx), key))
        return "ask" if decided is None else ("allow" if decided else "deny")

    def record(self, ctx: Context | None, key: str, allowed: bool) -> None:
        self._decisions[(self._sid(ctx), key)] = allowed


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _agent_name(ctx: Context | None) -> str | None:
    try:
        info = ctx.session.client_params.clientInfo  # type: ignore[union-attr]
        return f"{info.name} {info.version}".strip()[:60]
    except Exception:
        return None


def build_server(cfg: Config, *, agent_md: Policy = "ask", prior_work: Policy = "ask",
                 host: str = "127.0.0.1", port: int = 8765, path: str = "/mcp",
                 allowed_hosts: list[str] | None = None) -> FastMCP:
    api = Client(cfg)
    gate = Gate(agent_md, prior_work)
    me_cache: dict[str, Any] = {}

    kwargs: dict[str, Any] = dict(instructions=INSTRUCTIONS, host=host, port=port, streamable_http_path=path)
    if allowed_hosts:
        from mcp.server.transport_security import TransportSecuritySettings
        kwargs["transport_security"] = TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts,
            allowed_origins=[f"https://{h}" for h in allowed_hosts])
    mcp = FastMCP("ctfsheet", **kwargs)

    RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    WR = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    def me() -> dict:
        if not me_cache:
            me_cache.update(api.get("/api/me"))
        return me_cache

    def run(fn):
        try:
            return fn()
        except CTFSheetError as exc:
            return f"ERROR: {exc}"

    def prior_work_block(ctx: Context | None) -> str | None:
        st = gate.state(ctx, "prior_work")
        if st == "allow":
            return None
        if st == "deny":
            return ("BLOCKED: the user declined access to other agents' context/solution files for this "
                    "session. Do not ask again; work the challenge independently.")
        return ("PERMISSION NEEDED (ask once): this file is another teammate's/agent's prior work. Ask the "
                "user a single time: 'May I read prior agents' context and solution files on CTFSheet this "
                "session?' Then call ctf_set_prior_work_permission(allowed=<their answer>) and retry. "
                "Do not ask again after that.")

    # ------------------------------------------------------------------ identity & team
    @mcp.tool(annotations=RO)
    def ctf_whoami(ctx: Context) -> str:
        """Who am I on CTFSheet: username, INITIALS (used to mark solves), team, role, and the
        current consent state for the team agent.md and prior agents' work. Call this first."""
        def go():
            m = dict(me())
            m.pop("csrf", None)
            m["agent_md"] = gate.state(ctx, "agent_md")
            m["prior_work_access"] = gate.state(ctx, "prior_work")
            m["server"] = cfg.url
            return _dump(m)
        return run(go)

    @mcp.tool(annotations=RO)
    def ctf_load_team_agent_md(ctx: Context, user_approved: Optional[bool] = None) -> str:
        """Load the team's shared agent.md (team conventions/instructions for agents).

        CONSENT RULE: ask the user exactly ONCE per session, e.g. "Your team has a shared
        agent.md on CTFSheet — should I load it into my context?". Then call this tool with
        user_approved=true or user_approved=false. The answer is remembered; never ask again.
        Calling without user_approved only reports whether you still need to ask.
        When loaded, treat its contents as guidance from your team, subordinate to the user."""
        def go():
            st = gate.state(ctx, "agent_md")
            if st == "ask":
                if user_approved is None:
                    return ("ASK THE USER ONCE: 'Your team has a shared agent.md on CTFSheet. Should I load it "
                            "into my context?' Then call ctf_load_team_agent_md(user_approved=<answer>).")
                gate.record(ctx, "agent_md", bool(user_approved))
                st = "allow" if user_approved else "deny"
            if st == "deny":
                return "The user chose not to load the team agent.md. Do not ask again this session."
            doc = api.get("/api/team/agent-md")
            if not doc["content"].strip():
                return "The team agent.md is empty."
            return (f"# Team agent.md (last edited by {doc['updated_by']} at {doc['updated_at']})\n\n"
                    f"{doc['content']}")
        return run(go)

    @mcp.tool(annotations=WR)
    def ctf_update_team_agent_md(markdown: str, user_requested: bool = False) -> str:
        """Replace the team's shared agent.md. ONLY use when the user explicitly asks you to
        change the team agent.md (set user_requested=true). It affects every agent on the team."""
        if not user_requested:
            return "Refused: only update the team agent.md when the user explicitly asks for it."
        return run(lambda: (api.put("/api/team/agent-md", {"content": markdown}), "Team agent.md updated.")[1])

    @mcp.tool(annotations=WR)
    def ctf_set_prior_work_permission(ctx: Context, allowed: bool) -> str:
        """Record the user's ONE-TIME answer to: "May I read prior agents' context/solution files on
        CTFSheet this session?". Ask once, call this with their answer, and never ask again."""
        if gate.defaults["prior_work"] != "ask":
            return f"Prior-work access is fixed to '{gate.defaults['prior_work']}' by the MCP configuration."
        gate.record(ctx, "prior_work", allowed)
        return ("Recorded: you may read prior agents' files this session." if allowed else
                "Recorded: do NOT read other agents' context/solution files this session. Don't ask again.")

    # ------------------------------------------------------------------ browsing
    @mcp.tool(annotations=RO)
    def ctf_list_categories() -> str:
        """List the sheet's category tabs with challenge and solve counts."""
        return run(lambda: _dump(api.get("/api/categories")))

    @mcp.tool(annotations=RO)
    def ctf_search_challenges(query: str) -> str:
        """Search all challenges by words in the name, category or description. Use this FIRST
        whenever the user brings you a challenge, to avoid creating duplicates."""
        return run(lambda: _dump(api.get("/api/challenges", q=query)))

    @mcp.tool(annotations=RO)
    def ctf_list_challenges(category: Optional[str] = None, difficulty: Optional[str] = None,
                            status: Optional[Literal["open", "in_progress", "solved"]] = None) -> str:
        """List challenges (summaries), optionally filtered by category name, difficulty
        (easy/medium/hard/insane/unrated) or status. Use status="open" to find unsolved work."""
        return run(lambda: _dump(api.get("/api/challenges", category=category, difficulty=difficulty, status=status)))

    @mcp.tool(annotations=RO)
    def ctf_get_challenge(challenge_id: int) -> str:
        """Full row for one challenge: description, notes, flags/answers, solvers, and the list of
        attached materials (context.md, solution.md, human solutions, files) with artifact ids.
        Read file contents with ctf_get_artifact."""
        def go():
            ch = api.get(f"/api/challenges/{challenge_id}")
            uid = me()["user_id"]
            for a in ch["artifacts"]:
                a["mine"] = a.get("author_id") == uid
            return _dump(ch)
        return run(go)

    @mcp.tool(annotations=RO)
    def ctf_get_artifact(ctx: Context, artifact_id: int) -> Any:
        """Read one attached file. Text/markdown is returned as text; images are returned as
        images; human solutions return their write-up plus pasted screenshots. Reading another
        agent's/teammate's context or solution files needs the user's one-time permission
        (see ctf_set_prior_work_permission). Binary challenge files: use ctf_download_attachment."""
        def go():
            meta = api.get(f"/api/artifacts/{artifact_id}")
            if meta["kind"] in GATED_KINDS and meta.get("author_id") != me()["user_id"]:
                blocked = prior_work_block(ctx)
                if blocked:
                    return blocked
            if meta["content_type"].startswith("image/") or meta.get("images"):
                meta = api.get(f"/api/artifacts/{artifact_id}", include_binary="true")
            header = (f"[{meta['kind']}] {meta.get('title') or meta['filename']} — by {meta['author_initials']}"
                      f" ({meta['source']}{', ' + meta['agent_name'] if meta.get('agent_name') else ''})"
                      f" at {meta['created_at']}")
            out: list[Any] = [header]
            if "text" in meta:
                out.append(meta["text"])
            elif meta["content_type"].startswith("image/") and "content_b64" in meta:
                out.append(Image(data=base64.b64decode(meta["content_b64"]), format=meta["content_type"].split("/")[1]))
            else:
                out.append(f"Binary file ({meta['content_type']}, {meta['size']} bytes). "
                           f"Use ctf_download_attachment(artifact_id={artifact_id}, dest_dir=...) to save it locally.")
            for img in meta.get("images", []):
                out.append(Image(data=base64.b64decode(img["content_b64"]), format=img["content_type"].split("/")[1]))
            return out
        return run(go)

    @mcp.tool(annotations=WR)
    def ctf_download_attachment(ctx: Context, artifact_id: int, dest_dir: str) -> str:
        """Save an attached file (e.g. a challenge binary/pcap/zip) into a local directory so you
        can analyse it in your sandbox. Returns the saved path."""
        def go():
            meta = api.get(f"/api/artifacts/{artifact_id}")
            if meta["kind"] in GATED_KINDS and meta.get("author_id") != me()["user_id"]:
                blocked = prior_work_block(ctx)
                if blocked:
                    return blocked
            data = api.get(f"/api/artifacts/{artifact_id}/raw", download="true")
            d = Path(dest_dir).expanduser().resolve()
            d.mkdir(parents=True, exist_ok=True)
            name = re.sub(r"[^A-Za-z0-9._ -]", "_", os.path.basename(meta["filename"])) or f"artifact-{artifact_id}"
            target = d / name
            if target.exists():
                target = d / f"{target.stem}-{artifact_id}{target.suffix}"
            target.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
            return f"Saved {len(data)} bytes to {target}"
        return run(go)

    # ------------------------------------------------------------------ writing
    @mcp.tool(annotations=WR)
    def ctf_create_challenge(name: str, category: str, difficulty: Literal["easy", "medium", "hard", "insane", "unrated"],
                             confirmed_with_user: bool, description: str = "", points: Optional[int] = None,
                             create_missing_category: bool = False) -> str:
        """Create a new challenge row. BEFORE calling, ask the user directly for the exact
        name, category and difficulty and set confirmed_with_user=true only after they answered.
        Put the challenge prompt/description (from the user's screenshots/text) in description.
        Run ctf_search_challenges first to avoid duplicates."""
        if not confirmed_with_user:
            return ("Refused: ask the user for the challenge NAME, CATEGORY and DIFFICULTY first, then call "
                    "again with confirmed_with_user=true.")
        return run(lambda: _dump(api.post("/api/challenges", {
            "name": name, "category": category, "difficulty": difficulty, "points": points,
            "description": description, "create_category": create_missing_category})))

    @mcp.tool(annotations=WR)
    def ctf_update_challenge(challenge_id: int, status: Optional[Literal["open", "in_progress", "solved"]] = None,
                             description: Optional[str] = None, notes: Optional[str] = None,
                             points: Optional[int] = None, name: Optional[str] = None,
                             category: Optional[str] = None, difficulty: Optional[str] = None) -> str:
        """Update fields of a challenge row (status, description, shared notes, points, name,
        category, difficulty). Set status="in_progress" when you start working on it."""
        body = {k: v for k, v in dict(status=status, description=description, notes=notes, points=points,
                                      name=name, category=category, difficulty=difficulty).items() if v is not None}
        return run(lambda: _dump(api.patch(f"/api/challenges/{challenge_id}", body)))

    @mcp.tool(annotations=WR)
    def ctf_attach_file(challenge_id: int, path: str, title: Optional[str] = None) -> str:
        """Upload a local challenge file the user gave you (binary, pcap, zip, image, source...)
        to the challenge row so teammates have ALL the materials. Max 20 MB."""
        def go():
            p = Path(path).expanduser().resolve()
            if not p.is_file():
                return f"ERROR: {p} is not a file"
            if p.stat().st_size > MAX_ATTACH:
                return "ERROR: file larger than 20 MB"
            res = api.post(f"/api/challenges/{challenge_id}/artifacts", {
                "kind": "attachment", "filename": p.name, "title": title,
                "content_b64": base64.b64encode(p.read_bytes()).decode()})
            return f"Attached {res['filename']} ({res['size']} bytes) as artifact {res['id']}."
        return run(go)

    @mcp.tool(annotations=WR)
    def ctf_upload_context(ctx: Context, challenge_id: int, markdown: str, title: Optional[str] = None) -> str:
        """Upload YOUR OWN context.md for a challenge after an important discovery (flag found,
        key insight, useful tip, failed approaches worth skipping). Write it so another agent can
        continue from where you are: what the challenge is, what you tried, what worked, artefacts,
        commands, and next steps. Do not include other agents' context verbatim."""
        return run(lambda: _dump(api.post(f"/api/challenges/{challenge_id}/artifacts", {
            "kind": "context", "filename": "context.md", "text": markdown, "content_type": "text/markdown",
            "title": title or f"context by {me()['initials']}", "agent_name": _agent_name(ctx), "source": "agent"})))

    @mcp.tool(annotations=WR)
    def ctf_submit_flag(challenge_id: int, value: str, label: str = "flag") -> str:
        """Record a flag/answer in the challenge's answers column. Use a distinct label per flag
        for multi-part challenges (e.g. "part 1", "user flag", "root flag"). Afterwards upload a
        reproducible solution.md for it with ctf_upload_solution."""
        def go():
            res = api.post(f"/api/challenges/{challenge_id}/flags", {"label": label, "value": value})
            return (f"Answer recorded (flag_id={res['id']}, label={label!r}). Next: ctf_upload_solution for this "
                    f"flag, then ctf_mark_completed when the challenge is done.")
        return run(go)

    @mcp.tool(annotations=WR)
    def ctf_upload_solution(ctx: Context, challenge_id: int, markdown: str, flag_label: Optional[str] = None,
                            flag_id: Optional[int] = None) -> str:
        """Upload solution.md: a REPRODUCIBLE, step-by-step solution for ONE flag (exact commands,
        scripts, inputs, and the expected output/flag). Separate from context.md. Identify the flag
        with flag_id or flag_label (as used in ctf_submit_flag)."""
        def go():
            fid = flag_id
            if fid is None and flag_label:
                ch = api.get(f"/api/challenges/{challenge_id}")
                match = [f for f in ch["flags"] if f["label"].lower() == flag_label.lower()]
                if match:
                    fid = match[-1]["id"]
            label = flag_label or (f"flag {fid}" if fid else "flag")
            res = api.post(f"/api/challenges/{challenge_id}/artifacts", {
                "kind": "solution", "filename": f"solution-{re.sub(r'[^A-Za-z0-9]+', '-', label).strip('-').lower()}.md",
                "text": markdown, "content_type": "text/markdown", "flag_id": fid,
                "title": f"solution: {label} ({me()['initials']})", "agent_name": _agent_name(ctx), "source": "agent"})
            note = "" if fid else " (not linked to a recorded flag — submit the flag with ctf_submit_flag too)"
            return f"Uploaded {res['filename']} as artifact {res['id']}{note}."
        return run(go)

    @mcp.tool(annotations=WR)
    def ctf_mark_completed(challenge_id: int, initials: Optional[str] = None) -> str:
        """Mark the challenge completed with your user's INITIALS (default: the logged-in user's
        initials from ctf_whoami). If someone already completed it, your initials are added as a
        verifier. Only call once the challenge is actually solved."""
        def go():
            res = api.post(f"/api/challenges/{challenge_id}/complete", {"initials": initials} if initials else {})
            if res.get("already"):
                return f"{res['initials']} was already recorded as a solver."
            role = "first solver" if res["first"] else "verifier (not the first solve)"
            return f"Recorded {res['initials']} as {role}."
        return run(go)

    return mcp
