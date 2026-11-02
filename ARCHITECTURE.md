# SMORES-Topside Architecture

Originally the step 2 deliverable per `AGENTS.md`: this document fixes the
module layout, config schema, and DB row layout that the later steps
implement against, and is kept in sync as they land. All 12 steps are
implemented.

## 1. Module tree

```
src/                             Python package (root), imported as top-level modules
  __init__.py
  main.py                        Entry point: asyncio loop, config load, subsystem
                                  lifecycle (start/stop), signal handlers
  constants.py                   UNREADABLE_VALUE = -9999, the negative internal status
                                  codes (§5), MODBUS_MIN/MAX_UNIT_ADDRESS, exception
                                  classes: SensorTimeoutError, SensorReadError,
                                  BusScanError, ConfigValidationError
  sampler.py                     Periodic sampling task: drift-corrected asyncio.sleep
                                  loop that calls hardware.manager.query_all_sensors()
                                  and db.database.insert_reading() on config.sample_interval_seconds

  config/
    __init__.py
    schema.py                    Pydantic Config model + nested models (§3), defaults
    loader.py                    load_config(path), save_config(path, config) (atomic
                                  write: write to temp file + os.replace), raises
                                  ConfigValidationError on bad JSON/schema

  models/
    __init__.py
    readings.py                  SensorReading, ScanResult — pydantic models shared by
                                  hardware, db, and api layers (single source of truth
                                  for the "reading" shape; §5)

  hardware/                      Blue RDO sensor array subsystem (3 core files + support)
    __init__.py
    rdo_blue_constants.py        Register map constants transcribed from
                                  documentation/RDO-Blue-Manual-Modbus-Interface.md.
                                  Every constant not directly confirmed by that doc is
                                  tagged `# ASSUMED` at its definition.
    rdo_blue_interface.py        BlueRDOInterface(abc.ABC): read_dissolved_o2_percent,
                                  read_dissolved_o2_mg_l, read_temperature_c,
                                  read_partial_pressure_torr, read_status,
                                  read_serial_num, read_device_id, read_all()
    rdo_blue.py                  BlueRDOSensor(BlueRDOInterface): real implementation,
                                  takes a ModbusBus + integer address. Decodes
                                  multi-register values (float32 parameters, uint32
                                  serial number) with pymodbus's
                                  ModbusClientMixin.convert_from_registers (§2)
    modbus_bus.py                ModbusBus: one instance per RS485-to-USB converter.
                                  Wraps pymodbus AsyncModbusSerialClient (retries=0);
                                  serializes all requests on the port behind an
                                  asyncio.Lock (RS485 is half-duplex — a request blocks
                                  until its response arrives before the next request is
                                  sent), and owns per-address instrument wake-up (§4)
    manager.py                   High-level API: scan_all_buses(), get_sensor_mapping(),
                                  save_sensor_mapping(), query_all_sensors(),
                                  query_sensor(address). Owns the ModbusBus instances
                                  and the address→(bus, BlueRDOSensor) table. Takes an
                                  optional sensor_factory (§9) so tests can substitute
                                  mocked sensors without a real ModbusBus connection.

  db/
    __init__.py
    database.py                  aiosqlite CRUD only: init_schema(), insert_reading(),
                                  get_readings(start, end), delete_before(cutoff),
                                  delete_oldest(batch_size), count_rows()
    retention.py                 Disk-space-based pruning policy: periodic task using
                                  psutil to check free space, calls
                                  database.delete_oldest(50) in a loop until free space
                                  recovers

  api/
    __init__.py
    app.py                       aiohttp Application factory: registers routes,
                                  installs middleware, wires aiohttp-apigami
                                  (setup_aiohttp_apispec, swagger UI at /api/docs)
    middleware.py                @web.middleware concurrency_limit_middleware
                                  (asyncio.Semaphore(5), 503 + Retry-After when full)
                                  and timeout_middleware (504 on per-route timeout)
    routes.py                    Handlers for all endpoints in §6
    schemas.py                   marshmallow schemas for aiohttp-apigami request/
                                  response docs (see §4 note on two schema systems)

deploy/
  smores-topside.service         systemd unit (§10): Type=exec, User=pi,
                                  SupplementaryGroups=dialout,
                                  Environment=SMORES_DATA_DIR=/home/pi/SMORES_Data,
                                  Restart=always, StartLimitIntervalSec=0,
                                  AmbientCapabilities=CAP_NET_BIND_SERVICE
                                  (so api_port 80 works as user pi)

documentation/
  RDO-Blue-Manual-Modbus-Interface.md   (existing vendor doc)
  aiohttp-apigami.README.md             (existing vendor doc)

tests/
  __init__.py
  conftest.py                    Shared fixtures: tmp data dir via env var, mocked
                                  sensors, test config
  mocks/
    __init__.py
    mock_rdo_blue.py             MockBlueRDOSensor(BlueRDOInterface): returns
                                  configurable canned values, can simulate timeouts/
                                  errors per address for negative-path tests
  unit/
    __init__.py
    test_config.py
    test_models.py
    test_rdo_blue.py
    test_modbus_bus.py
    test_manager.py
    test_database.py
    test_retention.py
  integration/
    __init__.py
    test_api_sensors_current.py
    test_api_data_endpoints.py
    test_sampler_to_db.py
    test_main_lifecycle.py

ARCHITECTURE.md                  (this file)
AGENTS.md / CLAUDE.md            Functional spec (existing)
README.md                        (existing)
Pipfile / Pipfile.lock           (existing)
```

Rationale for a few naming choices:

- `hardware/` holds five files, not three — the "3 files" in `AGENTS.md`
  are `manager.py`, `modbus_bus.py`, and `rdo_blue.py`. `rdo_blue_constants.py`
  and `rdo_blue_interface.py` are support files explicitly called for
  elsewhere in `AGENTS.md` (dedicated constants file; abstract interface
  satisfied by real and mock implementations).
- `sampler.py` (periodic polling → DB) lives at the top level of `src/`,
  not inside `db/` or `hardware/`, per the note that periodic
  polling/saving is a higher-level concern than either subsystem.

## 2. External library usage

| Library                | Used in                                      | Purpose                                                             |
| ----------------------- | --------------------------------------------- | -------------------------------------------------------------------- |
| pymodbus (>=3.6)        | `hardware/modbus_bus.py`, `hardware/rdo_blue.py` | `AsyncModbusSerialClient` — async Modbus RTU client over serial; `ModbusClientMixin.convert_from_registers` decodes register words to float32/uint32 in `rdo_blue.py` (the successor to `pymodbus.payload.BinaryPayloadDecoder.fromRegisters`, which pymodbus removed in 3.9), with word order taken from `rdo_blue_constants.MULTI_REGISTER_WORD_ORDER_BIG_ENDIAN` |
| pyserial-asyncio        | `hardware/modbus_bus.py` (transitively, via pymodbus's asyncio serial transport) | Async serial I/O backing pymodbus |
| aiosqlite               | `db/database.py`                              | Non-blocking SQLite access from the event loop                      |
| psutil                  | `db/retention.py`                             | `psutil.disk_usage()` for free-space checks                         |
| pydantic (v2)           | `config/schema.py`, `models/readings.py`      | Typed config schema + validation; shared reading/scan-result models |
| aiohttp                 | `api/app.py`, `api/routes.py`, `api/middleware.py`, `main.py` | HTTP server, routing, middleware, `AppRunner` lifecycle   |
| aiohttp-apigami         | `api/app.py`, `api/routes.py`                 | `@docs`/`@request_schema`/`@response_schema` decorators, Swagger UI at `/api/docs` (maintained `aiohttp-apispec` fork, py3.13-compatible) |
| marshmallow             | `api/schemas.py`                              | Schema classes consumed by aiohttp-apigami (see note below)          |
| pytest / pytest-asyncio / pytest-aiohttp | `tests/`                    | Async test runner, `aiohttp_client` fixture for integration tests    |
| freezegun               | (unused)                    | Listed in `AGENTS.md`'s recommended libraries and `Pipfile`, but the drift-corrected loops in `sampler.py`/`db/retention.py` schedule against `asyncio`'s monotonic loop clock (`loop.time()`), which freezegun does not intercept. Step 8's timing tests instead use short *real* intervals (tens of milliseconds) with a bounded `asyncio.sleep` + cancel, which exercises the actual scheduling code path. |

**Two schema systems, deliberately:** `config/schema.py` and
`models/readings.py` use **pydantic**, the single source of truth for
config validation and the reading/scan-result shape used internally and
for DB rows. `api/schemas.py` uses **marshmallow**, because
aiohttp-apigami's `@request_schema`/`@response_schema` decorators and
Swagger generation require marshmallow schema classes. The marshmallow
schemas in `api/schemas.py` mirror the pydantic models field-for-field
purely for request validation and doc generation at the HTTP boundary;
`routes.py` converts to/from the pydantic models immediately at the
handler boundary so the rest of the codebase only ever sees pydantic
models/dataclasses.

## 3. Configuration

Config file: `<data_dir>/config.json`. `data_dir` defaults to
`~/SMORES_Data`, overridable via the `SMORES_DATA_DIR` environment
variable (used by integration tests to avoid touching real data).
DB file: `<data_dir>/smores.db`.

`GET /api/config` returns this file's contents as-is. `PUT /api/config`
validates the posted JSON against the same pydantic model, writes it
atomically, then exits the process (systemd `Restart=always` brings it
back up with the new config).

| Field                            | Type                    | Default            | Notes                                                                 |
| --------------------------------- | ----------------------- | ------------------- | ---------------------------------------------------------------------- |
| `serial_port_devices`                      | `list[str]`             | `[]`                 | Stable device identifiers for RS485 to usb serial adapters, e.g. `/dev/serial/by-id/usb-FTDI_...-port0` |
| `sensor_mapping`                  | `dict[str, list[int]]`  | `{}`                 | converter device path → list of Modbus addresses present on it. Populated by startup scan or `GET /api/scan`; used directly (no scan) when `scan_on_startup` is false |
| `scan_on_startup`                 | `bool`                  | `true`               | If false, trust `sensor_mapping` as-is and skip the startup bus scan   |
| `sample_interval_seconds`         | `float`                 | `60.0`               | Interval between DB-writing polls (drift-corrected loop in `sampler.py`) |
| `modbus_baudrate`                 | `int`                   | `19200`              | Serial params match RDO Blue factory defaults (per vendor doc)        |
| `modbus_parity`                   | `"N"\|"E"\|"O"`         | `"E"`                | ″                                                                      |
| `modbus_stopbits`                 | `int`                   | `1`                  | ″                                                                      |
| `modbus_bytesize`                 | `int`                   | `8`                  | ″                                                                      |
| `modbus_request_timeout_seconds`  | `float`                 | `1.0`                | Per-Modbus-request (single register read) timeout — "serial timeout" |
| `sensor_read_timeout_seconds`     | `float`                 | `3.0`                | Timeout for one sensor's full multi-register read (`read_all()`) — "sensor timeout" |
| `scan_probe_timeout_seconds`      | `float`                 | `1.0`                | Timeout probing the modbuss address space of one RS485-to-usb serial converter.                    |
| `scan_min_address`                | `int`                   | `1`                  | Lowest Modbus address a scan probes (inclusive)                        |
| `scan_max_address`                | `int`                   | `247`                | Highest Modbus address a scan probes (inclusive). Defaults to the whole legal RTU space; every *absent* address in the range costs two full `scan_probe_timeout_seconds` plus a wake-up settle (§4), so narrowing this to the highest address actually installed is the main lever on scan time |
| `api_host`                        | `str`                   | `"0.0.0.0"`          | aiohttp bind address                                                   |
| `api_port`                        | `int`                   | `8080`               | aiohttp bind port                                                      |
| `api_request_timeout_seconds`     | `float`                 | `10.0`               | Default per-request timeout for most endpoints (504 on expiry)         |
| `poll_timeout_seconds`            | `float`                 | `8.0`                | Timeout specific to `GET /api/sensors/current` (spec calls this out by name) |
| `api_max_concurrent_clients`      | `int`                   | `5`                  | `asyncio.Semaphore` size in `api/middleware.py`                        |
| `min_free_disk_space_mb`                | `int`                   | `500`                | Free space floor; below this, retention starts deleting oldest rows    |
| `disk_check_interval_seconds`     | `float`                 | `300.0`              | How often `db/retention.py` checks free space                          |
| `retention_delete_batch_size`            | `int`                   | `50`                 | Rows deleted per batch until free space recovers (spec-mandated value, kept configurable) |
| `log_level`                       | `str`                   | `"INFO"`             | Passed to `logging.basicConfig`                                        |

`modbus_baudrate`/`parity`/`stopbits`/`bytesize` aren't named explicitly
in `AGENTS.md`'s config bullet list but are required to construct the
serial client and match the vendor doc's factory defaults (RTU, 19200,
8E1) — included here for completeness and to keep them out of hardcoded
logic per the "keep register/config assumptions easy to patch" guidance.
`scan_min_address`/`scan_max_address` are there for the same reason: the
spec says to "scan the Modbus address space", and the full legal space is
the default, but the operator needs a lever on a scan that would otherwise
take minutes (see §4). Both are validated against
`constants.MODBUS_MIN_UNIT_ADDRESS`/`MODBUS_MAX_UNIT_ADDRESS`, with
`scan_min_address <= scan_max_address` enforced by a model validator.

Example `config.json`:

```json
{
  "serial_port_devices": [
    "/dev/serial/by-id/usb-FTDI_USB-RS485_Cable-if00-port0",
    "/dev/serial/by-id/usb-FTDI_USB-RS485_Cable-if01-port0"
  ],
  "sensor_mapping": {
    "/dev/serial/by-id/usb-FTDI_USB-RS485_Cable-if00-port0": [1, 2, 3],
    "/dev/serial/by-id/usb-FTDI_USB-RS485_Cable-if01-port0": [4, 5]
  },
  "scan_on_startup": true,
  "sample_interval_seconds": 60.0,
  "modbus_baudrate": 19200,
  "modbus_parity": "E",
  "modbus_stopbits": 1,
  "modbus_bytesize": 8,
  "modbus_request_timeout_seconds": 1.0,
  "sensor_read_timeout_seconds": 3.0,
  "scan_probe_timeout_seconds": 1.0,
  "scan_min_address": 1,
  "scan_max_address": 247,
  "api_host": "0.0.0.0",
  "api_port": 8080,
  "api_request_timeout_seconds": 10.0,
  "poll_timeout_seconds": 8.0,
  "api_max_concurrent_clients": 5,
  "min_free_disk_space_mb": 500,
  "disk_check_interval_seconds": 300.0,
  "retention_delete_batch_size": 50,
  "log_level": "INFO"
}
```

## 4. Concurrency & locking model

- **Per-bus serialization:** each `ModbusBus` (one per RS485-to-USB
  converter) holds an `asyncio.Lock`. Every request on that bus — scan
  probes, scheduled sampling reads, and `/api/sensors/current` reads —
  acquires the bus's lock for the duration of that single request/response
  round trip, then releases it. Concurrent callers (e.g. the sampler loop
  and an API request landing at the same moment) interleave at the
  request level in a nondeterministic but safe order; each caller still
  gets the correct response for the request it made, because the lock
  scope is exactly one request/response pair and callers await their own
  awaitable.
- **Instrument wake-up (per address, not per port):** the Blue RDO idles
  into a low-power state after `END_OF_SESSION_TIMEOUT_SECONDS` (5 s,
  vendor doc) without traffic, and answers only after "a carriage return
  (0x0D) or any Modbus command" plus one second to wake. At any
  `sample_interval_seconds` above 5 s, *every* read would otherwise fail, so
  `ModbusBus` tracks the last answered command per Modbus address and, when
  that address's session has lapsed, sends one throwaway Device Id read as
  the wake-up, waits `WAKEUP_SETTLE_SECONDS`, then issues the real request
  exactly once. This is not a retry (`AGENTS.md`: "Do not retry reads") —
  pymodbus is constructed with `retries=0`, so a non-answering instrument
  still costs exactly one request timeout. The wake-up command is sent
  under the bus lock; the settle sleep deliberately is **not**, so other
  addresses keep using the port while one instrument wakes, and a
  27-sensor poll costs one shared ~1 s settle rather than 27 serial ones.
  A per-address lock means concurrent readers of the same instrument wake
  it once between them. Note the timeout interaction: with the defaults an
  idle-but-healthy sensor's `read_all()` costs ~1.2 s of its 3 s
  `sensor_read_timeout_seconds` budget (settle plus four reads), while an
  absent one exhausts the whole budget (wake timeout + settle + first read
  timeout) and is reported unreachable by `read_all()`'s own timeout rather
  than by four consecutive per-request ones.
- **Scan mutual exclusion:** `hardware/manager.py` keeps the in-progress
  `asyncio.Task` for `scan_all_buses()`, plus the address range it is
  probing. A scan request arriving while one is already running does not
  start a second concurrent scan; `AGENTS.md` allows either erroring or
  waiting on the in-progress result — a request for the *same* range
  awaits the existing task, while one for a *different* range (via
  `/api/scan`'s `start_address`/`stop_address`) gets `BusScanError`
  (→ HTTP 503), since the in-progress result wouldn't answer it. Awaiting
  the existing task is done (shielded, so an API client that gives
  up doesn't cancel a scan the startup path may still be waiting on) and
  returns its result to avoid needlessly failing a second caller. While a
  scan is running and no mapping is established yet,
  `query_all_sensors`/`query_sensor` raise `BusScanError` (→ HTTP 503), per
  `AGENTS.md`'s "API calls made during scanning should return an
  informative error". An *established* mapping that happens to be empty is
  not an error — a system where no sensors were found simply has no
  readings.
- **Scan cost (sequential, per address):** an absent Modbus address can only
  be ruled out by letting its probe time out, and because the first probe of
  an idle instrument doubles as its wake-up, a non-answer on the first probe
  is inconclusive. `_scan_one_bus` therefore finishes each address before
  moving to the next: probe, and if unanswered, wait `WAKEUP_SETTLE_SECONDS`
  and re-probe once. (An earlier bulk wake-up pass followed by a re-probe
  pass overloaded the instruments with back-to-back probes, and on a wide
  range the early addresses had gone back to sleep before the second pass
  reached them.) A *present* sensor is probed once or twice; an absent
  address costs two probe timeouts plus one settle. Buses are scanned
  concurrently (separate ports), so the worst case is
  `address_count x (2 x scan_probe_timeout_seconds + WAKEUP_SETTLE_SECONDS)`
  regardless of converter count; `hardware.manager.estimate_scan_duration_seconds`
  computes it, and it is both logged before a scan (with a warning above
  60 s) and used as each `/api/scan` request's timeout, computed for the
  range that request will scan.
- **API concurrency cap:** `api/middleware.py`'s
  `concurrency_limit_middleware` uses an `asyncio.Semaphore(config.api_max_concurrent_clients)`.
  Acquisition is non-blocking: the middleware checks the semaphore's free
  count before calling `acquire()` and, if none is free, immediately
  returns `503` with a `Retry-After` header rather than queuing the
  request. (`asyncio.wait_for(sem.acquire(), timeout=0)` looks like the
  obvious way to write a non-blocking try-acquire but is actually always a
  timeout — `wait_for` wraps the coroutine in a fresh `Task`, which can
  never be `done()` before its first event-loop iteration, so the
  `timeout <= 0` fast path cancels it before it runs even when the
  semaphore has free capacity; caught by the step-6 smoke test.) This
  satisfies "rather than being silently dropped or queued indefinitely."
- **Per-route timeout:** `timeout_middleware` wraps the handler call in
  `asyncio.wait_for(handler(request), timeout=route_timeout)`, returning
  `504` on `TimeoutError`. `route_timeout` defaults to
  `config.api_request_timeout_seconds`; `/api/sensors/current` is
  registered with an override of `config.poll_timeout_seconds` (routes
  carry their timeout as route metadata read by the middleware).

## 5. Shared data models (`models/readings.py`)

```python
class SensorReading(BaseModel):
    row_id: int | None          # DB autoincrement id; None for a fresh, unsaved poll
    sensor_address: int
    serial_converter_id: str
    timestamp_utc: float        # unix epoch seconds, UTC
    temperature_c: float        # constants.UNREADABLE_VALUE (-9999) if unreadable
    do_percent_saturation: float
    do_partial_pressure_torr: float
    do_mg_l: float
    status_code: int            # worst-case Data Quality ID across the 4 parameters,
                                 # or a negative internal code (see below)
    status_text: str            # human-readable, e.g. "OK", "Sensor timeout",
                                 # "temperature: Error reading parameter"

class ScanResult(BaseModel):
    converter_id: str
    sensor_addresses: list[int]
    scanned_at: float           # unix epoch seconds
```

`SensorReading` is the single shape used by: `BlueRDOSensor.read_all()`
output (via `hardware/manager.py`), the DB row (`db/database.py` maps
1:1 to/from this model), the JSON body of `/api/sensors/current` and
`/api/data`, and each CSV row of `/api/data/csv`.

`status_code` is a Blue RDO Data Quality ID (0 = OK, 3, 5, ...) whenever the
instrument reported one, else one of three negative codes defined in
`constants.py`. All three pair with `UNREADABLE_VALUE` (-9999) in the
affected numeric fields:

| Code | Constant                          | Meaning                                                                 |
| ---- | --------------------------------- | ------------------------------------------------------------------------ |
| `-1` | `SENSOR_UNREACHABLE_STATUS_CODE`  | `read_all()` raised `SensorTimeoutError` — the instrument never answered; `status_text` is `SENSOR_UNREACHABLE_STATUS_TEXT` ("Sensor timeout") and every value field is -9999 |
| `-2` | `PARAMETER_TIMEOUT_STATUS_CODE`   | Some (not all) parameter reads timed out while the instrument kept answering others; only those fields are -9999 |
| `-3` | `SENSOR_READ_ERROR_STATUS_CODE`   | `SensorReadError` — a Modbus exception response or short/malformed reply rather than a Data Quality problem the instrument could describe |

When one read mixes outcomes, `hardware/rdo_blue.py`'s `_severity_rank`
picks the worst: OK < any Data Quality ID (higher = worse) < a
per-parameter timeout < a transport read error. `status_text` names the
vendor's own parameter names (Appendix A), e.g.
`"DO Percent Saturation: RDO Cap expired"`, joined by `"; "`.

## 6. DB row layout (`db/database.py`)

Single table, `sensor_readings`:

| Column                     | Type      | Constraints          | Notes                                                        |
| --------------------------- | --------- | --------------------- | -------------------------------------------------------------- |
| `id`                        | INTEGER   | PRIMARY KEY AUTOINCREMENT | Monotonically incrementing row index/counter (spec-mandated); `AUTOINCREMENT` guarantees no id reuse even after deletes |
| `timestamp_utc`             | REAL      | NOT NULL, indexed     | Unix epoch seconds, UTC                                        |
| `sensor_address`            | INTEGER   | NOT NULL              | Globally unique Modbus address                                 |
| `converter_id`              | TEXT      | NOT NULL              | Which RS485-to-USB converter this sensor was read from          |
| `temperature_c`             | REAL      | NOT NULL              | `-9999` (constants.UNREADABLE_VALUE) if unreadable              |
| `do_percent_saturation`     | REAL      | NOT NULL              | ″                                                                |
| `do_partial_pressure_torr`  | REAL      | NOT NULL              | ″                                                                |
| `do_mg_l`                   | REAL      | NOT NULL              | ″                                                                |
| `status_code`               | INTEGER   | NOT NULL              | Worst-case Data Quality ID, or negative internal code for timeout/unreachable |
| `status_text`               | TEXT      | NOT NULL              | Human-readable status/error summary                             |

```sql
CREATE TABLE IF NOT EXISTS sensor_readings (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc           REAL    NOT NULL,
    sensor_address          INTEGER NOT NULL,
    converter_id            TEXT    NOT NULL,
    temperature_c           REAL    NOT NULL,
    do_percent_saturation   REAL    NOT NULL,
    do_partial_pressure_torr REAL   NOT NULL,
    do_mg_l                 REAL    NOT NULL,
    status_code             INTEGER NOT NULL,
    status_text             TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sensor_readings_timestamp ON sensor_readings(timestamp_utc);
```

One row is written **per sensor, per sampling interval** (not one row
per poll cycle) — i.e. a system with 27 sensors and a 60s interval writes
27 rows/minute.

**Retention & disk space (`db/retention.py`):** SQLite does not shrink
its on-disk file on `DELETE` by default, which would make free-space-based
pruning ineffective. The DB is opened with:

```sql
PRAGMA journal_mode = WAL;
PRAGMA auto_vacuum = INCREMENTAL;
```

`retention.py`'s periodic task (every `disk_check_interval_seconds`)
checks `psutil.disk_usage(data_dir).free` against `min_free_disk_mb`; if
below threshold, it repeatedly calls `database.delete_oldest(retention_batch_size)`
followed by `PRAGMA incremental_vacuum(retention_batch_size)` to actually
release freed pages back to the filesystem, re-checking free space after
each batch, until free space recovers or the table is empty.

## 7. API endpoints → handlers

All handlers live in `api/routes.py`; request/response shapes documented
via `api/schemas.py` marshmallow schemas + `@docs` decorators, surfaced
at `GET /api/docs`.

| Route                     | Method | Handler                | Timeout source                     |
| -------------------------- | ------ | ------------------------ | ------------------------------------ |
| `/`                        | GET    | `index`                  | `api_request_timeout_seconds`        |
| `/api/docs`                | GET    | (aiohttp-apigami/swagger, auto-registered) | —            |
| `/api/sensors/current`     | GET    | `get_current_readings`   | `poll_timeout_seconds` (override)    |
| `/api/data`                | GET    | `get_data`                | `api_request_timeout_seconds`        |
| `/api/data/csv`            | GET    | `get_data_csv`            | `api_request_timeout_seconds`        |
| `/api/data`                | DELETE | `delete_data`             | `api_request_timeout_seconds`        |
| `/api/config`              | GET    | `get_config`              | `api_request_timeout_seconds`        |
| `/api/config`              | PUT    | `put_config`              | `api_request_timeout_seconds`        |
| `/api/scan`                | GET    | `scan_buses`              | `hardware.manager.estimate_scan_duration_seconds` for the requested range — the scan's worst case (§4), computed per request (`with_timeout` also accepts a `Callable[[web.Request], float]`) |

Query parameters:

| Route | Param | Type / default | Effect |
| --- | --- | --- | --- |
| `GET /api/sensors/current` | `save` | bool, `false` | If true, also inserts the readings (one row per sensor) via `sampler.sample_once` — the same path the periodic sampler uses — and returns them with their `row_id`s |
| `GET /api/scan` | `start_address` | int 1–247, saved `scan_min_address` | Lowest address probed; saved as `scan_min_address` after a successful scan |
| `GET /api/scan` | `stop_address` | int 1–247, saved `scan_max_address` | Highest address probed; saved as `scan_max_address` after a successful scan |

An invalid value, or `start_address > stop_address` once defaults are
filled in, is a 400 with nothing scanned or saved.

`get_current_readings` calls `hardware.manager.query_all_sensors()`
directly (no DB write), or `sampler.sample_once()` when `save=true`.
`scan_buses` calls `hardware.manager.scan_all_buses(min, max)` then
`config.loader.save_config()` to overwrite `sensor_mapping` and
`scan_min_address`/`scan_max_address` (no process restart — unlike
`PUT /api/config`, these are only fields the manager already applied to its
own in-memory state during the scan it just ran, or that only affect future
scans).

## 8. Lifecycle (`main.py`)

1. Read `SMORES_DATA_DIR` env var (default `~/SMORES_Data`), ensure it exists.
2. `config.loader.load_config()` (writing schema defaults first on a fresh
   install), and set the root logger to `config.log_level`.
3. Construct `db.database.Database` and `hardware.manager.SensorManager`
   (constructing the manager only builds `ModbusBus` objects; it opens no
   serial ports).
4. Register the `SIGTERM`/`SIGINT` handlers, which set a shutdown
   `asyncio.Event`.
5. `database.init_schema()`.
6. Build and start the aiohttp `AppRunner`/`TCPSite`.
7. Start three `asyncio.Task`s: the sensor bring-up (`manager.start()`,
   then either `scan_all_buses()` + persist, or `save_sensor_mapping()`
   from config), `sampler.run_forever`, and `db.retention.run_forever`.
8. Await the shutdown event; then restore default signal disposition,
   cancel the three tasks, and `cleanup()`/`aclose()` the runner, manager
   (closing each `ModbusBus`'s serial connection), and database, in that
   order — systemd's `Restart=always` handles restart-on-exit for both
   normal SIGTERM/SIGINT and the `PUT /api/config` restart path.

Three ordering choices are load-bearing:

- **Signal handlers before any subsystem starts** (step 4 before 5), so a
  `systemctl stop`/`restart` arriving during `init_schema()` or a long
  startup scan is handled gracefully instead of killing the process
  mid-write. Teardown then *removes* the handlers, so a second SIGTERM
  (systemd losing patience) terminates immediately rather than being
  swallowed by a handler that has already fired.
- **The listener before the sensors** (step 6 before 7). A full 1-247
  address-space scan takes minutes (§4), and `main.py` used to await it
  before binding the port, so for that whole window the backend was simply
  unreachable. Now the scan runs as a background task and the API is up
  throughout, answering `503 Bus scan in progress` from
  `SensorManager`'s guard. That guard distinguishes three states:
  *scan running* → 503; *mapping never established* (bring-up still
  opening ports, or it failed outright) → 503; *established but empty*
  (a scan that found nothing) → `200 []`, which is a real answer, not an
  error. Without the "never established" case, the reorder would have made
  a request landing in the gap between step 6 and step 7 report zero
  sensors instead of "not ready".
- **Sensor bring-up failures are logged, not fatal.** A missing
  `/dev/serial/by-id/...` path raises `BusScanError`; exiting on it would
  put the unit into a systemd restart loop. The API stays up instead, so an
  operator can fix `serial_port_devices` via `PUT /api/config` or retry
  with `GET /api/scan`, and every sensor endpoint returns the 503 above in
  the meantime. `sampler.run_forever` tolerates the same `BusScanError`
  from `query_all_sensors()` — it logs and skips that tick rather than
  dying, since it is started before the mapping exists.

Both periodic loops (`sampler.py`, `db/retention.py`) use the
`next_run += interval; sleep(max(0, next_run - now))` pattern from
`AGENTS.md`. `sampler.py` adds one thing on top: if a poll overruns its
interval, it realigns `next_run` to the next future tick and logs a warning
instead of firing a burst of back-to-back catch-up samples — one sample per
interval is the intent, not a fixed total count.

## 9. Testing structure

- `tests/mocks/mock_rdo_blue.py` implements `BlueRDOInterface` so unit
  tests never touch real serial hardware. `hardware.manager.SensorManager`
  takes an optional `sensor_factory: Callable[[ModbusBus, int],
  BlueRDOInterface]` (default: construct a real `BlueRDOSensor`);
  integration tests pass a factory returning pre-built
  `MockBlueRDOSensor`s and populate the manager via
  `save_sensor_mapping()` directly (never `start()`, which would open a
  real serial connection), satisfying "integration tests for API calls
  using mocked Blue RDO sensors, but a real SQLite DB and real network
  calls."
- Integration tests spin up the real aiohttp app via `pytest-aiohttp`'s
  `aiohttp_client` fixture, point `SMORES_DATA_DIR` at a pytest `tmp_path`,
  and use a short `sample_interval_seconds` with bounded real sleeps to
  assert row counts after a bounded runtime (see the `freezegun` row in
  §2 for why it isn't used for this).
- `constants.SENSOR_UNREACHABLE_STATUS_CODE`/`_STATUS_TEXT` (added in step
  8) are the `SensorReading.status_code`/`status_text` values
  `hardware.manager.SensorManager.query_all_sensors` is expected to use
  for a sensor whose `read_all()` raised `SensorTimeoutError` — needed so
  tests have a concrete, agreed-upon value to assert against.
- `hardware/modbus_bus.py` imports `pymodbus.client.AsyncModbusSerialClient`
  at module scope, so `tests/unit/test_modbus_bus.py` monkeypatches
  `hardware.modbus_bus.AsyncModbusSerialClient` with a fake client rather
  than opening a real serial port. Those tests construct `ModbusBus` with
  millisecond-scale `session_timeout_seconds`/`wakeup_settle_seconds`/
  `session_keepalive_margin_seconds` (all three are constructor arguments
  defaulting to the vendor-doc values in `rdo_blue_constants.py`) so the
  wake-up path in §4 is covered without adding seconds of sleeping.
- `tests/unit/test_manager.py` uses the same seam one level up: it
  monkeypatches `hardware.manager.ModbusBus` with a scriptable `FakeBus`
  (which addresses are "present", which ignore their first wake-up probe)
  and injects `MockBlueRDOSensor`s through the manager's own
  `sensor_factory`, covering the scan/mapping logic and the
  exception-to-`-9999` translations the API-level integration tests don't
  reach.
- `tests/integration/test_main_lifecycle.py` runs `main.run()` itself —
  real config file, real SQLite DB, real aiohttp listener on a real TCP
  port, real SIGTERM — patching `hardware.manager`'s `ModbusBus` (a
  slow-probing fake, so the startup scan lasts long enough to make requests
  during it) and `BlueRDOSensor` (the shared `MockBlueRDOSensor`). It
  covers the §8 ordering directly: a client polling during the startup scan
  gets 503, the *same* client sees readings once the scan lands, the
  discovered mapping is persisted to `config.json`, and SIGTERM tears
  everything down without an exception.

## 10. Deployment (`deploy/smores-topside.service`)

The systemd unit is the only supported way to run the backend unattended.
Install and operating commands live in `README.md`; the decisions behind the
unit are here.

| Directive | Value | Why |
| --- | --- | --- |
| `Type=exec` | | No readiness protocol (no `sd_notify`); `exec` at least makes `systemctl start` fail if the interpreter path is wrong, which `simple` would not. |
| `User=pi` / `Group=pi` | | Per `AGENTS.md` step 12. The code and the data directory both live under `/home/pi`. |
| `SupplementaryGroups=dialout` | | `/dev/ttyUSB*` is group `dialout`. `pi` is normally already a member, so this is belt-and-braces for a re-imaged Pi. |
| `Environment=SMORES_DATA_DIR=` | `/home/pi/SMORES_Data` | The same path `config.loader.get_data_dir()` would derive from `~`, stated explicitly so the service's data location does not depend on the resolved home directory. `main.py` creates it if absent, so no `StateDirectory=`/`ExecStartPre=mkdir` is needed. |
| `Environment=PYTHONUNBUFFERED=1` | | Without it, stdout to a pipe (journald) is block-buffered and log lines arrive in 8 KiB batches — useless for watching a scan progress with `journalctl -f`. |
| `ExecStart=` | `<venv>/bin/python .../src/main.py` | The pipenv virtualenv interpreter directly, not `pipenv run`: no wrapper process between systemd and the signal handlers in §8. The venv directory name is a sha256 digest of the absolute `Pipfile` path, so it is stable across `pipenv install` re-runs but not across moving the repo (README documents re-pointing it via `pipenv --venv`). |
| `Restart=always` | | **Not** `on-failure`: `PUT /api/config` persists the config and then exits through the normal §8 SIGTERM path, i.e. exit code 0. `on-failure` would leave the service dead after every config change. Restart-looping on a hardware fault is not a risk because §8 deliberately does not exit when sensor startup fails. |
| `StartLimitIntervalSec=0` | | Disables the default 5-starts-per-10s limit, which `PUT /api/config` (a legitimate, operator-driven restart) could otherwise trip, latching the unit into `failed`. |
| `KillSignal=SIGTERM`, `TimeoutStopSec=30` | | SIGTERM is what §8's handler waits on; 30 s is well beyond what teardown (cancel tasks, close serial ports, close the DB) needs. §8 removes the handlers once teardown begins, so systemd's follow-up `SIGKILL` — or an impatient operator's second `systemctl stop` — is not swallowed. |
| `SyslogIdentifier=smores-topside` | | Makes `journalctl -t smores-topside` work alongside `-u`. |
| `AmbientCapabilities=CAP_NET_BIND_SERVICE`, `CapabilityBoundingSet=CAP_NET_BIND_SERVICE` | | Lets `api_port` be a privileged port (< 1024, i.e. 80) while the process still runs as `pi`. systemd retains the capability across the switch to `pi` (it adds keep-caps to `SecureBits=` for ambient capabilities), and `NoNewPrivileges=` does not block it, since the capability is present at exec rather than gained through it. The bounding set is narrowed to the same single capability, so binding low ports is the only privilege the process can hold — nothing else about it is root-equivalent. Without these, a config asking for port 80 fails the `site.start()` bind in §8 with `PermissionError`, which `Restart=always` turns into a restart loop (README documents the recovery). |
| `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`, `RestrictSUIDSGID` | | Cheap hardening that doesn't interfere with the workload. Deliberately **absent**: `PrivateDevices=`/`DeviceAllow=` (the service's entire job is opening USB serial adapters) and `ProtectHome=` (both the code and the data directory are under `/home/pi`). `ProtectSystem=strict` is not used either, since it would make `/run` read-only and put the journald socket at risk. |
