Write a Python 3.13 backend for a dissolved oxygen sensor array system running on a 64-bit Debian Raspberry Pi 3B+.

The backend should broadly do 3 things, each as its own Python module:

* **Read the values and statuses of N Blue RDO oxygen sensors**, each configured with a globally unique Modbus address (e.g., 1 through 27, if N is 27). The Modbus interface is exposed via one or more RS485-to-USB converters plugged into the Raspberry Pi, with each Blue RDO sensor connected to one converter.

  - During initialization, scan the Modbus address space of each converter to determine which sensor addresses are present on each one. Log messages while scanning, when sensors are found, and when done; API calls made during scanning should return an informative error.
  - Structure this module into roughly 3 files:
    1. A high-level management API with functions to scan all buses, save sensor placement per bus, and query all sensors.
    2. A Modbus wrapper per RS485-to-USB converter that blocks asynchronous requests until the previous response is received, since RS485 is half-duplex. Use a Python Modbus library that supports asyncio with serial connections.
    3. A simple wrapper class around a Blue RDO sensor with calls to read individual registers by human-readable name (e.g., `read_dissolved_o2_percent`, `read_serial_num`), given a Modbus interface and sensor address, based on `./documentation/RDO-Blue-Manual-Modbus-Interface.md` (note that this doc is for a interfacing with a PLC, which we are not using, but the interface is the same).

* **Save current sensor values to a SQLite DB** at a regular interval (set in the config file): temperature, % dissolved O2, partial pressure, mg/L, and status number/data quality/error code(s) as a human-readable string, plus the current UTC timestamp and a monotonically incrementing row index/counter. Unreadable sensor values should use a named constant `-9999` in the DB.

* **Serve a REST-style HTTP/TCP API** returning JSON (unless otherwise specified), using aiohttp with aiohttp-apigami for documentation:

  * `GET /` — basic HTML page describing the system, linking to the API docs.
  * `GET /api/docs` — Swagger-style API docs via aiohttp-apigami.
  * `GET /api/sensors/current` — triggers a fresh, lock-guarded poll of all configured sensors (does NOT write a DB row); returns a JSON list of per-sensor readings using the same schema as stored rows, including `-9999`/error status for unreachable sensors. Must complete within `config.poll_timeout_seconds` or return 504.
  * `GET /api/data` — saved data between optional `start`/`end` UTC unix timestamps (both inclusive) as JSON.
  * `GET /api/data/csv` — same as above, but CSV-formatted. If no range is given, returns all data.
  * `DELETE /api/data` — deletes data before a required `cutoff` UTC timestamp.
  * `GET /api/config` — current config file contents.
  * `PUT /api/config` — validates and persists new config JSON, then triggers a full process restart (exit code handled by `systemd Restart=always` ). This keeps lifecycle simple: one way to start, one way to stop.
  * `GET /api/scan` — scans the RS485-to-USB converters for Modbus addresses and returns which addresses are present on each converter (same underlying function used at startup) - save this mapping to the config - overwriting any existing mapping in the config.

  All endpoints should honor a configurable timeout, returning HTTP 504 if exceeded. Limit concurrency to 5 simultaneous API clients via an `asyncio.Semaphore(5)` implemented as `@web.middleware` in a dedicated `api/middleware.py` (not inline in `main.py`); requests beyond capacity get HTTP 503 with a `Retry-After` header rather than being silently dropped or queued indefinitely.

`main.py` should start the asyncio loop, read the json config file, and manage the lifecycle of each module. Configuration parameters include:

- Flat list of stable device identifiers for RS485-to-USB converters e.g "/dev/serial/by-id/..."
- (optional) mapping of RS485-to-USB  converter to Blue RDO sensor address
 - boolean of whether to run a Modbus scan at startup or use the config mapping of sensors and assume they should exist
 - sensor query interval,
 - Network timeout(s),
 - Sensor timeout(s),
 - Required free disk space to keep before deleting & flushing oldest records in batches of 50 until disk space returns to normal.
 - Interval between disk space checks.

**Testing:** Write Python unit tests in a separate test folder, along with a mock implementation of the Blue RDO Modbus interface that returns valid values. Write integration tests for API calls using mocked Blue RDO sensors, but a real (separate, test-only) SQLite DB and real network calls to the API. Examples: verify `/api/sensors/current` returns correct mocked values; run with a config using a small interval and confirm the DB contains the expected number of correct rows after a suitable runtime; verify the date-range GET and DELETE endpoints work correctly.

## Notes

- Config file and SQLite DB live in `~/SMORES_Data`.
- Dedicated config module with a typed config schema (pydantic).
- Dedicated `models`/`schemas` module of dataclasses/pydantic models shared by all three subsystems — single source of truth, easy to serialize to JSON/CSV/DB row.
- Dedicated DB module based on aiosqlite (pure CRUD, per schema), plus a separate retention module (disk-space-based pruning policy). Periodic polling/saving of sensor data lives at a higher level, not inside the DB module.
- Dedicated Blue RDO constants file based on the documentation's register map. Clearly mark any assumed/unconfirmed register addresses, and keep them easy to patch without touching logic code.
- The Blue RDO class should follow an abstract interface satisfied by both real and mock implementations.
- No part of the system should block the asyncio loop from running other tasks concurrently. If this seems unavoidable without threads or given the current design, pause and ask the user.
- Add explicit signal handlers in `main.py` calling `.close()`/`.aclose()` on all subsystems, since serial ports, DB connections, and the aiohttp runner need clean teardown on SIGTERM (systemd sends this on stop/restart).
- Use stdlib logging to stdout with appropriate debug levels throughout.
- The periodic sampling task should use an asyncio.sleep-based loop with drift correction (`next_run += interval; sleep(max(0, next_run - now))`).
- Config should reference adapters via `/dev/serial/by-id/...` (or explicit serial-number matching), not raw `ttyUSB*` device nodes.
- Each sensor read should have an explicit timeout to avoid blocking on error. Do not retry reads — simply return the error value and/or raise an exception to be handled at a higher level.
- Modbus Scan operations must not run concurrently; a scan request received while one is in progress should error or wait and return the in-progress result.
- Sensor Queries should wait on a per-RS485 level due to the modbus half-duplex blocking implementation. E.g if a scheduled sensor reads and api read all current sensors query come in at the same time, they would interleave in a non deterministic manner, but this is ok so long as each sensor is queries and correct response is gotten to the right function that queried it because that function or blue RDO class will have an async lock on that rs485 modbus link.
- For integration tests, allow the data directory to be set via environment variables, so tests use a separate DB/config without touching the main ones.
- Add a small shared `constants.py` with global constants and exceptions (`SensorTimeoutError`, `SensorReadError`, `BusScanError`, `ConfigValidationError`), so hardware, DB, and API layers translate failures consistently (hardware failure → `-9999` + status text; API failure → structured JSON error + correct HTTP code) instead of ad hoc try/except per module.
- All public functions should have type hints; run mypy/ruff before declaring any step complete.
- There is documentation for aiohttp-apigami at ./documentation/aiohttp-apigami.README.md

## Recommended External Libraries

| Purpose                 | Library                                           | Notes                                                        |
| ----------------------- | ------------------------------------------------- | ------------------------------------------------------------ |
| Modbus RTU async        | pymodbus (>=3.x)                                  | Native asyncio serial client support                         |
| Async serial transport  | pyserial-asyncio                                  | Backing transport for pymodbus                               |
| Async SQLite            | aiosqlite                                         | Non-blocking DB access in the event loop                     |
| Config/model validation | pydantic (v2)                                     | Typed config schema, parsing, validation, defaults           |
| HTTP server             | aiohttp                                           | (already specified)                                          |
| API docs/schema         | aiohttp-apigami + marshmallow                     | Maintained aiohttp-apispec fork (py3.13-compatible); marshmallow is its schema dep |
| Disk space checks       | psutil                                            | Cross-platform free-space querying for retention policy      |
| Testing                 | pytest, pytest-asyncio, pytest-aiohttp, freezegun | Async test support, deterministic time-based test control    |
| Logging                 | stdlib logging to stdout                          | systemd/journald already captures stdout — no file rotation needed |

## Implementation Steps

Do not proceed to the next step until the user replies with an explicit approval keyword (e.g., "approved"). Each step's output should be a single reviewable diff/PR-sized change.

1. Basic Python project scaffold + dependency file(s) using pipenv + README.md including usage guide / shell commands to install deps, and run the (future) main.py file manually.
2. Write `ARCHITECTURE.md` with an explicit module tree, for what & where each external libraries will be used, config options and defaults, and a specific description of the DB data row layout. (Pause for review.)
3. Write config schema and shared data models.
4. Write function/method stubs with docstrings (no implementation) for all modules.
5. Write `main.py` lifecycle management against the stubs.
6. Write REST endpoints, schemas, and apispec docs.
7. Write SQLite storage + retention implementation.
8. Write mocks + unit/integration tests.
9. Implement the hardware layer (registers, RS485 wrapper, BlueRDO class).
10. Implement remaining stubs.
11. Run the full test suite from clean slate in verbose mode - run it 5 times to verify tests leave no leftovers and are not flaky. Also run unit and integration tests separately from a clean slate to verify unit tests don't depend on integration test state and visa versa.
12. Write systemd unit with data path env vars set to `~/SMORES_Data`, user pi. Add install & operate instructions to README.md (including systemd install & journalctl usage, Where to find live api docs, Basic API query example for CSV in date range, and csv output format example, status code meanings).
13. Add a separate service

**Definition of Done:** all tests pass, ruff/mypy clean, `ARCHITECTURE.md` matches implementation, systemd unit installs, runs without exiting and survives `systemctl restart`.

### Additional deployment details:
Given the following linear network topology:
Internet provided by University
|
Ethernet cable
|
Raspberry Pi (AKA "Shore-Side Computer") - used as network bridge
|
250kbps Serial Radio Modem "A"
|
Radio Link
|
250kbps Serial Radio Modem "B"
|
Raspberry Pi (AKA "Remote Bouy Computer") - Runs a webserver and a tailscale reverse proxy/vpn for remote access to this webserver. Can have static or dynamic IP

Seggest common linux included tools to run on each raspberry pi to enable a transparent link that allows the "Remote Bouy Computer" to access the internet as if it was connected to the university network directly. Add if services or startup routines are needed to persist this setup accross reboots and if/how this can successfuly resume a connection if for some reason one link in the chain becomes temprorarilly broken (Eg radio noise or reboot)

This should be a separate service from the webserver and tailscale components, ideally with only a few commands in a shell script for each raspberrry pi

Note that the serial radio modems are not yet attached to this raspberry pi, but will be RFD900x-US modems running SIK firmware with a default serial configuriation of: 57600 baud • No parity • 8 data bits • 1 stop bit However the baud rate should be configurable (eg by env var) in the implemenation
