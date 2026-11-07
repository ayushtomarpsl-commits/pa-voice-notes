"use strict";
/* PA — workday memory. Plain JavaScript, no build step, works offline. */

// ------------------------------------------------------------------ helpers
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const localISO = (d = new Date()) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
const today = () => localISO();
const yesterday = () => localISO(new Date(Date.now() - 864e5));
const hhmm = (ts) => (ts || "").slice(11, 16);
const dark = () => matchMedia("(prefers-color-scheme: dark)").matches;

function dayLabel(d) {
  if (!d) return "";
  if (d === today()) return "Today";
  if (d === yesterday()) return "Yesterday";
  const dt = new Date(d + "T12:00:00");
  return dt.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
}
function relDays(d) {
  if (!d) return "";
  const n = Math.round((new Date(today() + "T12:00:00") - new Date(d + "T12:00:00")) / 864e5);
  return n <= 0 ? "today" : n === 1 ? "yesterday" : `${n} days ago`;
}
function mins(sec) { const m = Math.round((sec || 0) / 60); return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${m % 60}m`; }
function initials(name) {
  const p = String(name || "?").replace(/[^\p{L}\p{N} ]/gu, "").trim().split(/\s+/);
  return ((p[0] || "?")[0] + (p[1] ? p[1][0] : "")).toUpperCase();
}
function hue(name) { let h = 0; for (const c of String(name)) h = (h * 31 + c.codePointAt(0)) % 360; return h; }
function avatar(name, opts = {}) {
  const h = hue(name);
  const bg = dark() ? `hsl(${h} 16% 24%)` : `hsl(${h} 24% 90%)`;
  const ink = dark() ? `hsl(${h} 30% 84%)` : `hsl(${h} 32% 28%)`;
  const me = opts.me || (state.myName && name === state.myName);
  return `<span class="av ${opts.lg ? "lg" : ""} ${me ? "me" : ""}" style="--av-bg:${bg};--av-ink:${ink}" title="${esc(name)}">${esc(initials(name))}</span>`;
}
function avatars(names, max = 4) {
  const list = (names || []).filter((n) => n && !["me", "call"].includes(String(n).toLowerCase()));
  return `<span class="avatars">${list.slice(0, max).map((n) => avatar(n)).join("")}${list.length > max ? `<span class="av">+${list.length - max}</span>` : ""}</span>`;
}
function prio(p) {
  if (!p || p === "none") return "";
  const cls = p.startsWith("P0") ? "p0" : p === "P1" ? "p1" : "p2";
  return `<span class="prio ${cls}">${esc(p)}</span>`;
}
function dueTag(d) {
  if (!d) return "";
  const t = today();
  if (d < t) return `<span class="due-over">overdue ${esc(dayLabel(d))}</span>`;
  if (d === t) return `<span class="due-today">due today</span>`;
  return `<span>due ${esc(dayLabel(d))}</span>`;
}
function pointHTML(p) {
  const m = String(p).match(/^([^:]{1,40}):\s+(.*)$/s);
  return m ? `<span class="spk">${esc(m[1])}:</span> ${esc(m[2])}` : esc(p);
}
function emptyState(title, text, icon = "sparkle") {
  return `<div class="empty"><span class="art">${ic(icon)}</span><b>${esc(title)}</b><span class="sub">${text}</span></div>`;
}
function skeleton(lines = 4) {
  return `<div class="stack">${Array.from({ length: lines }, (_, i) => `<div class="skel" style="height:${i % 2 ? 64 : 96}px"></div>`).join("")}</div>`;
}

// ------------------------------------------------------------------ icons (1.6 stroke, drawn inline)
const ICONS = {
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  stack: '<path d="M12 3 3 7.5l9 4.5 9-4.5L12 3Z"/><path d="m3 12 9 4.5 9-4.5"/><path d="m3 16.5 9 4.5 9-4.5"/>',
  kanban: '<rect x="3" y="4" width="5" height="16" rx="1.5"/><rect x="10" y="4" width="5" height="10" rx="1.5"/><rect x="17" y="4" width="4" height="13" rx="1.5"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.5 3.4-5.5 6.5-5.5s5.7 2 6.5 5.5"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M18.5 14.8c1.6.8 2.7 2.6 3 5.2"/>',
  note: '<path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5M9 13h7M9 17h5"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M4.6 4.6l2.1 2.1M17.3 17.3l2.1 2.1M2.5 12h3M18.5 12h3M4.6 19.4l2.1-2.1M17.3 6.7l2.1-2.1"/>',
  pause: '<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/>',
  play: '<path d="M7 4.5v15l13-7.5z"/>',
  lock: '<rect x="4.5" y="10.5" width="15" height="10" rx="2"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.3-4.9L4 8"/><path d="M4 4v4h4"/><path d="M4 13a8 8 0 0 0 14.3 4.9L20 16"/><path d="M20 20v-4h-4"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  pin: '<path d="M9 3h6l-1 6 4 3v2H6v-2l4-3z"/><path d="M12 14v7"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/>',
  sparkle: '<path d="M12 4v4M12 16v4M4 12h4M16 12h4"/><circle cx="12" cy="12" r="1.5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c1-4.4 4.2-7 8-7s7 2.6 8 7"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  archive: '<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v10h14V9M10 13h4"/>',
};
const ic = (n) => `<svg class="ic" viewBox="0 0 24 24" aria-hidden="true">${ICONS[n] || ""}</svg>`;
function hydrateIcons(root = document) { $$("i[data-icon]", root).forEach((i) => { i.outerHTML = ic(i.dataset.icon); }); }

// ------------------------------------------------------------------ api
async function api(path, opts = {}) {
  const o = { method: opts.method || "GET", headers: { "X-PA-Token": window.PA_TOKEN } };
  if (opts.body !== undefined) { o.body = JSON.stringify(opts.body); o.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, o);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && (data.detail || data.error)) || r.statusText);
  return data;
}
function toast(msg, err = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast show" + (err ? " err" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.className = "toast"), 3200);
}
async function guard(fn) { try { return await fn(); } catch (e) { toast(e.message, true); } }

// ------------------------------------------------------------------ state + router
const state = { tab: "", day: today(), days: [], settings: null, projects: ["Other"], myName: "", status: null,
                lastLiveId: 0, topicFilter: { status: "active", project: "", q: "" }, boardFilter: { project: "", owner: "" } };
const VIEWS = {};
const DRAWERS = { topics: (id) => openTopic(id), conv: (id) => openConv(id), todo: (id) => openTodo(id) };
function route() {
  const [tab, id] = (location.hash.slice(1) || "today").split("/");
  const page = tab === "conv" || tab === "todo" ? (state.tab || "today") : (VIEWS[tab] ? tab : "today");
  if (state.tab !== page) {
    state.tab = page;
    $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.tab === page));
    VIEWS[page]();
  }
  if (id && DRAWERS[tab]) DRAWERS[tab](+id);
  else closeDrawer(false);
}
window.addEventListener("hashchange", route);
function go(hash) { if (location.hash !== "#" + hash) location.hash = hash; else route(); }

// ------------------------------------------------------------------ status island
async function refreshStatus() {
  const s = await api("/api/status").catch(() => null);
  const isl = $("#island");
  if (!s) { isl.className = "island err"; $("#rec-label").textContent = "PA is not running"; return; }
  state.status = s;
  isl.className = "island" + (s.listening ? " live" : "") + ((s.sources || []).some((x) => x.state === "error") ? " err" : "");
  $("#rec-label").textContent = s.listening ? "Listening" : `Paused · ${s.pause_reason || ""}`;
  $("#meters").innerHTML = (s.sources || []).map((x) => {
    const lv = Math.min(1, x.level || 0);
    const bars = [0.35, 0.7, 1, 0.55].map((f) => `<i style="height:${Math.max(3, Math.round(12 * Math.min(1, lv * 1.6) * f))}px"></i>`).join("");
    return `<span class="meter ${s.listening && lv > 0.02 ? "on" : ""}" title="${esc(x.device || "")} — ${esc(x.state)}">${x.kind === "me" ? "mic" : "call"}<span class="bars">${bars}</span></span>`;
  }).join("");
  const t = s.transcriber;
  $("#stt-state").textContent = t ? `${t.model} · ${t.state.replaceAll("_", " ")}${t.queue ? ` · ${t.queue} waiting` : ""}${t.error ? " · " + t.error : ""}` : "";
  const pb = $("#btn-pause"); pb.classList.toggle("on", !!s.manual_pause);
  pb.innerHTML = s.manual_pause ? `${ic("play")}<span>Resume</span>` : `${ic("pause")}<span>Pause</span>`;
  $("#btn-private").classList.toggle("on", !!s.private);
}
$("#btn-pause").onclick = () => guard(async () => { await api("/api/pause", { method: "POST", body: { paused: !state.status?.manual_pause } }); refreshStatus(); });
$("#btn-private").onclick = () => guard(async () => { await api("/api/private", { method: "POST", body: { on: !state.status?.private } }); refreshStatus(); });

// ------------------------------------------------------------------ TODAY
VIEWS.today = async function () {
  const v = $("#view");
  v.innerHTML = `<div class="page-head"><div><div class="eyebrow">${esc(new Date().toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" }))}</div><h1>Your day</h1></div></div>${skeleton(5)}`;
  await loadDays();
  const d = state.day;
  const [ov, convs, todos] = await Promise.all([api("/api/overview?day=" + d), api("/api/conversations?day=" + d), api("/api/todos?status=open")]);
  if (state.tab !== "today") return;
  const focus = todos.filter((x) => x.owner.toLowerCase() === "me" && (x.priority.startsWith("P0") || (x.due && x.due <= today())));
  const peopleSecs = Math.max(1, ...ov.people.map((p) => p.seconds || 0));
  v.innerHTML = `
    <div class="page-head">
      <div><div class="eyebrow">${esc(dayLabel(d))} · ${esc(d)}</div><h1>${d === today() ? "Your day" : "That day"}</h1></div>
      <div class="row">
        <select class="select" id="day-pick" aria-label="Day">${state.days.map((x) => `<option value="${x.day}" ${x.day === d ? "selected" : ""}>${esc(dayLabel(x.day))}${x.unprocessed ? ` · ${x.unprocessed} new` : ""}</option>`).join("")}</select>
        <button class="btn" id="btn-process">${ic("refresh")}<span>Make notes</span></button>
        <button class="btn primary" id="btn-copy">${ic("copy")}<span>Copy for Claude</span></button>
      </div>
    </div>
    <section class="stats reveal">
      <div class="stat" style="--i:0"><div class="v num">${ov.speech_minutes}<small>min</small></div><div class="k">speech heard</div></div>
      <div class="stat" style="--i:1"><div class="v num">${ov.conversations}</div><div class="k">conversations</div></div>
      <div class="stat" style="--i:2"><div class="v num">${ov.topics.length}</div><div class="k">topics discussed</div></div>
      <div class="stat" style="--i:3"><div class="v num">${ov.open_todos}<small>${ov.urgent ? ov.urgent + " urgent" : ""}</small></div><div class="k">open to-dos · ${ov.due_today} due</div></div>
      <div class="stat" style="--i:4"><div class="v num">${ov.people.length}</div><div class="k">people heard</div></div>
    </section>
    <div class="today-grid">
      <div class="stack">
        <section>
          <div class="section-title"><h2>Topics discussed</h2><span class="count">${ov.topics.length}</span><span class="spacer"></span><a class="linkbtn" href="#topics">All topics</a></div>
          ${ov.topics.length ? `<div class="topic-grid today reveal">${ov.topics.map((t, i) => topicCard(t, i)).join("")}</div>`
            : emptyState("No topics yet", "Topics appear when a finished conversation is turned into notes. Press <b>Make notes</b>, or wait — PA does it on its own.", "stack")}
        </section>
        <section>
          <div class="section-title"><h2>Conversations</h2><span class="count">${convs.length}</span></div>
          ${convs.length ? `<div class="tl reveal">${convs.slice().reverse().map((c, i) => convItem(c, i)).join("")}</div>`
            : emptyState("Nothing heard yet", "When people talk near your mic or on a call, conversations show up here.", "mic")}
        </section>
      </div>
      <div class="stack">
        <section class="panel">
          <div class="panel-head"><h2>Focus</h2><span class="muted mono">${focus.length} of ${todos.length}</span></div>
          <div class="panel-body">${focus.length ? `<ul class="list">${focus.map(todoRow).join("")}</ul>` : `<p class="muted" style="margin:0">Nothing urgent or due. The full list is on the <a class="linkbtn" href="#board">Board</a>.</p>`}</div>
        </section>
        <section class="panel">
          <div class="panel-head"><h2>Live transcript</h2><span class="muted" style="font-size:12px">${d === today() ? "updates every few seconds" : esc(dayLabel(d))}</span></div>
          <div class="panel-body"><div class="live" id="live"></div></div>
        </section>
        <section class="panel">
          <div class="panel-head"><h2>People heard</h2><a class="linkbtn" href="#people">Name voices</a></div>
          <div class="panel-body">${ov.people.length ? `<div class="ppl">${ov.people.map((p) => `
            <div class="ppl-row">${avatar(p.name, { me: p.kind === "me" })}<div><div class="row"><b style="font-weight:550">${esc(p.name)}</b>${p.kind === "auto" ? '<span class="chip dashed">auto-named</span>' : ""}</div>
            <div class="bar"><i style="width:${Math.round(100 * (p.seconds || 0) / peopleSecs)}%"></i></div></div><span class="muted mono">${mins(p.seconds)}</span></div>`).join("")}</div>`
            : `<p class="muted" style="margin:0">Voices are recognised as people talk. You can rename them on the People page.</p>`}</div>
        </section>
      </div>
    </div>`;
  bindTopicCards(v); bindTodoRows(v); bindConvItems(v);
  $("#day-pick").onchange = (e) => { state.day = e.target.value; VIEWS.today(); };
  $("#btn-copy").onclick = () => guard(copyHandoff);
  $("#btn-process").onclick = () => guard(processNow);
  loadLive(true);
};

async function loadDays() {
  const days = await api("/api/days");
  state.days = days.some((x) => x.day === today()) ? days : [{ day: today() }, ...days];
}

function topicCard(t, i = 0) {
  const continued = t.first_day && t.first_day < (t.last_day || today());
  const people = Array.isArray(t.people) ? t.people : [];
  return `<article class="tcard ${t.status === "closed" ? "closed" : ""} ${t.pinned ? "pinned" : ""}" data-topic="${t.id}" style="--i:${i}" tabindex="0">
    <div class="meta"><span class="chip">${esc(t.project)}</span>${continued ? `<span class="chip accent">continued · since ${esc(dayLabel(t.first_day))}</span>` : `<span class="chip">new</span>`}${t.status === "closed" ? '<span class="chip">closed</span>' : ""}</div>
    <h3>${esc(t.title)}</h3>
    ${t.summary ? `<p class="summary">${esc(t.summary)}</p>` : ""}
    <div class="foot">${avatars(people)}<span class="spacer"></span>${t.updates ? `<span class="mono">${t.updates} section${t.updates > 1 ? "s" : ""}</span>` : ""}${t.open_todos ? `<span class="mono">${t.open_todos} to-do</span>` : ""}<span>${esc(relDays(t.last_day))}</span></div>
  </article>`;
}
function bindTopicCards(root) {
  $$(".tcard", root).forEach((c) => {
    c.onclick = () => go("topics/" + c.dataset.topic);
    c.onkeydown = (e) => { if (e.key === "Enter") go("topics/" + c.dataset.topic); };
    c.onpointermove = (e) => { const r = c.getBoundingClientRect(); c.style.setProperty("--mx", `${e.clientX - r.left}px`); c.style.setProperty("--my", `${e.clientY - r.top}px`); };
  });
}

function convItem(c, i) {
  const st = c.status;
  const label = st === "open" ? "in progress" : st === "skipped" ? `not office · ${(c.category || "").replace("_", " ")}` : st;
  const chipCls = st === "processed" ? "accent" : st === "error" ? "bad" : st === "open" ? "info" : "";
  return `<div class="tl-item ${st}" style="--i:${i}">
    <span class="tl-time">${hhmm(c.started_at)}</span>
    <div class="tl-title"><b data-conv="${c.id}">${esc(c.title || "Conversation " + c.id)}</b><span class="chip ${chipCls}">${esc(label)}</span>${c.project && st !== "skipped" ? `<span class="chip">${esc(c.project)}</span>` : ""}${avatars(c.people)}</div>
    ${c.summary ? `<p class="tl-sum">${esc(c.summary)}</p>` : ""}
    ${c.error ? `<p class="tl-sum" style="color:var(--bad)">${esc(c.error)}</p>` : ""}
    <div class="tl-actions"><span class="muted mono" style="font-size:12px">${hhmm(c.started_at)}–${hhmm(c.ended_at)} · ${c.segment_count} lines · ${c.note_count} notes · ${c.todo_count} to-dos</span>
      <button class="linkbtn" data-conv="${c.id}">open</button>
      ${st === "skipped" ? `<button class="linkbtn" data-proc="${c.id}" data-force="1">make notes anyway</button>` : st !== "open" ? `<button class="linkbtn" data-proc="${c.id}">${st === "processed" ? "redo notes" : "make notes"}</button>` : ""}
    </div></div>`;
}
function bindConvItems(root) {
  $$("[data-conv]", root).forEach((b) => (b.onclick = () => go("conv/" + b.dataset.conv)));
  $$("[data-proc]", root).forEach((b) => (b.onclick = () => guard(async () => {
    b.disabled = true; b.textContent = "working…";
    const r = await api(`/api/conversations/${b.dataset.proc}/process${b.dataset.force ? "?force=true" : ""}`, { method: "POST" });
    toast(r.skipped ? `Not office talk (${(r.category || "").replace("_", " ")})` : `${r.notes} notes, ${r.todos_added} new to-dos`);
    VIEWS[state.tab]();
  })));
}

function todoRow(t) {
  const who = t.owner.toLowerCase() === "me" ? "" : `<span class="chip warn">waiting on ${esc(t.owner)}</span>`;
  return `<li class="todo-row ${t.status === "done" ? "done" : ""}" data-todo="${t.id}">
    <input type="checkbox" class="check" ${t.status === "done" ? "checked" : ""} aria-label="Done">
    <div><div class="t">${esc(t.title)}</div><div class="m">${prio(t.priority)}<span>${esc(t.project)}</span>${dueTag(t.due)}${who}${t.topic_title ? `<a class="linkbtn" href="#topics/${t.topic_id}">${esc(t.topic_title)}</a>` : ""}</div></div>
    <span class="muted mono" style="font-size:11px">T${t.id}</span></li>`;
}
function bindTodoRows(root) {
  $$("[data-todo]", root).forEach((li) => {
    const id = li.dataset.todo;
    const box = $("input.check", li);
    if (box) box.onchange = () => guard(async () => { await api("/api/todos/" + id, { method: "PATCH", body: { status: box.checked ? "done" : "open" } }); toast(box.checked ? "Done" : "Reopened"); refreshBehind(); });
    const t = $(".t", li); if (t) t.onclick = () => go("todo/" + id);
  });
}

async function loadLive(reset) {
  if (state.tab !== "today" || !$("#live")) return;
  const box = $("#live");
  const segs = state.day === today() ? await api("/api/live?limit=60") : await api(`/api/segments?day=${state.day}&limit=200`);
  if (reset) { box.innerHTML = ""; state.lastLiveId = 0; }
  const fresh = segs.filter((s) => s.id > state.lastLiveId);
  if (!fresh.length) { if (!box.children.length) box.innerHTML = `<p class="muted" style="margin:0">Waiting for speech…</p>`; return; }
  if ($("p.muted", box)) box.innerHTML = "";
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
  box.insertAdjacentHTML("beforeend", fresh.map(liveLine).join(""));
  state.lastLiveId = Math.max(...fresh.map((s) => s.id));
  if (atBottom || reset) box.scrollTop = box.scrollHeight;
}
function liveLine(s) {
  const name = s.speaker_name || (s.source === "me" ? "Mic" : "Call");
  const hint = s.hint && s.hint.likely_todo;
  return `<div class="line ${hint ? "hint" : ""}">${avatar(name, { me: s.speaker_kind === "me" })}<div><div class="who">${esc(name)}<time>${s.started_at.slice(11, 19)}</time>${s.speaker_kind === "auto" ? '<span class="chip dashed">auto-named</span>' : ""}</div><div class="what">${esc(s.text)}</div></div></div>`;
}

async function copyHandoff() {
  const md = await api("/api/export/" + state.day);
  await navigator.clipboard.writeText(md).catch(() => {});
  const r = await api("/api/export/" + state.day, { method: "POST" });
  toast("Copied for Claude. Saved to " + r.path);
}
async function processNow() {
  const btn = $("#btn-process");
  const r = await api("/api/process", { method: "POST", body: { day: state.day } });
  if (r.busy) { toast("Already making notes…"); return; }
  btn.disabled = true; btn.innerHTML = `${ic("refresh")}<span>Making notes…</span>`;
  toast("Making notes in the background. The free plan may need short waits.");
  const timer = setInterval(async () => {
    const p = await api("/api/process").catch(() => null);
    if (p && p.running) return;
    clearInterval(timer);
    const last = (p && p.last) || { processed: [], failed: [] };
    if (last.failed && last.failed.length) toast(last.failed[0].error, true);
    else toast(`Done: ${last.processed.length} conversation(s) checked.`);
    if (state.tab === "today") VIEWS.today();
  }, 4000);
}

// ------------------------------------------------------------------ TOPICS
VIEWS.topics = async function () {
  const v = $("#view"); const f = state.topicFilter;
  v.innerHTML = `<div class="page-head"><div><div class="eyebrow">Every subject, with its history</div><h1>Topics</h1></div>
    <div class="row">
      <input class="input" id="t-q" placeholder="Filter topics…" value="${esc(f.q)}" style="width:220px">
      <select class="select" id="t-project"><option value="">All projects</option>${state.projects.map((p) => `<option ${p === f.project ? "selected" : ""}>${esc(p)}</option>`).join("")}</select>
      <div class="seg" id="t-status">${["active", "closed", "all"].map((s) => `<button data-v="${s}" class="${f.status === s ? "on" : ""}">${s[0].toUpperCase() + s.slice(1)}</button>`).join("")}</div>
    </div></div><div id="t-list">${skeleton(4)}</div>`;
  const load = async () => {
    const q = new URLSearchParams({ status: f.status }); if (f.project) q.set("project", f.project); if (f.q) q.set("q", f.q);
    const list = await api("/api/topics?" + q);
    if (!$("#t-list")) return;
    $("#t-list").innerHTML = list.length ? `<div class="topic-grid reveal">${list.map((t, i) => topicCard(t, i)).join("")}</div>`
      : emptyState("No topics here", "Each subject you discuss becomes a card. When it comes up again on another day, a new dated section is added — older sections are kept.", "stack");
    bindTopicCards($("#t-list"));
  };
  let tmr; $("#t-q").oninput = (e) => { clearTimeout(tmr); tmr = setTimeout(() => { f.q = e.target.value.trim(); load(); }, 250); };
  $("#t-project").onchange = (e) => { f.project = e.target.value; load(); };
  $$("#t-status button").forEach((b) => (b.onclick = () => { f.status = b.dataset.v; $$("#t-status button").forEach((x) => x.classList.toggle("on", x === b)); load(); }));
  await load();
};

async function openTopic(id) {
  openDrawer(skeleton(5));
  const t = await api("/api/topics/" + id).catch((e) => { toast(e.message, true); return null; });
  if (!t) return closeDrawer();
  const others = await api("/api/topics?status=all");
  const byDay = {};
  t.updates.forEach((u) => (byDay[u.day] = byDay[u.day] || []).push(u));
  const days = Object.keys(byDay).sort().reverse();
  const openTodos = t.todos.filter((x) => x.status === "open");
  $("#drawer-body").innerHTML = `
    <div class="drawer-top"><button class="btn ghost icon-btn" data-close title="Close (Esc)">${ic("close")}</button><span class="muted mono">TOPIC ${t.id}</span><span class="spacer"></span>
      <button class="btn sm ${t.pinned ? "on" : ""}" id="tp-pin">${ic("pin")}<span>${t.pinned ? "Pinned" : "Pin"}</span></button>
      <button class="btn sm" id="tp-status">${ic(t.status === "active" ? "archive" : "refresh")}<span>${t.status === "active" ? "Close topic" : "Reopen"}</span></button>
      <select class="select" id="tp-merge" style="height:28px;font-size:12.5px;max-width:200px"><option value="">Merge into…</option>${others.filter((o) => o.id !== t.id).map((o) => `<option value="${o.id}">${esc(o.title)}</option>`).join("")}</select>
    </div>
    <input class="title-input" id="tp-title" value="${esc(t.title)}" aria-label="Topic title">
    <div class="row" style="margin-top:8px">
      <select class="select" id="tp-project" style="height:28px;font-size:12.5px">${state.projects.map((p) => `<option ${p === t.project ? "selected" : ""}>${esc(p)}</option>`).join("")}</select>
      <span class="muted">${esc(dayLabel(t.first_day))}${t.first_day !== t.last_day ? " → " + esc(dayLabel(t.last_day)) : ""} · ${days.length} day${days.length > 1 ? "s" : ""} · ${t.updates.length} section${t.updates.length > 1 ? "s" : ""}</span>
    </div>
    ${t.summary ? `<div class="now"><div class="eyebrow">Where it stands now</div>${esc(t.summary)}</div>` : ""}
    ${openTodos.length || t.notes.length ? `<div class="today-grid" style="grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;margin-bottom:26px">
      <section class="panel"><div class="panel-head"><h2>Open to-dos</h2><span class="muted mono">${openTodos.length}</span></div><div class="panel-body">${openTodos.length ? `<ul class="list">${openTodos.map(todoRow).join("")}</ul>` : '<p class="muted" style="margin:0">None open.</p>'}</div></section>
      <section class="panel"><div class="panel-head"><h2>Notes</h2><span class="muted mono">${t.notes.length}</span></div><div class="panel-body">${t.notes.length ? `<ul class="list">${t.notes.map((n) => `<li><b class="type-${n.type}" style="font-size:12px">${esc(NOTE_LABEL[n.type] || n.type)}</b> · ${esc(n.title)}</li>`).join("")}</ul>` : '<p class="muted" style="margin:0">No notes.</p>'}</div></section>
    </div>` : ""}
    <div class="section-title"><h2>History</h2><span class="count">newest first · older sections are never changed</span></div>
    ${days.map((d) => `<div class="day-group"><div class="day-label">${esc(dayLabel(d))}<span class="muted" style="text-transform:none;letter-spacing:0">${esc(d)}</span></div>
      ${byDay[d].map((u) => `<div class="panel update">
        <div class="u-head">${u.started_at ? `<span class="mono">${hhmm(u.started_at)}–${hhmm(u.ended_at)}</span>` : ""}${u.conversation_id ? `<button class="linkbtn" data-goconv="${u.conversation_id}">${esc(u.conversation_title || "conversation " + u.conversation_id)}</button>` : ""}<span class="spacer"></span>${avatars(u.people)}</div>
        ${u.summary ? `<p class="u-sum">${esc(u.summary)}</p>` : ""}
        ${(u.points || []).length ? `<ol class="pts">${u.points.map((p) => `<li><span>${pointHTML(p)}</span></li>`).join("")}</ol>` : ""}
      </div>`).join("")}</div>`).join("")}
    ${t.todos.length > openTodos.length ? `<div class="section-title"><h2>Finished to-dos</h2></div><ul class="list">${t.todos.filter((x) => x.status !== "open").map(todoRow).join("")}</ul>` : ""}`;
  const body = $("#drawer-body");
  bindTodoRows(body);
  $$("[data-goconv]", body).forEach((b) => (b.onclick = () => go("conv/" + b.dataset.goconv)));
  const patch = (fields, msg) => guard(async () => { await api("/api/topics/" + id, { method: "PATCH", body: fields }); toast(msg); openTopic(id); refreshBehind(); });
  $("#tp-title").onchange = (e) => patch({ title: e.target.value.trim() }, "Renamed");
  $("#tp-project").onchange = (e) => patch({ project: e.target.value }, "Project changed");
  $("#tp-pin").onclick = () => patch({ pinned: t.pinned ? 0 : 1 }, t.pinned ? "Unpinned" : "Pinned");
  $("#tp-status").onclick = () => patch({ status: t.status === "active" ? "closed" : "active" }, t.status === "active" ? "Topic closed" : "Topic reopened");
  $("#tp-merge").onchange = (e) => guard(async () => {
    if (!e.target.value) return;
    const target = others.find((o) => o.id === +e.target.value);
    if (!confirm(`Merge "${t.title}" into "${target.title}"? All its history, notes and to-dos move there.`)) { e.target.value = ""; return; }
    await api(`/api/topics/${id}/merge`, { method: "POST", body: { into: target.id } });
    toast("Merged"); go("topics/" + target.id); refreshBehind();
  });
}

// ------------------------------------------------------------------ conversation drawer
async function openConv(id) {
  openDrawer(skeleton(5));
  const c = await api("/api/conversations/" + id).catch((e) => { toast(e.message, true); return null; });
  if (!c) return closeDrawer();
  const byTopic = {};
  (c.points || []).forEach((p) => (byTopic[p.topic] = byTopic[p.topic] || { id: p.topic_id, pts: [] }).pts.push(p.text));
  const names = [...new Set(c.segments.map((s) => s.speaker_name).filter(Boolean))];
  $("#drawer-body").innerHTML = `
    <div class="drawer-top"><button class="btn ghost icon-btn" data-close title="Close (Esc)">${ic("close")}</button><span class="muted mono">CONVERSATION ${c.id}</span><span class="spacer"></span>
      <button class="btn sm" id="cv-redo">${ic("refresh")}<span>${c.status === "processed" ? "Redo notes" : "Make notes"}</span></button></div>
    <h1 style="font-size:24px">${esc(c.title || "Conversation " + c.id)}</h1>
    <div class="row" style="margin:8px 0 0"><span class="muted mono">${esc(dayLabel(c.day))} · ${hhmm(c.started_at)}–${hhmm(c.ended_at)}</span>${c.project ? `<span class="chip">${esc(c.project)}</span>` : ""}<span class="chip">${esc(c.status)}${c.category ? " · " + esc(c.category.replace("_", " ")) : ""}</span>${avatars(names, 8)}</div>
    ${c.summary ? `<div class="now"><div class="eyebrow">Summary</div>${esc(c.summary)}</div>` : ""}
    <div class="tabs-mini seg" id="cv-tabs"><button class="on" data-v="points">Points</button><button data-v="tx">Transcript</button><button data-v="items">Notes & to-dos</button></div>
    <div id="cv-pane"></div>`;
  const panes = {
    points: () => Object.keys(byTopic).length ? Object.entries(byTopic).map(([title, o]) => `<div class="panel update"><div class="u-head"><a class="linkbtn" href="#topics/${o.id}"><b style="font-size:13.5px;color:var(--text)">${esc(title)}</b></a></div><ol class="pts">${o.pts.map((p) => `<li><span>${pointHTML(p)}</span></li>`).join("")}</ol></div>`).join("")
      : emptyState("No points yet", "Points appear after notes are made for this conversation.", "note"),
    tx: () => `<div class="tx">${c.segments.map((s) => `<div class="l"><time>${s.started_at.slice(11, 19)}</time><span class="n">${esc(s.speaker_name || (s.source === "me" ? "Mic" : "Call"))}</span><span>${esc(s.text)}</span></div>`).join("")}</div>`,
    items: () => `<div class="section-title"><h2>To-dos</h2></div>${c.todos.length ? `<ul class="list">${c.todos.map(todoRow).join("")}</ul>` : '<p class="muted">None.</p>'}
      <div class="section-title" style="margin-top:20px"><h2>Notes</h2></div>${c.notes.length ? c.notes.map(noteCard).join("") : '<p class="muted">None.</p>'}`,
  };
  const show = (k) => { $("#cv-pane").innerHTML = panes[k](); bindTodoRows($("#cv-pane")); $$("#cv-tabs button").forEach((b) => b.classList.toggle("on", b.dataset.v === k)); };
  $$("#cv-tabs button").forEach((b) => (b.onclick = () => show(b.dataset.v)));
  show("points");
  $("#cv-redo").onclick = () => guard(async () => {
    $("#cv-redo").disabled = true;
    const r = await api(`/api/conversations/${id}/process${c.status === "skipped" ? "?force=true" : ""}`, { method: "POST" });
    toast(r.skipped ? "Not office talk" : `${r.notes} notes, ${r.todos_added} new to-dos`); openConv(id); refreshBehind();
  });
}

// ------------------------------------------------------------------ drawer plumbing
function openDrawer(html) {
  $("#drawer-body").innerHTML = html; $("#drawer").hidden = false; $("#scrim").hidden = false;
  document.body.style.overflow = "hidden";
}
function closeDrawer(updateHash = true) {
  if ($("#drawer").hidden) return;
  $("#drawer").hidden = true; $("#scrim").hidden = true; document.body.style.overflow = "";
  if (updateHash && location.hash.includes("/")) history.replaceState(null, "", "#" + state.tab);
}
$("#scrim").onclick = () => closeDrawer();
document.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) closeDrawer(); });
function refreshBehind() { if (VIEWS[state.tab]) VIEWS[state.tab](); }

// ------------------------------------------------------------------ BOARD
const COLS = [["todo", "To do"], ["doing", "Doing"], ["blocked", "Blocked"], ["done", "Done"]];
VIEWS.board = async function () {
  const v = $("#view"); const f = state.boardFilter;
  v.innerHTML = `<div class="page-head"><div><div class="eyebrow">Drag cards between columns</div><h1>Board</h1></div>
    <div class="row"><select class="select" id="b-project"><option value="">All projects</option>${state.projects.map((p) => `<option ${p === f.project ? "selected" : ""}>${esc(p)}</option>`).join("")}</select>
    <div class="seg" id="b-owner"><button data-v="" class="${!f.owner ? "on" : ""}">Everyone</button><button data-v="me" class="${f.owner === "me" ? "on" : ""}">Mine</button><button data-v="others" class="${f.owner === "others" ? "on" : ""}">Waiting on others</button></div>
    <button class="btn primary" id="btn-copy2">${ic("copy")}<span>Copy for Claude</span></button></div></div>
    <form class="add-row" id="add">
      <input class="input" name="title" placeholder="Add a to-do…  (press N)" required autocomplete="off">
      <select class="select" name="priority" aria-label="Priority">${["none", "P0", "P0-1", "P0-2", "P0-3", "P1", "P2", "P3"].map((p) => `<option>${p}</option>`).join("")}</select>
      <select class="select" name="project" aria-label="Project">${state.projects.map((p) => `<option>${esc(p)}</option>`).join("")}</select>
      <input class="input" type="date" name="due" aria-label="Due date">
      <button class="btn accent">${ic("plus")}<span>Add</span></button>
    </form><div id="b-cols">${skeleton(3)}</div>`;
  const pq = f.project ? "&project=" + encodeURIComponent(f.project) : "";
  const [open, done] = await Promise.all([api("/api/todos?status=open" + pq), api("/api/todos?status=done" + pq)]);
  if (state.tab !== "board") return;
  const since = localISO(new Date(Date.now() - 7 * 864e5));
  const keep = (t) => !f.owner || (f.owner === "me" ? t.owner.toLowerCase() === "me" : t.owner.toLowerCase() !== "me");
  const items = { todo: [], doing: [], blocked: [], done: done.filter((t) => (t.done_at || "").slice(0, 10) >= since).filter(keep) };
  open.filter(keep).forEach((t) => items[["doing", "blocked"].includes(t.stage) ? t.stage : "todo"].push(t));
  $("#b-cols").innerHTML = `<div class="board">${COLS.map(([k, label]) => `<section class="col" data-col="${k}"><div class="col-head"><h2>${label}</h2><span class="c">${items[k].length}</span></div>
    ${items[k].map(kcard).join("") || `<p class="muted" style="font-size:12px;padding:0 6px">Drop cards here</p>`}</section>`).join("")}</div>`;
  bindBoard();
  $("#b-project").onchange = (e) => { f.project = e.target.value; VIEWS.board(); };
  $$("#b-owner button").forEach((b) => (b.onclick = () => { f.owner = b.dataset.v; VIEWS.board(); }));
  $("#btn-copy2").onclick = () => guard(copyHandoff);
  $("#add").onsubmit = (e) => guard(async () => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(e.target).entries());
    await api("/api/todos", { method: "POST", body }); toast("Added"); VIEWS.board();
  });
};
function kcard(t) {
  const who = t.owner.toLowerCase() === "me" ? "" : `<span class="chip warn">${esc(t.owner)}</span>`;
  return `<article class="kcard" draggable="true" data-id="${t.id}">
    <div class="t">${esc(t.title)}</div>
    <div class="m">${prio(t.priority)}${dueTag(t.due)}${who}${t.confidence < 0.6 ? '<span class="chip warn">check</span>' : ""}<span class="spacer"></span><span class="mono">T${t.id}</span></div>
    ${t.topic_title ? `<div class="m"><a class="linkbtn topic-link" href="#topics/${t.topic_id}">${esc(t.topic_title)}</a></div>` : ""}
  </article>`;
}
function bindBoard() {
  let dragId = null;
  $$(".kcard").forEach((c) => {
    c.ondragstart = (e) => { dragId = c.dataset.id; c.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", dragId); };
    c.ondragend = () => c.classList.remove("dragging");
    c.onclick = (e) => { if (!e.target.closest("a")) go("todo/" + c.dataset.id); };
  });
  $$(".col").forEach((col) => {
    col.ondragover = (e) => { e.preventDefault(); col.classList.add("drop"); };
    col.ondragleave = () => col.classList.remove("drop");
    col.ondrop = (e) => guard(async () => {
      e.preventDefault(); col.classList.remove("drop");
      const id = dragId || e.dataTransfer.getData("text/plain"); const k = col.dataset.col;
      const body = k === "done" ? { status: "done" } : { status: "open", stage: k };
      await api("/api/todos/" + id, { method: "PATCH", body });
      toast(k === "done" ? "Done" : `Moved to ${k}`); VIEWS.board();
    });
  });
}

async function openTodo(id) {
  openDrawer(skeleton(3));
  const [all, topics] = await Promise.all([api("/api/todos?status=all"), api("/api/topics?status=all")]);
  const t = all.find((x) => x.id === id);
  if (!t) { toast("To-do not found", true); return closeDrawer(); }
  $("#drawer-body").innerHTML = `
    <div class="drawer-top"><button class="btn ghost icon-btn" data-close>${ic("close")}</button><span class="muted mono">TO-DO ${t.id}</span><span class="spacer"></span>
      <button class="btn sm" id="td-done">${ic("check")}<span>${t.status === "done" ? "Reopen" : "Mark done"}</span></button>
      <button class="btn sm" id="td-drop">${ic("archive")}<span>Drop</span></button>
      <button class="btn sm ghost" id="td-del" title="Delete">${ic("trash")}</button></div>
    <input class="title-input" id="td-title" value="${esc(t.title)}" aria-label="Title">
    ${t.detail ? `<p class="sub">${esc(t.detail)}</p>` : ""}
    ${t.evidence ? `<blockquote class="quote">“${esc(t.evidence)}”</blockquote>` : ""}
    <form class="form" style="margin-top:20px" id="td-form">
      <div class="form-group">
        <div class="field"><label for="td-p">Priority</label><select class="select" id="td-p" name="priority">${["none", "P0", "P0-1", "P0-2", "P0-3", "P1", "P2", "P3"].map((p) => `<option ${p === t.priority ? "selected" : ""}>${p}</option>`).join("")}</select></div>
        <div class="field"><label for="td-s">Stage</label><select class="select" id="td-s" name="stage">${["todo", "doing", "blocked"].map((p) => `<option ${p === t.stage ? "selected" : ""}>${p}</option>`).join("")}</select></div>
        <div class="field"><label for="td-o">Owner</label><input class="input" id="td-o" name="owner" value="${esc(t.owner)}"><span class="help">"me" or a person's name</span></div>
        <div class="field"><label for="td-d">Due</label><input class="input" id="td-d" type="date" name="due" value="${esc(t.due || "")}"></div>
        <div class="field"><label for="td-pr">Project</label><select class="select" id="td-pr" name="project">${state.projects.map((p) => `<option ${p === t.project ? "selected" : ""}>${esc(p)}</option>`).join("")}</select></div>
        <div class="field"><label for="td-t">Topic</label><select class="select" id="td-t" name="topic_id"><option value="">No topic</option>${topics.map((o) => `<option value="${o.id}" ${o.id === t.topic_id ? "selected" : ""}>${esc(o.title)}</option>`).join("")}</select></div>
      </div>
      <div class="field"><label for="td-c">Comment</label><input class="input" id="td-c" name="comment" placeholder="What changed? Kept in the history below."></div>
      <div><button class="btn accent">Save</button></div>
    </form>
    <div class="section-title" style="margin-top:24px"><h2>History</h2></div>
    <ul class="list">${(t.log || []).slice().reverse().map((l) => `<li><span class="mono muted" style="font-size:12px">${esc(l.at.replace("T", " ").slice(0, 16))}</span> · ${esc(l.text)}</li>`).join("") || '<li class="muted">No changes yet.</li>'}</ul>`;
  const after = () => { openTodo(id); refreshBehind(); };
  $("#td-form").onsubmit = (e) => guard(async () => {
    e.preventDefault();
    const b = Object.fromEntries(new FormData(e.target).entries());
    b.topic_id = b.topic_id ? +b.topic_id : null; if (!b.comment) delete b.comment;
    b.title = $("#td-title").value.trim();
    await api("/api/todos/" + id, { method: "PATCH", body: b }); toast("Saved"); after();
  });
  $("#td-done").onclick = () => guard(async () => { await api("/api/todos/" + id, { method: "PATCH", body: { status: t.status === "done" ? "open" : "done" } }); after(); });
  $("#td-drop").onclick = () => guard(async () => { await api("/api/todos/" + id, { method: "PATCH", body: { status: "dropped", comment: "dropped by me" } }); toast("Dropped"); closeDrawer(); refreshBehind(); });
  $("#td-del").onclick = () => guard(async () => { if (!confirm("Delete this to-do for good?")) return; await api("/api/todos/" + id, { method: "DELETE" }); closeDrawer(); refreshBehind(); });
}

// ------------------------------------------------------------------ PEOPLE
VIEWS.people = async function () {
  const v = $("#view");
  v.innerHTML = `<div class="page-head"><div><div class="eyebrow">Recognised by voice · fingerprints stay on this laptop</div><h1>People</h1></div></div>
    <p class="sub" style="margin:-8px 0 22px">New voices get a placeholder name. Click a name to change it — the new name replaces the old one in every note, to-do and topic. If one person shows up twice, merge them.</p>
    <div id="p-list">${skeleton(3)}</div>`;
  const people = await api("/api/people");
  if (state.tab !== "people") return;
  if (!people.length) { $("#p-list").innerHTML = emptyState("No voices yet", "As soon as someone speaks for a few seconds, their voice gets a card here.", "users"); return; }
  $("#p-list").innerHTML = `<div class="people-grid reveal">${people.map((p, i) => `
    <article class="panel pcard" style="--i:${i}" data-p="${p.id}">
      <div class="head">${avatar(p.name, { lg: true, me: p.kind === "me" })}
        <div><input class="name-input" value="${esc(p.name)}" aria-label="Name">
          <div class="row" style="margin-top:2px">${p.kind === "me" ? '<span class="chip accent">You</span>' : p.kind === "auto" ? '<span class="chip dashed">auto-named · rename me</span>' : '<span class="chip">named</span>'}</div></div>
        <span></span></div>
      <div class="facts"><span><b class="mono">${mins(p.seconds)}</b> talking</span><span><b class="mono">${p.conversations}</b> conversations</span><span>${p.last_heard ? "last " + esc(relDays(p.last_heard.slice(0, 10))) : ""}</span></div>
      <div class="lines">${(p.lines || []).map((l) => `<div><span class="mono muted">${esc(dayLabel(l.started_at.slice(0, 10)))} ${hhmm(l.started_at)}</span> ${esc(l.text)}</div>`).join("") || '<span class="muted">No lines yet.</span>'}</div>
      <div class="row">${p.kind !== "me" ? `<button class="btn sm" data-me>${ic("user")}<span>This is me</span></button>` : ""}
        <select class="select" data-merge style="height:28px;font-size:12.5px" aria-label="Merge"><option value="">Same person as…</option>${people.filter((o) => o.id !== p.id).map((o) => `<option value="${o.id}">${esc(o.name)}</option>`).join("")}</select></div>
    </article>`).join("")}</div>`;
  $$(".pcard").forEach((card) => {
    const id = card.dataset.p; const input = $(".name-input", card); const orig = input.value;
    let saving = false;
    const save = () => guard(async () => {
      const name = input.value.trim(); if (saving || !name || name === orig) { if (!name) input.value = orig; return; }
      saving = true;
      await api("/api/people/" + id, { method: "PATCH", body: { name } }); toast(`Renamed to ${name} everywhere`); VIEWS.people();
    });
    input.onkeydown = (e) => { if (e.key === "Enter") input.blur(); if (e.key === "Escape") { input.value = orig; input.blur(); } };
    input.onblur = save;
    const me = $("[data-me]", card);
    if (me) me.onclick = () => guard(async () => {
      const name = prompt("Your name", state.myName || ""); if (!name) return;
      await api("/api/people/" + id, { method: "PATCH", body: { name, me: true } }); toast("Marked as you"); VIEWS.people();
    });
    $("[data-merge]", card).onchange = (e) => guard(async () => {
      const into = e.target.value; if (!into) return;
      const target = $(`.pcard[data-p="${into}"] .name-input`).value;
      if (!confirm(`Merge "${orig}" into "${target}"? Their lines, and the name everywhere, become "${target}".`)) { e.target.value = ""; return; }
      await api(`/api/people/${id}/merge`, { method: "POST", body: { into: +into } }); toast("Merged"); VIEWS.people();
    });
  });
};

// ------------------------------------------------------------------ NOTES
const NOTE_LABEL = { bug_explanation: "Bug", feature_requirement: "Feature", decision: "Decision", question: "Question", info: "Info" };
const FIELD_LABEL = { problem: "Problem", root_cause: "Root cause", fix: "Fix", modules_or_files: "Where", how_to_verify: "Verify",
  what: "What", why: "Why", acceptance_criteria: "Acceptance", screens_apis_db: "Screens / APIs", decision: "Decision",
  by_whom: "By", reason: "Reason", question: "Question", waiting_on: "Waiting on", text: "Text" };
function noteCard(n) {
  const b = n.body || {};
  const rows = Object.entries(FIELD_LABEL).filter(([k]) => b[k] && (!Array.isArray(b[k]) || b[k].length))
    .map(([k, l]) => `<dt>${l}</dt><dd>${Array.isArray(b[k]) ? b[k].map(esc).join("<br>") : esc(b[k])}</dd>`).join("");
  return `<article class="panel ncard" data-note="${n.id}">
    <div class="row" style="font-size:12px"><b class="type-${n.type}">${NOTE_LABEL[n.type] || n.type}</b><span class="chip">${esc(n.project)}</span><span class="muted mono">${esc(dayLabel(n.day))} · N${n.id}</span>${n.topic_id ? `<a class="linkbtn" href="#topics/${n.topic_id}">open topic</a>` : ""}${n.pinned ? '<span class="chip accent">pinned</span>' : ""}</div>
    <h3 style="margin-top:8px">${esc(n.title)}</h3><dl>${rows}</dl>
    ${n.evidence ? `<blockquote class="quote">“${esc(n.evidence)}”</blockquote>` : ""}
    <div class="row" style="margin-top:10px"><button class="linkbtn" data-pin>${n.pinned ? "unpin" : "pin"}</button><button class="linkbtn" data-todo-from>make to-do</button><button class="linkbtn" data-del>delete</button></div>
  </article>`;
}
VIEWS.notes = async function () {
  const v = $("#view");
  v.innerHTML = `<div class="page-head"><div><div class="eyebrow">Bugs, features, decisions, questions</div><h1>Notes</h1></div>
    <div class="row"><select class="select" id="n-type" aria-label="Type"><option value="">All types</option>${Object.entries(NOTE_LABEL).map(([k, l]) => `<option value="${k}">${l}</option>`).join("")}</select>
    <select class="select" id="n-project" aria-label="Project"><option value="">All projects</option>${state.projects.map((p) => `<option>${esc(p)}</option>`).join("")}</select></div></div><div id="n-list">${skeleton(3)}</div>`;
  const load = async () => {
    const q = new URLSearchParams(); if ($("#n-type").value) q.set("type", $("#n-type").value); if ($("#n-project").value) q.set("project", $("#n-project").value);
    const notes = await api("/api/notes?" + q);
    if (state.tab !== "notes") return;
    $("#n-list").innerHTML = notes.length ? `<div class="note-grid">${notes.map(noteCard).join("")}</div>` : emptyState("No notes yet", "Notes appear after conversations are processed.", "note");
    $$("[data-note]").forEach((el) => {
      const id = el.dataset.note; const n = notes.find((x) => String(x.id) === id);
      $("[data-pin]", el).onclick = () => guard(async () => { await api("/api/notes/" + id, { method: "PATCH", body: { pinned: n.pinned ? 0 : 1 } }); load(); });
      $("[data-todo-from]", el).onclick = () => guard(async () => { await api("/api/todos", { method: "POST", body: { title: n.title, detail: `From note N${n.id}`, project: n.project } }); toast("To-do added"); });
      $("[data-del]", el).onclick = () => guard(async () => { if (!confirm("Delete this note?")) return; await api("/api/notes/" + id, { method: "DELETE" }); load(); });
    });
  };
  $("#n-type").onchange = load; $("#n-project").onchange = load;
  await load();
};

// ------------------------------------------------------------------ SEARCH
VIEWS.search = async function () {
  const v = $("#view");
  v.innerHTML = `<div class="page-head"><div><div class="eyebrow">Everything you heard, every note and topic</div><h1>Search</h1></div></div>
    <input class="input" id="q" placeholder="Try: breadcrumb, Vinod, deploy, S3…" style="width:100%;height:46px;font-size:16px" autocomplete="off" aria-label="Search"><div id="results" style="margin-top:22px"></div>`;
  let tmr; $("#q").oninput = () => { clearTimeout(tmr); tmr = setTimeout(run, 250); };
  $("#q").focus();
  async function run() {
    const q = $("#q").value.trim(); if (!q) { $("#results").innerHTML = ""; return; }
    const r = await api("/api/search?q=" + encodeURIComponent(q));
    const hl = (s) => esc(s).replace(/\[/g, "<mark>").replace(/\]/g, "</mark>");
    $("#results").innerHTML = `
      <div class="section-title"><h2>Topics</h2><span class="count">${r.topics.length}</span></div>${r.topics.length ? `<div class="topic-grid">${r.topics.map((t, i) => topicCard(t, i)).join("")}</div>` : '<p class="muted">None.</p>'}
      <div class="section-title" style="margin-top:20px"><h2>To-dos</h2><span class="count">${r.todos.length}</span></div><ul class="list">${r.todos.map(todoRow).join("") || '<li class="muted">None.</li>'}</ul>
      <div class="section-title" style="margin-top:20px"><h2>Notes</h2><span class="count">${r.notes.length}</span></div>${r.notes.map(noteCard).join("") || '<p class="muted">None.</p>'}
      <div class="section-title" style="margin-top:20px"><h2>Heard</h2><span class="count">${r.segments.length}</span></div><div class="tx">${r.segments.map((s) => `<div class="l"><time>${esc(s.started_at.slice(5, 16).replace("T", " "))}</time><span class="n"><button class="linkbtn" data-conv="${s.conversation_id}">conversation</button></span><span>${hl(s.snippet)}</span></div>`).join("")}</div>`;
    bindTopicCards($("#results")); bindTodoRows($("#results")); bindConvItems($("#results"));
  }
};

// ------------------------------------------------------------------ SETTINGS
const SETTINGS = [
  ["Listening", [
    ["mic_device", "Microphone", "text", "Part of the device name, e.g. Headset. Empty = Windows default."],
    ["capture_calls", "Also record PC sound (calls)", "bool", "Hears the other side of Teams, Meet and Zoom."],
    ["work_hours.enabled", "Only during work hours", "bool"], ["work_hours.start", "Work starts", "time"], ["work_hours.end", "Work ends", "time"],
    ["lunch.enabled", "Pause at lunch", "bool"], ["lunch.start", "Lunch starts", "time"], ["lunch.end", "Lunch ends", "time"],
    ["hotkey", "Pause hotkey", "text", "Works in every app."]]],
  ["Speech to text", [
    ["stt_engine", "Engine", ["groq", "local"], "groq = free Groq Whisper large-v3 (best). local = laptop only."],
    ["groq_stt_model", "Groq model", ["whisper-large-v3", "whisper-large-v3-turbo"]],
    ["groq_language", "Language", ["hi", "", "en"], "hi = best for Hindi + English (tested). en translates Hindi — avoid."],
    ["whisper_model", "Laptop model (backup)", ["small", "base", "tiny", "medium", "large-v3-turbo", "hinglish-swift", "hinglish-prime"]],
    ["language", "Laptop model language", ["hi", "", "en"]],
    ["vocabulary", "Words to expect", "text", "Names and terms to spell right."],
    ["battery_mode", "On battery", ["defer", "always"], "defer = the laptop model waits for the charger. Groq works on battery."],
    ["keep_audio_days", "Keep audio (days)", "number", "0 = delete audio right after it becomes text."]]],
  ["Voices", [
    ["voice_id", "Recognise who is speaking", "bool", "Voice fingerprints stay on this laptop."],
    ["my_name", "Your name", "text", "The voice that dominates your mic gets this name."],
    ["speaker_threshold", "Match strictness (0.4–0.7)", "number", "Higher = more separate people, lower = more merging."],
    ["speaker_min_new_seconds", "New person after (seconds of speech)", "number"]]],
  ["Notes", [
    ["auto_process", "Make notes automatically", "bool"], ["llm_engine", "Notes made by", ["groq", "claude", "claude-code"]],
    ["groq_llm_model", "Groq notes model", ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"], "When its free daily limit is used up, PA tries the next one."],
    ["office_only", "Keep only office talk", "bool"], ["delete_non_work_text", "Delete text of non-office talk", "bool"],
    ["min_words_for_notes", "Ignore talks shorter than (words)", "number"],
    ["conversation_gap_min", "New conversation after silence (min)", "number"], ["max_conversation_min", "Cut non-stop talk every (min)", "number"],
    ["projects", "Projects (comma list)", "list"], ["exclude_personal_from_handoff", "Keep Personal out of the Claude hand-off", "bool"],
    ["claude_model", "Claude model (paid, optional)", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]]]],
];
const getPath = (o, p) => p.split(".").reduce((a, k) => a?.[k], o);
function setPath(o, p, val) { const ks = p.split("."); let x = o; ks.slice(0, -1).forEach((k) => (x = x[k] ??= {})); x[ks.at(-1)] = val; }
function fieldHTML([key, label, type, help], cfg) {
  const val = getPath(cfg, key);
  const fid = "f-" + key.replace(/\W/g, "-");
  if (type === "bool") return `<div class="field"><label class="switch"><input type="checkbox" name="${key}" ${val ? "checked" : ""}> ${esc(label)}</label>${help ? `<span class="help">${esc(help)}</span>` : ""}</div>`;
  let input;
  if (Array.isArray(type)) input = `<select class="select" id="${fid}" name="${key}">${type.map((o) => `<option value="${o}" ${o === val ? "selected" : ""}>${o || "(auto)"}</option>`).join("")}</select>`;
  else if (type === "list") input = `<input class="input" id="${fid}" name="${key}" value="${esc((val || []).join(", "))}">`;
  else input = `<input class="input" id="${fid}" name="${key}" type="${type === "number" ? "number" : type === "time" ? "time" : "text"}" step="any" value="${esc(val ?? "")}">`;
  return `<div class="field"><label for="${fid}">${esc(label)}</label>${input}${help ? `<span class="help">${esc(help)}</span>` : ""}</div>`;
}
VIEWS.settings = async function () {
  const v = $("#view");
  v.innerHTML = `<div class="page-head"><div><div class="eyebrow">Saved on this laptop</div><h1>Settings</h1></div></div>${skeleton(4)}`;
  const s = await api("/api/settings");
  if (state.tab !== "settings") return;
  state.settings = s; state.projects = s.config.projects; state.myName = s.config.my_name;
  v.innerHTML = `<div class="page-head"><div><div class="eyebrow">Saved on this laptop</div><h1>Settings</h1></div></div>
    <div class="settings-grid">
      <form class="stack" id="set-form">
        ${SETTINGS.map(([group, fields]) => `<section class="panel"><div class="panel-head"><h2>${group}</h2></div><div class="panel-body form">${fields.map((f) => fieldHTML(f, s.config)).join("")}</div></section>`).join("")}
        <div><button class="btn accent">Save settings</button></div>
      </form>
      <div class="stack">
        <section class="panel"><div class="panel-head"><h2>Free Groq key</h2><span class="chip ${s.keys.groq ? "accent" : "warn"}">${s.keys.groq ? "saved" : "missing"}</span></div>
          <div class="panel-body form"><div class="field"><label for="groq-key">API key (starts with gsk_)</label><input class="input" id="groq-key" type="password" placeholder="gsk_…" autocomplete="off"><span class="help">From console.groq.com. Saved encrypted for your Windows user.</span><span class="err" id="groq-err"></span></div>
          <div class="row"><button class="btn" id="btn-groq">Save</button><button class="btn" id="btn-groq-test">Test key</button><button class="btn ghost" id="btn-groq-del">Remove</button></div><p class="muted" id="groq-state" style="margin:0"></p></div></section>
        <section class="panel"><div class="panel-head"><h2>Claude key (optional, paid)</h2><span class="chip">${s.keys.anthropic ? "saved" : "not used"}</span></div>
          <div class="panel-body form"><div class="field"><label for="api-key">API key (starts with sk-ant-)</label><input class="input" id="api-key" type="password" placeholder="sk-ant-…" autocomplete="off"><span class="err" id="ant-err"></span></div>
          <div class="row"><button class="btn" id="btn-key">Save</button><button class="btn ghost" id="btn-key-del">Remove</button></div></div></section>
        <section class="panel"><div class="panel-head"><h2>My profile</h2></div><div class="panel-body form">
          <div class="field"><label for="profile">What the note-maker should know about your work</label><textarea class="input" id="profile" rows="16">${esc(s.profile)}</textarea><span class="help">Projects, people, which screens belong to which project, words.</span></div>
          <div class="row"><button class="btn" id="btn-profile">Save profile</button><span class="muted" id="profile-state"></span></div></div></section>
        <section class="panel"><div class="panel-head"><h2>Files</h2></div><div class="panel-body kv"><span class="muted">Data</span><code>${esc(s.paths.data)}</code><span class="muted">Hand-offs</span><code>${esc(s.paths.exports)}</code></div></section>
      </div>
    </div>`;
  $("#set-form").onsubmit = (e) => guard(async () => {
    e.preventDefault();
    const cfg = {}; const form = e.target;
    for (const [, fields] of SETTINGS) for (const [key, , type] of fields) {
      const el = form.elements[key]; if (!el) continue;
      let val = type === "bool" ? el.checked : el.value;
      if (type === "number") val = Number(val);
      if (type === "list") val = val.split(",").map((x) => x.trim()).filter(Boolean);
      setPath(cfg, key, val);
    }
    cfg.work_hours.days = s.config.work_hours.days;
    const r = await api("/api/settings", { method: "PUT", body: { config: cfg } });
    state.projects = r.config.projects; state.myName = r.config.my_name;
    toast(r.restart_needed ? "Saved. Restart PA to use the new device, model or hotkey." : "Saved");
  });
  const saveKey = (name, input, errEl) => guard(async () => {
    $(errEl).textContent = "";
    try { await api("/api/apikey", { method: "PUT", body: { name, key: input ? $(input).value : "" } }); }
    catch (e) { $(errEl).textContent = e.message; return; }
    toast(input ? "Key saved (encrypted)" : "Key removed"); VIEWS.settings();
  });
  $("#btn-groq").onclick = () => saveKey("groq", "#groq-key", "#groq-err");
  $("#btn-groq-del").onclick = () => saveKey("groq", null, "#groq-err");
  $("#btn-key").onclick = () => saveKey("anthropic", "#api-key", "#ant-err");
  $("#btn-key-del").onclick = () => saveKey("anthropic", null, "#ant-err");
  $("#btn-groq-test").onclick = () => guard(async () => { $("#groq-state").textContent = "Testing…"; const r = await api("/api/check/groq", { method: "POST" }); $("#groq-state").textContent = `Speech-to-text: ${r.speech_to_text} · Notes AI: ${r.notes_ai}`; });
  $("#btn-profile").onclick = () => guard(async () => { await api("/api/settings", { method: "PUT", body: { profile: $("#profile").value } }); $("#profile-state").textContent = "Saved " + new Date().toLocaleTimeString(); });
};

// ------------------------------------------------------------------ keyboard
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#drawer").hidden) { closeDrawer(); return; }
  if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) || e.ctrlKey || e.altKey || e.metaKey) return;
  const tabs = { 1: "today", 2: "topics", 3: "board", 4: "people", 5: "notes", 6: "settings" };
  if (tabs[e.key]) go(tabs[e.key]);
  else if (e.key === "/") { e.preventDefault(); go("search"); }
  else if (e.key === "n") { e.preventDefault(); go("board"); setTimeout(() => $("#add input[name=title]")?.focus(), 300); }
  else if (e.key === "P") $("#btn-pause").click();
});

// ------------------------------------------------------------------ start
(async function init() {
  hydrateIcons();
  const s = await api("/api/settings").catch(() => null);
  if (s) { state.projects = s.config.projects; state.myName = s.config.my_name; }
  route();
  refreshStatus();
  setInterval(refreshStatus, 2000);
  setInterval(() => loadLive(false), 3000);
})();
