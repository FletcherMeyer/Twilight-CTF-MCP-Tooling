/* CTFSheet spreadsheet UI. */
"use strict";

const state = { sheet: null, activeCat: null, revision: -1, query: "", revealed: new Set() };
const DIFF_LABEL = { easy: "Easy", medium: "Medium", hard: "Hard", insane: "Insane", unrated: "Unrated" };
const STATUS_LABEL = { open: "Open", in_progress: "In progress", solved: "Solved" };
const canWrite = () => CTF.me && CTF.me.role !== "viewer";

// ------------------------------------------------------------------ boot
(async function boot() {
  CTF.me = await api("/api/me");
  CTF.csrf = CTF.me.csrf;
  document.getElementById("team-name").textContent = CTF.me.team;
  document.getElementById("btn-account").textContent = `${CTF.me.initials} · ${CTF.me.role}`;
  if (!canWrite()) document.getElementById("btn-new").hidden = true;

  document.getElementById("btn-new").onclick = () => newChallengeModal();
  document.getElementById("btn-agentmd").onclick = agentMdModal;
  document.getElementById("btn-team").onclick = teamModal;
  document.getElementById("btn-tokens").onclick = tokensModal;
  document.getElementById("btn-account").onclick = accountModal;
  document.getElementById("btn-logout").onclick = async () => {
    await api("/api/auth/logout", { method: "POST" }).catch(() => {});
    location.href = "/login";
  };
  document.getElementById("search").addEventListener("input", (e) => { state.query = e.target.value.trim().toLowerCase(); render(); });
  window.addEventListener("hashchange", () => { state.activeCat = decodeURIComponent(location.hash.slice(1)); render(); });

  await load();
  setInterval(poll, 5000);
})();

async function load() {
  state.sheet = await api("/api/sheet");
  state.revision = state.sheet.revision;
  const fromHash = decodeURIComponent(location.hash.slice(1));
  const names = state.sheet.categories.map((c) => c.name);
  if (fromHash && names.includes(fromHash)) state.activeCat = fromHash;
  if (!names.includes(state.activeCat)) state.activeCat = names[0] || null;
  render();
}

async function poll() {
  if (document.hidden) return;
  try {
    const { revision } = await api("/api/team/revision");
    if (revision !== state.revision) await load();
  } catch (_) { /* transient */ }
}

const findChallenge = (id) => {
  for (const c of state.sheet.categories) for (const ch of c.challenges) if (ch.id === id) return ch;
  return null;
};

// ------------------------------------------------------------------ render sheet
function render() {
  renderTabs();
  const main = document.getElementById("sheet");
  main.replaceChildren();
  if (!state.sheet) return;
  if (state.query) return renderSearch(main);
  const cat = state.sheet.categories.find((c) => c.name === state.activeCat);
  if (!cat) { main.append(h("p.empty", "No categories yet. Add one with the + tab.")); return; }

  const solved = cat.challenges.filter((c) => c.status === "solved").length;
  main.append(h("div.cat-head",
    h("h2", cat.name),
    h("span.muted", `${cat.challenges.length} challenges · ${solved} solved`),
    h("div.spacer"),
    canWrite() && h("button", { onclick: () => newChallengeModal(cat.name) }, `+ Add to ${cat.name}`),
    canWrite() && h("button.ghost", { onclick: () => renameCategory(cat) }, "Rename"),
  ));

  for (const diff of state.sheet.difficulties) {
    const rows = cat.challenges.filter((c) => c.difficulty === diff);
    if (!rows.length && diff === "unrated") continue;
    main.append(h(`section.diff.diff-${diff}`,
      h("h3", h(`span.pill.d-${diff}`, DIFF_LABEL[diff] || diff), h("span.muted", ` ${rows.length}`)),
      rows.length ? table(rows) : h("p.empty-row", "No challenges at this difficulty yet."),
    ));
  }
}

function renderSearch(main) {
  const hits = [];
  for (const cat of state.sheet.categories)
    for (const ch of cat.challenges) {
      const hay = `${ch.name} ${cat.name} ${ch.description} ${ch.difficulty}`.toLowerCase();
      if (state.query.split(/\s+/).every((t) => hay.includes(t))) hits.push(ch);
    }
  main.append(h("div.cat-head", h("h2", `Search: “${state.query}”`), h("span.muted", `${hits.length} results`)));
  main.append(hits.length ? table(hits, true) : h("p.empty", "Nothing matches."));
}

function renderTabs() {
  const nav = document.getElementById("tabs");
  nav.replaceChildren();
  if (!state.sheet) return;
  for (const cat of state.sheet.categories) {
    const solved = cat.challenges.filter((c) => c.status === "solved").length;
    nav.append(h("a.tab" + (cat.name === state.activeCat && !state.query ? ".active" : ""),
      { href: "#" + encodeURIComponent(cat.name) },
      cat.name, h("span.count", `${solved}/${cat.challenges.length}`)));
  }
  if (canWrite()) nav.append(h("button.tab.add", { onclick: addCategory, title: "Add category" }, "+"));
}

function table(rows, showCat = false) {
  const head = ["Challenge", ...(showCat ? ["Category"] : []), "Pts", "Status", "Flags / answers", "Solved by",
    "Agent context", "Solutions", "Human solutions", "Files", ""];
  return h("div.table-wrap", h("table.grid",
    h("thead", h("tr", head.map((t) => h("th", t)))),
    h("tbody", rows.map((ch) => row(ch, showCat)))));
}

function row(ch, showCat) {
  const by = (k) => ch.artifacts.filter((a) => a.kind === k);
  return h("tr" + (ch.status === "solved" ? ".solved" : ""),
    h("td.name", h("a", { href: "#", onclick: (e) => { e.preventDefault(); challengeModal(ch.id); } }, ch.name),
      ch.description && h("div.desc", ch.description.slice(0, 140))),
    showCat && h("td", ch.category),
    h("td.num", ch.points ?? "–"),
    h("td", h(`span.pill.s-${ch.status}`, STATUS_LABEL[ch.status] || ch.status)),
    h("td.flags", ch.flags.map(flagChip)),
    h("td.solvers", ch.completions.map((c) => h("span.initials" + (c.first ? ".first" : ""),
      { title: (c.first ? "First solve · " : "Verified · ") + fmtTime(c.created_at) }, c.initials))),
    h("td", by("context").map(artChip)),
    h("td", by("solution").map(artChip)),
    h("td", by("human_solution").map(artChip),
      canWrite() && h("button.mini", { onclick: () => humanSolutionModal(ch), title: "Paste a human solution" }, "+ paste")),
    h("td", by("attachment").map(artChip)),
    h("td.actions", h("button.mini", { onclick: () => challengeModal(ch.id) }, "Open")),
  );
}

function flagChip(f) {
  const key = "f" + f.id;
  const shown = state.revealed.has(key);
  return h("span.flag", { title: `${f.label} · by ${f.submitted_by}` },
    h("span.flag-label", f.label),
    h("code" + (shown ? "" : ".masked"), {
      onclick: () => { shown ? state.revealed.delete(key) : state.revealed.add(key); render(); },
    }, shown ? f.value : "•••••••"),
    h("button.icon", { title: "Copy", onclick: () => navigator.clipboard.writeText(f.value).then(() => toast("Copied")) }, "⧉"));
}

function artChip(a) {
  const who = a.source === "agent" ? `🤖 ${a.author_initials}${a.agent_name ? " · " + a.agent_name : ""}` : `👤 ${a.author_initials}`;
  const label = a.title || a.filename;
  return h("a.chip.k-" + a.kind, { href: "#", title: `${label}\n${who}\n${fmtTime(a.created_at)}`,
    onclick: (e) => { e.preventDefault(); artifactModal(a.id); } },
    h("span.chip-name", label), h("span.chip-who", a.author_initials),
    a.images && a.images.length ? h("span.chip-who", `🖼${a.images.length}`) : null);
}

// ------------------------------------------------------------------ modal infra
function modal(title, body, { wide = false, onClose } = {}) {
  const root = document.getElementById("modal-root");
  const close = () => { wrap.remove(); document.removeEventListener("keydown", onKey); onClose && onClose(); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  const wrap = h("div.overlay", { onmousedown: (e) => { if (e.target === wrap) close(); } },
    h("div.modal" + (wide ? ".wide" : ""), { role: "dialog", "aria-label": title },
      h("div.modal-head", h("h2", title), h("button.icon.close", { onclick: close, title: "Close" }, "✕")),
      h("div.modal-body", body)));
  document.addEventListener("keydown", onKey);
  root.append(wrap);
  return { close, wrap };
}

async function guarded(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; }
  catch (e) { toast(e.message, "err"); throw e; }
}

const field = (label, input) => h("label", label, input);
const select = (name, opts, value) => h("select", { name },
  opts.map(([v, t]) => h("option", { value: v, selected: v === value }, t)));

// ------------------------------------------------------------------ categories
async function addCategory() {
  const name = prompt("New category name");
  if (!name) return;
  await guarded(() => api("/api/categories", { method: "POST", body: { name } }), "Category added");
  state.activeCat = name;
  location.hash = encodeURIComponent(name);
  await load();
}

async function renameCategory(cat) {
  const name = prompt("Rename category", cat.name);
  if (!name || name === cat.name) return;
  await guarded(() => api(`/api/categories/${cat.id}`, { method: "PATCH", body: { name } }), "Renamed");
  state.activeCat = name;
  location.hash = encodeURIComponent(name);
  await load();
}

// ------------------------------------------------------------------ new challenge
function newChallengeModal(catName) {
  const cats = state.sheet.categories.map((c) => [c.name, c.name]);
  const form = h("form.form",
    field("Name", h("input", { name: "name", required: true, maxLength: 120 })),
    h("div.row3",
      field("Category", select("category", cats, catName || state.activeCat)),
      field("Difficulty", select("difficulty", state.sheet.difficulties.map((d) => [d, DIFF_LABEL[d] || d]), "medium")),
      field("Points", h("input", { name: "points", type: "number", min: 0 }))),
    field("Description / prompt", h("textarea", { name: "description", rows: 6 })),
    h("button.primary", { type: "submit" }, "Create"));
  const m = modal("New challenge", form);
  form.onsubmit = async (e) => {
    e.preventDefault();
    const d = Object.fromEntries(new FormData(form).entries());
    d.points = d.points === "" ? null : Number(d.points);
    const ch = await guarded(() => api("/api/challenges", { method: "POST", body: d }), "Challenge created");
    m.close();
    state.activeCat = ch.category;
    await load();
    challengeModal(ch.id);
  };
}

// ------------------------------------------------------------------ challenge detail
async function challengeModal(id) {
  const ch = await guarded(() => api(`/api/challenges/${id}`));
  const ro = !canWrite();
  const refresh = async () => { m.close(); await load(); challengeModal(id); };

  const meta = h("form.form.meta",
    h("div.row4",
      field("Name", h("input", { name: "name", value: ch.name, disabled: ro })),
      field("Category", select("category", state.sheet.categories.map((c) => [c.name, c.name]), ch.category)),
      field("Difficulty", select("difficulty", state.sheet.difficulties.map((d) => [d, DIFF_LABEL[d] || d]), ch.difficulty)),
      field("Points", h("input", { name: "points", type: "number", value: ch.points ?? "", disabled: ro })),
      field("Status", select("status", Object.entries(STATUS_LABEL), ch.status))),
    field("Description", h("textarea", { name: "description", rows: 5, value: ch.description, disabled: ro })),
    field("Shared notes", h("textarea", { name: "notes", rows: 3, value: ch.notes, disabled: ro })),
    !ro && h("div.btns", h("button.primary", { type: "submit" }, "Save changes"),
      h("button.danger", { type: "button", onclick: delChallenge }, "Delete challenge")));
  if (ro) meta.querySelectorAll("select").forEach((s) => (s.disabled = true));
  meta.onsubmit = async (e) => {
    e.preventDefault();
    const d = Object.fromEntries(new FormData(meta).entries());
    d.points = d.points === "" ? null : Number(d.points);
    if (d.points === null) delete d.points;
    await guarded(() => api(`/api/challenges/${id}`, { method: "PATCH", body: d }), "Saved");
    refresh();
  };
  async function delChallenge() {
    if (!confirm(`Delete "${ch.name}" and all of its files? This cannot be undone.`)) return;
    await guarded(() => api(`/api/challenges/${id}`, { method: "DELETE" }), "Deleted");
    m.close(); load();
  }

  // flags
  const flagForm = h("form.inline",
    h("input", { name: "label", placeholder: "label (e.g. flag, part 2)", value: "flag" }),
    h("input.grow", { name: "value", placeholder: "flag{...}", required: true }),
    h("button", { type: "submit" }, "Add answer"));
  flagForm.onsubmit = async (e) => {
    e.preventDefault();
    await guarded(() => api(`/api/challenges/${id}/flags`, { method: "POST", body: Object.fromEntries(new FormData(flagForm)) }), "Answer added");
    refresh();
  };
  const flags = h("div.section",
    h("h3", "Flags / answers"),
    ch.flags.length ? h("ul.list", ch.flags.map((f) => h("li",
      h("strong", f.label), " ", h("code.sel", f.value), h("span.muted", ` by ${f.submitted_by} · ${fmtTime(f.created_at)}`),
      !ro && h("button.mini.danger", { onclick: async () => {
        if (!confirm("Delete this answer?")) return;
        await guarded(() => api(`/api/flags/${f.id}`, { method: "DELETE" })); refresh();
      } }, "delete")))) : h("p.muted", "No answers recorded yet."),
    !ro && flagForm);

  // completions
  const mine = ch.completions.some((c) => c.initials === CTF.me.initials);
  const comp = h("div.section",
    h("h3", "Solved by"),
    ch.completions.length ? h("p", ch.completions.map((c) => h("span.initials" + (c.first ? ".first" : ""),
      { title: fmtTime(c.created_at) }, c.initials, c.first ? " (first)" : " (verified)"))) : h("p.muted", "Not solved yet."),
    !ro && h("div.btns",
      mine ? h("button", { onclick: async () => {
        await guarded(() => api(`/api/challenges/${id}/complete`, { method: "DELETE" })); refresh();
      } }, `Remove my solve (${CTF.me.initials})`)
        : h("button.primary", { onclick: async () => {
          await guarded(() => api(`/api/challenges/${id}/complete`, { method: "POST", body: {} }), "Marked solved"); refresh();
        } }, `Mark solved as ${CTF.me.initials}`)));

  // artifacts
  const groups = [["context", "Agent context (context.md)"], ["solution", "Solutions (solution.md, one per flag)"],
    ["human_solution", "Human solutions"], ["attachment", "Challenge files"]];
  const arts = h("div.section", h("h3", "Materials"),
    groups.map(([k, t]) => {
      const list = ch.artifacts.filter((a) => a.kind === k);
      return h("div.art-group", h("h4", t),
        list.length ? h("ul.list", list.map((a) => h("li", artChip(a),
          h("span.muted", ` ${a.source === "agent" ? "agent" : "human"} · ${fmtSize(a.size)} · ${fmtTime(a.created_at)}`),
          a.flag_id && h("span.muted", ` · for “${(ch.flags.find((f) => f.id === a.flag_id) || {}).label || "flag"}”`))))
          : h("p.muted", "None yet."));
    }),
    !ro && h("div.btns",
      h("button.primary", { onclick: () => { m.close(); humanSolutionModal(ch, () => challengeModal(id)); } }, "Paste human solution"),
      uploadButton(id, "attachment", "Upload challenge file", refresh),
      uploadButton(id, "context", "Upload context.md", refresh),
      uploadButton(id, "solution", "Upload solution.md", refresh)));

  const m = modal(`${ch.category} › ${ch.name}`, [meta, h("div.cols", flags, comp), arts], { wide: true });
}

function uploadButton(cid, kind, label, done) {
  const input = h("input", { type: "file", hidden: true, multiple: kind === "attachment" });
  input.onchange = async () => {
    for (const file of input.files) {
      if (file.size > CTF.me.max_upload_mb * 1048576) { toast(`${file.name} is larger than ${CTF.me.max_upload_mb} MB`, "err"); continue; }
      const b64 = await fileToB64(file);
      await guarded(() => api(`/api/challenges/${cid}/artifacts`, { method: "POST",
        body: { kind, filename: file.name, content_b64: b64, content_type: file.type || null, source: "human" } }), `Uploaded ${file.name}`);
    }
    done();
  };
  return h("span", input, h("button", { onclick: () => input.click() }, label));
}

function fileToB64(file) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(String(r.result).split(",")[1]);
    r.onerror = rej;
    r.readAsDataURL(file);
  });
}

// ------------------------------------------------------------------ artifact viewer
async function artifactModal(aid) {
  const a = await guarded(() => api(`/api/artifacts/${aid}`));
  const raw = `/api/artifacts/${aid}/raw`;
  let body;
  if (a.content_type.startsWith("image/")) body = h("img.preview", { src: raw, alt: a.filename });
  else if (a.text !== undefined) {
    body = /markdown/.test(a.content_type) || a.filename.endsWith(".md")
      ? h("div.md", { html: renderMarkdown(a.text) }) : h("pre.code", a.text);
  } else body = h("p.muted", "Binary file — download it to inspect.");
  const imgs = (a.images || []).map((i) => h("a", { href: `/api/artifacts/${i.id}/raw`, target: "_blank", rel: "noopener" },
    h("img.preview", { src: `/api/artifacts/${i.id}/raw`, alt: i.filename })));
  const canDel = canWrite() && (CTF.me.role === "owner" || a.author_id === CTF.me.user_id);
  const m = modal(a.title || a.filename, [
    h("p.muted", `${a.kind.replace("_", " ")} · ${a.source === "agent" ? "🤖 agent" : "👤 human"} · ${a.author_initials}`,
      a.agent_name ? ` (${a.agent_name})` : "", ` · ${fmtSize(a.size)} · ${fmtTime(a.created_at)} · sha256 ${a.sha256.slice(0, 12)}…`),
    body, imgs.length ? h("div.gallery", imgs) : null,
    h("div.btns",
      h("a.button", { href: raw + "?download=1" }, "Download"),
      a.text !== undefined && h("button", { onclick: () => navigator.clipboard.writeText(a.text).then(() => toast("Copied")) }, "Copy text"),
      canDel && h("button.danger", { onclick: async () => {
        if (!confirm("Delete this file?")) return;
        await guarded(() => api(`/api/artifacts/${aid}`, { method: "DELETE" }), "Deleted");
        m.close(); load();
      } }, "Delete")),
  ], { wide: true });
}

// ------------------------------------------------------------------ human solution paste modal
function humanSolutionModal(ch, after) {
  const images = [];
  const thumbs = h("div.thumbs");
  const drawThumbs = () => thumbs.replaceChildren(...images.map((img, i) => h("div.thumb",
    h("img", { src: img.url, alt: img.filename }),
    h("button.icon", { title: "Remove", onclick: (e) => { e.preventDefault(); images.splice(i, 1); drawThumbs(); } }, "✕"))));
  const addFile = (file) => {
    if (!/^image\/(png|jpeg|gif|webp)$/.test(file.type)) { toast(`${file.name || "item"}: only PNG/JPEG/GIF/WebP`, "err"); return; }
    const r = new FileReader();
    r.onload = () => { images.push({ url: r.result, filename: file.name || `pasted-${images.length + 1}.png`, content_type: file.type }); drawThumbs(); };
    r.readAsDataURL(file);
  };
  const text = h("textarea", { name: "text", rows: 12, placeholder: "Paste or type your write-up here (markdown supported).\nPaste screenshots anywhere in this window (Ctrl/Cmd+V) or drop image files." });
  const picker = h("input", { type: "file", accept: "image/png,image/jpeg,image/gif,image/webp", multiple: true, hidden: true,
    onchange: () => [...picker.files].forEach(addFile) });
  const form = h("form.form",
    h("div.row2",
      field("Title", h("input", { name: "title", value: `Solution by ${CTF.me.initials}`, maxLength: 200 })),
      field("For flag (optional)", select("flag_id", [["", "— any / whole challenge —"], ...ch.flags.map((f) => [String(f.id), f.label])], ""))),
    text,
    h("div.drop", "Paste or drop screenshots here — ", h("a", { href: "#", onclick: (e) => { e.preventDefault(); picker.click(); } }, "or choose files"), picker),
    thumbs,
    h("button.primary", { type: "submit" }, "Submit human solution"));
  const onPaste = (e) => {
    const items = [...(e.clipboardData || {}).items || []].filter((i) => i.kind === "file");
    if (items.length) { e.preventDefault(); items.forEach((i) => addFile(i.getAsFile())); }
  };
  form.addEventListener("dragover", (e) => e.preventDefault());
  form.addEventListener("drop", (e) => { e.preventDefault(); [...e.dataTransfer.files].forEach(addFile); });
  const m = modal(`Human solution · ${ch.name}`, form, { wide: true, onClose: () => document.removeEventListener("paste", onPaste) });
  document.addEventListener("paste", onPaste);
  form.onsubmit = async (e) => {
    e.preventDefault();
    const d = Object.fromEntries(new FormData(form).entries());
    const body = { title: d.title, text: d.text, flag_id: d.flag_id ? Number(d.flag_id) : null,
      images: images.map((i) => ({ data_b64: i.url.split(",")[1], filename: i.filename, content_type: i.content_type })) };
    await guarded(() => api(`/api/challenges/${ch.id}/human-solution`, { method: "POST", body }), "Human solution submitted");
    m.close();
    await load();
    if (after) after();
  };
  setTimeout(() => text.focus(), 0);
}

// ------------------------------------------------------------------ team / agent.md / tokens / account
async function agentMdModal() {
  const doc = await guarded(() => api("/api/team/agent-md"));
  const ta = h("textarea.mono", { rows: 22, value: doc.content, disabled: !canWrite(),
    placeholder: "# Team agent instructions\nConventions, tooling, sandboxes, things every agent on this team should know…" });
  const preview = h("div.md");
  const upd = () => { preview.innerHTML = renderMarkdown(ta.value); };
  ta.addEventListener("input", upd); upd();
  const m = modal("Team agent.md", [
    h("p.muted", "Shared with every agent on the team. Agents ask their user once per session before loading it.",
      doc.updated_at ? ` Last edited by ${doc.updated_by} · ${fmtTime(doc.updated_at)}.` : ""),
    h("div.split", ta, preview),
    canWrite() && h("div.btns", h("button.primary", { onclick: async () => {
      await guarded(() => api("/api/team/agent-md", { method: "PUT", body: { content: ta.value } }), "agent.md saved");
      m.close();
    } }, "Save")),
  ], { wide: true });
}

async function teamModal() {
  const t = await guarded(() => api("/api/team"));
  const owner = t.my_role === "owner";
  const rows = t.members.map((u) => h("tr" + (u.disabled ? ".disabled" : ""),
    h("td", u.username), h("td", h("span.initials", u.initials)),
    h("td", owner && u.id !== CTF.me.user_id
      ? select("role", [["owner", "owner"], ["editor", "editor"], ["viewer", "viewer"]], u.role)
      : u.role),
    h("td", fmtTime(u.created_at)),
    h("td", owner && u.id !== CTF.me.user_id && h("button.mini" + (u.disabled ? "" : ".danger"), {
      onclick: async () => {
        await guarded(() => api(`/api/team/members/${u.id}`, { method: "PATCH", body: { disabled: !u.disabled } }));
        m.close(); teamModal();
      } }, u.disabled ? "Re-enable" : "Remove"))));
  const tbl = h("table.grid.small", h("thead", h("tr", ["User", "Initials", "Role", "Joined", ""].map((x) => h("th", x)))), h("tbody", rows));
  tbl.querySelectorAll("select").forEach((s, i) => {
    const target = t.members.filter((u) => u.id !== CTF.me.user_id)[i];
    s.onchange = () => guarded(() => api(`/api/team/members/${target.id}`, { method: "PATCH", body: { role: s.value } }), "Role updated");
  });
  const codes = h("div");
  const m = modal(`Team · ${t.name}`, [
    tbl,
    owner && h("div.section", h("h3", "Join codes"),
      h("p.muted", "Codes are stored hashed and cannot be shown again. Rotating invalidates the old codes."),
      h("button", { onclick: async () => {
        if (!confirm("Generate new join codes? The old ones stop working immediately.")) return;
        const c = await guarded(() => api("/api/team/rotate-codes", { method: "POST" }));
        codes.replaceChildren(h("dl.codes", h("dt", "Editor code"), h("dd", h("code.sel", c.editor_join_code)),
          h("dt", "Viewer code"), h("dd", h("code.sel", c.viewer_join_code))));
      } }, "Rotate join codes"), codes),
    owner && h("div.section", h("h3", "Audit log"), h("button", { onclick: async () => {
      const log = await guarded(() => api("/api/team/audit"));
      auditBox.replaceChildren(h("table.grid.small", h("tbody", log.map((r) =>
        h("tr", h("td", fmtTime(r.created_at)), h("td", r.username || "–"), h("td", r.action), h("td", r.detail), h("td.muted", r.ip))))));
    } }, "Show recent activity")),
  ], { wide: true });
  const auditBox = h("div.audit");
  m.wrap.querySelector(".modal-body").append(auditBox);
}

async function tokensModal() {
  const list = await guarded(() => api("/api/tokens"));
  const out = h("div");
  const form = h("form.inline", h("input.grow", { name: "label", placeholder: "Label, e.g. laptop-claude", required: true, maxLength: 80 }),
    h("button.primary", { type: "submit" }, "Create token"));
  form.onsubmit = async (e) => {
    e.preventDefault();
    const tok = await guarded(() => api("/api/tokens", { method: "POST", body: Object.fromEntries(new FormData(form)) }));
    out.replaceChildren(h("div.notice",
      h("p", h("strong", "Copy this token now — it will not be shown again.")),
      h("code.sel.block", tok.token),
      h("p.muted", `Expires ${fmtTime(tok.expires_at)}. Use it with: ctfsheet-mcp login --url ${location.origin} --token-stdin`)));
  };
  const m = modal("API tokens (for the MCP tool)", [
    h("p.muted", "Tokens let your AI agent act as you. Prefer `ctfsheet-mcp login`, which creates one for you."),
    form, out,
    h("table.grid.small", h("thead", h("tr", ["Label", "Created", "Last used", "Expires", ""].map((x) => h("th", x)))),
      h("tbody", list.map((t) => h("tr", h("td", t.label), h("td", fmtTime(t.created_at)), h("td", fmtTime(t.last_used_at) || "never"),
        h("td", fmtTime(t.expires_at)),
        h("td", h("button.mini.danger", { onclick: async () => {
          await guarded(() => api(`/api/tokens/${t.id}`, { method: "DELETE" }), "Revoked"); m.close(); tokensModal();
        } }, "Revoke")))))),
  ], { wide: true });
}

function accountModal() {
  const f1 = h("form.inline", h("input", { name: "initials", value: CTF.me.initials, maxLength: 4 }), h("button", { type: "submit" }, "Update initials"));
  f1.onsubmit = async (e) => {
    e.preventDefault();
    await guarded(() => api("/api/me", { method: "PATCH", body: { initials: new FormData(f1).get("initials") } }), "Initials updated");
    location.reload();
  };
  const f2 = h("form.form",
    field("Current password", h("input", { name: "current_password", type: "password", autocomplete: "current-password", required: true })),
    field("New password", h("input", { name: "new_password", type: "password", autocomplete: "new-password", required: true })),
    h("button", { type: "submit" }, "Change password"));
  f2.onsubmit = async (e) => {
    e.preventDefault();
    await guarded(() => api("/api/me", { method: "PATCH", body: Object.fromEntries(new FormData(f2)) }), "Password changed");
    f2.reset();
  };
  modal(`Account · ${CTF.me.username}`, [h("h3", "Initials (used on solves)"), f1, h("h3", "Password"), f2]);
}
