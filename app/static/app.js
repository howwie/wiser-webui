import { html, render, useState, useEffect, useRef } from "./vendor/htm-preact.js";

const MIN_TEMP = 5;
const MAX_TEMP = 30;
const TRACK_FROM = 10;
const TRACK_TO = 28;

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const deg = (v) => (v == null ? "–" : `${v.toFixed(1)}°`);
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
// The hub measures to 0.1° but the Wiser app shows readings to the nearest 0.5°, as targets are set
// in 0.5° steps. Without this a room held at 19° could show 18.8°, which looks like it isn't working.
const halfDeg = (v) => (v == null ? null : Math.round(v * 2) / 2);
const trackPct = (v) => clamp((v - TRACK_FROM) / (TRACK_TO - TRACK_FROM), 0, 1) * 100;

async function request(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(json.detail || `The request failed (${res.status}).`);
  return json;
}

// The room's next schedule entry after now, as { at: Date, temp, days }. The schedule is 7 lists
// (Monday first) of [minutes after midnight, degrees or null for off], in the hub's local time,
// which is this browser's local time too. With `current` given, entries that keep the same
// temperature are skipped, so "until" means until the temperature actually changes.
function nextChange(schedule, current, now = new Date()) {
  if (!schedule) return null;
  const today = (now.getDay() + 6) % 7;
  const nowMinutes = now.getHours() * 60 + now.getMinutes();
  for (let days = 0; days <= 7; days++) {
    for (const [minutes, temp] of schedule[(today + days) % 7]) {
      if ((days === 0 && minutes <= nowMinutes) || temp === current) continue;
      const at = new Date(now);
      at.setDate(now.getDate() + days);
      at.setHours(Math.floor(minutes / 60), minutes % 60, 0, 0);
      return { at, temp, days };
    }
  }
  return null;
}

const hhmm = (date) => date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
const when = ({ at, days }) =>
  days === 0 ? hhmm(at) : days === 1 ? `tomorrow ${hhmm(at)}` : `${at.toLocaleDateString("en-GB", { weekday: "short" })} ${hhmm(at)}`;

function statusLine(room) {
  if (room.temp == null) return "No reading. Check the radiator valve or thermostat.";
  if (room.boost) return `Boosting to ${deg(room.target)}${room.until ? ` until ${clock(room.until)}` : ""}`;
  if (room.origin === "FromAwayMode") return `Away mode, set back to ${deg(room.target)}`;
  if (room.target == null) return "Heating is off in this room";
  if (room.override) {
    // A hold lasts until the next schedule entry, whatever its temperature.
    const end = room.until ? clock(room.until) : nextChange(room.schedule) && when(nextChange(room.schedule));
    return `Held at ${deg(room.target)} until ${end || "the next schedule change"}`;
  }
  if (room.mode === "manual") return `Manual: holding ${deg(room.target)}`;
  const next = nextChange(room.schedule, room.scheduled);
  if (!next) return `Following the schedule (${deg(room.target)})`;
  return `On schedule until ${when(next)}, then ${next.temp == null ? "off" : deg(next.temp)}`;
}

const closeIcon = html`
  <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
    <path d="M6 6l12 12M18 6L6 18" />
  </svg>
`;

function Switch({ label, hint, checked, onChange, disabled }) {
  return html`
    <div class="switch-row">
      <button class="switch" role="switch" aria-checked=${checked} aria-label=${label} disabled=${disabled}
        onClick=${() => onChange(!checked)}></button>
      <span>
        <span class="label">${label}</span>
        ${hint && html`<span class="hint">${hint}</span>`}
      </span>
    </div>
  `;
}

const BOOST_OPTIONS = [[30, "30 min"], [60, "1 hour"], [120, "2 hours"], [180, "3 hours"]];

// A native <dialog>: showModal() gives us the backdrop, focus trapping and Esc to close.
function BoostDialog({ room, onPick, onClose }) {
  const ref = useRef(null);
  useEffect(() => { ref.current.showModal(); }, []);
  const close = () => ref.current.close();
  return html`
    <dialog ref=${ref} class="dialog" aria-labelledby="boost-title" onClose=${onClose}
      onClick=${(e) => e.target === ref.current && close()}>
      <div class="dialog-body">
        <h2 id="boost-title">Boost ${room.name}</h2>
        <p class="hint">Raises the target for a while, then goes back to normal by itself.</p>
        <div class="dialog-options">
          ${BOOST_OPTIONS.map(([minutes, label]) => html`
            <button class="btn" key=${minutes} onClick=${() => { close(); onPick(room, minutes); }}>${label}</button>
          `)}
        </div>
        <div class="dialog-foot">
          <button class="btn" data-kind="quiet" onClick=${close}>Cancel</button>
        </div>
      </div>
    </dialog>
  `;
}

function RoomCard({ room, system, actions }) {
  const heating = room.demand > 0;
  const target = room.target;
  const hasReading = room.temp != null;
  const shown = halfDeg(room.temp);

  return html`
    <article class="card" data-heating=${heating}>
      <div class="card-head">
        <h2>${room.name}</h2>
        <div class="pills">
          ${room.window_open && html`<span class="pill">Window open</span>`}
          ${heating && html`<span class="pill" data-kind="heat">Heating</span>`}
        </div>
      </div>

      <div>
        <p class="reading" data-missing=${!hasReading} aria-label=${hasReading ? `Currently ${deg(shown)}` : "No reading"}
          title=${hasReading ? `Measured ${deg(room.temp)}` : undefined}>${deg(shown)}</p>
        <div class="status-row">
          <p class="status">${statusLine(room)}</p>
          ${(room.boost || room.override) && html`
            <button class="icon-btn" disabled=${room.busy} onClick=${() => actions.boost(room, 0)}
              title=${room.boost ? "Cancel boost" : "Cancel hold"}
              aria-label=${room.boost ? `Cancel ${room.name} boost` : `Cancel ${room.name} hold`}>${closeIcon}</button>
          `}
        </div>
      </div>

      ${hasReading &&
      html`
        <div class="track" role="img" aria-label=${`Currently ${deg(shown)}${target != null ? `, target ${deg(target)}` : ""}`}>
          <div class="track-fill" style=${{ width: `${trackPct(shown)}%` }}></div>
          ${target != null && html`<div class="track-target" style=${{ left: `${trackPct(target)}%` }}></div>`}
        </div>
      `}

      <div class="stepper">
        <span class="caption">Target</span>
        <div class="stepper-mid">
          <button class="step" aria-label=${`Lower ${room.name} target by half a degree`}
            disabled=${target != null && target <= MIN_TEMP} onClick=${() => actions.step(room, -0.5)}>−</button>
          <span class="target" aria-live="polite">${deg(target)}</span>
          <button class="step" aria-label=${`Raise ${room.name} target by half a degree`}
            disabled=${target != null && target >= MAX_TEMP} onClick=${() => actions.step(room, 0.5)}>+</button>
        </div>
      </div>

      <div class="controls">
        <div class="segment" role="radiogroup" aria-label=${`${room.name} mode`}>
          <button role="radio" aria-checked=${room.mode === "auto"}
            onClick=${() => (room.mode !== "auto" || room.boost || room.override) && actions.mode(room, "auto")}>Schedule</button>
          <button role="radio" aria-checked=${room.mode === "manual"} onClick=${() => room.mode !== "manual" && actions.mode(room, "manual")}>Manual</button>
        </div>
        <button class="btn" data-kind=${room.boost ? "primary" : undefined} aria-pressed=${room.boost} disabled=${room.busy}
          title=${room.boost ? "Cancel boost" : undefined}
          onClick=${() => (room.boost ? actions.boost(room, 0) : actions.openBoost(room))}>Boost</button>
      </div>
    </article>
  `;
}

const arrowIcon = (up) => html`
  <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2.5"
    stroke-linecap="round" stroke-linejoin="round">
    <path d=${up ? "M6 15l6-6 6 6" : "M6 9l6 6 6-6"} />
  </svg>
`;

function ArrangeList({ rooms, onMove, onDone }) {
  return html`
    <section class="arrange" aria-labelledby="arrange-title">
      <div class="arrange-head">
        <div>
          <h2 id="arrange-title">Arrange rooms</h2>
          <p class="hint">Use the arrows to set the order. Every device shows the same order.</p>
        </div>
        <button class="btn" data-kind="primary" onClick=${onDone}>Done</button>
      </div>
      <ol class="arrange-list">
        ${rooms.map((room, i) => html`
          <li class="arrange-row" key=${room.id}>
            <span class="arrange-pos" aria-hidden="true">${i + 1}</span>
            <span class="arrange-name">${room.name}</span>
            <span class="arrange-temp">${deg(halfDeg(room.temp))}</span>
            <button class="step" id=${`move-up-${room.id}`} aria-label=${`Move ${room.name} up`}
              disabled=${i === 0} onClick=${() => onMove(room, -1)}>${arrowIcon(true)}</button>
            <button class="step" id=${`move-down-${room.id}`} aria-label=${`Move ${room.name} down`}
              disabled=${i === rooms.length - 1} onClick=${() => onMove(room, 1)}>${arrowIcon(false)}</button>
          </li>
        `)}
      </ol>
    </section>
  `;
}

function App() {
  const [data, setData] = useState(null);
  const [live, setLive] = useState(false);
  const [local, setLocal] = useState({}); // optimistic values by room id (and "sys")
  const [toast, setToast] = useState(null);
  const [boostRoomId, setBoostRoomId] = useState(null);
  const [, setMinute] = useState(0);
  const [arranging, setArranging] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const seqs = useRef({});
  const timers = useRef({});
  const toastTimer = useRef(null);

  const showToast = (message) => {
    setToast(message);
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 6000);
  };

  // Live updates: server-sent events, with a slow poll as a safety net. The server only reads the
  // hub while a page is connected, so a hidden tab (background tab, phone in a pocket, tablet with
  // its screen off) disconnects after a short while and reconnects when it's shown again.
  useEffect(() => {
    const visible = () => document.visibilityState === "visible";
    const load = () => fetch("/api/state").then((r) => r.json()).then(setData).catch(() => {});
    let source = null;
    let retry;
    let hideTimer;
    const connect = () => {
      source = new EventSource("/api/events");
      source.onmessage = (event) => { setData(JSON.parse(event.data)); setLive(true); };
      // The browser reconnects by itself after a dropped connection (e.g. a server restart), but
      // gives up for good if a reconnect gets an error page back, so then we start a fresh one.
      source.onerror = () => {
        setLive(false);
        if (source.readyState === EventSource.CLOSED) retry = setTimeout(() => visible() && connect(), 5000);
      };
    };
    const disconnect = () => {
      clearTimeout(retry);
      source?.close();
      source = null;
      setLive(false);
    };
    const onVisibility = () => {
      clearTimeout(hideTimer);
      if (visible()) {
        load();
        if (!source) connect();
      } else {
        // A grace period, so a quick look at another app doesn't drop and remake the connection.
        hideTimer = setTimeout(disconnect, 30000);
      }
    };
    load();
    if (visible()) connect();
    const poll = setInterval(() => visible() && load(), 60000);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      disconnect();
      clearTimeout(hideTimer);
      clearInterval(poll);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  // Re-render now and then so "until 22:00" moves on when the schedule does, even if no data changed.
  useEffect(() => {
    const tick = setInterval(() => setMinute((m) => m + 1), 30000);
    return () => clearInterval(tick);
  }, []);

  // Show the change immediately, send it in the background, and drop the
  // optimistic value once the server has answered (success or failure).
  const mutate = (key, fields) => {
    seqs.current[key] = (seqs.current[key] || 0) + 1;
    setLocal((l) => ({ ...l, [key]: { ...l[key], ...fields } }));
    return seqs.current[key];
  };
  const send = async (key, seq, path, body, method = "POST") => {
    try {
      setData(await request(method, path, body));
    } catch (err) {
      showToast(err.message);
    } finally {
      if (seqs.current[key] === seq) {
        setLocal((l) => { const next = { ...l }; delete next[key]; return next; });
      }
    }
  };

  const actions = {
    step(room, delta) {
      const base = room.target ?? room.scheduled ?? 18;
      const temp = clamp(Math.round((base + delta) * 2) / 2, MIN_TEMP, MAX_TEMP);
      const seq = mutate(room.id, { target: temp });
      clearTimeout(timers.current[room.id]);
      // Wait for taps to settle so several presses become one request.
      timers.current[room.id] = setTimeout(() => send(room.id, seq, `/api/rooms/${room.id}/target`, { temp }), 700);
    },
    mode(room, mode) {
      const seq = mutate(room.id, { mode });
      send(room.id, seq, `/api/rooms/${room.id}/mode`, { mode });
    },
    openBoost(room) {
      setBoostRoomId(room.id);
    },
    boost(room, minutes) {
      const seq = mutate(room.id, { busy: true });
      send(room.id, seq, `/api/rooms/${room.id}/boost`, { minutes });
    },
    away(enabled) {
      const seq = mutate("sys", { away: enabled });
      send("sys", seq, "/api/system/away", { enabled });
    },
    eco(enabled) {
      const seq = mutate("sys", { eco: enabled });
      send("sys", seq, "/api/system/eco", { enabled });
    },
    move(room, delta) {
      const ids = rooms.map((r) => r.id);
      const from = ids.indexOf(room.id);
      const to = from + delta;
      if (to < 0 || to >= ids.length) return;
      [ids[from], ids[to]] = [ids[to], ids[from]];
      const seq = mutate("order", { ids });
      setAnnouncement(`${room.name} moved to position ${to + 1} of ${ids.length}.`);
      // Moving a row in the page drops keyboard focus, so put it back on the arrow just used
      // (or the other one, if the room has reached the top or bottom).
      requestAnimationFrame(() => {
        const [same, other] = delta < 0 ? ["up", "down"] : ["down", "up"];
        const button = document.getElementById(`move-${same}-${room.id}`);
        (button && !button.disabled ? button : document.getElementById(`move-${other}-${room.id}`))?.focus();
      });
      clearTimeout(timers.current.order);
      timers.current.order = setTimeout(() => send("order", seq, "/api/rooms/order", { order: ids }, "PUT"), 600);
    },
  };

  if (!data) return html`<p class="boot">Reading your heating…</p>`;

  const system = { ...(data.system || {}), ...(local.sys || {}) };
  let rooms = data.rooms.map((r) => ({ ...r, ...(local[r.id] || {}) }));
  if (local.order) {
    const rank = new Map(local.order.ids.map((id, i) => [id, i]));
    rooms = [...rooms].sort((a, b) => (rank.get(a.id) ?? rank.size) - (rank.get(b.id) ?? rank.size));
  }
  const boostRoom = rooms.find((r) => r.id === boostRoomId);

  return html`
    <main>
      <header class="top">
        <div>
          <h1>Heating</h1>
          <p class="sub">
            <span class="dot" data-on=${!!system.heating}></span>
            ${data.system ? (system.heating ? "Boiler is firing" : "Boiler is idle") : "Waiting for the hub"}
          </p>
        </div>
        ${data.system && html`
          <div class="house">
            <${Switch} label="Away mode"
              hint=${system.away_temp != null ? `Sets every room back to ${deg(system.away_temp)}` : "Sets every room back while you're out"}
              checked=${!!system.away} onChange=${actions.away} />
            <${Switch} label="Eco mode" hint="Lowers heating to save energy"
              checked=${!!system.eco} onChange=${actions.eco} />
          </div>
        `}
      </header>

      ${data.mock && html`<p class="notice" data-kind="warn">Demo data. This isn't connected to your hub, so nothing here changes your heating.</p>`}
      ${data.error && html`<p class="notice" data-kind="warn">${data.error}${data.updated ? ` Showing readings from ${clock(data.updated)}.` : ""} Trying again soon.</p>`}
      ${system.away && html`<p class="notice" data-kind="away">Away mode is on. Rooms are set back${system.away_temp != null ? ` to ${deg(system.away_temp)}` : ""} until you turn it off.</p>`}

      ${!rooms.length
        ? html`<p class="notice">No rooms yet. Once the hub connects, your rooms appear here.</p>`
        : arranging
        ? html`<${ArrangeList} rooms=${rooms} onMove=${actions.move} onDone=${() => setArranging(false)} />`
        : html`<section class="rooms" aria-label="Rooms">
            ${rooms.map((room) => html`<${RoomCard} key=${room.id} room=${room} system=${system} actions=${actions} />`)}
          </section>`}

      <div class="foot">
        <p>
          ${data.updated ? `Hub last read at ${clock(data.updated)}.` : ""}
          ${live ? "" : " Reconnecting for live updates…"}
        </p>
        ${rooms.length > 1 && !arranging && html`
          <button class="btn" data-kind="quiet" onClick=${() => setArranging(true)}>Arrange rooms</button>
        `}
      </div>
      <p class="sr-only" aria-live="polite">${announcement}</p>
    </main>
    ${boostRoom && html`<${BoostDialog} room=${boostRoom} onPick=${actions.boost} onClose=${() => setBoostRoomId(null)} />`}
    ${toast &&html`<div class="toast" role="status">${toast}</div>`}
  `;
}

render(html`<${App} />`, document.getElementById("app"));
