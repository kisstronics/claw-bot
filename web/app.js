const state = { sessionId: null, settings: null };

const $ = (id) => document.getElementById(id);

function show(panel) {
  document.querySelectorAll(".panel").forEach((node) => node.classList.remove("visible"));
  document.querySelectorAll(".nav").forEach((node) => node.classList.toggle("active", node.dataset.panel === panel));
  $(panel).classList.add("visible");
  if (panel === "schedules") loadSchedules();
  if (panel === "models") loadSettings();
  if (panel === "email") loadEmail();
  if (panel === "usage") loadUsage();
}

document.querySelectorAll(".nav").forEach((button) => {
  button.addEventListener("click", () => show(button.dataset.panel));
});

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const token = localStorage.getItem("knics_token");
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && !path.startsWith("/api/auth/")) {
    localStorage.removeItem("knics_token");
    showGate();
  }
  if (!response.ok) {
    const detail = data.detail || response.statusText;
    const message = Array.isArray(detail)
      ? detail.map((item) => item.msg || item).join(" ")
      : typeof detail === "string"
        ? detail
        : JSON.stringify(detail);
    throw new Error(message);
  }
  return data;
}

function showGate() {
  $("shell").hidden = true;
  $("gate").hidden = false;
  mountGoogleLogin();
}

function loadGoogleScript() {
  if (window.google?.accounts?.id) return Promise.resolve();
  if (window.__knicsGoogleScript) return window.__knicsGoogleScript;
  window.__knicsGoogleScript = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://accounts.google.com/gsi/client";
    script.async = true;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("Could not load Google Sign-In. Check the connection and refresh."));
    document.head.appendChild(script);
  });
  return window.__knicsGoogleScript;
}

async function handleGoogleCredential(response) {
  $("login-status").textContent = "Signing in…";
  try {
    const data = await api("/api/auth/google", {
      method: "POST",
      body: JSON.stringify({ credential: response.credential }),
    });
    localStorage.setItem("knics_token", data.token);
    $("login-status").textContent = "";
    showApp(data);
  } catch (error) {
    $("login-status").textContent = error.message;
  }
}

function mountGoogleLogin() {
  const slot = $("google-login");
  if (!slot || slot.dataset.ready === "1") return;
  slot.dataset.ready = "1";
  $("login-status").textContent = "";

  fetch("/api/auth/google")
    .then(async (response) => {
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = data.detail || "Google login is unavailable.";
        throw new Error(typeof detail === "string" ? detail : "Google login is unavailable.");
      }
      if (!data.client_id) throw new Error("Google login is not configured.");
      return data;
    })
    .then(async (data) => {
      await loadGoogleScript();
      window.google.accounts.id.initialize({
        client_id: data.client_id,
        callback: handleGoogleCredential,
        auto_select: false,
        cancel_on_tap_outside: true,
      });
      slot.replaceChildren();
      window.google.accounts.id.renderButton(slot, {
        type: "standard",
        theme: "outline",
        size: "large",
        text: "signin_with",
        shape: "pill",
        logo_alignment: "left",
        width: 280,
      });
    })
    .catch((error) => {
      slot.dataset.ready = "";
      $("login-status").textContent = error.message;
    });
}

function userLabel(user) {
  const name = String(user?.name || "").trim();
  const email = String(user?.email || "").trim();
  if (name && email) return `${name} · ${email}`;
  if (email) return email;
  if (name) return name;
  const username = String(user?.username || "").trim().replace(/^@/, "");
  if (username) return username;
  return "Signed in";
}

function showApp(me) {
  $("gate").hidden = true;
  $("shell").hidden = false;
  const label = userLabel(me?.user);
  const brandUser = $("brand-user");
  brandUser.textContent = label;
  brandUser.title = label;
  loadSettings().catch((error) => {
    $("brand-model").textContent = error.message;
  });
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderInline(text) {
  let html = escapeHtml(text);
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
  );
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  html = html.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  return html;
}

function renderBlocks(text) {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const html = [];
  let list = null;
  let paragraph = [];

  function flushParagraph() {
    const body = paragraph.join(" ").trim();
    paragraph = [];
    if (body) html.push(`<p>${renderInline(body)}</p>`);
  }

  function closeList() {
    if (!list) return;
    html.push(list === "ul" ? "</ul>" : "</ol>");
    list = null;
  }

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) {
      flushParagraph();
      closeList();
      continue;
    }
    const heading = /^(#{1,3})\s+(.+)$/.exec(trimmed);
    if (heading) {
      flushParagraph();
      closeList();
      const level = heading[1].length;
      html.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      continue;
    }
    const bullet = /^[-*]\s+(.+)$/.exec(trimmed);
    if (bullet) {
      flushParagraph();
      if (list !== "ul") {
        closeList();
        html.push("<ul>");
        list = "ul";
      }
      html.push(`<li>${renderInline(bullet[1])}</li>`);
      continue;
    }
    const numbered = /^\d+[.)]\s+(.+)$/.exec(trimmed);
    if (numbered) {
      flushParagraph();
      if (list !== "ol") {
        closeList();
        html.push("<ol>");
        list = "ol";
      }
      html.push(`<li>${renderInline(numbered[1])}</li>`);
      continue;
    }
    closeList();
    paragraph.push(trimmed);
  }
  flushParagraph();
  closeList();
  return html.join("");
}

function renderMarkdown(source) {
  const text = String(source || "");
  const parts = [];
  const fence = /```[^\n]*\n?([\s\S]*?)```/g;
  let last = 0;
  let match = fence.exec(text);
  while (match) {
    if (match.index > last) parts.push(renderBlocks(text.slice(last, match.index)));
    parts.push(`<pre><code>${escapeHtml(match[1].replace(/\n$/, ""))}</code></pre>`);
    last = match.index + match[0].length;
    match = fence.exec(text);
  }
  if (last < text.length) parts.push(renderBlocks(text.slice(last)));
  return parts.join("") || "<p></p>";
}

function addBubble(role, text) {
  const node = document.createElement("article");
  node.className = `bubble ${role}`;
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = role === "user" ? "You" : "Agent";
  const body = document.createElement("div");
  if (role === "agent") {
    body.className = "md";
    body.innerHTML = renderMarkdown(text);
  } else {
    body.className = "plain";
    body.textContent = text;
  }
  node.append(who, body);
  $("thread").append(node);
  $("thread").scrollTop = $("thread").scrollHeight;
}

$("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("chat-input");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  addBubble("user", message);
  $("chat-send").disabled = true;
  try {
    const data = await api("/invoke", {
      method: "POST",
      body: JSON.stringify({ message, session_id: state.sessionId }),
    });
    state.sessionId = data.session_id;
    addBubble("agent", data.reply || "(no reply)");
  } catch (error) {
    addBubble("agent", error.message);
  } finally {
    $("chat-send").disabled = false;
    input.focus();
  }
});

$("new-chat").addEventListener("click", async () => {
  if (state.sessionId) {
    await fetch(`/sessions/${state.sessionId}`, { method: "DELETE" }).catch(() => {});
  }
  state.sessionId = null;
  $("thread").replaceChildren();
});

let editingJob = null;

function resetScheduleForm() {
  const form = $("schedule-form");
  form.reset();
  form.minutes.value = 60;
  editingJob = null;
  $("schedule-submit").textContent = "Save schedule";
  $("schedule-cancel").hidden = true;
}

function editSchedule(job) {
  const form = $("schedule-form");
  form.name.value = job.name;
  form.minutes.value = Math.max(1, Math.round((job.every_seconds || 60) / 60));
  form.handler.value = job.handler || "";
  form.prompt.value = job.prompt || "";
  editingJob = job;
  $("schedule-submit").textContent = "Update schedule";
  $("schedule-cancel").hidden = false;
  form.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function jobCard(job) {
  const card = document.createElement("article");
  card.className = `job${job.enabled === false ? " off" : ""}`;
  const kind = job.handler || "prompt";
  const minutes = Math.max(1, Math.round((job.every_seconds || 0) / 60));
  const title = document.createElement("h3");
  title.textContent = job.name;
  const pill = document.createElement("span");
  pill.className = "pill";
  pill.textContent = kind;
  title.append(" ", pill);
  const summary = document.createElement("p");
  summary.textContent = `Every ${minutes} min · ${job.enabled === false ? "disabled" : "enabled"}`;
  const detail = document.createElement("p");
  detail.className = "job-detail";
  detail.textContent = job.prompt || "Runs the built-in handler.";
  const copy = document.createElement("div");
  copy.append(title, summary, detail);
  const actions = document.createElement("div");
  actions.className = "job-actions";
  card.append(copy, actions);
  const edit = document.createElement("button");
  edit.type = "button";
  edit.textContent = "Edit";
  edit.addEventListener("click", () => editSchedule(job));
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.textContent = job.enabled === false ? "Enable" : "Disable";
  toggle.addEventListener("click", () => saveJob({ ...job, enabled: job.enabled === false }));
  const run = document.createElement("button");
  run.type = "button";
  run.textContent = "Run";
  run.addEventListener("click", async () => {
    run.disabled = true;
    try {
      await api(`/schedules/${encodeURIComponent(job.name)}/run`, { method: "POST" });
      await loadSchedules();
    } catch (error) {
      alert(error.message);
    } finally {
      run.disabled = false;
    }
  });
  const remove = document.createElement("button");
  remove.type = "button";
  remove.textContent = "Delete";
  remove.addEventListener("click", async () => {
    if (!confirm(`Delete ${job.name}?`)) return;
    await api(`/schedules/${encodeURIComponent(job.name)}`, { method: "DELETE" });
    await loadSchedules();
  });
  actions.append(edit, toggle, run, remove);
  return card;
}

async function saveJob(job) {
  await api(`/schedules/${encodeURIComponent(job.name)}`, {
    method: "PUT",
    body: JSON.stringify({
      name: job.name,
      every_seconds: job.every_seconds,
      enabled: job.enabled !== false,
      prompt: job.prompt || "",
      handler: job.handler || "",
      initial_delay_seconds: job.initial_delay_seconds || 5,
    }),
  });
  await loadSchedules();
}

async function loadSchedules() {
  const data = await api("/schedules");
  const list = $("schedule-list");
  list.replaceChildren();
  (data.jobs || []).forEach((job) => list.append(jobCard(job)));
  if (!data.jobs || !data.jobs.length) {
    list.innerHTML = `<p class="note">No schedules yet.</p>`;
  }
}

$("schedule-cancel").addEventListener("click", resetScheduleForm);

$("schedule-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const body = {
    name: form.name.value.trim(),
    every_seconds: Math.max(60, Number(form.minutes.value) * 60),
    enabled: editingJob ? editingJob.enabled !== false : true,
    handler: form.handler.value,
    prompt: form.prompt.value.trim(),
    initial_delay_seconds: editingJob ? editingJob.initial_delay_seconds || 5 : 5,
  };
  try {
    if (editingJob && editingJob.name === body.name) {
      await api(`/schedules/${encodeURIComponent(body.name)}`, {
        method: "PUT",
        body: JSON.stringify(body),
      });
    } else if (editingJob) {
      await api("/schedules", { method: "POST", body: JSON.stringify(body) });
      await api(`/schedules/${encodeURIComponent(editingJob.name)}`, { method: "DELETE" });
    } else {
      await api("/schedules", { method: "POST", body: JSON.stringify(body) });
    }
    resetScheduleForm();
    await loadSchedules();
  } catch (error) {
    alert(error.message);
  }
});

function fillModels() {
  const settings = state.settings;
  const provider = $("provider");
  const pick = $("model-pick");
  provider.replaceChildren();
  Object.keys(settings.catalog || {}).forEach((name) => {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    provider.append(option);
  });
  provider.value = settings.provider;
  const models = settings.catalog[provider.value] || [];
  pick.replaceChildren();
  models.forEach((model) => {
    const option = document.createElement("option");
    option.value = model;
    option.textContent = model;
    pick.append(option);
  });
  const custom = document.createElement("option");
  custom.value = "";
  custom.textContent = "Custom id…";
  pick.append(custom);
  if (models.includes(settings.model_id)) pick.value = settings.model_id;
  else pick.value = "";
  $("model-id").value = settings.model_id;
  $("brand-model").textContent = `${settings.provider} · ${settings.model_id}`;
}

async function loadSettings() {
  state.settings = await api("/api/settings");
  fillModels();
}

$("provider").addEventListener("change", () => {
  const provider = $("provider").value;
  state.settings.provider = provider;
  state.settings.model_id = (state.settings.catalog[provider] || [""])[0] || "";
  fillModels();
});

$("model-pick").addEventListener("change", () => {
  if ($("model-pick").value) $("model-id").value = $("model-pick").value;
});

$("model-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const status = $("model-status");
  status.textContent = "Switching…";
  try {
    const saved = await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        provider: $("provider").value,
        model_id: $("model-id").value.trim(),
      }),
    });
    status.textContent = `Now using ${saved.provider} / ${saved.model_id}`;
    await loadSettings();
  } catch (error) {
    status.textContent = error.message;
  }
});

function money(value) {
  return `$${Number(value || 0).toFixed(4)}`;
}

async function loadUsage() {
  const data = await api("/api/usage?limit=40");
  const cards = [
    ["Turns", data.turns],
    ["Input tokens", data.input_tokens.toLocaleString()],
    ["Output tokens", data.output_tokens.toLocaleString()],
    ["Estimated cost", money(data.estimated_cost_usd)],
  ];
  $("usage-cards").innerHTML = cards
    .map(([label, value]) => `<article class="stat"><b>${value}</b><span>${label}</span></article>`)
    .join("");
  $("usage-note").textContent = data.note || "";
  const rows = $("usage-rows");
  rows.replaceChildren();
  (data.recent || []).forEach((row) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${row.ts}</td><td>${row.model}</td><td>${row.input_tokens}</td><td>${row.output_tokens}</td><td>${money(row.estimated_cost_usd)}</td><td>${row.duration_sec}s</td>`;
    rows.append(tr);
  });
}

$("refresh-usage").addEventListener("click", loadUsage);
$("chat-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("chat-form").requestSubmit();
  }
});

async function loadEmail() {
  const data = await api("/api/email");
  const form = $("email-form");
  form.email.value = data.email || "";
  form.imap_server.value = data.imap_server || "imap.gmail.com";
  form.smtp_server.value = data.smtp_server || "smtp.gmail.com";
  form.smtp_port.value = data.smtp_port || 587;
  form.news_to.value = data.news_to || "";
  form.app_password.value = "";
  form.app_password.required = !data.password_set;
  $("email-status").textContent = data.password_set ? "Password saved" : "";
}

$("email-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  $("email-status").textContent = "Saving…";
  try {
    const saved = await api("/api/email", {
      method: "PUT",
      body: JSON.stringify({
        email: form.email.value.trim(),
        app_password: form.app_password.value,
        imap_server: form.imap_server.value.trim(),
        smtp_server: form.smtp_server.value.trim(),
        smtp_port: Number(form.smtp_port.value),
        news_to: form.news_to.value.trim(),
      }),
    });
    form.app_password.value = "";
    form.app_password.required = false;
    $("email-status").textContent = saved.password_set ? "Saved" : "Saved without a password";
  } catch (error) {
    $("email-status").textContent = error.message;
  }
});

$("logout").addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (error) {
    /* the local session is cleared either way */
  }
  localStorage.removeItem("knics_token");
  state.sessionId = null;
  showGate();
});

if (localStorage.getItem("knics_token")) {
  api("/api/auth/me").then(showApp).catch(() => showGate());
} else {
  showGate();
}
