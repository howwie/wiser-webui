"""Heating web UI: polls the Wiser hub, serves the page, and exposes a small JSON API.

Only this server talks to the hub; the browser never sees the hub secret.
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import math
import os
import secrets
import signal
import time
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .hub import HubError, MockHub, WiserHub, normalise

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("wiser")

STATIC = Path(__file__).parent / "static"


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


MOCK = _flag("WISER_MOCK")
HOST = os.getenv("WISER_HUB_HOST", "").strip()
SECRET = os.getenv("WISER_SECRET", "").strip()
PASSWORD = os.getenv("WISER_UI_PASSWORD", "")
POLL_SECONDS = max(5.0, float(os.getenv("WISER_POLL_SECONDS", "20")))
BOOST_INCREASE = float(os.getenv("WISER_BOOST_INCREASE", "2"))
ALLOWED_HOSTS = {h.strip().lower() for h in os.getenv("WISER_ALLOWED_HOSTS", "").split(",") if h.strip()}
DATA_DIR = Path(os.getenv("WISER_DATA_DIR", "data"))


# --------------------------------------------------------------------------
# Page settings shared by every device, kept in a small JSON file
# --------------------------------------------------------------------------
class Settings:
    def __init__(self, path: Path | None) -> None:
        """With no path, settings live in memory only (used by tests)."""
        self.path = path
        self.room_order: list[int] = []
        if path is None:
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable settings file %s: %s", path, exc)
            return
        order = data.get("room_order") if isinstance(data, dict) else None
        if isinstance(order, list) and all(isinstance(i, int) for i in order):
            self.room_order = order

    def save_room_order(self, order: list[int]) -> None:
        """Write via a temporary file, so a crash mid-write can't leave a half-written file behind."""
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps({"room_order": order}, indent=2), encoding="utf-8")
            os.replace(temp, self.path)
        self.room_order = order


# --------------------------------------------------------------------------
# State cache + serialised access to the hub (it's a small device)
# --------------------------------------------------------------------------
class Store:
    def __init__(self, hub: Any, mock: bool, settings: Settings | None = None) -> None:
        self.hub = hub
        self.mock = mock
        self.settings = settings or Settings(None)
        self.lock = asyncio.Lock()
        self.state: dict[str, Any] | None = None
        self.error: str | None = None
        self.updated: float | None = None
        self.subscribers: set[asyncio.Queue[str | None]] = set()
        self.closing = False
        self.last_read: float | None = None  # time.monotonic() of the last read attempt, ok or not
        self.page_opened = asyncio.Event()  # wakes the idle poll loop
        self._last_key = ""

    def close_streams(self) -> None:
        """End every live-update stream (None tells a stream to finish). Browsers reconnect by themselves."""
        self.closing = True
        for queue in list(self.subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(None)

    def snapshot(self) -> dict[str, Any]:
        base = self.state or {"rooms": [], "system": None}
        # Rooms in the saved order; any the order doesn't mention (e.g. new ones) follow in hub order.
        rank = {room_id: i for i, room_id in enumerate(self.settings.room_order)}
        rooms = sorted(base["rooms"], key=lambda r: rank.get(r["id"], len(rank)))
        return {**base, "rooms": rooms, "updated": self.updated, "error": self.error, "mock": self.mock}

    def broadcast(self) -> None:
        """Send the current state to every open page, unless nothing visible has changed."""
        snap = self.snapshot()
        key = json.dumps({**snap, "updated": None}, sort_keys=True)
        if key == self._last_key:
            return
        self._last_key = key
        message = json.dumps(snap)
        for queue in list(self.subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(message)

    async def _read_locked(self) -> None:
        self.last_read = time.monotonic()
        try:
            self.state = normalise(await self.hub.read())
            self.error = None
            self.updated = time.time()
        except HubError as exc:
            if str(exc) != self.error:
                log.warning("Hub read failed: %s", exc)
            self.error = str(exc)
        except Exception:
            # Anything else (usually a hub reply in a shape normalise() doesn't expect) must not
            # kill the poll loop, or the page would silently show stale readings forever.
            message = "The hub sent data this app couldn't read. See docker logs wiser-webui."
            if message != self.error:
                log.exception("Hub read failed unexpectedly")
            self.error = message
        self.broadcast()

    async def refresh(self) -> None:
        async with self.lock:
            await self._read_locked()

    def age(self) -> float:
        """Seconds since the hub was last read (infinite if never)."""
        return math.inf if self.last_read is None else time.monotonic() - self.last_read

    async def refresh_if_older_than(self, seconds: float) -> None:
        """Read the hub unless that happened recently. Checked under the lock, so callers that
        arrive together (a page's first request and its live-update connection) share one read."""
        async with self.lock:
            if self.age() >= seconds:
                await self._read_locked()

    async def run(
        self,
        calls: list[tuple[str, dict[str, Any]]],
        verify: Callable[[dict[str, Any]], bool] | None = None,
        unverified_message: str = "",
    ) -> None:
        """Send writes to the hub, re-read, and (optionally) check the change really took effect."""
        async with self.lock:
            failure: str | None = None
            try:
                for resource, body in calls:
                    log.info("PATCH %s %s", resource, json.dumps(body))
                    await self.hub.patch(resource, body)
            except HubError as exc:
                failure = str(exc)
                log.warning("Write failed: %s", failure)
            await self._read_locked()  # always resync, even after a failure
            if failure is None and verify and self.state and not verify(self.state):
                failure = unverified_message
                log.warning("Write accepted but had no effect: %s", failure)
        if failure:
            raise HTTPException(status_code=502, detail=failure)

    def require_room(self, room_id: int) -> dict[str, Any]:
        if self.state is None:
            raise HTTPException(status_code=503, detail=self.error or "Still connecting to the hub.")
        room = next((r for r in self.state["rooms"] if r["id"] == room_id), None)
        if room is None:
            raise HTTPException(status_code=404, detail="No such room.")
        return room


store: Store


async def _poll_forever() -> None:
    """Read the hub every POLL_SECONDS while a page is open, and not at all while none is.

    Writes re-read the hub too, which counts: the next poll is POLL_SECONDS after any read.
    """
    while True:
        store.page_opened.clear()
        if not store.subscribers:
            await store.page_opened.wait()  # idle until a page connects
            continue
        due = POLL_SECONDS - store.age()
        if due > 0:
            # Sleep until the next read is due. A page opening meanwhile changes nothing,
            # but waking anyway is harmless: the loop just works out the wait again.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(store.page_opened.wait(), timeout=due)
            continue
        try:
            await store.refresh_if_older_than(POLL_SECONDS)
        except Exception:  # belt and braces: the loop must outlive any single bad read
            log.exception("Poll failed")


def _close_streams_on_stop_signal() -> None:
    """On Ctrl+C, a --reload restart or `docker stop`, end the live-update streams first.

    uvicorn waits for open connections before it stops, and a live-update stream never ends
    by itself, so without this a single open browser tab keeps the server running forever.
    uvicorn installs its own handlers before the app starts, so we chain onto those.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        previous = signal.getsignal(sig) if sig is not None else None
        if not callable(previous):
            continue

        def handler(signum: int, frame: Any, previous: Callable[..., Any] = previous) -> None:
            loop.call_soon_threadsafe(store.close_streams)
            previous(signum, frame)

        try:
            signal.signal(sig, handler)
        except ValueError:  # not on the main thread (e.g. under a test client); nothing to chain onto
            return


@asynccontextmanager
async def lifespan(_: FastAPI):
    global store
    if MOCK:
        log.info("Running with a mock hub (demo data).")
        hub: Any = MockHub()
    else:
        if not HOST or not SECRET:
            raise RuntimeError("Set WISER_HUB_HOST and WISER_SECRET, or set WISER_MOCK=1 to try the demo.")
        hub = WiserHub(HOST, SECRET)
    store = Store(hub, MOCK, Settings(DATA_DIR / "settings.json"))
    _close_streams_on_stop_signal()
    await store.refresh()
    task = asyncio.create_task(_poll_forever())
    try:
        yield
    finally:
        task.cancel()
        await hub.aclose()


app = FastAPI(title="Wiser web UI", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# --------------------------------------------------------------------------
# Optional password (HTTP basic auth, any username)
# --------------------------------------------------------------------------
class BasicAuth:
    def __init__(self, app: Any, password: str) -> None:
        self.app = app
        self.password = password.encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"] == "/healthz":
            return await self.app(scope, receive, send)
        header = dict(scope["headers"]).get(b"authorization", b"").decode()
        ok = False
        if header.lower().startswith("basic "):
            try:
                supplied = base64.b64decode(header[6:]).decode().partition(":")[2]
                ok = secrets.compare_digest(supplied.encode(), self.password)
            except Exception:
                ok = False
        if not ok:
            response = Response("Password required", 401, headers={"WWW-Authenticate": 'Basic realm="Heating"'})
            return await response(scope, receive, send)
        await self.app(scope, receive, send)


if PASSWORD:
    app.add_middleware(BasicAuth, password=PASSWORD)


# --------------------------------------------------------------------------
# Host header check (blocks DNS rebinding: a web page you visit pointing its
# own hostname at this server so your browser sends it commands)
# --------------------------------------------------------------------------
def host_allowed(host_header: str, extra: set[str]) -> bool:
    """IP addresses and localhost are always fine; any other name must be listed in WISER_ALLOWED_HOSTS."""
    host = host_header.strip().lower()
    if host.startswith("["):  # [IPv6]:port
        host = host[1:].partition("]")[0]
    elif host.count(":") == 1:  # name:port or IPv4:port
        host = host.partition(":")[0]
    host = host.rstrip(".")
    if not host:
        return False
    if host == "localhost" or host in extra:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class HostCheck:
    def __init__(self, app: Any, allowed: set[str]) -> None:
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"] == "/healthz":
            return await self.app(scope, receive, send)
        host = dict(scope["headers"]).get(b"host", b"").decode("latin-1")
        if not host_allowed(host, self.allowed):
            log.warning("Refused request for unknown host %r. Add it to WISER_ALLOWED_HOSTS if it's yours.", host)
            response = Response("Unknown host name. Add it to WISER_ALLOWED_HOSTS.", 400)
            return await response(scope, receive, send)
        await self.app(scope, receive, send)


# Added last so it runs first, before the password check.
app.add_middleware(HostCheck, allowed=ALLOWED_HOSTS)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
class TargetIn(BaseModel):
    temp: float = Field(ge=5, le=30)


class BoostIn(BaseModel):
    minutes: Literal[0, 30, 60, 120, 180]  # 0 cancels
    increase: float | None = Field(default=None, ge=0.5, le=5)


class HoldIn(BaseModel):
    rooms: list[int] = Field(min_length=1, max_length=200)
    temp: float = Field(ge=5, le=30)
    minutes: Literal[30, 60, 120, 180]


class ModeIn(BaseModel):
    mode: Literal["auto", "manual"]


class FlagIn(BaseModel):
    enabled: bool


class OrderIn(BaseModel):
    order: list[int] = Field(max_length=200)  # room ids, first to last


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/state")
async def get_state() -> dict[str, Any]:
    if not store.subscribers and store.age() >= POLL_SECONDS:
        # Nothing is polling (no page open), so read on demand for API callers. Wait at most a few
        # seconds: if the hub is unreachable, answer with the last readings (and the error) instead.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(store.refresh_if_older_than(POLL_SECONDS)), timeout=5)
    return store.snapshot()


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=8)
    store.subscribers.add(queue)
    store.page_opened.set()  # start polling if nothing was; the page gets the last readings meanwhile

    async def stream():
        try:
            yield f"data: {json.dumps(store.snapshot())}\n\n"
            while not store.closing and not await request.is_disconnected():
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if message is None:  # the server is stopping
                    break
                yield f"data: {message}\n\n"
        finally:
            store.subscribers.discard(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.put("/api/rooms/order")
async def set_room_order(body: OrderIn) -> dict[str, Any]:
    # Ids of rooms the hub doesn't have are kept, so a room that's briefly missing keeps its place.
    order = list(dict.fromkeys(body.order))  # drop repeats, keep first position
    try:
        store.settings.save_room_order(order)
    except OSError as exc:
        log.warning("Couldn't save the room order: %s", exc)
        raise HTTPException(status_code=500, detail=f"Couldn't save the room order ({exc.strerror or exc}).") from exc
    store.broadcast()  # other open pages rearrange too
    return store.snapshot()


@app.post("/api/rooms/{room_id}/target")
async def set_target(room_id: int, body: TargetIn) -> dict[str, Any]:
    room = store.require_room(room_id)
    tenths = int(round(body.temp * 2)) * 5  # nearest 0.5 C, in tenths
    override: dict[str, Any] = {"Type": "Manual", "SetPoint": tenths}
    # Changing the target during a boost (or a hold with an end time) keeps that end time, like
    # the Wiser app does. Without a duration the hub would hold until the next schedule change.
    if (room["boost"] or room["override"]) and room["until"]:
        minutes = math.ceil((room["until"] - time.time()) / 60)
        if minutes > 0:
            override["DurationMinutes"] = minutes
    await store.run([(f"Room/{room_id}", {"RequestOverride": override})])
    return store.snapshot()


@app.post("/api/rooms/{room_id}/boost")
async def set_boost(room_id: int, body: BoostIn) -> dict[str, Any]:
    store.require_room(room_id)
    if body.minutes == 0:
        payload: dict[str, Any] = {"RequestOverride": {"Type": "None"}}
    else:
        increase = body.increase if body.increase is not None else BOOST_INCREASE
        payload = {
            "RequestOverride": {
                "Type": "Boost",
                "DurationMinutes": body.minutes,
                "IncreaseSetPointBy": int(round(increase * 10)),
            }
        }
    await store.run([(f"Room/{room_id}", payload)])
    return store.snapshot()


@app.post("/api/rooms/hold")
async def hold_rooms(body: HoldIn) -> dict[str, Any]:
    """Heat several rooms to the same temperature for a while (the Wiser app can only boost one
    room at a time, by a fixed amount). Each room gets a timed manual override, so it goes back
    to its schedule or manual setting by itself when the time is up."""
    room_ids = list(dict.fromkeys(body.rooms))
    for room_id in room_ids:
        store.require_room(room_id)  # check them all before changing any
    tenths = int(round(body.temp * 2)) * 5  # nearest 0.5 C, in tenths
    override = {"Type": "Manual", "SetPoint": tenths, "DurationMinutes": body.minutes}
    await store.run([(f"Room/{room_id}", {"RequestOverride": override}) for room_id in room_ids])
    return store.snapshot()


@app.post("/api/rooms/{room_id}/mode")
async def set_mode(room_id: int, body: ModeIn) -> dict[str, Any]:
    store.require_room(room_id)
    resource = f"Room/{room_id}"
    if body.mode == "auto":
        # Back to the schedule: switch mode and drop any hold/boost.
        calls = [(resource, {"Mode": "Auto"}), (resource, {"RequestOverride": {"Type": "None"}})]
    else:
        calls = [(resource, {"Mode": "Manual"})]
    await store.run(calls)
    return store.snapshot()


@app.post("/api/system/away")
async def set_away(body: FlagIn) -> dict[str, Any]:
    if store.state is None:
        raise HTTPException(status_code=503, detail=store.error or "Still connecting to the hub.")
    away_tenths = int(round((store.state["system"]["away_temp"] or 16.0) * 10))
    override = {"Type": 2 if body.enabled else 0, "Originator": "App", "DurationMinutes": 0, "SetPoint": away_tenths}
    await store.run(
        [("System", {"RequestOverride": override})],
        verify=lambda s: s["system"]["away"] == body.enabled,
        unverified_message="The hub accepted the command but away mode didn't change.",
    )
    return store.snapshot()


@app.post("/api/system/eco")
async def set_eco(body: FlagIn) -> dict[str, Any]:
    if store.state is None:
        raise HTTPException(status_code=503, detail=store.error or "Still connecting to the hub.")
    await store.run(
        [("System", {"EcoModeEnabled": body.enabled})],
        verify=lambda s: s["system"]["eco"] == body.enabled,
        unverified_message="The hub accepted the command but eco mode didn't change.",
    )
    return store.snapshot()


class RevalidatedStaticFiles(StaticFiles):
    """Make browsers check for a newer copy on every load (a cheap 304 when nothing changed),
    so an updated page shows up without a hard refresh."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


# The page itself. Mounted last so the API routes above take priority.
app.mount("/", RevalidatedStaticFiles(directory=STATIC, html=True), name="static")
