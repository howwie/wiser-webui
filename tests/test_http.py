"""Tests through the real app: middleware, routing, validation and static files."""
import base64

import pytest
from fastapi.testclient import TestClient

import app.main as server
from app.main import BasicAuth

ADDRESS = "http://192.168.1.20:8080"
ECO = "/api/system/eco"
BODY = b'{"enabled": true}'
JSON = {"content-type": "application/json"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "MOCK", True)
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "store", None, raising=False)
    with TestClient(server.app, base_url=ADDRESS) as c:
        yield c


@pytest.fixture
def guarded(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "MOCK", True)
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "store", None, raising=False)
    with TestClient(BasicAuth(server.app, "pass:word"), base_url=ADDRESS) as c:
        yield c


def basic(text):
    return {"authorization": "Basic " + base64.b64encode(text.encode()).decode()}


def test_password_is_required(guarded):
    assert guarded.get("/api/state").status_code == 401
    assert guarded.get("/").status_code == 401
    assert guarded.get("/api/state").headers["www-authenticate"].startswith("Basic")


@pytest.mark.parametrize("headers", [basic("me:wrong"), basic("pass:word"), {"authorization": "Basic !!!"}, {"authorization": "Bearer x"}])
def test_wrong_credentials_are_refused(guarded, headers):
    assert guarded.get("/api/state", headers=headers).status_code == 401


def test_any_username_with_the_password_is_let_in(guarded):
    assert guarded.get("/api/state", headers=basic("anyone:pass:word")).status_code == 200


def test_healthz_needs_no_password(guarded):
    assert guarded.get("/healthz").json() == {"ok": True}


def test_unknown_host_is_refused(client):
    assert client.get("/api/state", headers={"host": "evil.example.com"}).status_code == 400
    assert client.get("/healthz", headers={"host": "evil.example.com"}).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},  # what fetch() sends for a body with no type
        {"content-type": "text/plain"},
        {"content-type": "application/x-www-form-urlencoded"},
        {**JSON, "origin": "http://evil.example.com"},
        {**JSON, "origin": "null"},
    ],
)
def test_cross_site_writes_are_refused(client, headers):
    assert client.post(ECO, content=BODY, headers=headers).status_code == 403
    assert client.get("/api/state").json()["system"]["eco"] is False


@pytest.mark.parametrize(
    "headers",
    [
        JSON,  # a script: no Origin
        {"content-type": "application/json; charset=utf-8"},
        {**JSON, "origin": ADDRESS},
        {**JSON, "origin": "http://192.168.1.20:9000"},  # a reverse proxy's own port
    ],
)
def test_the_page_and_scripts_can_write(client, headers):
    response = client.post(ECO, content=BODY, headers=headers)
    assert response.status_code == 200
    assert response.json()["system"]["eco"] is True


def test_listed_name_is_accepted_when_a_proxy_changes_the_host():
    assert server.write_allowed("https://heating.local", "application/json", "192.168.1.20:8080", {"heating.local"})
    assert not server.write_allowed("https://evil.example.com", "application/json", "192.168.1.20:8080", {"heating.local"})


@pytest.mark.parametrize(
    "path, body",
    [
        ("/api/rooms/1/target", {"temp": 99}),
        ("/api/rooms/1/boost", {"minutes": 7}),
        ("/api/rooms/1/mode", {"mode": "off"}),
        ("/api/rooms/hold", {"rooms": [], "temp": 20, "minutes": 60}),
    ],
)
def test_bad_values_are_refused(client, path, body):
    assert client.post(path, json=body).status_code == 422


def test_unknown_room_is_not_found(client):
    assert client.post("/api/rooms/99/target", json={"temp": 20}).status_code == 404


def test_a_write_returns_the_new_state(client):
    room = next(r for r in client.post("/api/rooms/1/target", json={"temp": 21.5}).json()["rooms"] if r["id"] == 1)
    assert room["target"] == 21.5


def test_room_order_route_is_not_taken_for_a_room(client):
    rooms = client.put("/api/rooms/order", json={"order": [2, 1]}).json()["rooms"]
    assert [r["id"] for r in rooms][:2] == [2, 1]


def test_live_updates_send_the_state(client):
    server.store.closing = True  # ends the stream after its first message
    response = client.get("/api/events")
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.startswith('data: {"rooms"')
    assert not server.store.subscribers


def test_page_is_served_and_always_revalidated(client):
    page = client.get("/")
    assert "<title>Heating</title>" in page.text
    assert page.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("path", ["/app.js", "/schedule.mjs"])
def test_page_scripts_have_a_type_browsers_accept_for_modules(client, path):
    assert "javascript" in client.get(path).headers["content-type"]
