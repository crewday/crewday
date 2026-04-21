// crew.day — landing mock interactivity.
// Scenario picker keeps the DemoFrame URL preview in sync with the
// persona + intent selection (spec-site §01). Data lives here rather
// than in the HTML so the list shape matches what the future
// content/<locale>/scenarios.ts export will look like.

const SCENARIOS = {
  "villa-owner": {
    as: "owner",
    posterTitle: "Tomorrow — 22 Apr",
    intents: [
      { key: "organise-cleaner", label: "Organise my cleaner", start: "/schedule", caption: "The owner reviews tomorrow's schedule. The agent has already drafted the airport run." },
      { key: "see-home",         label: "See what's happening at home", start: "/dashboard", caption: "Everything at once, quietly — stays, issues, tasks, approvals." },
      { key: "airbnb-stays",     label: "Manage incoming Airbnb stays", start: "/stays", caption: "Stays roll in from the ICS feed. Turnovers auto-generate the moment the booking lands." },
      { key: "chat-agent",       label: "Chat with the agent about my property", start: "/chat", caption: "Ask the agent what's due, what's late, what's next. It already knows." },
    ],
  },
  "rental-manager": {
    as: "manager",
    posterTitle: "This week — 20 Apr → 26 Apr",
    intents: [
      { key: "schedule",    label: "Schedule staff across properties", start: "/schedule", caption: "Four crews across eleven villas, one weekly grid. Drag to reassign — the audit log keeps score." },
      { key: "work-orders", label: "Track work orders", start: "/work-orders", caption: "Open, blocked, waiting for parts. Nobody drops the ball when the ball has a row." },
      { key: "payroll",     label: "See payroll at a glance", start: "/payroll", caption: "Hours roll up to payslips. Approve in a block; ship the batch." },
      { key: "invite",      label: "Invite a new client", start: "/clients", caption: "One email, one magic link, one workspace boundary. They see only their villas." },
    ],
  },
  "housekeeper": {
    as: "worker",
    posterTitle: "Today — 21 Apr",
    intents: [
      { key: "today",       label: "See today's tasks", start: "/today", caption: "Three tasks, one break, home by four. The list is the list." },
      { key: "complete",    label: "Complete a task with photo", start: "/today?focus=next-task", caption: "Snap the linen cupboard. One tap closes the task. The evidence lives in the audit log." },
      { key: "log-hours",   label: "Log hours", start: "/me/hours", caption: "Hours come from the booking itself — no clock-in ritual, no fiddling. Correct if needed." },
      { key: "chat-manager",label: "Chat with the manager", start: "/chat", caption: "Message the manager in your own language. The agent translates both ways." },
    ],
  },
};

const $  = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  persona: "villa-owner",
  intentKey: "organise-cleaner",
};

function renderIntents() {
  const list = $("#axis-intent");
  const intents = SCENARIOS[state.persona].intents;
  list.innerHTML = "";
  intents.forEach((intent, i) => {
    const el = document.createElement("button");
    el.className = "axis__opt";
    el.setAttribute("role", "radio");
    el.setAttribute("data-intent", intent.key);
    el.setAttribute("data-start", intent.start);
    const selected = state.intentKey === intent.key;
    el.setAttribute("aria-selected", selected ? "true" : "false");
    const glyph = String.fromCharCode(9398 + i); // ⒜ ⒝ ⒞ ⒟ — circled letters
    el.innerHTML = `
      <span class="axis__glyph">${(i + 1)}</span>
      <span class="axis__label">
        <span class="axis__name">${intent.label}</span>
        <span class="axis__sub">start · ${intent.start}</span>
      </span>
    `;
    el.addEventListener("click", () => {
      state.intentKey = intent.key;
      $$("#axis-intent .axis__opt").forEach((n) => n.setAttribute("aria-selected", "false"));
      el.setAttribute("aria-selected", "true");
      updatePreview();
    });
    list.appendChild(el);
  });
}

function updateHash() {
  const hash = `#try-it?persona=${state.persona}&intent=${state.intentKey}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);
}

function currentIntent() {
  const list = SCENARIOS[state.persona].intents;
  return list.find((x) => x.key === state.intentKey) || list[0];
}

function updatePreview() {
  const scenario = state.persona;
  const as       = SCENARIOS[state.persona].as;
  const intent   = currentIntent();
  const start    = encodeURIComponent(intent.start);

  // URL preview
  const url = $("#cell-url");
  url.innerHTML = `demo.crew.day/app?scenario=<em>${scenario}</em>&as=<em>${as}</em>&start=<em>${start}</em>`;

  // Poster chrome url
  const posterUrl = $("#poster-url");
  if (posterUrl) {
    posterUrl.textContent = `demo.crew.day/w/demo-abc${intent.start}`;
  }
  const posterTitle = $("#poster-title");
  if (posterTitle) {
    posterTitle.textContent = SCENARIOS[state.persona].posterTitle;
  }

  // Caption
  const caption = $("#cell-caption");
  if (caption) {
    caption.innerHTML = `
      <span>Looped preview · 00:32</span>
      ${intent.caption}
    `;
  }

  // Live target
  const live = $("#cell-live");
  if (live) {
    live.href = `https://demo.crew.day/app?scenario=${scenario}&as=${as}&start=${start}`;
  }

  updateHash();
}

function bindPersona() {
  $$("#axis-persona .axis__opt").forEach((el) => {
    el.addEventListener("click", () => {
      $$("#axis-persona .axis__opt").forEach((n) => n.setAttribute("aria-selected", "false"));
      el.setAttribute("aria-selected", "true");
      state.persona = el.dataset.persona;
      state.intentKey = SCENARIOS[state.persona].intents[0].key;
      renderIntents();
      updatePreview();
    });
  });
}

function readHash() {
  const raw = location.hash.replace(/^#try-it\??/, "");
  if (!raw) return;
  const params = new URLSearchParams(raw);
  const persona = params.get("persona");
  const intent  = params.get("intent");
  if (persona && SCENARIOS[persona]) {
    state.persona = persona;
    const match = SCENARIOS[persona].intents.find((x) => x.key === intent);
    state.intentKey = match ? match.key : SCENARIOS[persona].intents[0].key;
  }
  // Reflect persona selection in DOM
  $$("#axis-persona .axis__opt").forEach((n) => {
    n.setAttribute("aria-selected", n.dataset.persona === state.persona ? "true" : "false");
  });
}

// ── Theme toggle (persists in localStorage) ───────────────────
function initTheme() {
  const saved = localStorage.getItem("crewday-landing-theme");
  if (saved === "dark") document.documentElement.setAttribute("data-theme", "dark");
  const btn = $("#theme-toggle");
  if (!btn) return;
  const glyph = () => document.documentElement.getAttribute("data-theme") === "dark" ? "☀" : "☾";
  btn.textContent = glyph();
  btn.addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", cur);
    localStorage.setItem("crewday-landing-theme", cur);
    btn.textContent = glyph();
  });
}

// ── Play button → trigger caption-cycle animation ─────────────
function initPlay() {
  const btn = $("#cell-play");
  if (!btn) return;
  btn.addEventListener("click", () => {
    const cap = $("#cell-caption");
    if (!cap) return;
    cap.style.opacity = "0.15";
    setTimeout(() => {
      cap.innerHTML = `
        <span>Playing · 00:04 / 00:32</span>
        (In a real page, a 30-second silent loop would play here.)
      `;
      cap.style.opacity = "1";
    }, 220);
    setTimeout(() => {
      cap.style.opacity = "0.15";
      setTimeout(() => { updatePreview(); cap.style.opacity = "1"; }, 220);
    }, 2800);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  readHash();
  renderIntents();
  bindPersona();
  updatePreview();
  initTheme();
  initPlay();
});
