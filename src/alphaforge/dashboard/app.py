from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from alphaforge.config import load_config_from_env
from alphaforge.contracts import canonical_utc_timestamp
from alphaforge.persistence import init_db

from .queries import fetch_latest_readiness, fetch_recent_lifecycle, fetch_reject_summary, fetch_signal_timeline

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _status_payload(engine: Any) -> dict[str, Any]:
    cfg = load_config_from_env().runtime
    return {
        "configured_execution_mode": cfg.execution_mode,
        "global_kill_switch": cfg.global_kill_switch,
        "require_live_qualification": cfg.require_live_qualification,
        "enable_shadow_mode": cfg.enable_shadow_mode,
        "enable_canary_mode": cfg.enable_canary_mode,
        "required_live_exchanges": list(cfg.required_live_exchanges),
        "enable_binance_readonly_reconciliation": cfg.enable_binance_readonly_reconciliation,
        "runtime_process_status": "UNVERIFIED",
        "runtime_process_status_reason": "PERSISTED_HEARTBEAT_NOT_IMPLEMENTED",
        "latest_readiness": fetch_latest_readiness(engine),
    }


def create_app(database_url: str | None = None) -> FastAPI:
    app = FastAPI(title="AlphaForge Dashboard", version="0.1.0")
    resolved_database_url = database_url or load_config_from_env().persistence.database_url
    app.state.engine = init_db(resolved_database_url)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"service": "alphaforge-dashboard", "status": "ok", "timestamp": canonical_utc_timestamp()}

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request) -> HTMLResponse:
        status = _status_payload(app.state.engine)
        rejects = fetch_reject_summary(app.state.engine)
        lifecycle = fetch_recent_lifecycle(app.state.engine, limit=10)
        return TEMPLATES.TemplateResponse(
            request=request,
            name="overview.html",
            context={"status": status, "rejects": rejects, "lifecycle": lifecycle, "page": "overview"},
        )

    @app.get("/partials/status-bar", response_class=HTMLResponse)
    async def status_bar(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="partials/status_bar.html",
            context={"status": _status_payload(app.state.engine)},
        )

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

    @app.get("/readiness", response_class=HTMLResponse)
    async def readiness(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="readiness.html",
            context={"readiness": fetch_latest_readiness(app.state.engine), "page": "readiness"},
        )

    @app.get("/api/v1/runtime/status")
    async def api_runtime_status() -> dict[str, Any]:
        return _status_payload(app.state.engine)

    @app.get("/api/v1/rejects/summary")
    async def api_reject_summary() -> dict[str, Any]:
        return fetch_reject_summary(app.state.engine)

    @app.get("/api/v1/lifecycle/{signal_id}")
    async def api_signal_timeline(signal_id: str) -> dict[str, Any]:
        return fetch_signal_timeline(app.state.engine, signal_id)

    @app.get("/api/v1/readiness/latest")
    async def api_latest_readiness() -> dict[str, Any]:
        return fetch_latest_readiness(app.state.engine)

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
