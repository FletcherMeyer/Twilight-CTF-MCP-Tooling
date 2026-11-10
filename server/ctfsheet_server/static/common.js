/* Shared helpers: API client, DOM builder, safe markdown renderer. */
"use strict";

const CTF = { csrf: null, me: null };

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {}, credentials: "same-origin" };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  if (init.method !== "GET" && CTF.csrf) init.headers["X-CSRF-Token"] = CTF.csrf;
  const res = await fetch(path, init);
  let data = null;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    if (res.status === 401 && !opts.noRedirect) { location.href = "/login"; }
    let msg = (data && data.detail) || res.statusText;
    if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join("; ");
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return data;
}

/* h("div.cls#id", {attr: v, onclick: fn}, child, "text", [children]) */
function h(sel, attrs, ...kids) {
  const m = sel.match(/^([a-z0-9]+)?((?:[.#][\w-]+)*)$/i);
  const el = document.createElement((m && m[1]) || "div");
  ((m && m[2]) || "").replace(/([.#])([\w-]+)/g, (_, t, v) => {
    if (t === ".") el.classList.add(v); else el.id = v;
  });
  if (attrs && (typeof attrs !== "object" || attrs instanceof Node || Array.isArray(attrs))) {
    kids.unshift(attrs); attrs = null;
  }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v; // only ever used with renderMarkdown() output
    else if (k in el && k !== "list" && k !== "type") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  const add = (c) => {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) c.forEach(add);
    else el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  };
  kids.forEach(add);
  return el;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* Minimal, XSS-safe markdown: everything is escaped first, then a small set of
   constructs is re-introduced. Links are limited to http(s). */
function renderMarkdown(src) {
  const blocks = String(src || "").replace(/\r\n/g, "\n").split(/^```/m);
  let out = "";
  blocks.forEach((blk, i) => {
    if (i % 2 === 1) {
      const nl = blk.indexOf("\n");
      const lang = nl >= 0 ? blk.slice(0, nl).trim() : "";
      const code = nl >= 0 ? blk.slice(nl + 1) : blk;
      out += `<pre class="code"${lang ? ` data-lang="${esc(lang)}"` : ""}><code>${esc(code.replace(/\n$/, ""))}</code></pre>`;
    } else {
      out += mdBlocks(blk);
    }
  });
  return out;
}

function mdInline(s) {
  s = esc(s);
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  s = s.replace(/!?\[([^\]]*)\]\((https?:\/\/[^\s)]+)\)/g, (_, t, u) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${t || u}</a>`);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  s = s.replace(/\u0000(\d+)\u0000/g, (_, n) => `<code>${codes[+n]}</code>`);
  return s;
}

function mdBlocks(text) {
  const lines = text.split("\n");
  let html = "", para = [], list = null;
  const flushPara = () => { if (para.length) { html += `<p>${para.map(mdInline).join("<br>")}</p>`; para = []; } };
  const flushList = () => { if (list) { html += `<${list.t}>${list.items.map((x) => `<li>${mdInline(x)}</li>`).join("")}</${list.t}>`; list = null; } };
  for (const line of lines) {
    let m;
    if (!line.trim()) { flushPara(); flushList(); continue; }
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) { flushPara(); flushList(); const n = Math.min(m[1].length + 2, 6); html += `<h${n}>${mdInline(m[2])}</h${n}>`; continue; }
    if (/^(-{3,}|\*{3,})\s*$/.test(line)) { flushPara(); flushList(); html += "<hr>"; continue; }
    if ((m = line.match(/^>\s?(.*)$/))) { flushPara(); flushList(); html += `<blockquote>${mdInline(m[1])}</blockquote>`; continue; }
    if ((m = line.match(/^\s*([-*+]|\d+\.)\s+(.*)$/))) {
      flushPara();
      const t = /\d/.test(m[1]) ? "ol" : "ul";
      if (!list || list.t !== t) { flushList(); list = { t, items: [] }; }
      list.items.push(m[2]);
      continue;
    }
    if ((m = line.match(/^\|(.*)\|\s*$/))) {
      flushList(); flushPara();
      if (/^[\s|:-]+$/.test(m[1])) continue;
      html += `<div class="md-tr">${m[1].split("|").map((c) => `<span>${mdInline(c.trim())}</span>`).join("")}</div>`;
      continue;
    }
    flushList();
    para.push(line);
  }
  flushPara(); flushList();
  return html;
}

function fmtSize(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function toast(msg, kind = "ok") {
  let box = document.getElementById("toasts");
  if (!box) { box = h("div#toasts"); document.body.appendChild(box); }
  const t = h(`div.toast.${kind}`, msg);
  box.appendChild(t);
  setTimeout(() => t.remove(), kind === "err" ? 6000 : 3000);
}
