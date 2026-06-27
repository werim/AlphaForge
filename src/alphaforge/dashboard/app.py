from __future__ import annotations

from pathlib import Path
import os
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url

from alphaforge.config import load_config_from_env
from alphaforge.config_registry import config_snapshot, write_dashboard_overrides, reset_dashboard_override
from alphaforge.contracts import canonical_utc_timestamp
from alphaforge.runtime import _build_runtime_from_env
from alphaforge.runtime_control import RuntimeControlStore, RuntimeSupervisor

from .backtest_control import default_form_values, parse_backtest_form, run_dashboard_backtest

async def _form_dict(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    return {key: values[-1] for key, values in parse_qs(body, keep_blank_values=True).items()}

from .queries import (
    fetch_latest_readiness,
    fetch_readiness_probe_matrix,
    fetch_recent_lifecycle,
    fetch_reject_summary,
    fetch_runtime_heartbeat_status,
    fetch_signal_timeline,
)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _create_dashboard_engine(database_url: str) -> Engine:
    """Connect without creating or migrating a missing runtime SQLite database."""
    parsed = make_url(database_url)
    if not parsed.get_backend_name().startswith("sqlite"):
        return create_engine(database_url, future=True)
    database = parsed.database
    if not database or database == ":memory:":
        return create_engine("sqlite+pysqlite:///:memory:", future=True)
    db_path = Path(database).expanduser()
    if not db_path.is_absolute():
        db_path = (Path.cwd() / db_path).resolve()
    if not db_path.exists():
        return create_engine("sqlite+pysqlite:///:memory:", future=True)
    read_only_url = f"sqlite+pysqlite:///file:{db_path.as_posix()}?mode=ro&uri=true"
    return create_engine(read_only_url, future=True)


def _status_payload(engine: Engine, control_store: RuntimeControlStore | None = None) -> dict[str, Any]:
    cfg = load_config_from_env().runtime
    heartbeat_status = fetch_runtime_heartbeat_status(engine)
    control = control_store.read().to_dict() if control_store is not None else {}
    return {
        "configured_execution_mode": cfg.execution_mode,
        "global_kill_switch": cfg.global_kill_switch,
        "require_live_qualification": cfg.require_live_qualification,
        "enable_shadow_mode": cfg.enable_shadow_mode,
        "enable_canary_mode": cfg.enable_canary_mode,
        "required_live_exchanges": list(cfg.required_live_exchanges),
        "enable_binance_readonly_reconciliation": cfg.enable_binance_readonly_reconciliation,
        **heartbeat_status,
        **control,
        "latest_readiness": fetch_latest_readiness(engine),
    }


def create_app(database_url: str | None = None) -> FastAPI:
    app = FastAPI(title="AlphaForge Dashboard", version="0.1.0")
    resolved_database_url = database_url or load_config_from_env().persistence.database_url
    app.state.engine = _create_dashboard_engine(resolved_database_url)
    app.state.control_engine = create_engine(resolved_database_url, future=True)
    app.state.control_store = RuntimeControlStore(app.state.control_engine)

    def _factory(mode: str):
        old_exec = os.environ.get("EXECUTION_MODE")
        old_alpha = os.environ.get("ALPHAFORGE_EXECUTION_MODE")
        os.environ["EXECUTION_MODE"] = mode
        os.environ["ALPHAFORGE_EXECUTION_MODE"] = mode
        try:
            return _build_runtime_from_env()
        finally:
            if old_exec is None:
                os.environ.pop("EXECUTION_MODE", None)
            else:
                os.environ["EXECUTION_MODE"] = old_exec
            if old_alpha is None:
                os.environ.pop("ALPHAFORGE_EXECUTION_MODE", None)
            else:
                os.environ["ALPHAFORGE_EXECUTION_MODE"] = old_alpha

    app.state.runtime_supervisor = RuntimeSupervisor(app.state.control_store, _factory)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.on_event("shutdown")
    async def dispose_dashboard_engine() -> None:
        app.state.engine.dispose()
        app.state.control_engine.dispose()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"service": "alphaforge-dashboard", "status": "ok", "timestamp": canonical_utc_timestamp()}

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request) -> HTMLResponse:
        status = _status_payload(app.state.engine, app.state.control_store)
        rejects = fetch_reject_summary(app.state.engine)
        lifecycle = fetch_recent_lifecycle(app.state.engine, limit=10)
        return TEMPLATES.TemplateResponse(
            request=request,
            name="overview.html",
            context={
                "status": status,
                "rejects": rejects,
                "lifecycle": lifecycle,
                "page": "overview",
                "backtest_form": default_form_values(),
                "backtest_errors": {},
                "backtest_result": None,
            },
        )


    @app.post("/backtest/run", response_class=HTMLResponse)
    async def run_backtest(request: Request) -> HTMLResponse:
        form_data = await _form_dict(request)
        parsed, errors = parse_backtest_form(form_data)
        result = None
        if parsed is not None:
            result = run_dashboard_backtest(parsed)
        status = _status_payload(app.state.engine, app.state.control_store)
        rejects = fetch_reject_summary(app.state.engine)
        lifecycle = fetch_recent_lifecycle(app.state.engine, limit=10)
        form_values = default_form_values()
        form_values.update({key: form_data.get(key, form_values.get(key)) for key in form_values.keys() if key not in {"timeframes", "filter_reasons", "filter_switches"}})
        form_values["filter_switches"] = {reason: f"filter_{reason}" in form_data for reason in form_values.get("filter_reasons", [])}
        return TEMPLATES.TemplateResponse(
            request=request,
            name="overview.html",
            context={
                "status": status,
                "rejects": rejects,
                "lifecycle": lifecycle,
                "page": "overview",
                "backtest_form": form_values,
                "backtest_errors": errors,
                "backtest_result": result,
            },
        )

    @app.get("/partials/status-bar", response_class=HTMLResponse)
    async def status_bar(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="partials/status_bar.html",
            context={"status": _status_payload(app.state.engine, app.state.control_store)},
        )


    @app.post("/runtime/mode")
    async def set_runtime_mode(request: Request) -> RedirectResponse:
        form = await _form_dict(request)
        try:
            app.state.control_store.set_requested_mode(str(form.get("mode", "PAPER")), operator_acknowledged=str(form.get("operator_acknowledged", "")).lower() in {"1", "true", "on", "yes"}, source="dashboard")
        except Exception as exc:
            app.state.control_store.set_status("ERROR", last_error=str(exc))
        return RedirectResponse(url="/", status_code=303)

    @app.post("/runtime/kill-switch")
    async def set_kill_switch(request: Request) -> RedirectResponse:
        form = await _form_dict(request)
        active = str(form.get("active", "false")).lower() in {"1", "true", "on", "yes"}
        app.state.control_store.set_kill_switch(active, source="dashboard")
        if active and app.state.runtime_supervisor.is_running():
            await app.state.runtime_supervisor.stop()
            app.state.control_store.set_kill_switch(True, source="dashboard")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/runtime/start")
    async def start_runtime() -> RedirectResponse:
        await app.state.runtime_supervisor.start()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/runtime/stop")
    async def stop_runtime() -> RedirectResponse:
        await app.state.runtime_supervisor.stop()
        return RedirectResponse(url="/", status_code=303)

    @app.get("/api/v1/runtime/control")
    async def api_runtime_control() -> dict[str, Any]:
        return _status_payload(app.state.engine, app.state.control_store)

    @app.get("/rejects", response_class=HTMLResponse)
    async def rejects(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="rejects.html",
            context={"rejects": fetch_reject_summary(app.state.engine), "page": "rejects"},
        )

    @app.get("/lifecycle", response_class=HTMLResponse)
    async def lifecycle(
        request: Request,
        signal_id: str | None = Query(default=None),
        symbol: str | None = Query(default=None),
    ) -> HTMLResponse:
        events = fetch_recent_lifecycle(app.state.engine, limit=100, signal_id=signal_id, symbol=symbol)
        timeline = fetch_signal_timeline(app.state.engine, signal_id) if signal_id else None
        return TEMPLATES.TemplateResponse(
            request=request,
            name="lifecycle.html",
            context={"events": events, "timeline": timeline, "signal_id": signal_id or "", "symbol": symbol or "", "page": "lifecycle"},
        )


    def _settings_context(message: str = "", error: str = "") -> dict[str, Any]:
        rows = config_snapshot(mode=load_config_from_env().runtime.execution_mode)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["category"], []).append(row)
        return {"grouped": grouped, "page": "settings", "message": message, "error": error}

    @app.get("/settings", response_class=HTMLResponse)
    async def settings(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request=request, name="settings.html", context=_settings_context())

    @app.post("/settings/save", response_class=HTMLResponse)
    async def settings_save(request: Request) -> HTMLResponse:
        form = await _form_dict(request)
        try:
            updates = {k: v for k, v in form.items() if k.startswith("ALPHAFORGE_") or k in {"MIN_EFFECTIVE_RR", "MIN_LIQUIDITY_USD"}}
            write_dashboard_overrides(updates)
            ctx = _settings_context(message="Settings saved to config/runtime_overrides.json. Restart may be required for active runtimes.")
        except Exception as exc:
            ctx = _settings_context(error=str(exc))
        return TEMPLATES.TemplateResponse(request=request, name="settings.html", context=ctx)

    @app.post("/settings/reset", response_class=HTMLResponse)
    async def settings_reset(request: Request) -> HTMLResponse:
        form = await _form_dict(request)
        try:
            reset_dashboard_override(str(form.get("reset", "")))
            ctx = _settings_context(message="Setting reset to typed/default lower-precedence source.")
        except Exception as exc:
            ctx = _settings_context(error=str(exc))
        return TEMPLATES.TemplateResponse(request=request, name="settings.html", context=ctx)

    @app.get("/settings/export")
    async def settings_export() -> JSONResponse:
        return JSONResponse({"config_snapshot": config_snapshot(mode=load_config_from_env().runtime.execution_mode)})

    @app.get("/readiness", response_class=HTMLResponse)
    async def readiness(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="readiness.html",
            context={
                "readiness": fetch_latest_readiness(app.state.engine),
                "probe_matrix": fetch_readiness_probe_matrix(app.state.engine),
                "page": "readiness",
            },
        )

    @app.get("/api/v1/runtime/status")
    async def api_runtime_status() -> dict[str, Any]:
        return _status_payload(app.state.engine, app.state.control_store)

    @app.get("/api/v1/rejects/summary")
    async def api_reject_summary() -> dict[str, Any]:
        return fetch_reject_summary(app.state.engine)

    @app.get("/api/v1/lifecycle/{signal_id}")
    async def api_signal_timeline(signal_id: str) -> dict[str, Any]:
        return fetch_signal_timeline(app.state.engine, signal_id)

    @app.get("/api/v1/readiness/latest")
    async def api_latest_readiness() -> dict[str, Any]:
        return fetch_latest_readiness(app.state.engine)

    @app.get("/api/v1/readiness/probes")
    async def api_readiness_probe_matrix() -> dict[str, Any]:
        return fetch_readiness_probe_matrix(app.state.engine)

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
