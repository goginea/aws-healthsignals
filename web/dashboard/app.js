/* HealthSignals Admin Dashboard — frontend logic.
 *
 * Auth: Cognito USER_PASSWORD_AUTH via the Cognito IDP REST endpoint (no SDK
 * bundle needed). The ID token is sent as `Authorization` on every API call;
 * the API Gateway Cognito authorizer enforces it server-side.
 */
(function () {
  "use strict";

  const CFG = window.DASHBOARD_CONFIG || {};
  const IDP = `https://cognito-idp.${CFG.region}.amazonaws.com/`;
  const TOKEN_KEY = "hs_id_token";

  // --- Cognito auth (SRP-free USER_PASSWORD flow) -------------------------

  async function cognito(target, body) {
    const resp = await fetch(IDP, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}`,
      },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.message || data.__type || "Auth failed");
    return data;
  }

  async function login(email, password, newPassword) {
    const auth = await cognito("InitiateAuth", {
      AuthFlow: "USER_PASSWORD_AUTH",
      ClientId: CFG.userPoolClientId,
      AuthParameters: { USERNAME: email, PASSWORD: password },
    });
    // First-login: Cognito requires a permanent password.
    if (auth.ChallengeName === "NEW_PASSWORD_REQUIRED") {
      if (!newPassword) {
        const err = new Error("NEW_PASSWORD_REQUIRED");
        err.challenge = "NEW_PASSWORD_REQUIRED";
        throw err;
      }
      const done = await cognito("RespondToAuthChallenge", {
        ChallengeName: "NEW_PASSWORD_REQUIRED",
        ClientId: CFG.userPoolClientId,
        Session: auth.Session,
        ChallengeResponses: { USERNAME: email, NEW_PASSWORD: newPassword },
      });
      return done.AuthenticationResult.IdToken;
    }
    return auth.AuthenticationResult.IdToken;
  }

  function token() { return sessionStorage.getItem(TOKEN_KEY); }

  async function api(path, opts) {
    const base = CFG.apiUrl.replace(/\/$/, "");
    const resp = await fetch(base + path, Object.assign({
      headers: { Authorization: token() },
    }, opts || {}));
    if (resp.status === 401 || resp.status === 403) { logout(); throw new Error("Session expired"); }
    if (!resp.ok) throw new Error(`API ${path} -> ${resp.status}`);
    return resp.json();
  }

  // --- rendering helpers --------------------------------------------------

  const STATUS_STYLE = {
    deployed:    "bg-emerald-100 text-emerald-700",
    in_progress: "bg-amber-100 text-amber-700",
    failed:      "bg-rose-100 text-rose-700",
    not_deployed:"bg-slate-100 text-slate-500",
    unknown:     "bg-slate-100 text-slate-500",
  };
  const RUN_STYLE = {
    SUCCEEDED: "text-emerald-600", RUNNING: "text-amber-600",
    FAILED: "text-rose-600", TIMED_OUT: "text-rose-600", ABORTED: "text-slate-500",
  };

  function statusPill(status) {
    const cls = STATUS_STYLE[status] || STATUS_STYLE.unknown;
    return `<span class="pill ${cls}">${(status || "unknown").replace("_", " ")}</span>`;
  }

  function stackCard(item) {
    const label = (item.stack || "").replace("HealthSignals-", "");
    const drift = item.drift_status ? `<div class="text-xs text-slate-400 mt-1">drift: ${item.drift_status}</div>` : "";
    return `<div class="bg-white rounded-lg shadow p-4">
      <div class="text-sm font-semibold text-cyan-900">${label}</div>
      <div class="mt-2">${statusPill(item.status)}</div>
      ${drift}
      <button data-drift="${item.stack}" class="drift-btn mt-2 text-xs text-cyan-700 hover:underline">check drift</button>
    </div>`;
  }

  // Short label for a Bedrock inference-profile / model id, e.g.
  // "us.anthropic.claude-sonnet-4-5-20250929-v1:0" -> "Claude Sonnet 4-5".
  function modelLabel(id) {
    if (!id) return "—";
    const m = id.match(/claude-(sonnet|haiku|opus)-?([0-9-]*)/i);
    if (m) {
      const fam = m[1][0].toUpperCase() + m[1].slice(1);
      const ver = (m[2] || "").replace(/-+$/, "").replace(/-/g, ".");
      return `Claude ${fam}${ver ? " " + ver : ""}`;
    }
    return id;
  }

  function sourceChip(s) {
    const on = s.enabled;
    const cls = on ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                   : "bg-slate-100 text-slate-400 border-slate-200";
    const dot = on ? "bg-emerald-500" : "bg-slate-300";
    const pri = s.priority === "primary"
      ? ' <span class="text-[10px] uppercase tracking-wide text-cyan-600">primary</span>' : "";
    return `<span class="inline-flex items-center gap-1.5 border ${cls} rounded-full px-2.5 py-1 text-xs">
      <span class="w-1.5 h-1.5 rounded-full ${dot}"></span>${escapeHtml(s.display_name)}${pri}</span>`;
  }

  // Config-driven panel replacing the old numbered step diagram: the pipeline's
  // live data sources and the Bedrock model(s) it uses (read from S3 config).
  function pipelineConfig(p) {
    const sources = p.data_sources || [];
    const chips = sources.length
      ? `<div class="flex flex-wrap gap-2">${sources.map(sourceChip).join("")}</div>`
      : `<div class="text-sm text-slate-400">No dedicated data sources.</div>`;
    const b = p.bedrock || {};
    const upgrade = (b.severity_threshold_for_upgrade || []).join(" / ");
    const hiRow = b.high_severity_model_id
      ? `<div class="text-xs text-slate-500 mt-1">High severity (${escapeHtml(upgrade || "HIGH/CRITICAL")}):
           <span class="font-medium text-slate-700">${escapeHtml(modelLabel(b.high_severity_model_id))}</span></div>`
      : "";
    return `
      <div class="grid md:grid-cols-2 gap-4">
        <div>
          <div class="text-xs font-semibold text-slate-500 uppercase mb-2">Data sources</div>
          ${chips}
        </div>
        <div>
          <div class="text-xs font-semibold text-slate-500 uppercase mb-2">Bedrock model</div>
          <div class="text-sm text-slate-700 font-medium">${escapeHtml(modelLabel(b.routine_model_id))}</div>
          ${hiRow}
        </div>
      </div>`;
  }

  function runRow(run, pipelineKey) {
    const cls = RUN_STYLE[run.status] || "text-slate-500";
    const when = run.started_at ? new Date(run.started_at).toLocaleString() : "";
    return `<button data-run="${encodeURIComponent(run.execution_arn)}"
        class="run-btn w-full text-left flex items-center justify-between px-3 py-2 rounded hover:bg-slate-50 border border-slate-100">
      <span class="text-sm font-mono text-slate-700 truncate">${run.name || "(run)"}</span>
      <span class="text-xs ${cls} font-semibold ml-3 shrink-0">${run.status} · ${when}</span>
    </button>`;
  }

  function renderRuns(container, runs, key) {
    if (!runs || !runs.length) {
      container.innerHTML = `<div class="text-sm text-slate-400">No runs yet.</div>`;
      return;
    }
    container.innerHTML = runs.map(r => runRow(r, key)).join("");
  }

  // --- data loading -------------------------------------------------------

  async function loadStatus() {
    const data = await api("/status");
    document.getElementById("coreStatusCards").innerHTML = data.core.map(stackCard).join("");
    return data;
  }

  async function loadPipelines() {
    const data = await api("/pipelines");
    const core = data.pipelines.find(p => p.key === "core");
    if (core) {
      document.getElementById("corePipelineSteps").innerHTML = pipelineConfig(core);
      renderRuns(document.getElementById("coreRuns"), core.recent_runs, "core");
    }
    // Plugins
    const status = await api("/status");
    const cont = document.getElementById("pluginsContainer");
    const plugins = data.pipelines.filter(p => p.key !== "core");
    if (!plugins.length) {
      cont.innerHTML = `<div class="text-sm text-slate-400">No plugins enabled.</div>`;
      return;
    }
    cont.innerHTML = plugins.map(p => {
      const st = (status.plugins || []).find(x => x.stack && x.stack.includes(p.name)) || {};
      return `<div class="bg-white rounded-lg shadow p-5">
        <div class="flex items-center justify-between">
          <h3 class="font-bold text-cyan-900">${p.name}</h3>
          <span>${statusPill(st.status || "deployed")}</span>
        </div>
        <div class="my-4">${pipelineConfig(p)}</div>
        <h4 class="text-sm font-semibold text-slate-500 uppercase mb-2">Last 5 runs</h4>
        <div class="space-y-1" data-plugin-runs="${p.key}"></div>
      </div>`;
    }).join("");
    plugins.forEach(p => {
      const el = cont.querySelector(`[data-plugin-runs="${p.key}"]`);
      if (el) renderRuns(el, p.recent_runs, p.key);
    });
  }

  async function loadRunDetail(arn) {
    const el = document.getElementById("runDetail");
    el.innerHTML = `<div class="text-sm text-slate-400">Loading run…</div>`;
    document.getElementById("run-detail").scrollIntoView({ behavior: "smooth" });
    try {
      const r = await api(`/runs?arn=${arn}`);
      const cls = r.classification || {};
      const sev = cls.severity || "—";
      const sevCls = { CRITICAL: "bg-rose-600", HIGH: "bg-rose-500", MODERATE: "bg-amber-500", LOW: "bg-emerald-500" }[sev] || "bg-slate-400";
      const delivery = r.delivery ? JSON.stringify(r.delivery, null, 2) : "—";
      el.innerHTML = `
        <div class="grid md:grid-cols-3 gap-4">
          <div class="bg-white rounded-lg shadow p-4">
            <div class="text-xs font-semibold text-slate-500 uppercase">Bedrock Classification</div>
            <div class="mt-2"><span class="pill text-white ${sevCls}">${sev}</span></div>
            ${cls.confidence != null ? `<div class="text-sm text-slate-500 mt-2">confidence: ${cls.confidence}</div>` : ""}
            ${cls.reasoning ? `<p class="text-xs text-slate-500 mt-2">${cls.reasoning}</p>` : ""}
          </div>
          <div class="bg-white rounded-lg shadow p-4">
            <div class="text-xs font-semibold text-slate-500 uppercase">Delivery</div>
            <pre class="text-xs text-slate-600 mt-2 overflow-auto max-h-40">${delivery}</pre>
          </div>
          <div class="bg-white rounded-lg shadow p-4">
            <div class="text-xs font-semibold text-slate-500 uppercase">Run</div>
            <div class="text-sm mt-2">${r.status}</div>
            <div class="text-xs text-slate-400">${r.started_at ? new Date(r.started_at).toLocaleString() : ""}</div>
          </div>
        </div>
        <div class="bg-white rounded-lg shadow p-5 mt-4">
          <div class="text-xs font-semibold text-slate-500 uppercase mb-2">Communication sent</div>
          ${communicationsBlock(r)}
        </div>
        ${briefDiffersFromEmail(r) ? `
        <details class="bg-white rounded-lg shadow p-5 mt-4">
          <summary class="cursor-pointer select-none text-xs font-semibold text-slate-500 uppercase">Full situation brief</summary>
          <div class="brief text-slate-700 mt-2">${renderMarkdown(r.brief)}</div>
        </details>` : ""}`;
    } catch (e) {
      el.innerHTML = `<div class="text-sm text-rose-600">Failed to load run: ${e.message}</div>`;
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  }

  // Render a SAFE subset of Markdown to HTML. Bedrock output is untrusted, so
  // we escape ALL HTML first, then apply formatting on the escaped text only.
  // No raw HTML from the model can survive this, so it cannot inject markup.
  // Supported: # headings, **bold**, *italic*, `code`, [links](url),
  // - / * bullet lists, 1. ordered lists, > blockquotes, | GFM tables |,
  // --- horizontal rules, blank-line paragraphs, single-newline line breaks.
  function renderMarkdown(src) {
    const escaped = escapeHtml(src == null ? "" : src);

    // Inline formatting, applied to already-escaped text only.
    // Links: only http(s)/mailto schemes are allowed; anything else (e.g.
    // javascript:, data:) is left as plain text so it cannot execute.
    const inline = (t) => t
      .replace(/`([^`]+)`/g, '<code class="bg-slate-100 px-1 rounded text-sm">$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, text, url) => {
        // url is already HTML-escaped; validate the scheme before trusting it.
        const safe = /^(https?:|mailto:)/i.test(url) || /^[/#]/.test(url);
        return safe
          ? `<a href="${url}" target="_blank" rel="noopener noreferrer" class="text-cyan-700 underline">${text}</a>`
          : m;
      });

    const splitRow = (row) => row.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());

    // Split into blocks on blank lines so lists and paragraphs are separable.
    const blocks = escaped.replace(/\r\n/g, "\n").split(/\n{2,}/);
    const html = blocks.map((block) => {
      const lines = block.split("\n");
      const nonEmpty = lines.filter((l) => l.trim() !== "");

      // Horizontal rule: a block that is only dashes/asterisks/underscores.
      if (/^\s*([-*_])\1{2,}\s*$/.test(block)) {
        return "<hr>";
      }
      // Headings: render each # .. ###### line as its own heading. Handles a
      // block that is one heading OR several stacked headings with no blank
      // line between them (e.g. a "# Title\n## Subtitle" brief header).
      const headingLine = (l) => {
        const m = l.match(/^(#{1,6})\s+(.*)$/);
        if (!m) return null;
        const level = Math.min(m[1].length, 6);
        const size = level <= 2 ? "text-base font-bold" : "text-sm font-semibold";
        return `<h${level} class="${size} text-slate-800 mt-1">${inline(m[2])}</h${level}>`;
      };
      if (nonEmpty.length > 0 && nonEmpty.every((l) => headingLine(l))) {
        return nonEmpty.map(headingLine).join("");
      }
      // GFM table: header row, a |---|---| separator, then body rows.
      if (nonEmpty.length >= 2 && /\|/.test(nonEmpty[0]) && /^\s*\|?[\s:|-]+\|?\s*$/.test(nonEmpty[1]) && /-/.test(nonEmpty[1])) {
        const head = splitRow(nonEmpty[0]).map((c) => `<th class="border border-slate-200 px-2 py-1 text-left font-semibold">${inline(c)}</th>`).join("");
        const body = nonEmpty.slice(2).map((row) =>
          `<tr>${splitRow(row).map((c) => `<td class="border border-slate-200 px-2 py-1">${inline(c)}</td>`).join("")}</tr>`
        ).join("");
        return `<table class="border-collapse text-sm my-2"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
      }
      // Ordered list: every non-empty line starts with "N." or "N)".
      if (nonEmpty.length > 0 && nonEmpty.every((l) => /^\s*\d+[.)]\s+/.test(l))) {
        const items = nonEmpty.map((l) => `<li>${inline(l.replace(/^\s*\d+[.)]\s+/, ""))}</li>`).join("");
        return `<ol class="list-decimal pl-5 space-y-1">${items}</ol>`;
      }
      // Bullet list: every non-empty line starts with - or * (optional indent).
      if (nonEmpty.length > 0 && nonEmpty.every((l) => /^\s*[-*]\s+/.test(l))) {
        const items = nonEmpty.map((l) => `<li>${inline(l.replace(/^\s*[-*]\s+/, ""))}</li>`).join("");
        return `<ul class="list-disc pl-5 space-y-1">${items}</ul>`;
      }
      // Blockquote: every non-empty line starts with ">". Note the text is
      // already HTML-escaped at this point, so ">" appears as "&gt;".
      if (nonEmpty.length > 0 && nonEmpty.every((l) => /^\s*&gt;\s?/.test(l))) {
        const inner = nonEmpty.map((l) => inline(l.replace(/^\s*&gt;\s?/, ""))).join("<br>");
        return `<blockquote class="border-l-4 border-slate-300 pl-3 text-slate-600 italic">${inner}</blockquote>`;
      }
      // Paragraph (possibly with a stray heading line mixed in). Emit any
      // heading line as its own heading and group the surrounding prose lines
      // into <p>…<br>…</p> runs, so a heading is never glued to body text.
      let out = "";
      let para = [];
      const flush = () => {
        if (para.length) { out += `<p>${para.map(inline).join("<br>")}</p>`; para = []; }
      };
      for (const l of lines) {
        const hl = headingLine(l);
        if (hl) { flush(); out += hl; }
        else if (l.trim() !== "") { para.push(l); }
      }
      flush();
      return out;
    }).join("");
    return html;
  }

  // Render the per-channel communications as collapsible <details> sections.
  // Email is expanded by default; other channels (SMS, and any future
  // destinations) are collapsed. Falls back to the flat r.email for older API
  // responses that don't include a communications list.
  function communicationsBlock(r) {
    const comms = Array.isArray(r.communications) ? r.communications : [];
    if (!comms.length) {
      const body = r.email ? renderMarkdown(r.email) : "(none)";
      return `<div class="brief text-slate-700 border-l-4 border-cyan-200 pl-4">${body}</div>`;
    }
    return comms.map((c) => {
      const open = c.channel === "email" ? " open" : "";
      const label = escapeHtml(c.label || c.channel || "Channel");
      const body = c.content ? renderMarkdown(c.content) : "(none)";
      return `<details${open} class="border border-slate-100 rounded mb-2">
        <summary class="cursor-pointer select-none px-3 py-2 text-sm font-semibold text-cyan-800 bg-slate-50 rounded">${label}</summary>
        <div class="brief text-slate-700 border-l-4 border-cyan-200 pl-4 px-3 py-2">${body}</div>
      </details>`;
    }).join("");
  }

  // Show the "Full situation brief" panel only when the brief carries content
  // distinct from the email. Core runs draft a separate communication email, so
  // brief != email; shortage/outbreak plugins derive the email from the brief,
  // so they are identical and the second panel would be pure duplication.
  function briefDiffersFromEmail(r) {
    const brief = (r.brief || "").trim();
    if (!brief) return false;
    const email = (r.email || "").trim();
    const norm = (s) => s.replace(/\s+/g, " ");
    return norm(brief) !== norm(email);
  }

  async function refreshAll() {
    try {
      await loadStatus();
      await loadPipelines();
    } catch (e) {
      console.error(e);
    }
  }

  // --- event wiring -------------------------------------------------------

  document.addEventListener("click", async (ev) => {
    const runBtn = ev.target.closest(".run-btn");
    if (runBtn) { loadRunDetail(runBtn.getAttribute("data-run")); return; }
    const driftBtn = ev.target.closest(".drift-btn");
    if (driftBtn) {
      const stack = driftBtn.getAttribute("data-drift");
      driftBtn.textContent = "checking…";
      try {
        const d = await api(`/drift/${encodeURIComponent(stack)}`, { method: "POST" });
        driftBtn.textContent = `drift: ${d.drift_status || d.detection_status || "started"}`;
      } catch (e) { driftBtn.textContent = "drift check failed"; }
    }
  });

  function showDashboard() {
    document.getElementById("loginView").classList.add("hidden");
    document.getElementById("dashView").classList.remove("hidden");
    renderStaticSections();
    refreshAll();
  }

  function logout() {
    sessionStorage.removeItem(TOKEN_KEY);
    document.getElementById("dashView").classList.add("hidden");
    document.getElementById("loginView").classList.remove("hidden");
  }

  document.getElementById("loginForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const err = document.getElementById("loginError");
    err.classList.add("hidden");
    const email = document.getElementById("email").value.trim();
    const password = document.getElementById("password").value;
    const newPassword = document.getElementById("newPassword").value;
    try {
      const idToken = await login(email, password, newPassword);
      sessionStorage.setItem(TOKEN_KEY, idToken);
      document.getElementById("whoami").textContent = email;
      showDashboard();
    } catch (e) {
      if (e.challenge === "NEW_PASSWORD_REQUIRED") {
        document.getElementById("newPwWrap").classList.remove("hidden");
        err.textContent = "First login — set a new password above and sign in again.";
        err.classList.remove("hidden");
        return;
      }
      err.textContent = e.message;
      err.classList.remove("hidden");
    }
  });

  document.getElementById("logoutBtn").addEventListener("click", logout);
  document.getElementById("refreshBtn").addEventListener("click", refreshAll);

  // Resume an existing session.
  if (token()) showDashboard();

  // Static sections (influenza spread, geographic zoom) are injected by
  // renderStaticSections(), defined in static-sections.js.
  function renderStaticSections() {
    if (window.renderHealthSignalsStatic) {
      window.renderHealthSignalsStatic(document.getElementById("staticSections"));
    }
  }
})();
