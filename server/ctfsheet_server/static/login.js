"use strict";

(function () {
  const err = document.getElementById("error");
  const show = (id) => {
    document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("active", p.id === id));
    document.querySelectorAll(".auth-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.pane === id));
    err.textContent = "";
  };
  document.querySelectorAll(".auth-tabs button").forEach((b) => b.addEventListener("click", () => show(b.dataset.pane)));

  api("/api/auth/config", { noRedirect: true }).then((cfg) => {
    if (!cfg.allow_team_creation) document.getElementById("tab-create").hidden = true;
    if (cfg.creation_code_required) document.getElementById("creation-code-wrap").hidden = false;
  }).catch(() => {});

  const formData = (f) => Object.fromEntries(new FormData(f).entries());

  async function submit(form, path, after) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      err.textContent = "";
      const btn = form.querySelector("button[type=submit]");
      btn.disabled = true;
      try {
        const res = await api(path, { method: "POST", body: formData(form), noRedirect: true });
        after(res);
      } catch (ex) {
        err.textContent = ex.message;
      } finally {
        btn.disabled = false;
      }
    });
  }

  submit(document.getElementById("login"), "/api/auth/login", () => (location.href = "/"));
  submit(document.getElementById("join"), "/api/auth/join", () => (location.href = "/"));
  submit(document.getElementById("create"), "/api/auth/create-team", (res) => {
    document.getElementById("editor-code").textContent = res.editor_join_code;
    document.getElementById("viewer-code").textContent = res.viewer_join_code;
    document.querySelector(".auth-tabs").hidden = true;
    show("codes");
  });
})();
