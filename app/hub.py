"""Wiser Heat Hub client, a mock hub for demos, and normalisation of hub data.

The hub reports temperatures in tenths of a degree Celsius (205 = 20.5 C),
uses -200 for "off" and -32768 when a sensor has been lost.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any

import httpx


class HubError(Exception):
    """Something went wrong talking to the hub. The message is safe to show to the user."""


# --------------------------------------------------------------------------
# Real hub
# --------------------------------------------------------------------------
class WiserHub:
    # Newer hubs take writes under /data/v2/domain; older ones under /data/domain.
    # We try v2 first and remember whichever one the hub accepts.
    PREFIXES = ("/data/v2/domain", "/data/domain")

    def __init__(self, host: str, secret: str, timeout: float = 15.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=f"http://{host}",
            headers={"SECRET": secret},
            timeout=timeout,
        )
        self._prefix: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def read(self) -> dict[str, Any]:
        """Fetch the whole system state. (The trailing slash matters on this hub.)"""
        response = None
        for attempt in (1, 2):
            try:
                response = await self._client.get("/data/domain/")
                break
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise HubError(f"Can't reach the hub ({type(exc).__name__}).") from exc
                await asyncio.sleep(1)
        assert response is not None
        self._check(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise HubError("The hub sent a reply that isn't valid JSON.") from exc
        if not isinstance(data, dict):
            raise HubError("The hub sent an unexpected reply.")
        data[SCHEDULES_KEY] = await self._read_schedules()
        return data

    async def _read_schedules(self) -> dict[str, Any]:
        """Schedules live in their own endpoint, grouped by type: {"Heating": [...], "OnOff": [...]}.

        Only used for the "until 22:00" text, so any failure just means no schedules (the page
        then says "Following the schedule") rather than a failed read.
        """
        try:
            response = await self._client.get("/data/v2/schedules/")
            data = response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            data = None
        return data if isinstance(data, dict) else {}

    async def patch(self, resource: str, body: dict[str, Any]) -> None:
        """PATCH a resource such as 'Room/3' or 'System'. Not retried: a repeated write could double-apply."""
        prefixes = [self._prefix] if self._prefix else list(self.PREFIXES)
        last: httpx.Response | None = None
        for prefix in prefixes:
            try:
                response = await self._client.patch(f"{prefix}/{resource}", json=body)
            except httpx.HTTPError as exc:
                raise HubError(f"Can't reach the hub ({type(exc).__name__}).") from exc
            if response.status_code == 404 and self._prefix is None:
                last = response
                continue
            self._check(response)
            self._prefix = prefix
            return
        assert last is not None
        self._check(last)

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise HubError("The hub rejected the secret. Check WISER_SECRET.")
        if response.status_code >= 400:
            snippet = response.text.strip().replace("\n", " ")[:120]
            raise HubError(f"The hub refused the request ({response.status_code}) {snippet}".strip())


# --------------------------------------------------------------------------
# Normalising hub data into what the UI needs
# --------------------------------------------------------------------------
def _deg(tenths: Any) -> float | None:
    """Tenths of a degree -> degrees C; None for 'off' (-200) or a lost sensor (-32768)."""
    if not isinstance(tenths, (int, float)) or tenths <= -200:
        return None
    return round(tenths / 10, 1)


DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# WiserHub.read() puts the reply of /data/v2/schedules/ under this key of the domain data.
SCHEDULES_KEY = "_schedules"


def _day_points(entry: dict[str, Any]) -> list[tuple[Any, Any]]:
    """A day's (HHMM, tenths) pairs. The schedules endpoint gives parallel lists,
    {"Time": [630, 900], "DegreesC": [210, 170]} (as aioWiserHeatAPI reads them); a
    "SetPoints" list of {"Time", "DegreesC"} objects is also accepted, for older hubs."""
    if isinstance(entry.get("Time"), list):
        return list(zip(entry["Time"], entry.get("DegreesC") or []))
    return [(p.get("Time"), p.get("DegreesC")) for p in entry.get("SetPoints") or [] if isinstance(p, dict)]


def _schedule_days(schedule: dict[str, Any]) -> list[list[list[Any]]] | None:
    """A hub heating schedule -> 7 lists (Monday first) of [minutes after midnight, degrees C or None].

    The hub stores times as HHMM numbers (630 = 06:30) and setpoints in tenths, with null or
    -200 for off. Days are normally keyed by name; Weekdays/Weekends/All also occur.
    """
    days = []
    for i, day in enumerate(DAYS):
        entry = schedule.get(day) or schedule.get("Weekdays" if i < 5 else "Weekends") or schedule.get("All") or {}
        points = []
        for hhmm, tenths in _day_points(entry):
            # Times past 23:59 (e.g. 3000 = sunrise in lighting schedules) aren't clock times.
            if isinstance(hhmm, int) and 0 <= hhmm <= 2359 and hhmm % 100 < 60:
                points.append([hhmm // 100 * 60 + hhmm % 100, _deg(tenths)])
        days.append(sorted(points, key=lambda p: p[0]))
    return days if any(days) else None


def _heating_schedules(domain: dict[str, Any]) -> dict[Any, dict[str, Any]]:
    """Heating schedules by id, from the schedules endpoint ({"Heating": [...]}) or, failing
    that, a "Schedule" list in the domain data."""
    grouped = domain.get(SCHEDULES_KEY) or {}
    found = [s for s in grouped.get("Heating") or [] if isinstance(s, dict)]
    found += [
        s for s in domain.get("Schedule") or [] if isinstance(s, dict) and s.get("Type", "Heating") == "Heating"
    ]
    schedules: dict[Any, dict[str, Any]] = {}
    for s in found:
        schedules.setdefault(s.get("id"), s)
    return schedules


DEVICE_NAMES = {
    "iTRV": "Radiator valve",
    "RoomStat": "Room thermostat",
    "HeatingActuator": "Heating actuator",
    "UnderFloorHeating": "Underfloor heating",
}


def _rssi(reception: Any) -> int | None:
    """A {"Rssi": -71, "Lqi": 120} reading -> the RSSI in dBm (0 or missing means no reading)."""
    rssi = reception.get("Rssi") if isinstance(reception, dict) else None
    return int(rssi) if isinstance(rssi, (int, float)) and rssi != 0 else None


def _signal_percent(rssi: int | None) -> int | None:
    """RSSI in dBm -> 0-100, as aioWiserHeatAPI works it out: 2 x (RSSI + 100), at most 100."""
    return None if rssi is None else max(0, min(100, int(2 * (rssi + 100))))


def _signal_label(text: Any) -> str | None:
    """The hub's own rating, e.g. "VeryGood" -> "Very good"."""
    if not isinstance(text, str) or not text or text == "Unknown":
        return None
    words = "".join(f" {c}" if c.isupper() else c for c in text).split()
    return " ".join(words).capitalize()


# Battery, as aioWiserHeatAPI 1.7.7 works it out (helpers/battery.py and const.py).
ROOMSTAT_BATTERY_VOLTS = (1.7, 2.7)  # empty, full
TRV_BATTERY_PERCENT = {3.0: 100, 2.9: 80, 2.8: 60, 2.7: 40, 2.6: 20, 2.5: 10, 2.4: 5, 2.3: 0}
BATTERY_LEVEL_PERCENT = {"normal": 100, "twothirds": 66, "onethird": 33, "low": 10}


def _battery(d: dict[str, Any]) -> dict[str, Any] | None:
    """{"percent", "level", "volts"} for a battery device; None for one with no battery data
    (mains powered, like a smart plug or heating actuator)."""
    raw_volts, level = d.get("BatteryVoltage"), d.get("BatteryLevel")
    volts = round(raw_volts / 10, 1) if isinstance(raw_volts, (int, float)) and raw_volts > 0 else None
    level = level if isinstance(level, str) and level and level != "Unknown" else None
    if volts is None and level is None:
        return None
    product = d.get("ProductType")
    if volts is not None and product == "RoomStat":
        empty, full = ROOMSTAT_BATTERY_VOLTS
        percent = max(0, min(100, round((volts - empty) / (full - empty) * 100)))
    elif volts is not None and product == "iTRV":
        percent = 100 if volts >= 3.0 else TRV_BATTERY_PERCENT.get(volts, 0)
    else:
        percent = BATTERY_LEVEL_PERCENT.get((level or "").lower())
    return {"percent": percent, "level": _signal_label(level), "volts": volts}


def _device_info(device_id: Any, d: dict[str, Any], name: str) -> dict[str, Any]:
    """A device's name, signal and battery, from its entry in the domain's Device list."""
    # Each direction has its own reading: the hub hearing the device (ReceptionOfDevice) and the
    # device hearing the hub (ReceptionOfController). The weaker one is the one that matters.
    hub_hears = _rssi(d.get("ReceptionOfDevice"))
    device_hears = _rssi(d.get("ReceptionOfController"))
    readings = [r for r in (hub_hears, device_hears) if r is not None]
    return {
        "id": device_id,
        "name": name,
        "signal": _signal_percent(min(readings)) if readings else None,
        "signal_label": _signal_label(d.get("DisplayedSignalStrength")),
        "hub_hears_dbm": hub_hears,
        "device_hears_dbm": device_hears,
        "battery": _battery(d),
    }


def _room_devices(r: dict[str, Any], devices: dict[Any, dict[str, Any]]) -> list[dict[str, Any]]:
    """The room's valves and thermostat, with each one's signal. Rooms list their devices' ids
    (as aioWiserHeatAPI matches them); the readings are in the domain's Device list."""
    ids = [r.get("RoomStatId"), *(r.get("SmartValveIds") or []), *(r.get("HeatingActuatorIds") or []), r.get("UnderFloorHeatingId")]
    found = []
    for device_id in dict.fromkeys(i for i in ids if i is not None):
        d = devices.get(device_id)
        if not d:
            continue
        product = d.get("ProductType") or ""
        found.append(_device_info(device_id, d, DEVICE_NAMES.get(product, product or "Device")))
    # Number same-kind devices (Radiator valve 1, Radiator valve 2) so they can be told apart.
    for name in {d["name"] for d in found}:
        same = [d for d in found if d["name"] == name]
        if len(same) > 1:
            for n, d in enumerate(same, start=1):
                d["name"] = f"{name} {n}"
    return found


def _link_parents(
    rooms: list[dict[str, Any]], plugs: list[dict[str, Any]], devices: dict[Any, dict[str, Any]], domain: dict[str, Any]
) -> None:
    """Give each room device and smart plug the device it's connected to. Zigbee is a mesh: a valve can reach
    the hub through a repeater such as a smart plug. Each Device has its own NodeId and the
    ParentNodeId of the device it's connected to, as aioWiserHeatAPI reads them."""
    names: dict[Any, str] = {}
    for room in rooms:
        for d in room["devices"]:
            names[d["id"]] = f"{d['name']} in {room['name']}"  # e.g. "Radiator valve 2 in Living room"
    for plug in domain.get("SmartPlug") or []:
        if isinstance(plug, dict):
            name = (plug.get("Name") or "").strip()
            # "Landing" -> "Landing smart plug", but "Master bedroom plug" stays as it is.
            label = name if "plug" in name.lower() else f"{name} smart plug" if name else "Smart plug"
            names.setdefault(plug.get("DeviceId", plug.get("id")), label)
    by_node = {d.get("NodeId"): device_id for device_id, d in devices.items() if d.get("NodeId") is not None}
    for d in [d for room in rooms for d in room["devices"]] + [p["device"] for p in plugs]:
        node = (devices.get(d["id"]) or {}).get("ParentNodeId")
        parent_id = by_node.get(node)
        parent = devices.get(parent_id) or {}
        if node is None:
            d["parent"] = None
        elif parent.get("ProductType") == "Controller" or (parent_id is None and node == 0):
            d["parent"] = {"name": "the hub", "hub": True}
        elif parent_id is not None:
            product = parent.get("ProductType") or "Device"
            d["parent"] = {"name": names.get(parent_id) or DEVICE_NAMES.get(product, product), "hub": False}
        else:
            d["parent"] = {"name": f"Zigbee node {node}", "hub": False}  # not in the Device list


def _plugs(domain: dict[str, Any], devices: dict[Any, dict[str, Any]], room_names: dict[Any, str]) -> list[dict[str, Any]]:
    """Smart plugs, with the fields aioWiserHeatAPI reads from each SmartPlug entry."""
    plugs = []
    for p in domain.get("SmartPlug") or []:
        if not isinstance(p, dict) or p.get("id") is None:
            continue
        device_id = p.get("DeviceId", p["id"])
        power, energy = p.get("InstantaneousDemand"), p.get("CurrentSummationDelivered")
        output, target = p.get("OutputState"), p.get("TargetState")
        plugs.append(
            {
                "id": p["id"],
                "name": (p.get("Name") or "").strip() or f"Smart plug {p['id']}",
                "room": room_names.get(p.get("RoomId")),
                "on": output == "On",
                # TargetState leads OutputState while the plug is still switching.
                "switching_to": (target == "On") if target in ("On", "Off") and target != output else None,
                "mode": "manual" if p.get("Mode") == "Manual" else "auto",
                "scheduled_on": {"On": True, "Off": False}.get(p.get("ScheduledState")),
                "power_w": power if isinstance(power, (int, float)) else None,
                "energy": energy if isinstance(energy, (int, float)) else None,
                "away_off": p.get("AwayAction") == "Off",
                # Signal and connection, the same as a room's devices (filled in by _link_parents).
                # A plug is a Zigbee repeater (router): hubs report no single parent for it.
                "device": {**_device_info(device_id, devices.get(device_id) or {}, "Smart plug"), "repeater": True},
            }
        )
    return plugs


def normalise(domain: dict[str, Any]) -> dict[str, Any]:
    system = domain.get("System") or {}
    schedules = _heating_schedules(domain)
    devices = {d.get("id"): d for d in domain.get("Device") or [] if isinstance(d, dict)}
    rooms = []
    for r in domain.get("Room") or []:
        # Both spellings occur; aioWiserHeatAPI checks both too, and counts any origin
        # mentioning Boost (e.g. FromBoost) as a boost.
        origin = r.get("SetpointOrigin") or r.get("SetPointOrigin") or ""
        mode = str(r.get("Mode") or "Auto").lower()
        boost = "Boost" in origin
        override = (not boost) and (r.get("OverrideType") or "None") not in ("None", "")
        until = r.get("OverrideTimeoutUnixTime") or None
        schedule = schedules.get(r.get("ScheduleId"))
        rooms.append(
            {
                "id": r["id"],
                "name": (r.get("Name") or "").strip() or f"Room {r['id']}",
                "temp": _deg(r.get("CalculatedTemperature")),
                "target": _deg(r.get("DisplayedSetPoint", r.get("CurrentSetPoint"))),
                "scheduled": _deg(r.get("ScheduledSetPoint")),
                "mode": mode if mode in ("auto", "manual") else "auto",
                "boost": boost,
                "override": override,
                "until": until if (boost or override) else None,
                "demand": int(r.get("PercentageDemand") or 0),
                "window_open": r.get("WindowState") == "Open",
                "origin": origin,
                # Times are the hub's local time, so the browser (in the same home) works out
                # "until when" itself rather than this server, whose clock may be on UTC.
                "schedule": _schedule_days(schedule) if schedule else None,
                "devices": _room_devices(r, devices),
            }
        )
    plugs = _plugs(domain, devices, {r["id"]: r["name"] for r in rooms})
    _link_parents(rooms, plugs, devices, domain)
    channels = domain.get("HeatingChannel") or []
    return {
        "rooms": rooms,
        "plugs": plugs,
        "system": {
            "away": system.get("OverrideType") == "Away",
            "away_temp": _deg(system.get("AwayModeSetPointLimit")),
            "eco": bool(system.get("EcoModeEnabled")),
            "heating": any(c.get("HeatingRelayState") == "On" for c in channels),
        },
    }


# --------------------------------------------------------------------------
# Mock hub: same data shape and same write payloads as the real thing
# --------------------------------------------------------------------------
class MockHub:
    """In-memory stand-in so the UI can be tried without a hub. Temperatures drift realistically."""

    # Schedule id -> (weekday setpoints, weekend setpoints), as (HHMM, tenths) like the hub stores them.
    SCHEDULES = {
        1: ([(630, 210), (900, 170), (1700, 210), (2200, 170)], [(800, 210), (2230, 170)]),
        2: ([(700, 160), (2100, 190)], [(900, 160), (2130, 190)]),  # bedroom: warm at night
        3: ([(700, 200)], [(700, 200)]),  # study: the same temperature all the time
    }

    # Room id -> its devices as (device id, ProductType, RSSI, the hub's rating, parent device id).
    # The bathroom valve is a weak one, as you'd see with a valve behind a radiator at the far end
    # of the house; it and the bedroom valve reach the hub through the landing smart plug.
    DEVICES = {
        1: [(10, "RoomStat", -58, "VeryGood", 0), (11, "iTRV", -64, "Good", 0), (12, "iTRV", -71, "Good", 0)],
        2: [(13, "iTRV", -66, "Good", 0)],
        3: [(14, "iTRV", -77, "Medium", 20)],
        4: [(15, "iTRV", -61, "VeryGood", 0)],
        5: [(16, "iTRV", -88, "Poor", 20)],
    }
    HUB_ID, PLUG_ID = 0, 20  # device ids; each device's Zigbee NodeId is its id + 1000 here
    PLUG_SWITCH_SECONDS = 2.0  # how long the demo plug takes to switch
    # Device id -> (BatteryVoltage in tenths of a volt, BatteryLevel). The bedroom valve is running low.
    BATTERIES = {
        10: (26, "Normal"), 11: (30, "Normal"), 12: (29, "Normal"), 13: (28, "TwoThirds"),
        14: (25, "Low"), 15: (30, "Normal"), 16: (27, "OneThird"),
    }

    def __init__(self) -> None:
        self._last = time.time()
        self.eco = False
        self.away = False
        self.away_limit = 160
        start = {"Living room": 19.2, "Kitchen": 18.4, "Bedroom": 17.1, "Study": 20.3, "Bathroom": 18.9}
        self.rooms: dict[int, dict[str, Any]] = {
            i: {"Name": name, "temp": temp, "Mode": "Auto", "manual": 210, "ov": None}
            for i, (name, temp) in enumerate(start.items(), start=1)
        }
        # The landing smart plug: a 35 W lamp, on a 17:00-23:00 schedule. "override" is a switch
        # made in Auto mode, kept until the schedule next changes (scheduled state it was made in).
        self.plug = {"Mode": "Auto", "manual": "Off", "AwayAction": "Off", "override": None, "wh": 2030.0, "pending": None}

    async def aclose(self) -> None:  # parity with WiserHub
        return None

    @staticmethod
    def _plug_scheduled() -> str:
        return "On" if 17 <= time.localtime().tm_hour < 23 else "Off"

    def _plug_output(self) -> tuple[str, str]:
        """(OutputState, ControlSource) of the demo plug right now."""
        p, scheduled = self.plug, self._plug_scheduled()
        if p["pending"] and p["pending"][1] <= time.time():
            if p["Mode"] == "Manual":
                p["manual"] = p["pending"][0]
            else:
                p["override"] = (p["pending"][0], scheduled)
            p["pending"] = None
        if self.away and p["AwayAction"] == "Off":
            return "Off", "FromAwayMode"
        if p["Mode"] == "Manual":
            return p["manual"], "FromManualMode"
        if p["override"] and p["override"][1] == scheduled:
            return p["override"][0], "FromManualOverride"
        p["override"] = None
        return scheduled, "FromSchedule"

    # -- simulation --------------------------------------------------------
    @staticmethod
    def _schedule_id(rid: int) -> int:
        return {3: 2, 4: 3}.get(rid, 1)  # room 3 is the bedroom, room 4 the study

    def _points(self, sid: int, weekday: int) -> list[tuple[int, int]]:
        weekdays, weekends = self.SCHEDULES[sid]
        return weekdays if weekday < 5 else weekends

    def _scheduled(self, rid: int) -> int:
        """The setpoint the schedule gives right now: the last one today, else the last one yesterday."""
        now = time.localtime()
        sid, hhmm = self._schedule_id(rid), now.tm_hour * 100 + now.tm_min
        earlier = [sp for t, sp in self._points(sid, now.tm_wday) if t <= hhmm]
        return earlier[-1] if earlier else self._points(sid, (now.tm_wday - 1) % 7)[-1][1]

    def _next_change(self, sid: int) -> float:
        """Unix time of the schedule's next entry after now, whatever its temperature."""
        now = datetime.now()
        for days in range(8):
            day = now + timedelta(days=days)
            for hhmm, _ in self._points(sid, day.weekday()):
                at = day.replace(hour=hhmm // 100, minute=hhmm % 100, second=0, microsecond=0)
                if at > now:
                    return at.timestamp()
        raise AssertionError("demo schedules always have an entry within a week")

    def _effective(self, rid: int) -> tuple[int, str]:
        r = self.rooms[rid]
        now = time.time()
        if self.away:
            return self.away_limit, "FromAwayMode"
        ov = r["ov"]
        if ov and ov["until"] > now:
            return ov["sp"], "FromBoost" if ov["type"] == "Boost" else "FromManualOverride"
        r["ov"] = None
        if r["Mode"] == "Manual":
            return r["manual"], "FromManualMode"
        return self._scheduled(rid), "FromSchedule"

    def _tick(self) -> None:
        now = time.time()
        minutes = (now - self._last) / 60
        self._last = now
        if self._plug_output()[0] == "On":
            self.plug["wh"] += 35 * minutes / 60  # the lamp's energy, in watt-hours
        for rid, r in self.rooms.items():
            target = self._effective(rid)[0] / 10
            if target > r["temp"]:
                r["temp"] += min(0.25 * minutes, target - r["temp"])
            else:
                r["temp"] -= min(0.03 * minutes, max(r["temp"] - 15.0, 0))

    def _domain(self) -> dict[str, Any]:
        rooms, heating = [], False
        for rid, r in self.rooms.items():
            sp, origin = self._effective(rid)
            demand = int(max(0, min(100, (sp / 10 - r["temp"]) * 50)))
            heating = heating or demand > 0
            ov = r["ov"]
            devices = self.DEVICES.get(rid, [])
            rooms.append(
                {
                    "id": rid,
                    "Name": r["Name"],
                    "RoomStatId": next((d[0] for d in devices if d[1] == "RoomStat"), None),
                    "SmartValveIds": [d[0] for d in devices if d[1] == "iTRV"],
                    "Mode": r["Mode"],
                    "OverrideType": ov["type"] if ov else "None",
                    "OverrideTimeoutUnixTime": int(ov["until"]) if ov else 0,
                    "CurrentSetPoint": sp,
                    "DisplayedSetPoint": sp,
                    "ScheduledSetPoint": self._scheduled(rid),
                    "CalculatedTemperature": int(round(r["temp"] * 10)),
                    "PercentageDemand": demand,
                    "WindowState": "Closed",
                    "SetpointOrigin": origin,
                    "ScheduleId": self._schedule_id(rid),
                }
            )
        # As /data/v2/schedules/ gives them, which WiserHub.read() adds under SCHEDULES_KEY.
        schedules = [
            {
                "id": sid,
                **{
                    day: {"Time": [t for t, _ in self._points(sid, i)], "DegreesC": [sp for _, sp in self._points(sid, i)]}
                    for i, day in enumerate(DAYS)
                },
            }
            for sid in self.SCHEDULES
        ]
        return {
            "System": {
                "OverrideType": "Away" if self.away else "None",
                "AwayModeSetPointLimit": self.away_limit,
                "EcoModeEnabled": self.eco,
            },
            "HeatingChannel": [{"id": 1, "HeatingRelayState": "On" if heating else "Off"}],
            "Room": rooms,
            "Device": [
                {"id": self.HUB_ID, "ProductType": "Controller", "NodeId": self.HUB_ID + 1000},
                {"id": self.PLUG_ID, "ProductType": "SmartPlug", "NodeId": self.PLUG_ID + 1000,
                 "ParentNodeId": self.HUB_ID + 1000, "DisplayedSignalStrength": "Online",
                 "ReceptionOfDevice": {"Rssi": -74, "Lqi": 90}},
                *(
                    {
                        "id": device_id,
                        "ProductType": product,
                        "NodeId": device_id + 1000,
                        "ParentNodeId": parent + 1000,
                        "DisplayedSignalStrength": rating,
                        "ReceptionOfController": {"Rssi": rssi, "Lqi": 120},
                        "ReceptionOfDevice": {"Rssi": rssi - 2, "Lqi": 120},
                        "BatteryVoltage": self.BATTERIES[device_id][0],
                        "BatteryLevel": self.BATTERIES[device_id][1],
                    }
                    for devices in self.DEVICES.values()
                    for device_id, product, rssi, rating, parent in devices
                ),
            ],
            "SmartPlug": [self._plug_entry()],
            SCHEDULES_KEY: {"Heating": schedules},
        }

    def _plug_entry(self) -> dict[str, Any]:
        output, source = self._plug_output()
        return {
            "id": self.PLUG_ID,
            "Name": "Landing",
            "Mode": self.plug["Mode"],
            "ManualState": self.plug["manual"],
            "ScheduledState": self._plug_scheduled(),
            "AwayAction": self.plug["AwayAction"],
            "OutputState": output,
            "TargetState": self.plug["pending"][0] if self.plug["pending"] else output,
            "ControlSource": source,
            "InstantaneousDemand": 35 if output == "On" else 0,
            "CurrentSummationDelivered": int(self.plug["wh"]),
        }

    # -- same interface as WiserHub ---------------------------------------
    async def read(self) -> dict[str, Any]:
        self._tick()
        return self._domain()

    async def patch(self, resource: str, body: dict[str, Any]) -> None:
        await asyncio.sleep(0.15)
        self._tick()
        now = time.time()
        if resource == "System":
            if "EcoModeEnabled" in body:
                self.eco = bool(body["EcoModeEnabled"])
            override = body.get("RequestOverride")
            if override is not None:
                self.away = override.get("Type") == 2
            return
        if resource == f"SmartPlug/{self.PLUG_ID}":
            p = self.plug
            if body.get("Mode") in ("Auto", "Manual"):
                p["Mode"], p["override"] = body["Mode"], None
            if body.get("AwayAction") in ("Off", "NoChange"):
                p["AwayAction"] = body["AwayAction"]
            if body.get("RequestOutput") in ("On", "Off"):
                # Like a real plug, it reports the new state only after a moment (TargetState shows it first).
                p["pending"] = (body["RequestOutput"], now + self.PLUG_SWITCH_SECONDS)
            return
        if resource.startswith("Room/"):
            room = self.rooms.get(int(resource.split("/", 1)[1]))
            if room is None:
                raise HubError("The hub refused the request (404) no such room")
            if "Mode" in body:
                room["Mode"] = body["Mode"]
            override = body.get("RequestOverride")
            if override:
                kind = override.get("Type")
                rid = next(k for k, v in self.rooms.items() if v is room)
                if kind == "Manual":
                    minutes = int(override.get("DurationMinutes") or 0)
                    if minutes > 0:
                        # A timed hold (e.g. a target change during a boost), in either mode.
                        room["ov"] = {"type": "Manual", "sp": int(override["SetPoint"]), "until": now + minutes * 60}
                    elif room["Mode"] == "Manual":
                        # In manual mode a new target is the room's manual temperature, kept until changed.
                        room["manual"] = int(override["SetPoint"])
                        room["ov"] = None
                    else:
                        # On the schedule it's a hold that lasts until the next schedule entry.
                        until = self._next_change(self._schedule_id(rid))
                        room["ov"] = {"type": "Manual", "sp": int(override["SetPoint"]), "until": until}
                elif kind == "Boost":
                    minutes = int(override.get("DurationMinutes", 0))
                    if minutes <= 0:
                        room["ov"] = None
                    else:
                        # The hub boosts from the room's current temperature, not its target.
                        base = int(round(room["temp"] * 2)) * 5
                        room["ov"] = {
                            "type": "Boost",
                            "sp": base + int(override.get("IncreaseSetPointBy", 20)),
                            "until": now + minutes * 60,
                        }
                elif kind == "None":
                    room["ov"] = None
                else:
                    raise HubError(f"The hub refused the request (400) unknown override {kind!r}")
            return
        raise HubError(f"The hub refused the request (404) unknown resource {resource!r}")
