// ─── HELPERS ──────────────────────────────────────────────────────────────────
function $(id) { return document.getElementById(id); }
function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "className") e.className = v;
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2).toLowerCase(), v);
    // Boolean attributes (disabled, checked, hidden, readonly, etc.) — the
    // presence of the attribute is what matters, not its value. setAttribute("disabled", false)
    // still leaves the element disabled. Set the property directly and skip the attribute.
    else if (typeof v === "boolean") { if (v) e[k] = true; }
    else if (v != null) e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return e;
}
function esc(s) { const d = document.createElement("div"); d.textContent = String(s ?? ""); return d.innerHTML; }
async function api(path, opts = {}) {
  let resp;
  try {
    resp = await fetch(path, opts);
  } catch (networkErr) {
    const e = new Error(networkErr.message || "Network error");
    e.status = 0;
    showErrorToast(e);
    throw e;
  }
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    const msg = err.detail || err.error || resp.statusText || `HTTP ${resp.status}`;
    const e = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    e.status = resp.status;
    e.retryAfter = err.retry_after_seconds || Number(resp.headers.get("retry-after")) || null;
    e.payload = err;
    showErrorToast(e);
    throw e;
  }
  return resp.json();
}

let _toastTimer = null;
function showErrorToast(e) {
  const t = document.getElementById("error-toast");
  if (!t) return;
  let label;
  if (e.status === 429 && e.retryAfter) {
    label = `⏱ Gemini rate-limited. Retry in ~${e.retryAfter}s. ${e.message}`;
  } else if (e.status === 401) {
    label = `🔒 Sign in expired — refresh the page and sign in again.`;
  } else if (e.status === 502 || e.status === 503) {
    label = `⚠ Upstream service unavailable: ${e.message}`;
  } else if (e.status >= 500) {
    label = `❌ Server error: ${e.message}`;
  } else if (e.status >= 400) {
    label = `❌ ${e.message}`;
  } else if (e.status === 0) {
    label = `❌ Network error: ${e.message}`;
  } else {
    label = `❌ ${e.message}`;
  }
  t.innerHTML = "";
  t.appendChild(document.createTextNode(label));
  const close = document.createElement("button");
  close.textContent = "Dismiss";
  close.onclick = () => t.classList.add("hidden");
  t.appendChild(close);
  t.classList.remove("hidden");
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.classList.add("hidden"), 12000);
}

// ─── DATE ─────────────────────────────────────────────────────────────────────
const now = new Date();
const days = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"];
const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
$("date-day").textContent = now.getDate();
$("date-label").textContent = `${days[now.getDay()]} · ${months[now.getMonth()]} ${now.getFullYear()}`;

// ─── STATE ────────────────────────────────────────────────────────────────────
const state = {
  user: null,
  calls: [],
  activeCall: null,
  briefing: null,        // { id, output, edits }
  edits: {},             // pending edits to merge before approve
};

// ─── HEALTH BANNER ────────────────────────────────────────────────────────────
async function loadHealth() {
  try {
    const h = await api("/api/health");
    const missing = [];
    if (!h.gemini) missing.push("GEMINI_API_KEY");
    if (!h.calendar_oauth) missing.push("GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET");
    if (!h.app_secret) missing.push("APP_SECRET_KEY");
    if (!h.sheets) missing.push("GOOGLE_CREDENTIALS_JSON / GOOGLE_SHEET_ID (Sheets)");
    if (!h.slack) missing.push("SLACK_WEBHOOK_URL (optional)");
    if (missing.length) {
      const banner = $("health-banner");
      banner.classList.remove("hidden");
      banner.textContent = `⚠️ Missing env vars: ${missing.join(", ")}. Set them in .env and restart.`;
    }
  } catch {}
}

// ─── CALENDAR ─────────────────────────────────────────────────────────────────
// ─── AUTH ─────────────────────────────────────────────────────────────────────
$("google-login-btn").addEventListener("click", () => {
  window.location.href = "/api/auth/google";
});

$("google-logout-btn").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" });
  state.user = null;
  state.calls = [];
  state.activeCall = null;
  $("workbench").classList.add("hidden");
  renderAuth();
  renderCalls();
});

async function loadAuth() {
  try {
    const result = await api("/api/auth/me");
    state.user = result.authenticated ? result.user : null;
  } catch {
    state.user = null;
  }
  renderAuth();
}

function renderAuth() {
  const panel = $("auth-panel");
  const subtitle = $("auth-subtitle");
  const login = $("google-login-btn");
  const logout = $("google-logout-btn");
  panel.classList.remove("hidden");
  if (state.user) {
    subtitle.textContent = `Signed in as ${state.user.email}. Calls are loaded from this Google Calendar.`;
    login.classList.add("hidden");
    logout.classList.remove("hidden");
  } else {
    subtitle.textContent = "Sign in with Google to load your own scheduled partner calls.";
    login.classList.remove("hidden");
    logout.classList.add("hidden");
  }
}

// Reload button bypasses the server-side 5-min cache; auto-load uses it.
$("reload-calls-btn").addEventListener("click", () => loadCalls(true));

async function loadCalls(force = false) {
  const list = $("calls-list");
  if (!state.user) {
    list.innerHTML = `<div class="muted">Sign in with Google to load your calendar.</div>`;
    return;
  }
  list.innerHTML = `<div class="muted"><span class="spinner"></span> Reading your calendar...</div>`;
  try {
    const url = force ? "/api/calendar/today?refresh=true" : "/api/calendar/today";
    const data = await api(url);
    state.calls = data.calls || [];
    state.calendarCached = !!data.cached;
    state.calendarCachedAge = data.cached_age_seconds || 0;
    renderCalls();
  } catch (e) {
    list.innerHTML = `<div class="muted" style="color:var(--red)">Could not load calendar: ${esc(e.message)}</div>`;
  }
}

function renderCalls() {
  const list = $("calls-list");
  list.innerHTML = "";
  if (!state.user) {
    list.innerHTML = `<div class="muted">Sign in with Google to load your calendar.</div>`;
    return;
  }
  if (state.calendarCached) {
    const age = state.calendarCachedAge || 0;
    const ageLabel = age < 60 ? `${age}s ago` : `${Math.floor(age / 60)}m ago`;
    list.appendChild(el("div", { className: "muted", style: "font-size:10px;margin-bottom:6px" },
      `cached ${ageLabel} · click Reload to re-fetch`));
  }
  if (!state.calls.length) {
    list.appendChild(el("div", { className: "muted" }, "No external partner calls today."));
    return;
  }
  const tierOrder = { P0: 0, P1: 1, P2: 2, unlisted: 3 };
  const sorted = [...state.calls].sort((a, b) =>
    (tierOrder[a.tier] ?? 9) - (tierOrder[b.tier] ?? 9)
    || (a.time || "").localeCompare(b.time || ""),
  );
  for (const call of sorted) {
    const tierClass = `tier-${(call.tier || "unlisted").toLowerCase()}`;
    const tierBadge = call.tier
      ? el("span", { className: `tier-badge ${tierClass}` }, call.tier)
      : null;
    const partnerLine = el("div", { className: "call-partner" }, call.partner);
    if (tierBadge) partnerLine.appendChild(tierBadge);
    const row = el("div", {
      className: "call-row",
      onclick: () => openWorkbench(call),
    },
      el("div", { className: "call-meta" },
        el("div", { className: "call-time" }, call.time || "—"),
        el("div", {},
          partnerLine,
          el("div", { className: "call-title" }, call.title || ""),
        ),
      ),
      el("div", { className: "muted" }, "→"),
    );
    if (state.activeCall && state.activeCall.partner === call.partner) row.classList.add("active");
    list.appendChild(row);
  }
}

// ─── WORKBENCH ────────────────────────────────────────────────────────────────
$("wb-close").addEventListener("click", () => {
  $("workbench").classList.add("hidden");
  state.activeCall = null;
  renderCalls();
});

document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    document.querySelector(`[data-panel="${t.dataset.tab}"]`).classList.add("active");
  }),
);

function openWorkbench(call) {
  state.activeCall = call;
  state.briefing = null;
  state.edits = {};
  $("workbench").classList.remove("hidden");
  $("wb-partner").textContent = call.partner;
  $("wb-subtitle").textContent = `${call.time || ""} · ${call.title || ""}`;
  $("brief-output").innerHTML = "";
  $("approve-row").classList.add("hidden");
  $("view-original-row").classList.add("hidden");
  $("clone-btn").classList.add("hidden");
  $("edit-approved-btn").classList.add("hidden");
  $("slack-preview").classList.add("hidden");
  state.viewOriginal = false;
  renderCalls();
  loadNotes();
  loadPerformance();
  loadTranscripts();
  document.querySelectorAll(".tab")[0].click();
}

// ─── BRIEFING ─────────────────────────────────────────────────────────────────
$("run-briefing-btn").addEventListener("click", runBriefing);

async function runBriefing() {
  const call = state.activeCall;
  if (!call) return;
  const btn = $("run-briefing-btn");
  btn.disabled = true;
  $("brief-status").innerHTML = `<span class="spinner"></span> Gathering news, transcripts, performance, notes... synthesizing...`;
  $("brief-output").innerHTML = "";
  $("approve-row").classList.add("hidden");
  try {
    const result = await api("/api/briefing/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ partner: call.partner, call }),
    });
    state.briefing = result;
    state.edits = {};
    state.viewOriginal = false;
    renderBrief(result.output);
    $("approve-row").classList.remove("hidden");
    $("view-original-row").classList.remove("hidden");
    $("view-original-btn").textContent = "👁 View original (pre-edit)";
    $("view-mode-label").textContent = "";
    $("approve-btn").textContent = "✓ Approve";
    $("approve-btn").disabled = false;
    $("clone-btn").classList.add("hidden");
    $("edit-approved-btn").classList.add("hidden");
    $("approve-status").textContent = "";
    $("brief-status").textContent = `Draft generated · briefing #${result.id}`;
    loadHistory();
  } catch (e) {
    $("brief-status").textContent = `Error: ${e.message}`;
    $("brief-status").style.color = "var(--red)";
  } finally {
    btn.disabled = false;
  }
}

const CURRENCY_SYMBOLS = { USD: "$", EUR: "€", GBP: "£", CHF: "CHF ", INR: "₹", JPY: "¥", CAD: "C$", AUD: "A$", BRL: "R$" };
function fmtMoney(n, currency) {
  if (n == null) return "—";
  const num = Math.round(n).toLocaleString();
  const prefix = CURRENCY_SYMBOLS[currency] ?? (currency ? `${currency} ` : "$");
  return prefix + num;
}
function fmtNum(n) { return n == null ? "—" : Math.round(n).toLocaleString(); }
function fmtPct(n) { return n == null ? "—" : (n * 100).toFixed(1) + "%"; }

function listEditor(items, setter, label = "item") {
  const wrap = el("div");
  const arr = (items || []).slice();
  function paint() {
    wrap.innerHTML = "";
    arr.forEach((v, i) => {
      const ta = el("textarea", { className: "editable", rows: "2" });
      ta.value = v;
      ta.addEventListener("input", () => { arr[i] = ta.value; setter(arr.slice()); });
      const del = el("button", {
        className: "btn-secondary",
        style: "padding:4px 8px;font-size:10px;margin-left:6px;",
      }, "✕");
      del.addEventListener("click", () => { arr.splice(i, 1); setter(arr.slice()); paint(); });
      wrap.appendChild(el("div", { style: "display:flex;gap:6px;margin-bottom:6px;" }, ta, del));
    });
    const add = el("button", {
      className: "btn-secondary",
      style: "padding:4px 10px;font-size:10px;",
    }, `+ Add ${label}`);
    add.addEventListener("click", () => { arr.push(""); setter(arr.slice()); paint(); });
    wrap.appendChild(add);
  }
  paint();
  return wrap;
}

function renderBrief(brief) {
  const out = $("brief-output");
  out.innerHTML = "";
  if (!brief) return;

  // Snapshot
  const snap = brief.account_snapshot || {};
  out.appendChild(briefSection("📌 Account Overview", () => el("div", {},
    el("div", { className: "lead" }, snap.headline || "—"),
    el("h5", { style: "margin:8px 0 4px;font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:0.08em" }, "Recent Changes"),
    bulletList(snap.what_changed || []),
    el("h5", { style: "margin:8px 0 4px;font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:0.08em" }, "Current Priorities"),
    el("div", { className: "tp-rationale" }, snap.what_matters_now || "—"),
  ), () => editorBlock([
    ["headline", "Headline", snap.headline || "", (v) => editPath("account_snapshot", "headline", v)],
    ["what_changed", "Recent Changes", snap.what_changed || [], (arr) => editPath("account_snapshot", "what_changed", arr), "list"],
    ["what_matters_now", "Current Priorities", snap.what_matters_now || "", (v) => editPath("account_snapshot", "what_matters_now", v), "long"],
  ])));

  // Performance Summary — render one metric block per uploaded summary so multi-currency
  // partners aren't squashed into one misleading row.
  const perf = brief.performance_read || {};
  const summaries = Array.isArray(brief.performance_summaries) && brief.performance_summaries.length
    ? brief.performance_summaries
    : (brief.performance_summary ? [{ filename: null, summary: brief.performance_summary }] : []);

  let metricsBlock;
  if (!summaries.length) {
    metricsBlock = el("div", { className: "muted" }, "No performance CSV uploaded for this partner.");
  } else {
    const wrap = el("div");
    summaries.forEach((entry) => {
      const s = entry.summary || {};
      const t = s.totals || {};
      const cur = s.currency;
      const label = entry.filename
        ? el("div", { className: "muted", style: "font-size:10px;margin:4px 0 4px;display:flex;justify-content:space-between" },
            el("span", {}, cur ? `Currency ${cur}` : (entry.filename || "")),
            el("span", {}, `${s.active_clients ?? "—"}/${s.total_clients ?? "—"} active clients`))
        : null;
      const grid = el("div", { className: "metrics-grid" },
        metric(cur ? `Revenue (${cur})` : "Revenue", fmtMoney(t.revenue, cur)),
        metric("Live spend", fmtMoney(t.live_spend, cur)),
        metric("Clicks", fmtNum(t.clicks)),
        metric("Applies", fmtNum(t.applies)),
        metric("CPA", fmtMoney(t.cpa, cur)),
        metric("Apply rate", fmtPct(t.cta_pct)),
      );
      if (label) wrap.appendChild(label);
      wrap.appendChild(grid);
    });
    metricsBlock = wrap;
  }
  out.appendChild(briefSection("📊 Performance Summary", () => el("div", {},
    el("div", { className: "lead" }, perf.headline || "—"),
    metricsBlock,
    el("div", { className: "cols-3" },
      el("div", {}, el("h5", {}, "Strengths"), bulletList(perf.whats_working || [])),
      el("div", {}, el("h5", {}, "Can do Better!"), bulletList(perf.whats_not || [])),
      el("div", {}, el("h5", {}, "Opportunities"), bulletList(perf.where_to_lean_in || [])),
    ),
  ), () => editorBlock([
    ["headline", "Headline", perf.headline || "", (v) => editPath("performance_read", "headline", v)],
    ["whats_working", "Strengths", perf.whats_working || [], (arr) => editPath("performance_read", "whats_working", arr), "list"],
    ["whats_not", "Can do Better!", perf.whats_not || [], (arr) => editPath("performance_read", "whats_not", arr), "list"],
    ["where_to_lean_in", "Opportunities", perf.where_to_lean_in || [], (arr) => editPath("performance_read", "where_to_lean_in", arr), "list"],
  ])));

  // News
  const news = asArray(brief.news).filter((n) => n && typeof n === "object");
  if (news.length) {
    out.appendChild(briefSection("🗞 Recent Highlights", () => {
      const ul = el("ul", { className: "news-list" });
      for (const n of news) {
        ul.appendChild(el("li", {},
          el("div", { className: "news-headline" }, n.headline),
          el("div", { className: "news-detail" }, n.detail || ""),
          el("div", { style: "display:flex;gap:6px;align-items:center" },
            n.source_url
              ? el("a", { className: "news-source", href: n.source_url, target: "_blank" }, "↗ " + (n.source || "source"))
              : el("span", { className: "news-source" }, n.source || ""),
            el("span", { className: `news-tier t${n.source_tier || 3}` }, "T" + (n.source_tier || 3)),
            el("span", { className: "muted" }, (n.recency || "") + (n.category ? " · " + n.category : "")),
          ),
        ));
      }
      return ul;
    }));
  }

  // Past-call transcripts
  const av = brief.avoma_summary;
  if (av) {
    out.appendChild(briefSection("🧵 Previous Discussion", () => el("div", {},
      el("div", { className: "lead" }, av.last_call_summary || "(no recent transcripts uploaded)"),
      el("div", { className: "cols-3" },
        el("div", {}, el("h5", {}, "Action Items"),
          bulletList(asArray(av.commitments).map((c) =>
            typeof c === "string" ? c : `${c.by || ""}: ${c.what || ""}${c.due ? " (" + c.due + ")" : ""}`
          ))),
        el("div", {}, el("h5", {}, "Outstanding Items"), bulletList(av.open_threads)),
        el("div", {}, el("h5", {}, "Sentiment"),
          el("div", { className: "tp-rationale" }, `${av.sentiment || "—"} · ${av.transcripts_used || 0} transcripts analysed`)),
      ),
    )));
  }

  // Discussion Points
  const tps = asArray(brief.talking_points).map((tp) =>
    typeof tp === "string" ? { point: tp, rationale: "", tied_to: "" } : tp,
  );
  out.appendChild(briefSection("💬 Discussion Points", () => {
    const wrap = el("div");
    tps.forEach((tp) => {
      wrap.appendChild(el("div", { className: "tp-card" },
        el("div", { className: "tp-point" }, tp.point || ""),
        el("div", { className: "tp-rationale" }, (tp.rationale || "") +
          (tp.tied_to ? ` · tied to ${tp.tied_to}` : "")),
      ));
    });
    return wrap;
  }, () => editTalkingPoints(tps)));

  // Recommendations
  const recs = asArray(brief.recommendations).map((r) =>
    typeof r === "string" ? { title: r, rationale: "", expected_impact: "", risk: "" } : r,
  );
  out.appendChild(briefSection("🚀 Strategic Recommendations", () => {
    const wrap = el("div");
    recs.forEach((r) => {
      wrap.appendChild(el("div", { className: "rec-card" },
        el("div", { className: "rec-title" }, r.title || ""),
        el("div", { className: "rec-row" }, "Why: " + (r.rationale || "")),
        el("div", { className: "rec-row" }, "Impact: " + (r.expected_impact || "")),
        r.risk ? el("div", { className: "rec-row warn" }, "Watch: " + r.risk) : null,
      ));
    });
    return wrap;
  }, () => editRecommendations(recs)));

  // Threads & Risks
  const closes = asArray(brief.open_threads_to_close);
  const risks = asArray(brief.risks_to_flag);
  if (closes.length || risks.length) {
    out.appendChild(briefSection("⚠️ Key Considerations", () => el("div", { className: "cols-3" },
      el("div", {}, el("h5", {}, "Items to Address"), bulletList(closes)),
      el("div", {}, el("h5", {}, "Risks to Monitor"), bulletList(risks)),
    ), () => editorBlock([
      ["open_threads_to_close", "Items to Address", closes, (arr) => state.edits.open_threads_to_close = arr, "list"],
      ["risks_to_flag", "Risks to Monitor", risks, (arr) => state.edits.risks_to_flag = arr, "list"],
    ])));
  }
}

function metric(label, value) {
  return el("div", { className: "metric" },
    el("div", { className: "metric-label" }, label),
    el("div", { className: "metric-value" }, value),
  );
}

// Coerce whatever the model returned into an array. Flash-Lite occasionally
// emits a single string instead of an array, or omits a field entirely.
function asArray(v) {
  if (Array.isArray(v)) return v;
  if (v == null) return [];
  if (typeof v === "string") return v.trim() ? [v.trim()] : [];
  return [v];
}

function bulletList(items) {
  const arr = asArray(items);
  const ul = el("ul", { className: "brief-list" });
  if (!arr.length) ul.appendChild(el("li", {}, "—"));
  else arr.forEach((s) => ul.appendChild(el("li", {}, typeof s === "string" ? s : JSON.stringify(s))));
  return ul;
}

function briefSection(label, viewFn, editFn) {
  const wrap = el("div", { className: "brief-section" });
  const head = el("div", { className: "brief-label" }, label);
  if (editFn) {
    let editing = false;
    const btn = el("button", { className: "edit-btn" }, "Edit");
    btn.addEventListener("click", () => {
      editing = !editing;
      btn.textContent = editing ? "Done" : "Edit";
      body.innerHTML = "";
      body.appendChild(editing ? editFn() : viewFn());
    });
    head.appendChild(btn);
  }
  wrap.appendChild(head);
  const body = el("div");
  body.appendChild(viewFn());
  wrap.appendChild(body);
  return wrap;
}

// ─── EDIT HELPERS ─────────────────────────────────────────────────────────────
function editPath(parent, key, value) {
  const base = state.briefing?.output?.[parent] || {};
  state.edits[parent] = { ...base, ...(state.edits[parent] || {}), [key]: value };
}

function editorBlock(rows) {
  const wrap = el("div");
  for (const [, label, val, setter, kind] of rows) {
    wrap.appendChild(el("h5", { style: "margin:6px 0 4px;font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:0.08em" }, label));
    if (kind === "list") {
      wrap.appendChild(listEditor(val, setter, "item"));
    } else {
      const ta = el("textarea", { className: "editable", rows: kind === "long" ? "3" : "2" });
      ta.value = val || "";
      ta.addEventListener("input", () => setter(ta.value));
      wrap.appendChild(ta);
    }
  }
  return wrap;
}

function editTalkingPoints(tps) {
  const wrap = el("div");
  const arr = tps.map((t) => ({ ...t }));
  function paint() {
    wrap.innerHTML = "";
    arr.forEach((tp, i) => {
      const card = el("div", { className: "tp-card" });
      const point = el("textarea", { className: "editable", rows: "2", placeholder: "Talking point" });
      point.value = tp.point || "";
      point.addEventListener("input", () => { arr[i].point = point.value; state.edits.talking_points = arr; });
      const rationale = el("textarea", { className: "editable", rows: "2", placeholder: "Why this lands" });
      rationale.value = tp.rationale || "";
      rationale.addEventListener("input", () => { arr[i].rationale = rationale.value; state.edits.talking_points = arr; });
      const del = el("button", { className: "btn-secondary", style: "padding:4px 10px;font-size:10px;margin-top:4px" }, "✕ Kill");
      del.addEventListener("click", () => { arr.splice(i, 1); state.edits.talking_points = arr; paint(); });
      card.appendChild(point);
      card.appendChild(el("div", { style: "height:4px" }));
      card.appendChild(rationale);
      card.appendChild(del);
      wrap.appendChild(card);
    });
    const add = el("button", { className: "btn-secondary", style: "padding:6px 12px;font-size:11px" }, "+ Add talking point");
    add.addEventListener("click", () => { arr.push({ point: "", rationale: "", tied_to: "notes" }); state.edits.talking_points = arr; paint(); });
    wrap.appendChild(add);
  }
  paint();
  return wrap;
}

function editRecommendations(recs) {
  const wrap = el("div");
  const arr = recs.map((r) => ({ ...r }));
  function paint() {
    wrap.innerHTML = "";
    arr.forEach((r, i) => {
      const card = el("div", { className: "rec-card" });
      const fields = [
        ["title", "Title"], ["rationale", "Why"], ["expected_impact", "Impact"], ["risk", "Risk to watch"],
      ];
      fields.forEach(([key, lbl]) => {
        const ta = el("textarea", { className: "editable", rows: "1", placeholder: lbl });
        ta.value = r[key] || "";
        ta.addEventListener("input", () => { arr[i][key] = ta.value; state.edits.recommendations = arr; });
        card.appendChild(ta);
        card.appendChild(el("div", { style: "height:4px" }));
      });
      const del = el("button", { className: "btn-secondary", style: "padding:4px 10px;font-size:10px" }, "✕ Kill");
      del.addEventListener("click", () => { arr.splice(i, 1); state.edits.recommendations = arr; paint(); });
      card.appendChild(del);
      wrap.appendChild(card);
    });
    const add = el("button", { className: "btn-secondary", style: "padding:6px 12px;font-size:11px" },
      "+ Add recommendation");
    add.addEventListener("click", () => { arr.push({ title: "", rationale: "", expected_impact: "", risk: "" }); state.edits.recommendations = arr; paint(); });
    wrap.appendChild(add);
  }
  paint();
  return wrap;
}

// ─── APPROVE / DECK / SLACK ───────────────────────────────────────────────────
$("approve-btn").addEventListener("click", async () => {
  if (!state.briefing) return;
  $("approve-status").textContent = "Saving edits + approving...";
  try {
    await api(`/api/briefing/${state.briefing.id}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ edits: state.edits }),
    });
    state.briefing.status = "approved";
    $("approve-btn").textContent = "✓ Approved";
    $("approve-btn").disabled = true;
    $("clone-btn").classList.remove("hidden");
    $("edit-approved-btn").classList.remove("hidden");
    $("approve-status").textContent = "✓ Approved";
    $("approve-status").style.color = "var(--green)";
    $("brief-status").textContent = `Approved · briefing #${state.briefing.id}`;
    loadHistory();
  } catch (e) {
    $("approve-status").textContent = "Error: " + e.message;
    $("approve-status").style.color = "var(--red)";
  }
});

// "Edit this briefing" — only shown when viewing an already-approved briefing.
// Unlocks the Approve button (relabeled "✓ Re-approve") so the user can save
// their edits to the SAME briefing record instead of forking a new one.
$("edit-approved-btn").addEventListener("click", () => {
  if (!state.briefing) return;
  const approveBtn = $("approve-btn");
  approveBtn.disabled = false;
  approveBtn.textContent = "✓ Re-approve";
  $("edit-approved-btn").classList.add("hidden");
  $("approve-status").textContent = "Editing approved briefing — Re-approve will update this same briefing.";
  $("approve-status").style.color = "var(--muted)";
  $("brief-status").textContent = `Editing approved briefing #${state.briefing.id} — Re-approve to save changes in place, or Save as new draft to fork.`;
});

// "Save as new briefing" — only shown when viewing an already-approved
// briefing. Creates a brand-new draft briefing record from the original
// content plus the user's current edits, leaving the original untouched.
$("clone-btn").addEventListener("click", async () => {
  if (!state.briefing) return;
  const btn = $("clone-btn");
  btn.disabled = true;
  const origLabel = btn.textContent;
  btn.textContent = "⏳ Saving...";
  $("approve-status").textContent = "Saving as new draft...";
  $("approve-status").style.color = "var(--muted)";
  try {
    const result = await api(`/api/briefing/${state.briefing.id}/clone`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ edits: state.edits }),
    });
    btn.textContent = origLabel;
    btn.disabled = false;
    // Switch the workbench over to the new draft so subsequent edits/
    // approve/sends/PDFs target it instead of the original.
    await openBriefingFromHistory(result.id);
    $("approve-status").textContent = `📝 New draft #${result.id} (from #${result.cloned_from}) — edit further, then click Approve.`;
    $("approve-status").style.color = "var(--green)";
  } catch (e) {
    btn.textContent = origLabel;
    btn.disabled = false;
    $("approve-status").textContent = "Error: " + e.message;
    $("approve-status").style.color = "var(--red)";
  }
});

$("open-deck-btn").addEventListener("click", () => {
  if (!state.briefing) return;
  window.open(`/api/briefing/${state.briefing.id}/deck`, "_blank");
});

// Shared by the workbench "Download PDF" button and the per-row PDF button on
// the Past Briefings list. Tries server-side Playwright render first (best
// fidelity, requires Chromium installed locally); on failure, falls back to
// opening the deck in a new tab with ?print=1 so the browser's Save-as-PDF
// dialog auto-fires. Optional onStatus(msg, color) callback receives progress
// updates so the workbench can echo them into the approve-status pill.
async function downloadBriefingPdf(briefingId, partnerName, onStatus) {
  const setStatus = onStatus || (() => {});
  setStatus("Generating PDF...", "var(--text2)");
  let serverHint = "";
  try {
    const resp = await fetch(`/api/briefing/${briefingId}/pdf`);
    if (resp.ok && resp.headers.get("content-type")?.includes("pdf")) {
      const blob = await resp.blob();
      // "{Publisher} {DD-MM-YYYY}.pdf" — strip only Windows-unsafe filename chars.
      const partner = (partnerName || "partner").replace(/[<>:"/\\|?*]+/g, "").trim() || "partner";
      const d = new Date();
      const dateStr = `${String(d.getDate()).padStart(2, "0")}-${String(d.getMonth() + 1).padStart(2, "0")}-${d.getFullYear()}`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${partner} ${dateStr}.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setStatus("📄 PDF downloaded · A4 landscape", "var(--green)");
      return;
    }
    // Try to extract the server's hint about why it failed (501 = no Playwright/Chromium).
    if (resp.status === 501) {
      serverHint = "Playwright Chromium not installed — install with `playwright install chromium` to get auto-A4 PDFs.";
    } else {
      try {
        const errPayload = await resp.json();
        serverHint = errPayload.detail || "";
      } catch {}
    }
    console.warn("Server-side PDF unavailable (status", resp.status, "); falling back to browser print", serverHint);
  } catch (e) {
    console.warn("PDF endpoint unreachable; falling back to browser print:", e);
  }
  // Browser-print fallback: opens the deck with ?print=1 and triggers window.print().
  // The deck CSS sets @page { size: A4 landscape } so Chrome's print dialog
  // defaults to A4. User picks "Save as PDF" in the destination dropdown.
  window.open(`/api/briefing/${briefingId}/deck?print=1`, "_blank");
  const tip = "📄 Browser print opened — set 'Paper size' to A4, orientation Landscape, then Save as PDF.";
  setStatus(tip, "var(--accent)");
  if (serverHint) console.info("Server hint:", serverHint);
}

$("pdf-btn").addEventListener("click", () => {
  if (!state.briefing) return;
  downloadBriefingPdf(state.briefing.id, state.briefing.partner, (msg, color) => {
    $("approve-status").textContent = msg;
    $("approve-status").style.color = color;
  });
});

$("slack-btn").addEventListener("click", async () => {
  if (!state.briefing) return;
  const channel = $("slack-channel").value.trim();
  $("approve-status").textContent = "Sending to Slack...";
  try {
    const result = await api(`/api/briefing/${state.briefing.id}/slack`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel: channel || undefined }),
    });
    if (result.sent) {
      $("approve-status").textContent = "✓ Sent to Slack";
      $("approve-status").style.color = "var(--green)";
    } else {
      $("approve-status").textContent = "No webhook configured — preview below";
      $("slack-preview").classList.remove("hidden");
      $("slack-preview").textContent = result.preview || "";
    }
  } catch (e) {
    $("approve-status").textContent = "Error: " + e.message;
    $("approve-status").style.color = "var(--red)";
  }
});

// Email — sends the full brief as a styled HTML email to the logged-in user via
// the Gmail API. No app launching, no truncation. Requires the gmail.send scope
// (granted at Google sign-in) — if missing, prompts the user to re-sign-in.
$("email-btn").addEventListener("click", async () => {
  if (!state.briefing) return;
  $("approve-status").textContent = "Sending email...";
  $("approve-status").style.color = "var(--muted)";
  try {
    const result = await api(`/api/briefing/${state.briefing.id}/email`, { method: "POST" });
    $("approve-status").textContent = `📧 Email sent to ${result.to}`;
    $("approve-status").style.color = "var(--accent)";
  } catch (e) {
    let msg = "Error: " + e.message;
    if (e.status === 403 && /sign.?in|scope/i.test(e.message)) {
      msg = "Email send needs the Gmail scope — sign out and sign back in with Google, then retry.";
    } else if (e.status === 401) {
      msg = "Sign in with Google to enable email sending.";
    }
    $("approve-status").textContent = msg;
    $("approve-status").style.color = "var(--red)";
  }
});

// View original toggle — flips between the merged-with-edits view and the
// untouched first-draft output. Useful for reviewers who want to see what
// the model produced before the rep edited.
$("view-original-btn").addEventListener("click", () => {
  if (!state.briefing) return;
  state.viewOriginal = !state.viewOriginal;
  const merged = state.viewOriginal
    ? (state.briefing.output || {})
    : { ...(state.briefing.output || {}), ...(state.edits || {}) };
  renderBrief(merged);
  $("view-original-btn").textContent = state.viewOriginal
    ? "↩ Back to edited view"
    : "👁 View original (pre-edit)";
  $("view-mode-label").textContent = state.viewOriginal
    ? "showing untouched model output"
    : "";
});

// ─── NOTES ────────────────────────────────────────────────────────────────────
$("save-note-btn").addEventListener("click", async () => {
  const partner = state.activeCall?.partner;
  if (!partner) return;
  const kind = $("note-kind").value;
  const title = $("note-title").value.trim();
  const body = $("note-body").value.trim();
  if (!body) return;
  await api(`/api/notes/${encodeURIComponent(partner)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, title, body }),
  });
  $("note-title").value = "";
  $("note-body").value = "";
  loadNotes();
});

async function loadNotes() {
  const partner = state.activeCall?.partner;
  if (!partner) return;
  const list = $("notes-list");
  list.innerHTML = `<div class="muted"><span class="spinner"></span> Loading...</div>`;
  try {
    const { notes } = await api(`/api/notes/${encodeURIComponent(partner)}`);
    list.innerHTML = "";
    if (!notes.length) { list.innerHTML = `<div class="muted">No notes yet.</div>`; return; }
    for (const n of notes) {
      const item = el("div", { className: "note-item" },
        el("div", { className: "note-meta" },
          el("span", { className: "note-kind" }, n.kind),
          el("button", {
            className: "note-delete",
            onclick: async () => {
              await api(`/api/notes/${n.id}`, { method: "DELETE" });
              loadNotes();
            },
          }, "✕"),
        ),
        n.title ? el("div", { className: "note-title" }, n.title) : null,
        el("div", { className: "note-body" }, n.body),
        el("div", { className: "muted", style: "margin-top:6px" }, n.created_at),
      );
      list.appendChild(item);
    }
  } catch (e) {
    list.innerHTML = `<div class="muted" style="color:var(--red)">${esc(e.message)}</div>`;
  }
}

// ─── PERFORMANCE ──────────────────────────────────────────────────────────────
$("upload-perf-btn").addEventListener("click", async () => {
  const partner = state.activeCall?.partner;
  if (!partner) return;
  const fileEl = $("perf-file");
  if (!fileEl.files[0]) return;
  const fd = new FormData();
  fd.append("partner", partner);
  fd.append("file", fileEl.files[0]);
  const btn = $("upload-perf-btn");
  btn.disabled = true;
  btn.textContent = "Uploading...";
  try {
    const resp = await fetch("/api/performance/upload", { method: "POST", body: fd });
    if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || "upload failed");
    const result = await resp.json();
    fileEl.value = "";
    if (!result.row_count) {
      alert(
        `Saved, but the parser found 0 client rows in this file. Make sure it's a Mojo "All Clients - Client Report" CSV with a "Client name" first column. The brief will show "performance unavailable" until a CSV with parseable rows is uploaded.`,
      );
    }
    loadPerformance();
  } catch (e) {
    alert("Upload failed: " + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Upload Mojo CSV";
  }
});

async function loadPerformance() {
  const partner = state.activeCall?.partner;
  if (!partner) return;
  const list = $("perf-list");
  list.innerHTML = `<div class="muted"><span class="spinner"></span> Loading...</div>`;
  try {
    const { uploads } = await api(`/api/performance/${encodeURIComponent(partner)}`);
    list.innerHTML = "";
    if (!uploads.length) { list.innerHTML = `<div class="muted">No uploads yet.</div>`; return; }
    for (const u of uploads) {
      const t = u.summary?.totals || {};
      const cur = u.summary?.currency;
      const active = u.summary?.active_clients;
      const total = u.summary?.total_clients;
      const noRows = !total;
      const item = el("div", { className: "note-item" },
        el("div", { className: "note-meta" },
          el("span", { className: "note-kind" }, cur ? `Currency ${cur}` : (u.filename || "")),
          el("button", {
            className: "note-delete",
            onclick: async () => {
              if (!confirm(`Delete "${u.filename}"?`)) return;
              await api(`/api/performance/${u.id}`, { method: "DELETE" });
              loadPerformance();
            },
          }, "✕"),
        ),
        noRows
          ? el("div", { className: "note-body", style: "color:var(--red)" },
              "⚠ Parser found 0 client rows. The first column must be 'Client name'. The brief will treat this as no performance data.")
          : el("div", { className: "note-body" },
              `Revenue ${fmtMoney(t.revenue, cur)} · Live spend ${fmtMoney(t.live_spend, cur)} · Clicks ${fmtNum(t.clicks)} · Applies ${fmtNum(t.applies)} · CPA ${fmtMoney(t.cpa, cur)}`,
              (active != null && total != null) ? ` · ${active}/${total} active clients` : "",
            ),
        el("div", { className: "muted", style: "margin-top:6px" }, u.created_at),
      );
      list.appendChild(item);
    }
  } catch (e) {
    list.innerHTML = `<div class="muted" style="color:var(--red)">${esc(e.message)}</div>`;
  }
}

// ─── TRANSCRIPTS ──────────────────────────────────────────────────────────────
$("upload-transcript-btn").addEventListener("click", async () => {
  const partner = state.activeCall?.partner;
  if (!partner) return;
  const fileEl = $("transcript-file");
  if (!fileEl.files[0]) { $("transcript-status").textContent = "Pick a .txt file first."; return; }

  const fd = new FormData();
  fd.append("file", fileEl.files[0]);
  const date = $("transcript-date").value.trim();
  const title = $("transcript-title").value.trim();
  if (date) fd.append("call_date", date);
  if (title) fd.append("call_title", title);

  const btn = $("upload-transcript-btn");
  btn.disabled = true;
  btn.textContent = "Uploading...";
  $("transcript-status").textContent = "";
  try {
    const resp = await fetch(`/api/transcripts/${encodeURIComponent(partner)}`, { method: "POST", body: fd });
    if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || "upload failed");
    const result = await resp.json();
    fileEl.value = "";
    $("transcript-date").value = "";
    $("transcript-title").value = "";
    let msg = "Saved.";
    if (result.truncated) msg += " (Content was over the 45K-char limit and was truncated.)";
    if (result.deleted_ids?.length) msg += ` Pruned older transcript(s): ${result.deleted_ids.join(", ")}.`;
    $("transcript-status").textContent = msg;
    loadTranscripts();
  } catch (e) {
    $("transcript-status").textContent = "Error: " + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Upload transcript";
  }
});

async function loadTranscripts() {
  const partner = state.activeCall?.partner;
  if (!partner) return;
  const list = $("transcript-list");
  list.innerHTML = `<div class="muted"><span class="spinner"></span> Loading transcripts...</div>`;
  try {
    const { transcripts } = await api(`/api/transcripts/${encodeURIComponent(partner)}`);
    list.innerHTML = "";
    if (!transcripts?.length) {
      list.innerHTML = `<div class="muted">No transcripts uploaded yet. Upload up to 3 — older ones are auto-pruned.</div>`;
      return;
    }
    for (const t of transcripts) {
      const sizeKb = Math.round((t.content_length || 0) / 1024);
      list.appendChild(el("div", { className: "note-item" },
        el("div", { className: "note-meta" },
          el("span", { className: "note-kind" }, t.filename),
          el("button", {
            className: "note-delete",
            onclick: async () => {
              if (!confirm("Delete this transcript?")) return;
              await api(`/api/transcripts/${t.id}`, { method: "DELETE" });
              loadTranscripts();
            },
          }, "✕"),
        ),
        t.call_title ? el("div", { className: "note-title" }, t.call_title) : null,
        el("div", { className: "note-body" },
          `${t.call_date || "no date"} · ${sizeKb} KB${t.truncated ? " · (truncated)" : ""}`,
        ),
        el("div", { className: "muted", style: "margin-top:6px" }, t.created_at),
      ));
    }
  } catch (e) {
    list.innerHTML = `<div class="muted" style="color:var(--red)">${esc(e.message)}</div>`;
  }
}

// ─── PAST BRIEFINGS ───────────────────────────────────────────────────────────
$("reload-history-btn").addEventListener("click", loadHistory);

function fmtRelative(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const ms = Date.now() - d.getTime();
  const min = Math.floor(ms / 60000);
  if (min < 1) return "just now";
  if (min < 60) return `${min} min ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} hr ago`;
  const days = Math.floor(hr / 24);
  if (days < 7) return `${days}d ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function statusBadge(status) {
  const cls = { sent: "tag-done", approved: "tag-loading", draft: "tag-pending" }[status] || "tag-pending";
  return el("span", { className: `tag ${cls}` }, status || "draft");
}

// Past Briefings paging — 5 per page. The full list lives in state; switching
// pages just re-renders without re-fetching.
const HISTORY_PAGE_SIZE = 5;
let _historyAll = [];
let _historyPage = 0;

async function loadHistory() {
  const list = $("history-list");
  list.innerHTML = `<div class="muted"><span class="spinner"></span> Loading past briefings...</div>`;
  try {
    const { briefings } = await api("/api/briefings");
    _historyAll = briefings || [];
    // Reset to page 0 on every fresh load (keeps things sane after generating
    // a new briefing — newest goes to page 0 and that's what the user sees).
    _historyPage = 0;
    renderHistoryPage();
  } catch (e) {
    list.innerHTML = `<div class="muted" style="color:var(--red)">${esc(e.message)}</div>`;
  }
}

function renderHistoryPage() {
  const list = $("history-list");
  list.innerHTML = "";
  if (!_historyAll.length) {
    list.innerHTML = `<div class="muted">No briefings generated yet. Click a partner call above and hit "Generate briefing".</div>`;
    return;
  }
  const totalPages = Math.max(1, Math.ceil(_historyAll.length / HISTORY_PAGE_SIZE));
  // Clamp the page index in case briefings were deleted / list shrunk.
  if (_historyPage >= totalPages) _historyPage = totalPages - 1;
  if (_historyPage < 0) _historyPage = 0;
  const start = _historyPage * HISTORY_PAGE_SIZE;
  const slice = _historyAll.slice(start, start + HISTORY_PAGE_SIZE);

  for (const b of slice) {
      const tier = b.tier ? el("span", { className: `tier-badge tier-${String(b.tier).toLowerCase()}` }, b.tier) : null;
      const who = b.generated_by?.name || b.generated_by?.email || "Unknown rep";
      const partnerLine = el("div", { className: "call-partner" }, b.partner || "—");
      if (tier) partnerLine.appendChild(tier);
      const headline = b.snapshot_headline
        ? el("div", { className: "tp-rationale", style: "margin-top:6px;color:var(--text2);font-size:11px;line-height:1.45" }, b.snapshot_headline)
        : null;

      const meta = el("div", { className: "muted", style: "font-size:10px;display:flex;gap:10px;flex-wrap:wrap;margin-top:4px" },
        el("span", {}, `#${b.id}`),
        el("span", {}, fmtRelative(b.created_at)),
        el("span", {}, `by ${who}`),
        b.call_time ? el("span", {}, `call ${b.call_time}`) : null,
        el("span", {}, `${b.talking_points_count || 0} talking points`),
        el("span", {}, `${b.recommendations_count || 0} recs`),
      );

      const pdfBtn = el("button", {
        className: "btn-secondary",
        style: "padding:5px 10px;font-size:11px",
        onclick: (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          pdfBtn.disabled = true;
          const origLabel = pdfBtn.textContent;
          pdfBtn.textContent = "⏳ PDF...";
          downloadBriefingPdf(b.id, b.partner, (msg, _color) => {
            // Surface the final state on the button itself so the user
            // doesn't have to scroll to a status pill.
            pdfBtn.textContent = msg.replace(/^[^a-zA-Z0-9]+/, "📄 ").slice(0, 24);
          }).finally(() => {
            // Restore after a beat so the user sees the result.
            setTimeout(() => {
              pdfBtn.disabled = false;
              pdfBtn.textContent = origLabel;
            }, 2500);
          });
        },
      }, "📄 PDF");
      // Email button — only enabled for approved briefings (the endpoint refuses
      // others with a 400). Send goes via Gmail API to the logged-in user.
      const emailDisabled = b.status !== "approved";
      const emailBtn = el("button", {
        className: "btn-secondary",
        style: "padding:5px 10px;font-size:11px",
        title: emailDisabled ? "Approve the briefing first to enable email" : "Send the brief to your inbox",
        disabled: emailDisabled,
        onclick: (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          if (emailBtn.disabled) return;
          emailBtn.disabled = true;
          const origLabel = emailBtn.textContent;
          emailBtn.textContent = "⏳ Sending...";
          api(`/api/briefing/${b.id}/email`, { method: "POST" })
            .then((result) => {
              emailBtn.textContent = "📧 Sent ✓";
              emailBtn.title = `Sent to ${result.to}`;
            })
            .catch((e) => {
              if (e.status === 403 && /sign.?in|scope/i.test(e.message)) {
                emailBtn.textContent = "📧 Re-sign-in";
                emailBtn.title = "Sign out and back in to grant Gmail send permission";
              } else {
                emailBtn.textContent = "📧 Failed";
                emailBtn.title = e.message || "Email send failed";
              }
            })
            .finally(() => {
              setTimeout(() => {
                emailBtn.disabled = false;
                emailBtn.textContent = origLabel;
              }, 2500);
            });
        },
      }, "📧 Email");
      // Edit button — only on approved briefings. Opens the briefing in the
      // workbench, where the user can edit and use "Save as new draft" to
      // fork it. Approved briefings can't be edited in-place (the audit trail
      // of the originally-approved version stays intact); use the clone flow.
      const editBtn = b.status === "approved" ? el("button", {
        className: "btn-secondary",
        style: "padding:5px 10px;font-size:11px",
        title: "Open in workbench to edit and save as a new draft",
        onclick: (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          openBriefingFromHistory(b.id);
        },
      }, "✏️ Edit") : null;

      const actions = el("div", { style: "display:flex;gap:6px;align-items:center" },
        statusBadge(b.status),
        el("a", {
          className: "btn-secondary",
          style: "padding:5px 10px;font-size:11px;text-decoration:none",
          href: `/api/briefing/${b.id}/deck`,
          target: "_blank",
        }, "📊 Deck"),
        pdfBtn,
        emailBtn,
        editBtn,
      );

      // Left half is clickable — opens the briefing in the workbench so the
      // user can review/edit/approve. Right-side action buttons stay isolated
      // (they already call stopPropagation), so clicking Deck/PDF/Email won't
      // also trigger the row open.
      const contentArea = el("div", {
        style: "flex:1;min-width:0;cursor:pointer",
        title: "Click to open in the workbench",
        onclick: () => openBriefingFromHistory(b.id),
      },
        partnerLine,
        el("div", { className: "call-title" }, b.call_title || "(no event title)"),
        headline,
        meta,
      );
      const row = el("div", { className: "call-row", style: "align-items:flex-start" },
        contentArea,
        actions,
      );
      list.appendChild(row);
  }

  if (totalPages > 1) {
    const pager = el("div", {
      style: "display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:10px;padding:8px 4px",
    });
    const prev = el("button", {
      className: "btn-secondary",
      style: "padding:6px 12px;font-size:11px",
      disabled: _historyPage === 0,
      onclick: () => { _historyPage--; renderHistoryPage(); },
    }, "← Newer");
    const indicator = el("span", { className: "muted", style: "font-size:11px" },
      `Page ${_historyPage + 1} of ${totalPages} · ${_historyAll.length} total`);
    const next = el("button", {
      className: "btn-secondary",
      style: "padding:6px 12px;font-size:11px",
      disabled: _historyPage >= totalPages - 1,
      onclick: () => { _historyPage++; renderHistoryPage(); },
    }, "Older →");
    pager.appendChild(prev);
    pager.appendChild(indicator);
    pager.appendChild(next);
    list.appendChild(pager);
  }
}

// Load an existing briefing into the workbench so the user can review,
// edit, and approve it (works for both drafts and already-approved briefings —
// in the approved case the approve button shows as already-approved).
async function openBriefingFromHistory(briefingId) {
  try {
    const b = await api(`/api/briefing/${briefingId}`);
    // openWorkbench resets state.briefing to null, so we have to populate it
    // AFTER opening (not before).
    openWorkbench({
      partner: b.partner,
      tier: b.call?.tier || "",
      time: b.call?.time || "",
      title: b.call?.title || "",
    });
    state.briefing = b;
    state.edits = b.edits || {};
    state.viewOriginal = false;
    const merged = (state.edits && Object.keys(state.edits).length)
      ? { ...(b.output || {}), ...state.edits }
      : (b.output || {});
    renderBrief(merged);
    $("approve-row").classList.remove("hidden");
    $("view-original-row").classList.remove("hidden");
    $("view-original-btn").textContent = "👁 View original (pre-edit)";
    $("view-mode-label").textContent = "";
    $("brief-status").textContent = b.status === "approved"
      ? `Approved · briefing #${b.id}`
      : `Draft · briefing #${b.id} — edit any field, then click Approve`;
    const approveBtn = $("approve-btn");
    const cloneBtn = $("clone-btn");
    const editApprovedBtn = $("edit-approved-btn");
    if (b.status === "approved") {
      approveBtn.textContent = "✓ Approved";
      approveBtn.disabled = true;
      cloneBtn.classList.remove("hidden");
      editApprovedBtn.classList.remove("hidden");
    } else {
      approveBtn.textContent = "✓ Approve";
      approveBtn.disabled = false;
      cloneBtn.classList.add("hidden");
      editApprovedBtn.classList.add("hidden");
    }
    $("approve-status").textContent = "";
    $("workbench").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    console.error("Failed to open briefing", e);
  }
}

// ─── MANUAL BRIEFING (any publisher, no calendar event needed) ──────────────
async function loadPublishers() {
  const sel = $("manual-publisher");
  if (!sel) return;
  try {
    const grouped = await api("/api/publishers");
    sel.innerHTML = `<option value="">Select a publisher…</option>`;
    for (const tier of ["P0", "P1", "P2"]) {
      const list = grouped[tier] || [];
      if (!list.length) continue;
      const og = document.createElement("optgroup");
      og.label = `${tier} · ${list.length} publishers`;
      for (const p of list) {
        const opt = document.createElement("option");
        opt.value = p.name;
        opt.dataset.tier = tier;
        opt.textContent = p.name;
        og.appendChild(opt);
      }
      sel.appendChild(og);
    }
  } catch (e) {
    console.warn("Failed to load publisher list:", e.message);
  }
}

$("manual-brief-btn").addEventListener("click", () => {
  const sel = $("manual-publisher");
  const opt = sel.options[sel.selectedIndex];
  const partner = opt?.value;
  if (!partner) {
    sel.focus();
    return;
  }
  const tier = opt.dataset.tier || "unlisted";
  // Open the same workbench used for calendar-driven briefings; no time/title since this is ad-hoc.
  openWorkbench({ partner, tier, time: "", title: "" });
});

async function init() {
  // Wipe any leftover uploads from previous sessions — each page load is a fresh slate.
  try { await api("/api/uploads/clear", { method: "POST" }); } catch {}
  await Promise.all([loadHealth(), loadAuth(), loadPublishers()]);
  if (state.user) loadCalls();
  else renderCalls();
  loadHistory();
}

init();
