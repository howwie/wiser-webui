import { html, render, useState, useEffect, useRef } from "./vendor/htm-preact.js";
import { nextChange } from "./schedule.mjs";

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
  if (next) return `On schedule until ${when(next)}, then ${next.temp == null ? "off" : deg(next.temp)}`;
  // A schedule that never changes temperature (e.g. 12.5° at 07:00 every day): like the Wiser
  // app, say when its next entry is, without a "then" that would repeat the same temperature.
  const entry = nextChange(room.schedule);
  if (entry) return `On schedule until ${when(entry)}`;
  return `Following the schedule (${deg(room.target)})`;
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

// Several rooms to one temperature for a while: something the Wiser app can't do, as its boost
// works one room at a time and adds a fixed amount to each room's own target.
function HoldDialog({ rooms, onApply, onClose }) {
  const ref = useRef(null);
  const [picked, setPicked] = useState([]);
  const [temp, setTemp] = useState(20);
  const [minutes, setMinutes] = useState(120);
  useEffect(() => { ref.current.showModal(); }, []);
  const close = () => ref.current.close();
  const toggle = (id, on) => setPicked((ids) => (on ? [...ids, id] : ids.filter((x) => x !== id)));
  const step = (delta) => setTemp((t) => clamp(t + delta, MIN_TEMP, MAX_TEMP));
  const count = picked.length;
  return html`
    <dialog ref=${ref} class="dialog" aria-labelledby="hold-title" onClose=${onClose}
      onClick=${(e) => e.target === ref.current && close()}>
      <div class="dialog-body">
        <h2 id="hold-title">Boost rooms</h2>
        <p class="hint">Heats the rooms you pick to one temperature, then each goes back to normal by itself.</p>
        <fieldset class="pick-rooms">
          <legend class="caption">Rooms</legend>
          ${rooms.map((room) => html`
            <label class="check pick-row" key=${room.id}>
              <input type="checkbox" checked=${picked.includes(room.id)} onChange=${(e) => toggle(room.id, e.currentTarget.checked)} />
              <span class="pick-name">${room.name}</span>
              <span class="pick-temp">${deg(halfDeg(room.temp))}</span>
            </label>
          `)}
        </fieldset>
        <div class="stepper">
          <span class="caption" id="hold-temp-label">Temperature</span>
          <div class="stepper-mid">
            <button class="step" aria-label="Lower by half a degree" disabled=${temp <= MIN_TEMP} onClick=${() => step(-0.5)}>−</button>
            <span class="target" aria-live="polite" aria-labelledby="hold-temp-label">${deg(temp)}</span>
            <button class="step" aria-label="Raise by half a degree" disabled=${temp >= MAX_TEMP} onClick=${() => step(0.5)}>+</button>
          </div>
        </div>
        <div class="dialog-options" role="group" aria-label="For how long">
          ${BOOST_OPTIONS.map(([value, label]) => html`
            <button class="btn" key=${value} aria-pressed=${minutes === value}
              data-kind=${minutes === value ? "primary" : undefined} onClick=${() => setMinutes(value)}>${label}</button>
          `)}
        </div>
        <div class="dialog-foot">
          <button class="btn" data-kind="quiet" onClick=${close}>Cancel</button>
          <button class="btn" data-kind="primary" disabled=${!count}
            onClick=${() => { close(); onApply(picked, temp, minutes); }}>
            ${count ? `Boost ${count} room${count > 1 ? "s" : ""}` : "Pick a room"}
          </button>
        </div>
      </div>
    </dialog>
  `;
}

// Signal as 0-4 bars, one per step of the hub's own rating (it's what the Wiser app shows).
// Only "Very good" and "Good" have been seen on a real hub; the weaker three are from memory.
// A device whose rating isn't one of these gets its bars from its radio reading instead.
const RATINGS = ["no signal", "poor", "medium", "good", "very good"]; // weakest first, index = bars
function signalBars(device) {
  const rated = RATINGS.indexOf(device.signal_label?.toLowerCase());
  if (rated >= 0) return rated;
  const s = device.signal;
  if (s == null) return null;
  return s >= 75 ? 4 : s >= 50 ? 3 : s >= 30 ? 2 : s > 0 ? 1 : 0;
}
const signalScore = signalBars; // what picks a room's weakest device
const signalText = (device) =>
  device.signal_label ?? (device.signal != null ? `${device.signal}%` : "Unknown");

// Received power in each direction, e.g. "Hub hears −71 · it hears −69 dBm". Closer to 0 is stronger.
// For a device connected through a repeater it isn't known whether the readings are of that link
// or of the hub, so they're shown without saying who hears whom.
const minus = (v) => String(v).replace("-", "−");
function signalPower(device) {
  const values = [device.hub_hears_dbm, device.device_hears_dbm];
  if (values.every((v) => v == null)) return "";
  if (device.parent && !device.parent.hub) return `Received ${values.filter((v) => v != null).map(minus).join(" · ")} dBm`;
  const parts = [];
  if (values[0] != null) parts.push(`Hub hears ${minus(values[0])}`);
  if (values[1] != null) parts.push(`${parts.length ? "it" : "It"} hears ${minus(values[1])}`);
  return `${parts.join(" · ")} dBm`;
}
// Battery, e.g. "Battery 80% · 2.9 V". Percentages are worked out as aioWiserHeatAPI does.
const lowBattery = (b) => !!b && ((b.percent != null && b.percent <= 20) || b.level?.toLowerCase() === "low");
function batteryText(b) {
  if (!b) return "";
  const parts = [b.percent != null ? `Battery ${b.percent}%` : `Battery ${b.level?.toLowerCase() ?? "unknown"}`];
  if (b.volts != null) parts.push(`${b.volts.toFixed(1)} V`);
  return parts.join(" · ");
}

const connection = (device) =>
  !device.parent
    ? device.repeater ? "A repeater: it links to whichever nearby devices it hears best, so it has no single parent" : ""
    : device.parent.hub ? "Connected to the hub" : `Connected through ${device.parent.name}`;

const barsIcon = (bars) => html`
  <svg class="bars" viewBox="0 0 20 16" width="20" height="16" aria-hidden="true">
    ${[0, 1, 2, 3].map((i) => html`
      <rect key=${i} x=${i * 5 + 1} y=${12 - i * 3.5} width="3" height=${4 + i * 3.5} rx="1"
        fill="currentColor" opacity=${i < bars ? 1 : 0.25} />
    `)}
  </svg>
`;

function DeviceItem({ device: d }) {
  return html`
    <li data-weak=${signalBars(d) != null && signalBars(d) <= 1}>
      <span class="signal-name">
        ${d.name}
        ${connection(d) && html`<span class="signal-power" data-via=${d.parent && !d.parent.hub}>${connection(d)}</span>`}
        ${signalPower(d) && html`<span class="signal-power">${signalPower(d)}</span>`}
        ${d.battery && html`<span class="signal-power" data-low=${lowBattery(d.battery)}>${batteryText(d.battery)}</span>`}
      </span>
      ${signalBars(d) != null && barsIcon(signalBars(d))}
      <span class="signal-text">${signalText(d)}</span>
    </li>
  `;
}

function SignalDialog({ room, onClose }) {
  const ref = useRef(null);
  useEffect(() => { ref.current.showModal(); }, []);
  const close = () => ref.current.close();
  return html`
    <dialog ref=${ref} class="dialog" aria-labelledby="signal-title" onClose=${onClose}
      onClick=${(e) => e.target === ref.current && close()}>
      <div class="dialog-body">
        <h2 id="signal-title">${room.title}</h2>
        <p class="hint">Each device's signal, battery, and what it's connected to: devices can reach the hub through another device. Closer to 0 dBm is stronger.</p>
        <ul class="signal-list">
          ${room.devices.map((d) => html`<${DeviceItem} key=${d.id} device=${d} />`)}
        </ul>
        <div class="dialog-foot">
          <button class="btn" data-kind="quiet" onClick=${close}>Close</button>
        </div>
      </div>
    </dialog>
  `;
}

// A room's valves and thermostat, and the smart plugs in it.
const roomDevices = (room) => [...(room.devices || []), ...(room.plugs || []).map((p) => p.device).filter(Boolean)];

function RoomCard({ room, system, actions }) {
  const heating = room.demand > 0;
  const target = room.target;
  const hasReading = room.temp != null;
  const shown = halfDeg(room.temp);
  // The weakest device sets the room's icon: one bad valve is what you'd want to know about.
  const devices = roomDevices(room);
  const rated = devices.filter((d) => signalScore(d) != null);
  const weakest = rated.length ? rated.reduce((a, b) => (signalScore(b) < signalScore(a) ? b : a)) : null;
  const lowCount = devices.filter((d) => lowBattery(d.battery)).length;

  return html`
    <article class="card" data-heating=${heating}>
      <div class="card-head">
        <h2>${room.name}</h2>
        <div class="pills">
          ${room.window_open && html`<span class="pill">Window open</span>`}
          ${heating && html`<span class="pill" data-kind="heat">Heating</span>`}
          ${lowCount > 0 && html`
            <button class="pill" data-kind="warn" onClick=${() => actions.openSignal(room)}
              aria-label=${`${room.name}: ${lowCount > 1 ? `${lowCount} devices have` : "a device has"} a low battery. Show devices`}>
              Low battery
            </button>
          `}
          ${weakest && html`
            <button class="icon-btn signal-btn" data-weak=${signalBars(weakest) <= 1}
              onClick=${() => actions.openSignal(room)}
              title=${`Signal: ${signalText(weakest)}${rated.length > 1 ? ` (weakest of ${devices.length} devices)` : ""}`}
              aria-label=${`${room.name} signal: ${signalText(weakest)}${rated.length > 1 ? `, weakest of ${devices.length} devices` : ""}. Show devices`}>
              ${barsIcon(signalBars(weakest))}
            </button>
          `}
        </div>
      </div>

      <div>
        <p class="reading" data-missing=${!hasReading} aria-label=${hasReading ? `Currently ${deg(shown)}` : "No reading"}
          title=${hasReading ? `Measured ${deg(room.temp)}` : undefined}>${deg(shown)}</p>
        <div class="status-row">
          <p class="status" title=${statusLine(room)}>${statusLine(room)}</p>
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

      ${room.plugs?.length > 0 && html`
        <ul class="plug-rows" aria-label=${`${room.name} smart plugs`}>
          ${room.plugs.map((plug) => html`<${PlugRow} key=${plug.id} plug=${plug} actions=${actions} />`)}
        </ul>
      `}
    </article>
  `;
}

// The hub reports a plug's energy as a running total; it's taken to be watt-hours (unconfirmed).
const kwh = (wh) => `${(wh / 1000).toLocaleString("en-GB", { maximumFractionDigits: 2 })} kWh`;

function plugStatus(plug) {
  if (plug.switching_to != null) return `Switching ${plug.switching_to ? "on" : "off"}…`;
  const state = plug.on ? "on" : "off";
  if (plug.mode === "manual") return `Manual: stays ${state} until you switch it`;
  if (plug.scheduled_on != null && plug.scheduled_on !== plug.on) return `Switched ${state} until the schedule next changes`;
  return `Following its schedule (${state})`;
}

const plugIcon = html`
  <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2"
    stroke-linecap="round" stroke-linejoin="round">
    <path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4" />
  </svg>
`;

// While a plug is switching, its switch shows where it's going rather than flicking back.
const PlugSwitch = ({ plug, actions }) => {
  const target = plug.switching_to ?? plug.on;
  return html`
    <button class="switch" role="switch" aria-checked=${target} aria-label=${`${plug.name} power`}
      data-pending=${plug.switching_to != null} onClick=${() => actions.plugPower(plug, !target)}></button>
  `;
};

// A smart plug in its room's card: one line, with the rest of its settings in a dialog.
function PlugRow({ plug, actions }) {
  // Shorter than plugStatus, to fit one line beside the switch; the dialog has the full wording.
  const how = plug.switching_to != null ? `Switching ${plug.switching_to ? "on" : "off"}…`
    : plug.mode === "manual" ? "Manual"
    : plug.scheduled_on != null && plug.scheduled_on !== plug.on ? "Until the schedule changes"
    : "On schedule";
  const status = [plug.on && plug.power_w ? `${plug.power_w} W` : "", how].filter(Boolean).join(" · ");
  return html`
    <li class="plug-row" data-on=${plug.on}>
      <button class="plug-open" onClick=${() => actions.openPlug(plug)} title=${status}
        aria-label=${`${plug.name}: ${status}. Show plug settings`}>
        ${plugIcon}
        <span class="plug-text">
          <span class="plug-name">${plug.name}</span>
          <span class="status">${status}</span>
        </span>
      </button>
      <${PlugSwitch} plug=${plug} actions=${actions} />
    </li>
  `;
}

// A plug's state and settings: in its own card for a plug in no room, else in the plug dialog.
function PlugControls({ plug, actions }) {
  return html`
    <div class="plug-main">
      <div>
        <p class="plug-state">${plug.on ? "On" : "Off"}</p>
        <p class="status" title=${plugStatus(plug)}>${plugStatus(plug)}</p>
      </div>
      <${PlugSwitch} plug=${plug} actions=${actions} />
    </div>

    ${(plug.power_w != null || plug.energy != null) && html`
      <p class="plug-meter">
        ${plug.power_w != null && html`<span><strong>${plug.power_w} W</strong> now</span>`}
        ${plug.energy != null && html`<span><strong>${kwh(plug.energy)}</strong> used</span>`}
      </p>
    `}

    <div class="controls">
      <div class="segment" role="radiogroup" aria-label=${`${plug.name} mode`}>
        <button role="radio" aria-checked=${plug.mode === "auto"} onClick=${() => plug.mode !== "auto" && actions.plugMode(plug, "auto")}>Schedule</button>
        <button role="radio" aria-checked=${plug.mode === "manual"} onClick=${() => plug.mode !== "manual" && actions.plugMode(plug, "manual")}>Manual</button>
      </div>
      <label class="check">
        <input type="checkbox" checked=${plug.away_off} onChange=${(e) => actions.plugAway(plug, e.currentTarget.checked)} />
        Off in away mode
      </label>
    </div>
  `;
}

function PlugDialog({ plug, actions, onClose }) {
  const ref = useRef(null);
  useEffect(() => { ref.current.showModal(); }, []);
  const close = () => ref.current.close();
  return html`
    <dialog ref=${ref} class="dialog plug" aria-labelledby="plug-title" onClose=${onClose}
      onClick=${(e) => e.target === ref.current && close()}>
      <div class="dialog-body">
        <div>
          <h2 id="plug-title">${plug.name}</h2>
          ${plug.room && html`<p class="plug-room">Smart plug in ${plug.room}</p>`}
        </div>
        <${PlugControls} plug=${plug} actions=${actions} />
        ${plug.device && html`<ul class="signal-list"><${DeviceItem} device=${plug.device} /></ul>`}
        <div class="dialog-foot">
          <button class="btn" data-kind="quiet" onClick=${close}>Close</button>
        </div>
      </div>
    </dialog>
  `;
}

function PlugCard({ plug, actions }) {
  const bars = plug.device ? signalBars(plug.device) : null;
  return html`
    <article class="card plug" data-on=${plug.on}>
      <div class="card-head">
        <div>
          <h2>${plug.name}</h2>
          ${plug.room && html`<p class="plug-room">${plug.room}</p>`}
        </div>
        <div class="pills">
          ${plug.on && html`<span class="pill" data-kind="on">On</span>`}
          ${bars != null && html`
            <button class="icon-btn signal-btn" data-weak=${bars <= 1} onClick=${() => actions.openSignal({ plug })}
              title=${`Signal: ${signalText(plug.device)}`}
              aria-label=${`${plug.name} signal: ${signalText(plug.device)}. Show details`}>${barsIcon(bars)}</button>
          `}
        </div>
      </div>

      <${PlugControls} plug=${plug} actions=${actions} />
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

// Whether this device stays live while nobody touches it (see App). Saved per device; opening the
// page as /?keep-live turns it on, for kiosk apps that make the setting awkward to reach.
const KEEP_LIVE_KEY = "wiser.keepLive";
function initialKeepLive() {
  try {
    if (new URLSearchParams(location.search).has("keep-live")) localStorage.setItem(KEEP_LIVE_KEY, "1");
    return localStorage.getItem(KEEP_LIVE_KEY) === "1";
  } catch {
    return new URLSearchParams(location.search).has("keep-live");
  }
}

function App() {
  const [data, setData] = useState(null);
  const [live, setLive] = useState(false);
  const [paused, setPaused] = useState(false);
  const [local, setLocal] = useState({}); // optimistic values by room id (and "sys")
  const [toast, setToast] = useState(null);
  const [boostRoomId, setBoostRoomId] = useState(null);
  const [holding, setHolding] = useState(false); // the "Boost rooms" dialog is open
  const [signalRoomId, setSignalRoomId] = useState(null);
  const [plugDialogId, setPlugDialogId] = useState(null);
  const [, setMinute] = useState(0);
  const [arranging, setArranging] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const seqs = useRef({});
  const timers = useRef({});
  const toastTimer = useRef(null);
  const [keepLive, setKeepLive] = useState(initialKeepLive);
  const keepLiveRef = useRef(keepLive);
  const wakeRef = useRef(null);

  const changeKeepLive = (on) => {
    setKeepLive(on);
    keepLiveRef.current = on;
    try {
      on ? localStorage.setItem(KEEP_LIVE_KEY, "1") : localStorage.removeItem(KEEP_LIVE_KEY);
    } catch {}
    wakeRef.current?.();
  };

  const showToast = (message) => {
    setToast(message);
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 6000);
  };

  // Live updates: server-sent events, with a slow poll as a safety net. The server only reads the
  // hub while a page is connected, so the page lets go when nobody is looking:
  // - a hidden tab (background tab, minimised window, phone in a pocket) after 30 seconds;
  // - a page nobody has touched for IDLE_MS. Browsers count a tab as visible whenever it's the
  //   front tab of a window that isn't minimised, even behind other windows or on a locked or
  //   switched-off screen, so visibility alone would keep a forgotten desktop tab live all night.
  //   "Keep live on this device" turns this off for wall tablets and other always-on displays.
  // Moving the mouse over the page, a tap, a key or showing the tab again reconnects and refreshes.
  useEffect(() => {
    const IDLE_MS = 15 * 60000;
    const visible = () => document.visibilityState === "visible";
    const load = () => fetch("/api/state").then((r) => r.json()).then(setData).catch(() => {});
    let source = null;
    let retry;
    let hideTimer;
    let lastActive = Date.now();
    const idle = () => !keepLiveRef.current && Date.now() - lastActive >= IDLE_MS;
    const connect = () => {
      setPaused(false);
      source = new EventSource("/api/events");
      source.onmessage = (event) => { setData(JSON.parse(event.data)); setLive(true); };
      // The browser reconnects by itself after a dropped connection (e.g. a server restart), but
      // gives up for good if a reconnect gets an error page back, so then we start a fresh one.
      source.onerror = () => {
        setLive(false);
        if (source.readyState === EventSource.CLOSED) {
          retry = setTimeout(() => {
            source = null; // lets a later wake() reconnect if this one doesn't
            if (visible() && !idle()) connect();
          }, 5000);
        }
      };
    };
    const disconnect = () => {
      clearTimeout(retry);
      source?.close();
      source = null;
      setLive(false);
      setPaused(true);
    };
    const wake = () => {
      lastActive = Date.now();
      if (visible() && !source) {
        load();
        connect();
      }
    };
    wakeRef.current = wake;
    const onVisibility = () => {
      clearTimeout(hideTimer);
      if (visible()) wake();
      // A grace period, so a quick look at another app doesn't drop and remake the connection.
      else hideTimer = setTimeout(disconnect, 30000);
    };
    load();
    if (visible()) connect();
    const poll = setInterval(() => {
      if (idle()) source && disconnect();
      else if (visible()) load();
    }, 60000);
    const activity = ["pointermove", "pointerdown", "keydown", "wheel", "focus"];
    activity.forEach((type) => window.addEventListener(type, wake, { passive: true }));
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      disconnect();
      clearTimeout(hideTimer);
      clearInterval(poll);
      activity.forEach((type) => window.removeEventListener(type, wake));
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
    openPlug(plug) {
      setPlugDialogId(plug.id);
    },
    openSignal(target) {
      // A room, or { plug } for a smart plug's own details.
      setSignalRoomId(target.plug ? `plug-${target.plug.id}` : `room-${target.id}`);
    },
    boost(room, minutes) {
      const seq = mutate(room.id, { busy: true });
      send(room.id, seq, `/api/rooms/${room.id}/boost`, { minutes });
    },
    hold(ids, temp, minutes) {
      const until = Date.now() / 1000 + minutes * 60;
      const pending = ids.map((id) => [id, mutate(id, { target: temp, override: true, boost: false, until, busy: true })]);
      request("POST", "/api/rooms/hold", { rooms: ids, temp, minutes })
        .then(setData)
        .catch((err) => showToast(err.message))
        .finally(() => setLocal((l) => {
          const next = { ...l };
          for (const [id, seq] of pending) if (seqs.current[id] === seq) delete next[id];
          return next;
        }));
    },
    plugPower(plug, on) {
      // Shown as switching until the server answers, which it does once the plug has switched.
      const key = `plug-${plug.id}`;
      send(key, mutate(key, { switching_to: on === plug.on ? null : on }), `/api/plugs/${plug.id}/power`, { enabled: on });
    },
    plugMode(plug, mode) {
      const key = `plug-${plug.id}`;
      send(key, mutate(key, { mode }), `/api/plugs/${plug.id}/mode`, { mode });
    },
    plugAway(plug, off) {
      const key = `plug-${plug.id}`;
      send(key, mutate(key, { away_off: off }), `/api/plugs/${plug.id}/away`, { enabled: off });
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
  const plugs = (data.plugs || []).map((p) => ({ ...p, ...(local[`plug-${p.id}`] || {}) }));
  // A plug in a room is shown in that room's card; only plugs in no room get cards of their own.
  rooms = rooms.map((r) => ({ ...r, plugs: plugs.filter((p) => p.room_id === r.id) }));
  const loosePlugs = plugs.filter((p) => !rooms.some((r) => r.id === p.room_id));
  const dialogPlug = plugs.find((p) => p.id === plugDialogId);
  const boostRoom = rooms.find((r) => r.id === boostRoomId);
  // What the device list shows: a room's devices, or a smart plug on its own.
  const signalRoomMatch = rooms.find((r) => `room-${r.id}` === signalRoomId);
  const signalPlugMatch = plugs.find((p) => `plug-${p.id}` === signalRoomId && p.device);
  const signalRoom = signalRoomMatch
    ? { title: `${signalRoomMatch.name} devices`, devices: roomDevices(signalRoomMatch) }
    : signalPlugMatch && { title: signalPlugMatch.name, devices: [signalPlugMatch.device] };

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
        : html`${rooms.length > 1 && html`
            <div class="toolbar">
              <button class="btn" onClick=${() => setHolding(true)}>Boost rooms…</button>
            </div>
          `}
          <section class="rooms" aria-label="Rooms">
            ${rooms.map((room) => html`<${RoomCard} key=${room.id} room=${room} system=${system} actions=${actions} />`)}
          </section>`}

      ${loosePlugs.length > 0 && !arranging && html`
        <section aria-labelledby="plugs-title">
          <h2 class="section-title" id="plugs-title">Smart plugs</h2>
          <div class="rooms">
            ${loosePlugs.map((plug) => html`<${PlugCard} key=${plug.id} plug=${plug} actions=${actions} />`)}
          </div>
        </section>
      `}

      <div class="foot">
        <p>
          ${data.updated ? `Hub last read at ${clock(data.updated)}.` : ""}
          ${live ? "" : paused ? " Live updates paused while the page is idle. Move the mouse or tap to resume." : " Reconnecting for live updates…"}
        </p>
        ${!arranging && html`
          <div class="foot-actions">
            <label class="check" title="For a wall tablet or other always-on screen: stay live even when nobody touches it. Turning the screen off still pauses it.">
              <input type="checkbox" checked=${keepLive} onChange=${(e) => changeKeepLive(e.currentTarget.checked)} />
              Keep live on this device
            </label>
            ${rooms.length > 1 && html`
              <button class="btn" data-kind="quiet" onClick=${() => setArranging(true)}>Arrange rooms</button>
            `}
          </div>
        `}
      </div>
      <p class="sr-only" aria-live="polite">${announcement}</p>
    </main>
    ${boostRoom && html`<${BoostDialog} room=${boostRoom} onPick=${actions.boost} onClose=${() => setBoostRoomId(null)} />`}
    ${signalRoom && html`<${SignalDialog} room=${signalRoom} onClose=${() => setSignalRoomId(null)} />`}
    ${dialogPlug && html`<${PlugDialog} plug=${dialogPlug} actions=${actions} onClose=${() => setPlugDialogId(null)} />`}
    ${holding && html`<${HoldDialog} rooms=${rooms} onApply=${actions.hold} onClose=${() => setHolding(false)} />`}
    ${toast &&html`<div class="toast" role="status">${toast}</div>`}
  `;
}

render(html`<${App} />`, document.getElementById("app"));
