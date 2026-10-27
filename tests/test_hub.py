"""Tests for normalise(): turning the hub's raw /data/domain/ reply into what the page needs."""
import asyncio

import httpx

from app.hub import DAYS, SCHEDULES_KEY, WiserHub, normalise


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


def test_boost_is_found_under_either_origin_spelling():
    raw = room(OverrideType="Manual")
    del raw["SetpointOrigin"]
    raw["SetPointOrigin"] = "FromBoost"
    assert normalise({"Room": [raw]})["rooms"][0]["boost"] is True


def test_schedules_endpoint_format_is_read():
    # /data/v2/schedules/ as aioWiserHeatAPI reads it: grouped by type, days as parallel lists.
    week = {day: {"Time": [2200, 630, 900], "DegreesC": [170, 210, None]} for day in DAYS}
    lights = {"id": 4, "Monday": {"Time": [3000], "Level": [100]}}
    out = normalise({"Room": [room(ScheduleId=4)], SCHEDULES_KEY: {"Heating": [{"id": 4, **week}], "Level": [lights]}})
    assert out["rooms"][0]["schedule"][0] == [[390, 21.0], [540, None], [1320, 17.0]]


def fake_hub(schedules_status):
    def reply(request):
        if request.url.path == "/data/domain/":
            return httpx.Response(200, json={"Room": [room(ScheduleId=4)]})
        if request.url.path == "/data/v2/schedules/":
            return httpx.Response(schedules_status, json={"Heating": [{"id": 4, "Monday": {"Time": [630], "DegreesC": [210]}}]})
        return httpx.Response(404)

    hub = WiserHub("hub", "secret")
    hub._client = httpx.AsyncClient(base_url="http://hub", transport=httpx.MockTransport(reply))
    return hub


def test_real_hub_read_includes_schedules():
    out = normalise(asyncio.run(fake_hub(200).read()))
    assert out["rooms"][0]["schedule"][0] == [[390, 21.0]]


def test_real_hub_read_survives_a_missing_schedules_endpoint():
    out = normalise(asyncio.run(fake_hub(404).read()))
    assert out["rooms"][0]["schedule"] is None


def test_room_devices_and_their_signal():
    raw = room(RoomStatId=10, SmartValveIds=[11, 12, 99])  # 99 isn't in the Device list
    devices = [
        {"id": 10, "ProductType": "RoomStat", "DisplayedSignalStrength": "VeryGood", "ReceptionOfController": {"Rssi": -55}},
        # The weaker direction counts: -80 -> 40%.
        {"id": 11, "ProductType": "iTRV", "DisplayedSignalStrength": "Medium",
         "ReceptionOfController": {"Rssi": -70}, "ReceptionOfDevice": {"Rssi": -80}},
        {"id": 12, "ProductType": "iTRV", "DisplayedSignalStrength": "NoSignal"},
        {"id": 13, "ProductType": "iTRV", "ReceptionOfController": {"Rssi": -60}},  # another room's
    ]
    out = normalise({"Room": [raw], "Device": devices})["rooms"][0]["devices"]
    assert out == [
        {"id": 10, "name": "Room thermostat", "signal": 90, "signal_label": "Very good",
         "hub_hears_dbm": None, "device_hears_dbm": -55, "parent": None},
        {"id": 11, "name": "Radiator valve 1", "signal": 40, "signal_label": "Medium",
         "hub_hears_dbm": -80, "device_hears_dbm": -70, "parent": None},
        {"id": 12, "name": "Radiator valve 2", "signal": None, "signal_label": "No signal",
         "hub_hears_dbm": None, "device_hears_dbm": None, "parent": None},
    ]


def test_each_device_names_what_it_is_connected_to():
    rooms = [
        room(id=1, Name="Lounge", RoomStatId=10, SmartValveIds=[11, 12, 13]),
        room(id=2, Name="Hall", SmartValveIds=[14]),
    ]
    devices = [
        {"id": 0, "ProductType": "Controller", "NodeId": 100},
        {"id": 10, "ProductType": "RoomStat", "NodeId": 110, "ParentNodeId": 100},
        {"id": 11, "ProductType": "iTRV", "NodeId": 111, "ParentNodeId": 110},  # via the thermostat
        {"id": 12, "ProductType": "iTRV", "NodeId": 112, "ParentNodeId": 120},  # via a smart plug
        {"id": 13, "ProductType": "iTRV", "NodeId": 113, "ParentNodeId": 999},  # parent not listed
        {"id": 14, "ProductType": "iTRV", "NodeId": 114, "ParentNodeId": 111},  # via another room's valve
        {"id": 20, "ProductType": "SmartPlug", "NodeId": 120, "ParentNodeId": 100},
    ]
    out = normalise({"Room": rooms, "Device": devices, "SmartPlug": [{"id": 20, "Name": "Landing"}]})
    parents = {d["id"]: d["parent"] for r in out["rooms"] for d in r["devices"]}
    assert parents == {
        10: {"name": "the hub", "hub": True},
        11: {"name": "Room thermostat in Lounge", "hub": False},
        12: {"name": "Landing smart plug", "hub": False},
        13: {"name": "Zigbee node 999", "hub": False},
        14: {"name": "Radiator valve 1 in Lounge", "hub": False},
    }


def test_room_without_devices():
    assert only_room()["devices"] == []


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
