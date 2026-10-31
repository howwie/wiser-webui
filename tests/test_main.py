"""Tests for the server's own safety nets: the Host check and surviving bad hub replies."""
import asyncio
import time

import pytest

import app.main as server
from app.hub import HubError, MockHub
from fastapi import HTTPException

from app.main import BoostIn, HoldIn, OrderIn, Settings, Store, TargetIn, host_allowed


@pytest.mark.parametrize(
    "host",
    ["192.168.1.20:8080", "192.168.1.20", "localhost:8080", "127.0.0.1", "[::1]:8080", "[fe80::1]", "nas.lan:8080", "NAS.LAN."],
)
def test_allowed_hosts(host):
    assert host_allowed(host, {"nas.lan"})


@pytest.mark.parametrize("host", ["", "evil.example.com", "evil.example.com:8080", "nas.lan.evil.example.com"])
def test_refused_hosts(host):
    assert not host_allowed(host, {"nas.lan"})


class OddHub:
    """A hub whose reply is missing the room id that normalise() relies on."""

    async def read(self):
        return {"Room": [{"Name": "No id"}]}


def run_with_demo_hub(monkeypatch, *steps):
    """Run API handlers in order against a fresh demo hub; returns room 1 from the last snapshot."""
    monkeypatch.setattr(server, "store", Store(MockHub(), mock=True), raising=False)

    async def go():
        await server.store.refresh()
        snap = None
        for step in steps:
            snap = await step()
        return next(r for r in snap["rooms"] if r["id"] == 1)

    return asyncio.run(go())


def test_target_change_during_boost_keeps_the_boost_end_time(monkeypatch):
    boosted = run_with_demo_hub(monkeypatch, lambda: server.set_boost(1, BoostIn(minutes=60)))
    changed = run_with_demo_hub(
        monkeypatch,
        lambda: server.set_boost(1, BoostIn(minutes=60)),
        lambda: server.set_target(1, TargetIn(temp=23.5)),
    )
    assert boosted["boost"] is True
    assert (changed["boost"], changed["override"], changed["target"]) == (False, True, 23.5)
    assert abs(changed["until"] - boosted["until"]) <= 60


def test_second_target_change_keeps_the_hold_end_time(monkeypatch):
    first = run_with_demo_hub(monkeypatch, lambda: server.set_target(1, TargetIn(temp=22.0)))
    second = run_with_demo_hub(
        monkeypatch,
        lambda: server.set_target(1, TargetIn(temp=22.0)),
        lambda: server.set_target(1, TargetIn(temp=22.5)),
    )
    assert second["target"] == 22.5
    assert abs(second["until"] - first["until"]) <= 60


def test_hold_sets_several_rooms_to_one_temperature_for_a_while(monkeypatch):
    monkeypatch.setattr(server, "store", Store(MockHub(), mock=True), raising=False)

    async def go():
        await server.store.refresh()
        await server.set_boost(2, BoostIn(minutes=30))  # a boost is replaced by the hold
        return await server.hold_rooms(HoldIn(rooms=[1, 2, 1], temp=20.2, minutes=120))

    snap = asyncio.run(go())
    rooms = {r["id"]: r for r in snap["rooms"]}
    for room_id in (1, 2):
        room = rooms[room_id]
        assert (room["target"], room["override"], room["boost"]) == (20.0, True, False)
        assert abs(room["until"] - (time.time() + 120 * 60)) <= 60
    assert not rooms[3]["override"]


def test_hold_with_an_unknown_room_changes_nothing(monkeypatch):
    monkeypatch.setattr(server, "store", Store(MockHub(), mock=True), raising=False)

    async def go():
        await server.store.refresh()
        with pytest.raises(HTTPException) as caught:
            await server.hold_rooms(HoldIn(rooms=[1, 99], temp=20, minutes=60))
        return caught.value.status_code, server.store.snapshot()

    status, snap = asyncio.run(go())
    assert status == 404
    assert not any(r["override"] for r in snap["rooms"])


class RecordingHub(MockHub):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def patch(self, resource, body):
        self.sent.append((resource, body))
        await super().patch(resource, body)


def test_writes_match_aiowiserheatapi(monkeypatch):
    """The payloads aioWiserHeatAPI 1.7.7 sends for the same actions."""
    hub = RecordingHub()
    monkeypatch.setattr(server, "store", Store(hub, mock=True), raising=False)

    async def go():
        await server.store.refresh()
        await server.set_boost(1, BoostIn(minutes=30))
        await server.set_mode(1, server.ModeIn(mode="manual"))  # cancels the boost first
        await server.set_away(server.FlagIn(enabled=True))
        await server.set_away(server.FlagIn(enabled=False))

    asyncio.run(go())
    assert hub.sent == [
        ("Room/1", {"RequestOverride": {"Type": "Boost", "DurationMinutes": 30, "IncreaseSetPointBy": 20}}),
        ("Room/1", {"RequestOverride": {"Type": "None"}}),
        ("Room/1", {"Mode": "Manual"}),
        ("System", {"RequestOverride": {"Type": 2}}),
        ("System", {"RequestOverride": {"Type": 0}}),
    ]


def test_plug_commands_match_aiowiserheatapi(monkeypatch):
    hub = RecordingHub()
    hub.PLUG_SWITCH_SECONDS = 0  # switch at once, so the test doesn't wait
    monkeypatch.setattr(server, "store", Store(hub, mock=True), raising=False)
    plug = MockHub.PLUG_ID

    async def go():
        await server.store.refresh()
        await server.set_plug_mode(plug, server.ModeIn(mode="manual"))
        await server.set_plug_power(plug, server.FlagIn(enabled=True))
        on = next(p for p in server.store.snapshot()["plugs"] if p["id"] == plug)
        await server.set_plug_away(plug, server.FlagIn(enabled=False))
        await server.set_plug_power(plug, server.FlagIn(enabled=False))
        off = next(p for p in server.store.snapshot()["plugs"] if p["id"] == plug)
        return on, off

    on, off = asyncio.run(go())
    assert hub.sent == [
        (f"SmartPlug/{plug}", {"Mode": "Manual"}),
        (f"SmartPlug/{plug}", {"RequestOutput": "On"}),
        (f"SmartPlug/{plug}", {"AwayAction": "NoChange"}),
        (f"SmartPlug/{plug}", {"RequestOutput": "Off"}),
    ]
    assert (on["on"], on["mode"], on["power_w"]) == (True, "manual", 35)
    assert (off["on"], off["away_off"], off["power_w"]) == (False, False, 0)


def test_plug_that_does_not_switch_in_time_is_reported(monkeypatch):
    hub = MockHub()
    hub.PLUG_SWITCH_SECONDS = 60  # the plug never confirms within the wait
    monkeypatch.setattr(server, "store", Store(hub, mock=True), raising=False)
    monkeypatch.setattr(server, "PLUG_SETTLE_SECONDS", 1.5)

    async def go():
        await server.store.refresh()
        await server.set_plug_mode(MockHub.PLUG_ID, server.ModeIn(mode="manual"))
        with pytest.raises(HTTPException) as caught:
            await server.set_plug_power(MockHub.PLUG_ID, server.FlagIn(enabled=True))
        plug = server.store.snapshot()["plugs"][0]
        return caught.value, plug

    error, plug = asyncio.run(go())
    assert error.status_code == 502 and "hasn't switched on yet" in error.detail
    assert (plug["on"], plug["switching_to"]) == (False, True)


def test_unknown_plug_is_refused(monkeypatch):
    monkeypatch.setattr(server, "store", Store(MockHub(), mock=True), raising=False)

    async def go():
        await server.store.refresh()
        with pytest.raises(HTTPException) as caught:
            await server.set_plug_power(99, server.FlagIn(enabled=True))
        return caught.value.status_code

    assert asyncio.run(go()) == 404


def test_room_order_is_saved_and_read_back(tmp_path):
    path = tmp_path / "data" / "settings.json"
    Settings(path).save_room_order([3, 1, 2])
    assert Settings(path).room_order == [3, 1, 2]
    assert not path.with_suffix(".tmp").exists()


@pytest.mark.parametrize("content", ["{not json", '{"room_order": ["a", 1]}', "[1, 2]"])
def test_unreadable_settings_are_ignored(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(content)
    assert Settings(path).room_order == []


def test_snapshot_puts_rooms_in_saved_order_with_unknown_ones_last():
    settings = Settings(None)
    settings.room_order = [3, 99, 1]  # 99: a room the hub doesn't have (any more)
    store = Store(MockHub(), mock=True, settings=settings)
    asyncio.run(store.refresh())
    assert [r["id"] for r in store.snapshot()["rooms"]] == [3, 1, 2, 4, 5]


def test_set_room_order_saves_and_tells_open_pages(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(server, "store", Store(MockHub(), mock=True, settings=Settings(path)), raising=False)

    async def go():
        await server.store.refresh()
        page = asyncio.Queue(maxsize=8)
        server.store.subscribers.add(page)
        snap = await server.set_room_order(OrderIn(order=[5, 4, 5]))
        return snap, page.get_nowait()

    snap, pushed = asyncio.run(go())
    assert [r["id"] for r in snap["rooms"]] == [5, 4, 1, 2, 3]
    assert Settings(path).room_order == [5, 4]  # repeats dropped
    assert '"id": 5' in pushed.split('"rooms": [', 1)[1][:20]


def test_room_order_save_failure_is_reported(monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("")
    settings = Settings(blocker / "settings.json")
    monkeypatch.setattr(server, "store", Store(MockHub(), mock=True, settings=settings), raising=False)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(server.set_room_order(OrderIn(order=[2, 1])))
    assert caught.value.status_code == 500
    assert "Couldn't save the room order" in caught.value.detail
    assert settings.room_order == []


class CountingHub(MockHub):
    """The demo hub, counting how often it's read."""

    def __init__(self):
        super().__init__()
        self.reads = 0

    async def read(self):
        self.reads += 1
        return await super().read()


def test_polls_only_while_a_page_is_open(monkeypatch):
    monkeypatch.setattr(server, "POLL_SECONDS", 0.05)
    hub = CountingHub()
    monkeypatch.setattr(server, "store", Store(hub, mock=True), raising=False)

    async def go():
        poller = asyncio.create_task(server._poll_forever())
        await asyncio.sleep(0.3)
        idle = hub.reads

        page = asyncio.Queue(maxsize=8)  # what /api/events does when a page connects
        server.store.subscribers.add(page)
        server.store.page_opened.set()
        await asyncio.sleep(0.3)
        while_open = hub.reads - idle

        server.store.subscribers.discard(page)
        await asyncio.sleep(0.1)  # a read already due may still finish
        closed_at = hub.reads
        await asyncio.sleep(0.3)
        after_close = hub.reads - closed_at
        poller.cancel()
        return idle, while_open, after_close

    idle, while_open, after_close = asyncio.run(go())
    assert idle == 0
    assert while_open >= 3
    assert after_close == 0


def test_state_reads_on_demand_only_when_nothing_is_polling(monkeypatch):
    monkeypatch.setattr(server, "POLL_SECONDS", 10)
    hub = CountingHub()
    monkeypatch.setattr(server, "store", Store(hub, mock=True), raising=False)

    async def go():
        counts = []
        await server.get_state()  # never read: reads now
        counts.append(hub.reads)
        await server.get_state()  # read moments ago: uses that
        counts.append(hub.reads)
        server.store.last_read -= 20  # now stale...
        server.store.subscribers.add(asyncio.Queue())  # ...but a page is open, so the poller handles it
        await server.get_state()
        counts.append(hub.reads)
        return counts

    assert asyncio.run(go()) == [1, 1, 1]


def test_write_with_no_page_open_acts_on_the_hub_as_it_is_now(monkeypatch):
    hub = RecordingHub()
    monkeypatch.setattr(server, "POLL_SECONDS", 10)
    monkeypatch.setattr(server, "store", Store(hub, mock=True), raising=False)

    async def go():
        await server.store.refresh()
        boost = {"RequestOverride": {"Type": "Boost", "DurationMinutes": 60, "IncreaseSetPointBy": 20}}
        await MockHub.patch(hub, "Room/1", boost)  # from the Wiser app, unseen as nothing is polling
        server.store.last_read -= 20
        await server.set_mode(1, server.ModeIn(mode="manual"))

    asyncio.run(go())
    assert hub.sent == [("Room/1", {"RequestOverride": {"Type": "None"}}), ("Room/1", {"Mode": "Manual"})]


class FlakyHub(MockHub):
    """The demo hub, with reads that fail while `down` is set."""

    down = False

    async def read(self):
        if self.down:
            raise HubError("Can't reach the hub (ReadTimeout).")
        return await super().read()


def test_write_works_once_a_hub_that_was_down_at_startup_is_back(monkeypatch):
    hub = FlakyHub()
    hub.down = True
    monkeypatch.setattr(server, "POLL_SECONDS", 10)
    monkeypatch.setattr(server, "store", Store(hub, mock=True), raising=False)

    async def go():
        await server.store.refresh()
        with pytest.raises(HTTPException) as caught:
            await server.set_mode(1, server.ModeIn(mode="manual"))
        hub.down = False
        server.store.last_read -= 20
        snap = await server.set_mode(1, server.ModeIn(mode="manual"))
        return caught.value.status_code, snap

    status, snap = asyncio.run(go())
    assert status == 503
    assert next(r for r in snap["rooms"] if r["id"] == 1)["mode"] == "manual"


def test_failed_read_back_is_not_reported_as_a_change_that_did_not_happen(monkeypatch):
    hub = FlakyHub()
    monkeypatch.setattr(server, "store", Store(hub, mock=True), raising=False)

    async def fail_after_patch(resource, body):
        await MockHub.patch(hub, resource, body)
        hub.down = True

    hub.patch = fail_after_patch

    async def go():
        await server.store.refresh()
        with pytest.raises(HTTPException) as caught:
            await server.set_eco(server.FlagIn(enabled=True))
        return caught.value

    error = asyncio.run(go())
    assert error.status_code == 502
    assert "couldn't be read to confirm" in error.detail and "ReadTimeout" in error.detail
    assert hub.eco is True


def test_slow_plug_does_not_hold_up_other_writes(monkeypatch):
    hub = MockHub()
    hub.PLUG_SWITCH_SECONDS = 60
    monkeypatch.setattr(server, "store", Store(hub, mock=True), raising=False)
    monkeypatch.setattr(server, "PLUG_SETTLE_SECONDS", 3)

    async def go():
        await server.store.refresh()
        await server.set_plug_mode(MockHub.PLUG_ID, server.ModeIn(mode="manual"))
        plug = asyncio.create_task(server.set_plug_power(MockHub.PLUG_ID, server.FlagIn(enabled=True)))
        await asyncio.sleep(0.5)
        started = time.monotonic()
        await server.set_target(1, TargetIn(temp=21))
        waited = time.monotonic() - started
        with pytest.raises(HTTPException):
            await plug
        return waited

    assert asyncio.run(go()) < 1.5


def test_unexpected_hub_reply_is_reported_not_raised():
    store = Store(OddHub(), mock=False)
    asyncio.run(store.refresh())
    assert store.state is None
    assert "couldn't read" in store.error
