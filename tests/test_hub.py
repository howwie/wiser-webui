"""Tests for normalise(): turning the hub's raw /data/domain/ reply into what the page needs."""
from app.hub import DAYS, normalise


def room(**fields):
    """A minimal hub room, following the schedule at 20.0 C with a 19.5 C reading."""
    base = {
        "id": 1,
        "Name": "Lounge",
        "Mode": "Auto",
        "CalculatedTemperature": 195,
        "CurrentSetPoint": 200,
        "DisplayedSetPoint": 200,
        "ScheduledSetPoint": 200,
        "SetpointOrigin": "FromSchedule",
        "OverrideType": "None",
        "PercentageDemand": 0,
        "WindowState": "Closed",
    }
    return {**base, **fields}


def only_room(**fields):
    return normalise({"Room": [room(**fields)]})["rooms"][0]


def test_temperatures_are_converted_from_tenths():
    r = only_room(CalculatedTemperature=205, DisplayedSetPoint=215, ScheduledSetPoint=180)
    assert (r["temp"], r["target"], r["scheduled"]) == (20.5, 21.5, 18.0)


def test_off_and_lost_sensor_become_none():
    r = only_room(CalculatedTemperature=-32768, DisplayedSetPoint=-200)
    assert r["temp"] is None
    assert r["target"] is None


def test_target_falls_back_to_current_setpoint():
    raw = room(CurrentSetPoint=170)
    del raw["DisplayedSetPoint"]
    assert normalise({"Room": [raw]})["rooms"][0]["target"] == 17.0


def test_schedule_room():
    r = only_room()
    assert r["mode"] == "auto"
    assert not r["boost"] and not r["override"]
    assert r["until"] is None


def test_boost_is_not_also_an_override():
    r = only_room(SetpointOrigin="FromBoost", OverrideType="Manual", OverrideTimeoutUnixTime=1_700_000_000)
    assert r["boost"] is True
    assert r["override"] is False
    assert r["until"] == 1_700_000_000


def test_manual_override_keeps_its_end_time():
    r = only_room(SetpointOrigin="FromManualOverride", OverrideType="Manual", OverrideTimeoutUnixTime=1_700_000_000)
    assert r["override"] is True
    assert r["until"] == 1_700_000_000


def test_end_time_ignored_without_an_override():
    assert only_room(OverrideTimeoutUnixTime=1_700_000_000)["until"] is None


def test_unknown_mode_falls_back_to_auto():
    assert only_room(Mode="Manual")["mode"] == "manual"
    assert only_room(Mode="Off")["mode"] == "auto"


def test_demand_and_window():
    r = only_room(PercentageDemand=45, WindowState="Open")
    assert r["demand"] == 45
    assert r["window_open"] is True


def test_missing_name_gets_a_default():
    raw = room(id=7)
    del raw["Name"]
    assert normalise({"Room": [raw]})["rooms"][0]["name"] == "Room 7"


def test_system_flags():
    out = normalise(
        {
            "System": {"OverrideType": "Away", "AwayModeSetPointLimit": 160, "EcoModeEnabled": True},
            "HeatingChannel": [{"HeatingRelayState": "Off"}, {"HeatingRelayState": "On"}],
        }
    )
    assert out["system"] == {"away": True, "away_temp": 16.0, "eco": True, "heating": True}


def test_schedule_is_attached_in_minutes_and_degrees():
    week = {day: {"SetPoints": [{"Time": 2200, "DegreesC": 170}, {"Time": 630, "DegreesC": 210}]} for day in DAYS}
    out = normalise({"Room": [room(ScheduleId=4)], "Schedule": [{"id": 4, "Type": "Heating", **week}]})
    schedule = out["rooms"][0]["schedule"]
    assert len(schedule) == 7
    assert schedule[0] == [[390, 21.0], [1320, 17.0]]  # sorted by time


def test_schedule_off_and_bad_times():
    week = {"Monday": {"SetPoints": [{"Time": 700, "DegreesC": -200}, {"Time": 2460}, {"Time": "7:00"}]}}
    out = normalise({"Room": [room(ScheduleId=1)], "Schedule": [{"id": 1, **week}]})
    assert out["rooms"][0]["schedule"][0] == [[420, None]]
    assert out["rooms"][0]["schedule"][1] == []


def test_schedule_grouped_days():
    week = {"Weekdays": {"SetPoints": [{"Time": 630, "DegreesC": 210}]}, "Weekends": {"SetPoints": [{"Time": 800, "DegreesC": 200}]}}
    schedule = normalise({"Room": [room(ScheduleId=1)], "Schedule": [{"id": 1, **week}]})["rooms"][0]["schedule"]
    assert schedule[4] == [[390, 21.0]] and schedule[5] == [[480, 20.0]]


def test_non_heating_schedule_with_same_id_is_ignored():
    plug = {"id": 1, "Type": "OnOff", "Monday": {"SetPoints": [{"Time": 700, "State": "On"}]}}
    assert only_room(ScheduleId=1)["schedule"] is None
    assert normalise({"Room": [room(ScheduleId=1)], "Schedule": [plug]})["rooms"][0]["schedule"] is None


def test_empty_domain():
    out = normalise({})
    assert out["rooms"] == []
    assert out["system"] == {"away": False, "away_temp": None, "eco": False, "heating": False}
