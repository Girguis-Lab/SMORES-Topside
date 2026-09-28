# SMORES-Topside

Backend for a dissolved oxygen sensor array: reads N In-Situ Blue RDO sensors
over Modbus RTU (RS485-to-USB), logs readings to a local SQLite database on a
schedule, and serves a REST/JSON API (aiohttp) for current readings, history,
and configuration.

Target platform: 64-bit Debian (Raspberry Pi OS "trixie") on a Raspberry Pi
3B+, Python 3.13.

See [AGENTS.md](AGENTS.md) for the full functional spec and implementation
plan, and [ARCHITECTURE.md](ARCHITECTURE.md) for the module layout, config
schema, and DB row layout.

> [!note]
> AI was used extensively in the development of this software.

## Initial setup of a fresh Raspberry Pi

This is the whole path from a blank SD card to a Pi running the backend on
boot, reachable both on the local wire and from anywhere. Do it in order; the
later steps assume the user `pi`, a working network, and the repo cloned to
`/home/pi/SMORES-Topside`.

### 1. Flash the SD card with Raspberry Pi Imager

Install [Raspberry Pi Imager](https://www.raspberrypi.com/software/) on your
laptop, insert the SD card (16 GB or larger), and choose:

- **Raspberry Pi Device:** Raspberry Pi 3
- **Operating System:** Raspberry Pi OS (64-bit) — the Debian 13 "trixie"
  build. The 64-bit image matters: this project targets 64-bit Debian and
  Python 3.13.
- **Storage:** your SD card

Click **Next**, then **Edit Settings** when Imager offers to customise the OS,
and fill in:

| Tab | Setting | Value |
| --- | --- | --- |
| General | Set hostname | `smores-top` (anything, but it becomes `<hostname>.local` on the LAN) |
| General | Set username and password | username **`pi`**, and a password you'll remember |
| General | Set locale settings | your timezone and keyboard layout |
| General | Configure wireless LAN | optional — this project uses Ethernet, so you can leave it off |
| Services | Enable SSH | **Use password authentication** (or paste a public key if you'd rather log in by key only) |

The username genuinely has to be `pi`. Raspberry Pi OS no longer creates a
default user, and the systemd unit, the data directory
(`/home/pi/SMORES_Data`), and the virtualenv path in
[deploy/smores-topside.service](deploy/smores-topside.service) are all written
against `/home/pi`.

Write the image, put the card in the Pi, plug in Ethernet, then power it up.
First boot takes a minute or two while the filesystem is resized.

### 2. Connect Ethernet to an internet-facing network

For setup, plug the Pi into a LAN that has a DHCP server and a route to the
internet — a lab switch, an office jack, or a home router. The Pi needs
internet access exactly once, to install Tailscale and this project's
dependencies; after that it only needs to be reachable.

From your laptop on the same network, log in by hostname (mDNS/avahi is
running, so `.local` works without knowing the address):

```bash
ssh pi@smores-top.local
```

If `.local` doesn't resolve (common on corporate networks that block mDNS),
find the address on your router's client list, or get it from the Pi's own
console with `hostname -I`.

Confirm the Pi can reach the internet and take the pending updates:

```bash
ping -c3 deb.debian.org
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

### 3. Serial console access from another computer

Worth wiring up before you need it: a USB-to-TTL adapter gives you a login
prompt even when the network is broken, the static IP is misconfigured, or the
Pi doesn't finish booting. It's the one path that doesn't depend on anything
this README changes.

You need a **3.3 V** USB-to-serial adapter (CP2102, FT232R, PL2303 and similar
all work). A 5 V adapter can damage the Pi's GPIO pins — check the jumper if
yours has one.

Wire three jumpers to the Pi's 40-pin header, with the Pi powered off:

| Adapter pin | Pi header pin | Pi function |
| --- | --- | --- |
| GND | pin 6 | GND |
| RX | pin 8 | GPIO14 / TXD |
| TX | pin 10 | GPIO15 / RXD |

TX and RX cross over — the adapter's receive line goes to the Pi's transmit
pin. Leave the adapter's 5 V/3.3 V wire **disconnected**; the Pi runs from its
own power supply, and back-feeding it through the header bypasses the
protection circuitry.

Then open the port on the other computer at **115200 baud, 8N1**:
Note the specific device after `/dev/` is unique to your serial converter and can be found by typing `ls /dev/` with and without the serial adapter plugged in and looking for the new device.

```bash
# Linux
screen /dev/ttyUSB0 115200          # or: picocom -b 115200 /dev/ttyUSB0

# macOS
screen /dev/cu.usbserial-0001 115200
```

On Windows, use PuTTY: connection type **Serial**, the adapter's `COMx` port,
speed `115200`.

Press Enter and you should get `smores-top login:`. (In `screen`, quit with
Ctrl-A then K.)

The serial console is enabled by default on current Raspberry Pi OS images —
`/boot/firmware/cmdline.txt` contains `console=serial0,115200`. If yours
doesn't, run `sudo raspi-config` → *Interface Options* → *Serial Port*, answer
**Yes** to "login shell accessible over serial" and **Yes** to "serial port
hardware enabled", and reboot.

One thing to keep straight: the RS485-to-USB converters for the sensors also
appear as `/dev/ttyUSB*`, and so does this adapter if you ever plug it into the
Pi itself. That's why the config lists converters by
`/dev/serial/by-id/...` — those names are stable per device and never collide.

### 4. Remote access from anywhere with Tailscale

Tailscale puts the Pi on a private WireGuard network, so you can SSH in from
any of your own machines without port forwarding, a VPN concentrator, or a
public IP. This is the practical way to reach a deployed Pi that lives behind
someone else's router.

Two walkthroughs, if you'd like to watch rather than read:

- [Setting up a Raspberry Pi with Tailscale](https://youtu.be/dneNjDu4HKU?si=piz-58NNOsPLPFYX&t=954)
  (starts at the Tailscale portion, 15:54)
- [Tailscale in under 10 minutes](https://www.youtube.com/watch?v=sPdvyR7bLqI)

On the Pi:

```bash
curl -fsSL https://tailscale.com/install.sh | sh   # adds Tailscale's apt repo and installs
sudo systemctl enable --now tailscaled             # start now, and on every boot
sudo tailscale up --ssh                            # log in, and enable Tailscale SSH
```

`enable --now` is what makes this survive a reboot — installing the package
alone leaves the Pi off the tailnet after a power cycle.

`tailscale up` prints a URL. Open it in a browser (on any machine — you can
copy the URL out of the SSH session), sign in, and the Pi joins your tailnet.

`--ssh` turns on **Tailscale SSH**: the tailnet handles authentication and key
management, so you log in from another of your devices with no SSH keys or
passwords to distribute:

```bash
tailscale ssh pi@smores-top      # from any device on the tailnet
ssh pi@smores-top                # also works — Tailscale answers on port 22
```

If the node is already up and you forgot the flag, enable it after the fact
with `sudo tailscale set --ssh`. Tailscale SSH also needs to be permitted by
your tailnet's ACLs — the default "allow all" policy already does.

Two settings worth changing in the [admin
console](https://login.tailscale.com/admin/machines) while you're there:

- **Disable key expiry** on this machine. Otherwise its node key expires (180
  days by default) and the Pi silently drops off the tailnet — awkward for
  something deployed in a hard-to-reach place.
- Note its **tailnet name**. `tailscale status` shows it on the Pi; the full
  DNS name looks like `smores-top.<your-tailnet>.ts.net`, and the API is
  reachable there too:

  ```bash
  curl http://smores-top:8080/api/sensors/current      # short name, from the tailnet
  ```

### 5. Clone the repo and install dependencies

```bash
sudo apt install -y git pipenv
git clone https://github.com/Girguis-Lab/SMORES-Topside.git /home/pi/SMORES-Topside
cd /home/pi/SMORES-Topside
pipenv install --dev
```

The path matters — see [Install dependencies](#install-dependencies) below for
what this pulls in, and the systemd notes for why the unit expects the repo at
`/home/pi/SMORES-Topside`. Check it runs by hand before making it a service:

```bash
pipenv run python src/main.py     # Ctrl-C to stop
```

### 6. Give the Pi a fixed IP

So the API answers on a known address whether the Pi is on a real network or
cabled straight to a laptop:

```bash
sudo deploy/setup_static_ip.sh
sudo deploy/setup_static_ip.sh --status
```

This adds `192.168.1.55/24` to `eth0` *alongside* its DHCP address, rather
than replacing it — full explanation, laptop-side configuration, and other
options in [A fixed IP for the Pi](#a-fixed-ip-for-the-pi).

### 7. Install the service so it starts on boot

```bash
sudo cp deploy/smores-topside.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smores-topside.service
systemctl status smores-topside.service
```

Then fill in your RS485 converters in `~/SMORES_Data/config.json` and restart
— the config file is written with defaults on the first start, and lists no
converters until you do. Full details, including the virtualenv path the unit
hardcodes, serving on port 80, and reading the journal, are in [Running as a
systemd service](#running-as-a-systemd-service).

## Prerequisites

- Raspberry Pi OS / Debian 64-bit, Python 3.13 (`python3 --version`).
- `pipenv` for dependency and virtualenv management. This system's Python is
  "externally managed" (PEP 668), so install pipenv via apt rather than
  `pip install`:

  ```bash
  sudo apt install pipenv
  ```

  (Alternative if apt is unavailable: `pipx install pipenv`, or
  `pip install --user --break-system-packages pipenv`.)

## Install dependencies

From the repo root:

```bash
pipenv install --dev
```

This creates a project-local virtualenv (via pipenv's standard venv
location under `~/.local/share/virtualenvs/`) and installs both runtime and
development dependencies pinned in `Pipfile.lock`.

Runtime dependencies: `pymodbus`, `pyserial-asyncio`, `aiosqlite`,
`pydantic`, `aiohttp`, `aiohttp-apigami`, `marshmallow`, `psutil`.

Dev/test dependencies: `pytest`, `pytest-asyncio`, `pytest-aiohttp`,
`freezegun`, `mypy`, `ruff`.

## Running

Run the backend manually with:

```bash
pipenv run python src/main.py
```

Config file (`config.json`) and SQLite database (`smores.db`) live under
`~/SMORES_Data`, created automatically on first run — the config file is
written out with schema defaults if it doesn't exist yet. Set
`SMORES_DATA_DIR` to use a different directory (the integration tests do
this to keep off the real one).

Stop it with Ctrl-C or `SIGTERM`; both run the same graceful shutdown, which
is also what `PUT /api/config` triggers so the process comes back up under
the new config.

### The API is up before the sensors are

The HTTP listener starts *first*, and opening the serial ports and
establishing the sensor mapping (a bus scan, if `scan_on_startup` is set)
happens in the background — a full-range scan takes minutes (see below), and
the backend answering during it is more useful than a refused connection.
While the mapping is still being built, `GET /api/sensors/current` and
`GET /api/data` behave differently on purpose:

- `/api/sensors/current` returns `503` with `{"error": "Bus scan in
  progress", ...}` — there is nothing to poll yet.
- `/api/data` works immediately: it reads the database, which is open before
  the listener starts.

If sensor startup fails outright (e.g. a `/dev/serial/by-id/...` path that
isn't plugged in), the process does **not** exit — that would just restart-
loop under systemd. It logs the failure, keeps serving that same `503`, and
waits for you to fix `serial_port_devices` with `PUT /api/config` or retry
with `GET /api/scan`.

### Bus scans take as long as the address range you give them

With `scan_on_startup: true`, startup probes every Modbus address in
`[scan_min_address, scan_max_address]` on every converter. An address with
nothing on it can only be ruled out by letting its probe time out, and each
address is probed up to twice (the first probe doubles as the instrument
wake-up the vendor doc requires). So the worst case is roughly:

```
2 x (scan_max_address - scan_min_address + 1) x scan_probe_timeout_seconds
```

The defaults (`1`-`247`, 1 s) therefore allow up to ~8 minutes. Set
`scan_max_address` to the highest address actually installed — e.g. `27` for
a 27-sensor array, which brings the same scan down to under a minute — or
leave `scan_on_startup: false` and let the saved `sensor_mapping` be used
as-is, re-scanning on demand with `GET /api/scan`. The startup log states
the address count and the worst-case estimate, and warns when that estimate
exceeds a minute.

## Running as a systemd service

For unattended operation, install the unit shipped at
[deploy/smores-topside.service](deploy/smores-topside.service). It runs
`src/main.py` from this repo's pipenv virtualenv as user `pi`, with
`SMORES_DATA_DIR=/home/pi/SMORES_Data`, and logs to the journal.

(The unit sets `PrivateTmp=yes`, so keep `SMORES_DATA_DIR` outside `/tmp` —
under a private `/tmp` the service would silently get its own empty data
directory rather than the one you created.)

### Install

```bash
cd /home/pi/SMORES-Topside
sudo cp deploy/smores-topside.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smores-topside.service
systemctl status smores-topside.service
```

`enable --now` both starts it immediately and makes it come back on boot.

The unit's `ExecStart` hardcodes this repo's virtualenv interpreter,
`/home/pi/.local/share/virtualenvs/SMORES-Topside-<hash>/bin/python`. That
hash is a digest of the absolute path to `Pipfile`, so it stays correct
across `pipenv install` re-runs — but not if the repo moves. If you
relocated the repo or use a different virtualenv, re-point it after copying:

```bash
sudo sed -i "s|^ExecStart=.*|ExecStart=$(pipenv --venv)/bin/python $(pwd)/src/main.py|" \
  /etc/systemd/system/smores-topside.service
sudo systemctl daemon-reload && sudo systemctl restart smores-topside
```

Opening `/dev/ttyUSB*` requires membership in the `dialout` group. The unit
sets `SupplementaryGroups=dialout` so this holds regardless, but if you also
want to run the backend by hand as `pi`, check with `id pi` and add it if
missing (`sudo usermod -aG dialout pi`, then log out and back in).

### First start: point it at your converters

`serial_port_devices` defaults to empty, so a fresh install has no sensors to
read — `/api/sensors/current` returns an empty list and the sampler writes no
rows. On its first start the backend writes `~/SMORES_Data/config.json` with
schema defaults; fill in your converters and restart. List them with:

```bash
ls -l /dev/serial/by-id/
```

Then either edit the file and restart the service:

```bash
nano ~/SMORES_Data/config.json     # set serial_port_devices, scan_max_address
sudo systemctl restart smores-topside
```

...or push the whole config over the API, which validates it, persists it,
and restarts the process for you (see `PUT /api/config` below).

Set `scan_max_address` to the highest Modbus address actually installed while
you're in there: the default of 247 makes every startup scan take ~8 minutes
(see [Bus scans take as long as the address range you give
them](#bus-scans-take-as-long-as-the-address-range-you-give-them) above).

### Serving on port 80

The listen address and port come from the config file, not the unit:

```json
{ "api_host": "0.0.0.0", "api_port": 80 }
```

Ports below 1024 are privileged, and the service runs as `pi`, not root. The
shipped unit covers this with

```ini
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
```

so `"api_port": 80` needs nothing beyond the config change — set it with
`nano ~/SMORES_Data/config.json && sudo systemctl restart smores-topside`, or
push it with `PUT /api/config` (which restarts the process itself). The API
then answers on plain `http://<pi>/`, with the docs at
`http://<pi>/api/docs`. Nothing else may already be listening on port 80 —
check with `sudo ss -ltnp '( sport = :80 )'` first; on a Pi image with a web
server installed, `sudo systemctl disable --now apache2` (or `nginx`,
`lighttpd`) frees it.

`AmbientCapabilities=` grants only the right to bind low ports, not root:
systemd hands the capability across the switch to `pi`, and
`CapabilityBoundingSet=` caps the process at that one capability for its
whole lifetime. If you never serve below port 1024, both lines can be
deleted.

Two things to know if you go off this path:

- **If the unit lacks the capability** (an older copy in
  `/etc/systemd/system/`, or the lines removed) **and the config asks for port
  80**, the bind fails with `Permission denied` at startup and `Restart=always`
  retries it every 2 s indefinitely. `journalctl -u smores-topside -n 20`
  shows the `PermissionError`. Fix it by re-copying the unit
  (`sudo cp deploy/smores-topside.service /etc/systemd/system/ && sudo
  systemctl daemon-reload`), or by editing `api_port` back to 8080 in
  `~/SMORES_Data/config.json` — the API is unreachable in this state, so the
  config has to be corrected on disk.
- **Running it by hand** (`pipenv run python src/main.py`, no systemd) gets no
  capability, so a config with `api_port` 80 fails the same way. For manual
  runs either keep the port at 8080, or lower the privileged range once per
  boot with `sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80`.

### Operate

```bash
sudo systemctl status smores-topside          # state + last few log lines
sudo systemctl restart smores-topside         # graceful SIGTERM, then start
sudo systemctl stop smores-topside
sudo systemctl disable --now smores-topside   # stop, and don't start at boot
```

The unit is `Restart=always`, deliberately not `Restart=on-failure`:
`PUT /api/config` saves the new config and then exits through the same
SIGTERM shutdown path as a normal stop, so it exits **0** and systemd still
has to bring it back for the new config to take effect. `on-failure` would
leave the service dead after every config change.
`StartLimitIntervalSec=0` disables systemd's start rate limit for the same
reason — repeated config pushes must not latch the unit into `failed`.

A missing or unplugged serial adapter is *not* fatal (see [The API is up
before the sensors are](#the-api-is-up-before-the-sensors-are)), so
`Restart=always` doesn't turn a hardware fault into a restart loop; the
service stays up serving `503` from `/api/sensors/current` and logs the
reason.

### Logs

Everything logs to stdout, which systemd captures into the journal under the
identifier `smores-topside`:

```bash
journalctl -u smores-topside -f                     # follow live
journalctl -u smores-topside -n 200                 # last 200 lines
journalctl -u smores-topside -b                     # since this boot
journalctl -u smores-topside --since "1 hour ago"
journalctl -u smores-topside -p warning             # warnings and worse only
journalctl -u smores-topside --since today > smores-today.log
```

For per-register Modbus traffic and per-address scan probes, set
`"log_level": "DEBUG"` in `config.json` and restart.

## A fixed IP for the Pi

The Pi has to be reachable in two quite different situations:

- **Plugged into a real network** — a lab or building LAN with a router and a
  DHCP server, which hands out whatever address it likes and will not honour
  one we picked.
- **Plugged into a laptop** — one Ethernet cable, no router, no DHCP server,
  the other end configured by hand. Nothing will assign the Pi an address at
  all, so without help it ends up with no usable IPv4 address and the API is
  unreachable.

The fix is to give `eth0` a second, fixed address **in addition to** the DHCP
one rather than instead of it. An interface can hold as many IPv4 addresses as
you like; they are not exclusive. `api_host` defaults to `0.0.0.0`, so the
aiohttp server already accepts connections on every address the machine has —
the DHCP one keeps working exactly as before, and `192.168.1.55` is there too
whether or not a DHCP server ever answers.

[deploy/setup_static_ip.sh](deploy/setup_static_ip.sh) sets this up:

```bash
sudo deploy/setup_static_ip.sh
```

That adds `192.168.1.55/24` to the NetworkManager profile that owns `eth0`,
alongside its existing `ipv4.method=auto`. The change is written to the
profile's keyfile in `/etc/NetworkManager/system-connections/`, so it survives
reboots and cable swaps with no service or timer of its own. It's applied with
`nmcli device reapply`, which merges the new address in without taking the
link down — an SSH session over `eth0` stays up.

A manual address on its own is *not* enough, though, and the script sets three
more properties to stop NetworkManager taking it away again — see [Why the
extra IPv6 and DHCP settings](#why-the-extra-ipv6-and-dhcp-settings) below.

Other modes:

```bash
sudo deploy/setup_static_ip.sh --status                  # what's configured vs. live
sudo deploy/setup_static_ip.sh --self-test               # prove it survives no DHCP
sudo deploy/setup_static_ip.sh --remove                  # undo it
sudo deploy/setup_static_ip.sh --address 10.10.0.55/24   # different address
sudo deploy/setup_static_ip.sh --interface eth1          # different interface
```

`--status` exits non-zero if the static address isn't currently on the
interface, so it works as a health check. Before making any change the script
warns if the chosen address overlaps the subnet DHCP already gave you (two
addresses on one network is never what you want here), and `arping`s to check
no other host is already using it.

### What each end of the cable needs

On the Pi, nothing beyond the script. On the laptop, configure its Ethernet
interface manually with an address in the *same* `/24` but not `.55` — e.g.
`192.168.1.10`, netmask `255.255.255.0`. Leave the gateway and DNS blank; this
link isn't a route to anywhere else. Then:

```
http://192.168.1.55:8080/api/docs
```

### Why the extra IPv6 and DHCP settings

Adding a manual address is only half the job. NetworkManager tears down an
entire activation — manual addresses included — when *every* address family
fails to configure. On a cable with no DHCP server and no IPv6 router, both
do: DHCPv4 times out, IPv6 SLAAC never sees a router advertisement, and the
device lands in `ip-config-unavailable`. The Pi answers on `192.168.1.55` for
about thirty seconds after plugging in and then silently goes dark:

```
dhcp4 (eth0): state changed no lease
device (eth0): state change: ip-config -> failed (reason 'ip-config-unavailable')
device (eth0): Activation: failed for connection 'Wired connection 1'
avahi-daemon: Withdrawing address record for 192.168.1.55 on eth0.
```

Worse, NetworkManager then gives up after four autoconnect attempts, so the
Pi stays unreachable until somebody unplugs and re-seats the cable. Three more
properties close that off, and the script sets all three:

| Property | Value | Why |
| --- | --- | --- |
| `ipv6.method` | `link-local` | Gives one address family that cannot fail. `fe80::` is derived from the interface itself — no router, no server, no timeout — so the activation always succeeds and the IPv4 addresses stay put. |
| `ipv4.dhcp-timeout` | `infinity` | IPv4 never enters the failed state at all; DHCP just keeps asking in the background. Also means moving the Pi from a bare cable onto a real network picks up a lease on its own, with no re-plug. |
| `connection.autoconnect-retries` | `0` (forever) | Last line of defence. If activation ever does fail, keep retrying instead of giving up and leaving the Pi dark. |

The cost of `ipv6.method=link-local` is that `eth0` no longer gets a global
IPv6 address on networks that offer one. For an IPv4-only sensor appliance
that is a good trade, but `--ipv6 auto` puts it back if you need it — the
script warns, because that reopens the failure above.

### Verify it without unplugging anything

A dummy interface behaves exactly like a cable with nothing on the far end:
NetworkManager runs the same IP configuration over it, DHCP requests go
nowhere, and no router advertisements ever arrive. `--self-test` uses one to
reproduce the failure and confirm the fix, without touching `eth0`:

```bash
sudo deploy/setup_static_ip.sh --self-test
```

It runs two ~30 s cases on a throwaway `smoresprobe0` device — the old
settings, which should lose the address, and the ones the script applies,
which should hold it — and deletes the device afterwards either way. If the
first case *doesn't* fail, it says so rather than claiming a pass, since then
the model isn't faithful and only the real cable can settle it.

On the real hardware:

```bash
# Pi cabled directly to the laptop, nothing else plugged in:
deploy/setup_static_ip.sh --status                 # on the Pi
curl -s http://192.168.1.55:8080/api/data | head   # from the laptop
```

Leave it connected for a few minutes before calling it good — the original bug
took about thirty seconds to bite, and a link that works immediately after
plug-in can still drop later.

### Alternatives, and why not

- **Static address only, no DHCP** (`ipv4.method=manual`). Simple and always
  predictable, but the Pi then can't get onto a building LAN at all without
  being reconfigured by hand for each one. Dual addressing costs nothing and
  keeps both.
- **`smores-top.local` over mDNS.** `avahi-daemon` is already running on this
  Pi, so `http://smores-top.local:8080` resolves on both kinds of network with
  no configuration at all, and it keeps working if the fixed address ever has
  to change. Good as a *complement* — but it needs a working mDNS resolver at
  the other end (fine on macOS and modern Linux/Windows, absent on plenty of
  embedded clients and some corporate images) and it can't be typed into
  something that only accepts an IP. It is also *not* a fallback for the
  failure above: avahi advertises whatever addresses the interface has, so
  when the activation collapsed it withdrew `192.168.1.55` and `.local`
  stopped resolving at the same moment. Use the fixed address as the
  guarantee and `.local` as the convenience.
- **IPv4 link-local (169.254.x.y).** Both ends self-assign with no
  configuration, which handles the direct-cable case — but the address is
  picked at random each time, so you're back to not knowing where the Pi is.
  NetworkManager can add one alongside everything else if you want it as a
  last resort: `sudo nmcli connection modify "Wired connection 1"
  ipv4.link-local enabled`.
- **Tailscale.** Already installed here, and its `100.x` address is stable
  across networks — but it needs the Pi to have working internet, which is
  exactly what the direct-cable case lacks. Useful for remote access, not a
  substitute for this.
- **A DHCP reservation on the router.** Gives a predictable address on one
  specific network, needs admin access to that router, and does nothing on the
  next network or on a bare cable.
- **A NetworkManager dispatcher hook** that re-adds the address by hand after
  every interface event. This is the brute-force backstop if the profile
  settings above ever stop holding on some future NetworkManager version — it
  papers over the teardown instead of preventing it, so reach for it only
  after `--self-test` says something is wrong:

  ```bash
  sudo tee /etc/NetworkManager/dispatcher.d/50-smores-static-ip >/dev/null <<'EOF'
  #!/bin/sh
  [ "$1" = "eth0" ] || exit 0
  case "$2" in up|dhcp4-change) ip addr add 192.168.1.55/24 dev eth0 2>/dev/null || true ;; esac
  EOF
  sudo chmod 755 /etc/NetworkManager/dispatcher.d/50-smores-static-ip
  ```

### Not running NetworkManager?

The script only handles NetworkManager (it checks, and refuses otherwise).
`nmcli device status` confirms which stack is in charge. On a Pi image using
`dhcpcd` instead, the equivalent goes in `/etc/dhcpcd.conf`:

```
interface eth0
static ip_address=192.168.1.55/24
```

...which on `dhcpcd` *replaces* DHCP rather than supplementing it; use
`profile static_eth0` / `fallback static_eth0` to get the fallback behaviour
instead. On `systemd-networkd`, a `.network` file with both `DHCP=yes` and an
`[Address]` section gives the same dual-address result as the NetworkManager
setup above.

## Using the API

The backend binds `api_host`:`api_port`, `0.0.0.0:8080` by default — so
`http://localhost:8080` on the Pi itself, or the Pi's LAN address from
another machine. Examples below use `localhost:8080`; adjust them if you
changed the port. Port 80 (dropping the `:8080` from every URL below) is
supported under the shipped systemd unit — see [Serving on port
80](#serving-on-port-80). For a LAN address that doesn't move, see [A fixed IP
for the Pi](#a-fixed-ip-for-the-pi).

### Live API documentation

Interactive Swagger UI, generated from the route decorators by
aiohttp-apigami, is served at:

```
http://localhost:8080/api/docs
```

The raw OpenAPI/Swagger JSON is at `/api/docs/swagger.json`, and the landing
page at `http://localhost:8080/` links to the docs.

### Endpoints

| Method and path | Purpose |
| --- | --- |
| `GET /` | HTML overview page, links to the docs |
| `GET /api/docs` | Swagger UI |
| `GET /api/sensors/current` | Fresh poll of every configured sensor; does **not** write a DB row |
| `GET /api/data` | Stored readings as JSON; optional `start`/`end` |
| `GET /api/data/csv` | The same rows, CSV |
| `DELETE /api/data` | Delete rows older than a required `cutoff` |
| `GET /api/config` | Current `config.json` contents |
| `PUT /api/config` | Validate and persist a new config, then restart the process |
| `GET /api/scan` | Re-scan every converter, save the mapping to `config.json`, return it |

`start`, `end`, and `cutoff` are UTC unix timestamps in seconds (fractional
allowed). `start`/`end` are both **inclusive**; `cutoff` is **exclusive** —
`DELETE` removes rows with `timestamp_utc < cutoff`.

### CSV export for a date range

Generate the bounds with `date -u`:

```bash
curl -s -o smores_2026-08-31.csv \
  "http://localhost:8080/api/data/csv?start=$(date -u -d '2026-08-31 00:00:00' +%s)&end=$(date -u -d '2026-08-31 23:59:59' +%s)"
```

Pass only one bound for an open-ended range, or neither to export every
stored row:

```bash
curl -s -o smores_all.csv http://localhost:8080/api/data/csv
curl -s "http://localhost:8080/api/data/csv?start=$(date -u -d '1 hour ago' +%s)"
```

The response carries `Content-Type: text/csv; charset=utf-8` and
`Content-Disposition: attachment; filename="smores_data.csv"`, so a browser
hitting the same URL downloads a file.

Other useful calls:

```bash
curl -s http://localhost:8080/api/sensors/current | python3 -m json.tool
curl -s http://localhost:8080/api/config > my_config.json
curl -s -X PUT -H 'Content-Type: application/json' \
  --data @my_config.json http://localhost:8080/api/config      # restarts the process
curl -s -X DELETE "http://localhost:8080/api/data?cutoff=$(date -u -d '30 days ago' +%s)"
curl -s http://localhost:8080/api/scan | python3 -m json.tool   # can take minutes
```

### CSV output format

A header row followed by one row per stored reading, in this column order
(the second row below is a healthy sensor, the third an unreachable one):

```csv
row_id,sensor_address,serial_converter_id,timestamp_utc,temperature_c,do_percent_saturation,do_partial_pressure_torr,do_mg_l,status_code,status_text
1,1,/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A5XK3RJT-if00-port0,1756598400.0,21.437,98.62,157.04,8.712,0,OK
2,2,/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A5XK3RJT-if00-port0,1756598400.0,-9999.0,-9999.0,-9999.0,-9999.0,-1,Sensor timeout
```

| Column | Meaning |
| --- | --- |
| `row_id` | Monotonically incrementing DB row counter (`INTEGER PRIMARY KEY AUTOINCREMENT`); empty for `/api/sensors/current`, which doesn't store a row |
| `sensor_address` | The sensor's globally unique Modbus address |
| `serial_converter_id` | `/dev/serial/by-id/...` path of the converter it was read through |
| `timestamp_utc` | Unix epoch seconds, UTC, when the reading was taken |
| `temperature_c` | Degrees Celsius |
| `do_percent_saturation` | Dissolved O2, % saturation |
| `do_partial_pressure_torr` | Dissolved O2 partial pressure, torr |
| `do_mg_l` | Dissolved O2 concentration, mg/L |
| `status_code` | Numeric status; see below |
| `status_text` | Human-readable status |

Any of the four measurement columns that could not be read holds **`-9999`**
(`constants.UNREADABLE_VALUE`) rather than a blank or `null`, so the column
stays numeric in every consumer. `/api/data` returns exactly these fields as
JSON, with `row_id: null` in place of an empty `row_id`.

### `status_code` meanings

`status_code` is the *worst* outcome across the sensor's four parameters, and
`status_text` is the per-parameter detail joined with `; ` (for example
`Temperature: Timed out reading parameter; DO Concentration: Modbus read
error`). Non-negative codes are Data Quality IDs reported by the instrument
itself; negative codes are this backend's own.

| Code | `status_text` | Meaning |
| --- | --- | --- |
| `0` | `OK` | Every parameter read cleanly |
| `3` | `...: Error reading parameter` | Instrument reported Data Quality ID 3 for that parameter |
| `5` | `...: RDO Cap expired` | The RDO sensing cap is past its service life — replace it; readings are suspect |
| other `> 0` | `...: Unknown data quality id N` | A Data Quality ID the vendor doc doesn't enumerate. In-Situ's guidance is to contact their technical support |
| `-1` | `Sensor timeout` | The instrument never answered; all four values are `-9999` |
| `-2` | `...: Timed out reading parameter` | Some (not all) parameters timed out while the instrument kept answering others |
| `-3` | `...: Modbus read error` | The instrument answered with a Modbus exception, or a short/malformed reply |

A `-1` row still gets written every sampling interval, so a dead or
unplugged sensor shows up as a continuous run of `-9999`s in the data rather
than as a gap.

### HTTP status codes

| Code | When |
| --- | --- |
| `200` | Success |
| `400` | Bad query parameter (`start`/`end`/`cutoff` not a number, `cutoff` missing) or a `PUT /api/config` body that fails schema validation. Body is `{"error": ..., "detail": ...}` |
| `404` / `405` | Unknown path / wrong method for that path |
| `500` | Internal error (also returned if `config.json` can't be written) |
| `503` | Either **at capacity** — more than `api_max_concurrent_clients` (default 5) requests in flight, with a `Retry-After: 1` header, never queued — or **not ready**: `/api/sensors/current` and `/api/scan` while a bus scan is in progress or before the sensor mapping is established. The `error` field distinguishes them |
| `504` | The handler exceeded its timeout: `poll_timeout_seconds` for `/api/sensors/current`, the scan worst-case estimate for `/api/scan`, `api_request_timeout_seconds` for everything else |

## Development

Run tests:

```bash
pipenv run pytest
```

Lint and type-check (settings for both live in `pyproject.toml`, including
`mypy_path`/`explicit_package_bases` so intra-`src` imports resolve as the
top-level modules they're written as, e.g. `config.schema` not
`src.config.schema`):

```bash
pipenv run ruff check .
pipenv run mypy src tests
```

Or activate the virtualenv directly instead of prefixing every command with
`pipenv run`:

```bash
pipenv shell
```

## Project layout

```
src/                    Application source (Python package, imported as top-level modules)
  main.py               Entry point: config load, subsystem lifecycle, signal handlers
  constants.py          UNREADABLE_VALUE + shared exception classes
  sampler.py            Periodic sensor-poll-to-DB task (drift-corrected loop)
  config/
    schema.py           Config pydantic model (typed schema, defaults, validation)
    loader.py           load_config/save_config (atomic write via temp file + os.replace)
  models/
    readings.py         SensorReading, ScanResult pydantic models (shared DB/API/hardware shape)
  hardware/              Blue RDO sensor array subsystem (implemented)
    rdo_blue_constants.py  Register map transcribed from the vendor doc
    rdo_blue_interface.py  BlueRDOInterface abstract base
    rdo_blue.py            BlueRDOSensor (register decoding, per-parameter status)
    modbus_bus.py          ModbusBus (per RS485-to-USB converter: half-duplex
                           request lock, no retries, instrument wake-up)
    manager.py             SensorManager (scan/query high-level API)
  db/                    SQLite storage subsystem (implemented)
    database.py            Database CRUD
    retention.py           Disk-space-based pruning policy
  api/                   REST/JSON HTTP API subsystem (implemented)
    app.py                 aiohttp Application factory
    middleware.py          Concurrency limit + per-route timeout middleware
    routes.py              Endpoint handlers
    schemas.py             marshmallow schemas for aiohttp-apigami
tests/                  Unit and integration tests (mocked sensors + real SQLite/HTTP)
  mocks/mock_rdo_blue.py  Configurable fake BlueRDOInterface (canned values,
                          simulated per-parameter/whole-sensor faults)
  unit/                   Per-module tests
  integration/            Full aiohttp app + real DB + mocked sensors, plus a
                          test that runs main.run() end to end (real port,
                          real SIGTERM)
deploy/
  smores-topside.service  systemd unit (user pi, SMORES_DATA_DIR=/home/pi/SMORES_Data,
                          Restart=always, CAP_NET_BIND_SERVICE so api_port
                          can be 80)
  setup_static_ip.sh      Adds a persistent static IPv4 address to eth0
                          alongside DHCP, so the API is reachable at a known
                          address on a LAN and on a bare cable alike
documentation/          Vendor docs (Blue RDO Modbus register map, etc.)
Pipfile / Pipfile.lock  Dependency manifest (pipenv)
pyproject.toml          ruff/mypy configuration
AGENTS.md               Functional spec and implementation plan for this project
ARCHITECTURE.md         Module tree, config schema, and DB row layout
```
