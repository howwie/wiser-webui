"""The demo hub should behave like a real one, so the page can be tried out honestly without a hub."""
import asyncio
import time

from app.hub import MockHub


def room_after(hub, *writes, rid=1):
    async def go():
        for body in writes:
            await hub.patch(f"Room/{rid}", body)
        return next(r for r in (await hub.read())["Room"] if r["id"] == rid)

    return asyncio.run(go())


def test_target_change_on_schedule_holds_until_next_schedule_entry():
    hub = MockHub()
    before = time.time()
    r = room_after(hub, {"RequestOverride": {"Type": "Manual", "SetPoint": 225}})

    assert (r["OverrideType"], r["SetpointOrigin"], r["CurrentSetPoint"]) == ("Manual", "FromManualOverride", 225)
    until = r["OverrideTimeoutUnixTime"]
    assert before < until < before + 24 * 3600  # demo schedules never go a day without an entry
    end = time.localtime(until)
    weekdays, weekends = MockHub.SCHEDULES[1]
    entry_times = {t for t, _ in (weekdays if end.tm_wday < 5 else weekends)}
    assert end.tm_hour * 100 + end.tm_min in entry_times
    assert end.tm_sec == 0


def test_target_change_in_manual_mode_sets_the_manual_temperature():
    hub = MockHub()
    r = room_after(hub, {"Mode": "Manual"}, {"RequestOverride": {"Type": "Manual", "SetPoint": 185}})

    assert (r["OverrideType"], r["SetpointOrigin"], r["CurrentSetPoint"]) == ("None", "FromManualMode", 185)
    assert r["OverrideTimeoutUnixTime"] == 0


def test_manual_temperature_is_kept_after_a_boost_ends():
    hub = MockHub()
    room_after(hub, {"Mode": "Manual"}, {"RequestOverride": {"Type": "Manual", "SetPoint": 185}})
    r = room_after(
        hub,
        {"RequestOverride": {"Type": "Boost", "DurationMinutes": 30, "IncreaseSetPointBy": 20}},
        {"RequestOverride": {"Type": "None"}},
    )
    assert (r["SetpointOrigin"], r["CurrentSetPoint"]) == ("FromManualMode", 185)
