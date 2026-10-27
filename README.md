# Wiser web UI

A small web app to control a Drayton Wiser heating system from any browser, without the phone app.
One Python container reads your Wiser Heat Hub over your home network and serves both the page and a JSON API.
Your hub secret stays on the server and never reaches the browser.

**What you can do:** see every room's temperature, change its target, switch it between schedule and manual, boost it for 30 minutes to 3 hours, boost several rooms at once to the same temperature (which the Wiser app can't do), check the signal and battery of each room's valves and thermostat, and turn away mode and eco mode on or off. The page updates itself, including when someone changes something in the Wiser app.

## Try it first (no hub needed)

```bash
cp .env.example .env
# edit .env: set WISER_MOCK=1
docker compose up --build
```

Open `http://<nas-address>:8080`. You'll see five demo rooms and a banner saying it's demo data. Nothing is sent to a real hub.

## Connect it to your hub

1. **Give the hub a fixed address.** In your router, reserve a DHCP address for the Wiser hub.
2. **Get the hub secret.** From the community docs: press the setup button on the hub until its light flashes, join the temporary `WiserHeat…` Wi-Fi network from a computer, then open `http://192.168.8.1/secret` and copy the string it returns. Press the button again to return the hub to normal. While it's in setup mode the phone app won't work, so do this when nobody needs the heating.
3. **Edit `.env`:** set `WISER_HUB_HOST` to the hub's address, paste the secret into `WISER_SECRET`, set `WISER_MOCK=0`, and set `WISER_UI_PASSWORD` to something you'll remember.
4. `docker compose up -d --build`

`.env` holds your secret. Don't commit it or share it.

## Settings (in `.env`)

| Setting | Meaning |
|---|---|
| `WISER_HUB_HOST` | Hub address on your network |
| `WISER_SECRET` | The hub's secret |
| `WISER_UI_PASSWORD` | Optional. Asks for this password in the browser (any username) |
| `WISER_ALLOWED_HOSTS` | Optional. Comma-separated host names you open the page by, such as `nas.lan,heating.local`. Addresses like `192.168.1.20` and `localhost` always work |
| `WISER_POLL_SECONDS` | How often the hub is read while a page is open. Default 20, minimum 5. With no page open the hub isn't polled; `GET /api/state` then reads it on demand. A page lets go 30 seconds after its tab is hidden, or after 15 minutes with no mouse, touch or key use, and picks up again when you come back. For a wall tablet, tick **Keep live on this device** at the bottom of the page (or open it as `/?keep-live`) |
| `WISER_BOOST_INCREASE` | Degrees a boost adds. Default 2 |
| `WISER_MOCK` | `1` runs demo data with no hub |
| `WISER_DATA_DIR` | Where saved settings (the room order) go. Default `data` next to where you start the server; the Docker image uses `/srv/data`, kept in the `wiser-data` volume |

## Boosting several rooms

**Boost rooms…** above the rooms heats the rooms you pick to one temperature for 30 minutes to 3 hours: say the bedroom at 15° and the living room at 16°, both to 20° for 2 hours. A normal boost adds a fixed amount to each room's own target, so the rooms would end up at different temperatures.

Each room gets a timed hold, so its card says "Held at 20.0° until 15:30" and it goes back to its schedule (or manual setting) by itself when the time is up. The ✕ next to that line ends it early for one room.

## Signal strength

The bars at the top of each room card show the signal of the room's **weakest** device, since one valve dropping out is what you'd want to spot. They turn red when it's poor. Tap them to see every valve and thermostat in the room with its own signal, and the received power in each direction: "Hub hears −66 · it hears −64 dBm". Closer to 0 dBm is stronger.

The list also shows what each device is connected to. Wiser devices use Zigbee, a mesh network, so a valve far from the hub can reach it through another device, such as a smart plug or a room thermostat: "Connected through Landing smart plug". For a device connected that way, the dBm readings are shown without saying who hears whom, as it isn't known whether they're of that link or of the hub.

The bars follow the hub's own rating, as in the Wiser app: 4 for "Very good", 3 for "Good". Those two have been seen on a real hub. The weaker ratings are expected to be "Medium" (2), "Poor" (1) and "No signal" (0), but those names are from memory, not confirmed. A device whose rating isn't one of these shows the hub's word as it is, and gets its bars from its radio reading instead: 4 from −62 dBm, 3 from −75, 2 from −85, 1 below that. A valve with a poor signal can miss commands; moving the hub, or adding a Wiser smart plug between them as a repeater, usually helps.

## Batteries

The device list also shows each battery-powered device's battery: "Battery 80% · 2.9 V". A room card shows **Low battery** when any of its devices is at 20% or less (or the hub calls its battery low); tap it to see which. Mains-powered devices, such as smart plugs, have no battery line.

The percentage is worked out from the voltage the way aioWiserHeatAPI does it: room thermostats run from 1.7 V (empty) to 2.7 V (full); radiator valves use a table from 3.0 V (100%) down to 2.3 V (0%). A device that reports no voltage uses the hub's level instead: Normal 100%, Two thirds 66%, One third 33%, Low 10%.

## Arranging rooms

Press **Arrange rooms** at the bottom of the page and use the arrows to set the order. The order is saved on the server, so every phone and computer shows the same one, and other open pages rearrange straight away. A room added to the hub later appears at the end.

The order lives in `settings.json` in the data folder. With Docker it's kept in the `wiser-data` volume, so it survives rebuilds and updates; `docker compose down -v` deletes it.

## The image

A ready-made image for Intel/AMD and ARM is published at **`ghcr.io/howwie/wiser-webui`**. GitHub Actions ([`.github/workflows/image.yml`](.github/workflows/image.yml)) runs the tests and, if they pass, builds and publishes it:

| When | Tags |
|---|---|
| Every push to `main` | `latest`, and `sha-<commit>` to pin an exact build |
| A version tag such as `v1.2.0` | `1.2.0` and `1.2` |

To go back to an earlier build, use its `sha-…` or version tag instead of `latest` (tags are listed under the repository's **Packages**).

The image holds only the app code. `.env` (your secret) and saved settings are excluded by `.dockerignore`.

**Building your own** (for example from a fork), on any machine with Docker:

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t <you>/wiser-webui:latest --push .
```

## Running on a Synology NAS

Needs DSM 7.2 or later with **Container Manager** (Package Center).

1. In **File Station**, create a folder `docker/wiser-webui`. Upload your `.env` into it. (It holds the hub secret: keep it here, not in the compose file.)
2. In **Container Manager → Project → Create**: name it `wiser-webui`, set the path to that folder, choose **Create docker-compose.yml**, and paste:

   ```yaml
   services:
     wiser-webui:
       image: ghcr.io/howwie/wiser-webui:latest
       container_name: wiser-webui
       env_file: .env
       ports:
         - "8080:8080" # change the left number if 8080 is taken on the NAS
       volumes:
         - wiser-data:/srv/data # the room order
       restart: unless-stopped

   volumes:
     wiser-data:
   ```

   `wiser-data` is a named volume: Docker stores it on the NAS for you, with the right owner, and keeps it across updates. To keep the file in a NAS folder instead, see [Saving to a NAS folder](#saving-to-a-nas-folder-instead) below.
3. Finish the wizard. Open `http://<nas-address>:8080`.

### Saving to a NAS folder instead

A NAS folder lets you see `settings.json` in File Station and include it in Hyper Backup. It only holds the room order, so this is optional.

The app normally runs as a limited user (ID 999) that can't write to folders DSM creates, so run it as your DSM user instead:

1. Find your user and group IDs: over SSH (Control Panel → Terminal & SNMP → Enable SSH service), run `id <your-username>`. The first user on a Synology is usually `uid=1026` and group `users` is `gid=100`, but check yours.
2. In File Station, create a `data` folder inside `docker/wiser-webui`. Create it yourself: if Docker creates it, it belongs to root and the app can't write to it.
3. In the compose file, add a `user:` line, change the volume to the folder, and delete the `volumes: wiser-data:` lines at the bottom:

   ```yaml
   services:
     wiser-webui:
       image: ghcr.io/howwie/wiser-webui:latest
       container_name: wiser-webui
       user: "1026:100" # your uid:gid from step 1
       env_file: .env
       ports:
         - "8080:8080"
       volumes:
         - ./data:/srv/data # the room order, in docker/wiser-webui/data
       restart: unless-stopped
   ```

4. Rebuild the project, then rearrange the rooms once (a new folder starts empty). `settings.json` should appear in the `data` folder. If the page says "Couldn't save the room order (Permission denied)", the IDs don't match the folder's owner.

To move an order you've already set from the named volume, just arrange the rooms again. It's quicker than copying the file out of Docker's storage.

**Opening it by a name:** if you use the NAS's name, a Tailscale name, or DSM's reverse proxy (Login Portal → Advanced → Reverse Proxy) with its own host name, add that name to `WISER_ALLOWED_HOSTS` in `.env`, then rebuild the project.

**Updating:** once a new image has been published (a few minutes after a push to `main`), in Container Manager stop the project, open **Action → Build** to pull the new image and recreate the container, and start it. The room order is kept. If you prefer SSH:

```bash
cd /volume1/docker/wiser-webui && sudo docker compose pull && sudo docker compose up -d
```

## Security

- Anyone who can reach the page can change your heating unless you set `WISER_UI_PASSWORD`. Set it.
- The password travels unencrypted over your home network. It's fine for a home LAN, but don't expose the port to the internet.
- For access away from home, use a VPN such as Tailscale or WireGuard. Don't port-forward this.
- The server only answers requests addressed to an IP address, `localhost`, or a name in `WISER_ALLOWED_HOSTS`. This stops a web page you visit from pointing its own domain at your NAS and sending your heating commands through your browser ("DNS rebinding"). If you open the page by a name, such as `http://nas.lan:8080` or a Tailscale name, add that name or you'll see "Unknown host name".
- The hub itself speaks plain HTTP, which is how every Wiser tool works.

## How sure are we about each hub call?

The Wiser local API isn't officially documented. Every call below has been checked against the source of [aioWiserHeatAPI](https://pypi.org/project/aioWiserHeatAPI/) 1.7.7, the library behind the Home Assistant Wiser integration, and sends the same thing. `tests/test_main.py` pins these payloads.

| Action | What the server sends | Matches aioWiserHeatAPI's |
|---|---|---|
| Read everything | `GET /data/domain/` with `SECRET` header | `get_hub_data` (it uses `/data/v2/domain/`; both work) |
| Room schedules ("until 22:00") | `GET /data/v2/schedules/`: `{"Heating": [...]}`, matched to rooms by `ScheduleId`; each day is `{"Time": [630, …], "DegreesC": [210, …]}`. If it fails, rooms just say "Following the schedule" | Schedule collection |
| Device signal | From the same reply: rooms list their devices (`RoomStatId`, `SmartValveIds`, `HeatingActuatorIds`, `UnderFloorHeatingId`), and each `Device` has `DisplayedSignalStrength` and `ReceptionOfController`/`ReceptionOfDevice` → `Rssi` (strength = 2 × (RSSI + 100), at most 100) | Device collection and signal helper |
| Device battery | Each `Device`'s `BatteryVoltage` (tenths of a volt) and `BatteryLevel` (`Normal`, `TwoThirds`, `OneThird`, `Low`) | Battery helper |
| What a device is connected to | Each `Device`'s `ParentNodeId` is the `NodeId` of the device it's connected to; the hub is the `Controller` | `device.parent_node_id` and `devices.get_by_node_id` |
| Boost a room | `PATCH /data/v2/domain/Room/{id}` `{"RequestOverride":{"Type":"Boost","DurationMinutes":30,"IncreaseSetPointBy":20}}` (the hub adds this to the room's current temperature) | `room.boost` |
| Set a target | `PATCH …/Room/{id}` `{"RequestOverride":{"Type":"Manual","SetPoint":215}}` (tenths of a degree) | `room.set_target_temperature` |
| Boost several rooms to one temperature | For each room: `{"RequestOverride":{"Type":"Manual","SetPoint":200,"DurationMinutes":120}}` | `room.set_target_temperature_for_duration` |
| Change the target during a boost or timed hold | Same as setting a target, plus `"DurationMinutes"` for the time left, so it ends when the boost would have (as the Wiser app does) | `room.set_target_temperature_for_duration` |
| Cancel a boost or hold | `{"RequestOverride":{"Type":"None"}}` | `room.cancel_overrides` |
| Schedule or manual mode | `{"RequestOverride":{"Type":"None"}}` if a boost or hold is on, then `{"Mode":"Auto"}` or `{"Mode":"Manual"}` | `room.set_mode` |
| Away mode | `PATCH …/System` `{"RequestOverride":{"Type":2}}` (0 to cancel); the hub uses its own away temperature | `system.set_away_mode_enabled` |
| Eco mode | `PATCH …/System` `{"EcoModeEnabled":true}` | `system.set_eco_mode_enabled` |

### Why not use aioWiserHeatAPI itself?

The app has its own small hub client (`app/hub.py`) and only copies the library's payloads. Using the library directly would mean:

- **More load on the hub.** Each update reads five endpoints (domain, network, schedules, status and OpenTherm); this app reads two. With a poll every 20 seconds while a page is open, that adds up on a small device.
- **Behaviour we'd have to work around.** It retries every request up to 5 times, writes included (this app never retries a write, so a slow reply can't apply a change twice). It also writes its own config file when a temperature is set in manual mode, and calls itself "HomeAssistant" to the hub.
- **A rewrite, not a swap.** The page's data, the demo hub and most tests are built on a plain read-and-patch interface. Moving to the library's object model would mean redoing them, and adding `aiohttp` next to `httpx`.
- **Much more than we use.** It also covers hot water, lights, shutters, smart plugs, automations and schedule editing.

What we give up:
- **HTTPS for newer hubs.** The library switches to HTTPS for second-generation hubs on firmware 4.42.22 or later. This app uses plain HTTP, which works on hubs today. If a firmware update ever stops HTTP from working, copying that switch is a small change.
- **Fixes when the hub's API changes,** which the library's maintainers would make.

If the app grows to hot water or schedule editing, using the library becomes worth it.

Newer hubs take writes under `/data/v2/domain`, older ones under `/data/domain`. The server tries v2 first and falls back automatically.

Away and eco are checked after every change: if the hub accepts the command but the state doesn't change, the page says so instead of pretending it worked. Every failed write also shows the hub's own error message on the page and in `docker logs wiser-webui`, along with the request that was sent. If something doesn't work on your hub, [open an issue](https://github.com/howwie/wiser-webui/issues) with that message (never include your secret); the fix is usually a one-line change in `app/main.py`.

## Troubleshooting

- **"Can't reach the hub"**: check `WISER_HUB_HOST`, and that the container can reach the hub's network. Bridge networking is fine.
- **"The hub rejected the secret"**: the secret in `.env` is wrong or has stray spaces or quotes.
- **A room shows "No reading"**: the hub has lost contact with that radiator valve or thermostat. Check its batteries.
- **"Unknown host name"**: you opened the page by a name the server doesn't know. Add it to `WISER_ALLOWED_HOSTS`.
- **"The hub sent data this app couldn't read"**: the hub's reply had an unexpected shape. The full error is in `docker logs wiser-webui`; the app keeps retrying.

## API

| Method and path | Body |
|---|---|
| `GET /api/state` | |
| `GET /api/events` | live updates (server-sent events) |
| `POST /api/rooms/{id}/target` | `{"temp": 21.5}` (5 to 30, steps of 0.5) |
| `POST /api/rooms/{id}/boost` | `{"minutes": 60}` (30, 60, 120, 180; 0 cancels) |
| `POST /api/rooms/hold` | `{"rooms": [1, 3], "temp": 20, "minutes": 120}` (30, 60, 120, 180) |
| `POST /api/rooms/{id}/mode` | `{"mode": "auto"}` or `{"mode": "manual"}` |
| `POST /api/system/away` | `{"enabled": true}` |
| `POST /api/system/eco` | `{"enabled": true}` |
| `PUT /api/rooms/order` | `{"order": [3, 1, 2]}` (room ids, first to last) |
| `GET /healthz` | always open, used by Docker's healthcheck |

## Layout

```
app/main.py        server, hub polling, API
app/hub.py         hub client, mock hub, data normalising
app/static/        the page (Preact + htm, no build step)
tests/             pytest tests (pip install -r requirements-dev.txt, then pytest)
```

The page uses a vendored copy of Preact and htm (`app/static/vendor/`), so it works with no internet access. Edit the files in `app/static/` and rebuild the image to change the UI.

## License

[MIT](LICENSE). The vendored Preact (MIT) and htm (Apache 2.0) keep their own licenses, in `app/static/vendor/`.

This project isn't affiliated with or endorsed by Drayton or Schneider Electric. "Wiser" is their trademark.
