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

  function stepsRow(steps) {
    return steps.map(s =>
      `<div class="step-box text-center">
        <div class="w-10 h-10 mx-auto rounded-full bg-cyan-100 text-cyan-800 flex items-center justify-center font-bold">${steps.indexOf(s) + 1}</div>
        <div class="text-xs text-slate-600 mt-1 w-24">${s}</div>
      </div>`).join("");
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
      document.getElementById("corePipelineSteps").innerHTML = stepsRow(core.steps);
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
        <div class="flex flex-wrap gap-6 my-4">${stepsRow(p.steps)}</div>
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
          <div class="text-xs font-semibold text-slate-500 uppercase mb-2">Email that was sent</div>
          <div class="brief text-slate-700 border-l-4 border-cyan-200 pl-4">${escapeHtml(r.email || "(none)")}</div>
        </div>
        <div class="bg-white rounded-lg shadow p-5 mt-4">
          <div class="text-xs font-semibold text-slate-500 uppercase mb-2">Full situation brief</div>
          <div class="brief text-slate-700">${escapeHtml(r.brief || "(none)")}</div>
        </div>`;
    } catch (e) {
      el.innerHTML = `<div class="text-sm text-rose-600">Failed to load run: ${e.message}</div>`;
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
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
