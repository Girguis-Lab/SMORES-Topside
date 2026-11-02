"""aiohttp `Application` factory: wires routes, middleware, and
aiohttp-apigami Swagger docs together. Built once by `main.py`; the
resulting `Application` is handed to an `AppRunner`/`TCPSite` there.
"""

from pathlib import Path

from aiohttp import web
from aiohttp_apigami import OpenApiVersion, setup_aiohttp_apispec

from api.middleware import create_concurrency_limit_middleware, create_timeout_middleware
from api.routes import register_routes
from config.schema import Config
from db.database import Database
from hardware.manager import SensorManager


def create_app(
    config: Config,
    manager: SensorManager,
    database: Database,
    data_dir: Path,
) -> web.Application:
    """Build the fully wired `Application`.

    Stores `config`, `manager`, `database`, and `data_dir` on `app` (e.g.
    `app["config"]`) for `api/routes.py` handlers to read, installs the
    concurrency-limit middleware (outermost, so an over-capacity request is
    rejected before the timeout clock starts) and the timeout middleware
    (innermost, wrapping the actual handler call), registers every route via
    `api.routes.register_routes`, and sets up `aiohttp-apigami`
    (`setup_aiohttp_apispec`) so `GET /api/docs` serves Swagger UI.
    """
    app = web.Application()
    app["config"] = config
    app["manager"] = manager
    app["database"] = database
    app["data_dir"] = data_dir

    app.middlewares.append(create_concurrency_limit_middleware(config.api_max_concurrent_clients))
    app.middlewares.append(create_timeout_middleware(config.api_request_timeout_seconds))



    register_routes(app)

    setup_aiohttp_apispec(
        app=app,
        title="SMORES-Topside API",
        version="1.0.0",
        url="/api/docs/swagger.json",
        swagger_path="/api/docs",
        openapi_version=OpenApiVersion.V303,
        in_place=True,
    )
    # aiohttp auto-adds a HEAD route for every add_get(); it reuses the @docs-decorated
    # GET handler, so apigami documents it too. Hide those from Swagger only — the HEAD
    # handlers themselves stay registered.
    for path_item in app["swagger_dict"]["paths"].values():
        path_item.pop("head", None)

    return app
