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
        return data

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


def _schedule_days(schedule: dict[str, Any]) -> list[list[list[Any]]] | None:
    """A hub heating schedule -> 7 lists (Monday first) of [minutes after midnight, degrees C or None].

    The hub stores times as HHMM numbers (630 = 06:30) and setpoints in tenths. Days are
    normally keyed by name; the Weekdays/Weekends/All keys are a fallback in case a hub groups them.
    """
    days = []
    for i, day in enumerate(DAYS):
        entry = schedule.get(day) or schedule.get("Weekdays" if i < 5 else "Weekends") or schedule.get("All") or {}
        points = []
        for point in entry.get("SetPoints") or []:
            hhmm = point.get("Time")
            if isinstance(hhmm, int) and 0 <= hhmm <= 2359 and hhmm % 100 < 60:
                points.append([hhmm // 100 * 60 + hhmm % 100, _deg(point.get("DegreesC"))])
        days.append(sorted(points, key=lambda p: p[0]))
    return days if any(days) else None


def normalise(domain: dict[str, Any]) -> dict[str, Any]:
    system = domain.get("System") or {}
    schedules = {
        s.get("id"): s
        for s in domain.get("Schedule") or []
        if isinstance(s, dict) and s.get("Type", "Heating") == "Heating"
    }
    rooms = []
    for r in domain.get("Room") or []:
        origin = r.get("SetpointOrigin") or ""
        mode = str(r.get("Mode") or "Auto").lower()
        boost = origin == "FromBoost"
        override = (not boost) and (r.get("OverrideType") or "None") not in ("None", "")
        until = r.get("OverrideTimeoutUnixTime") or None
        schedule = schedules.get(r.get("ScheduleId"))
        rooms.append(
            {
                "id": r["id"],
                "name": r.get("Name") or f"Room {r['id']}",
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
            }
        )
    channels = domain.get("HeatingChannel") or []
    return {
        "rooms": rooms,
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

    async def aclose(self) -> None:  # parity with WiserHub
        return None

    # -- simulation --------------------------------------------------------
    @staticmethod
    def _schedule_id(rid: int) -> int:
        return 2 if rid == 3 else 1  # room 3 is the bedroom

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
            rooms.append(
                {
                    "id": rid,
                    "Name": r["Name"],
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
        schedules = [
            {
                "id": sid,
                "Type": "Heating",
                **{
                    day: {"SetPoints": [{"Time": t, "DegreesC": sp} for t, sp in self._points(sid, i)]}
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
            "Schedule": schedules,
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
                if "SetPoint" in override and self.away:
                    self.away_limit = int(override["SetPoint"])
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
                        base = self._effective(rid)[0]
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
