from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from snowball.halt import clear_halt, write_halt
from snowball.snapshot import build_snapshot
from snowball.stocks.snapshot import build_stocks_snapshot
from snowball.futures.snapshot import build_futures_snapshot
from snowball.crash.snapshot import build_crash_snapshot
from snowball.fed.snapshot import build_fed_snapshot
from snowball.state import AppState

log = logging.getLogger("snowball.dashboard")
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="Snowball", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "mode": state.settings.mode, "paper": True}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/snapshot")
    def snapshot() -> JSONResponse:
        return JSONResponse(build_snapshot(state))

    @app.get("/api/stream")
    async def stream() -> StreamingResponse:
        async def gen():
            while state.running:
                payload = json.dumps(build_snapshot(state), default=str)
                yield f"data: {payload}\n\n"
                await asyncio.sleep(1.0)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/api/halt")
    def halt() -> dict:
        if state.settings.mode != "paper":
            raise HTTPException(status_code=403, detail="Halt file ops only in paper mode")
        write_halt(state.settings.halt_file)
        log.info("halt file written via dashboard")
        return {"ok": True, "halt_active": True}

    @app.post("/api/resume")
    def resume() -> dict:
        if state.settings.mode != "paper":
            raise HTTPException(status_code=403, detail="Halt file ops only in paper mode")
        try:
            existed = clear_halt(state.settings.halt_file)
        except IsADirectoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.info("halt file cleared via dashboard")
        return {"ok": True, "halt_active": False, "was_present": existed}

    @app.get("/api/watcher")
    def watcher() -> JSONResponse:
        snap = build_snapshot(state)
        return JSONResponse(snap.get("watcher") or {})

    @app.get("/api/yolo-demon")
    def yolo_demon() -> JSONResponse:
        snap = build_snapshot(state)
        return JSONResponse(snap.get("yolo_demon") or {})

    @app.get("/api/clerk")
    def clerk() -> JSONResponse:
        snap = build_snapshot(state)
        return JSONResponse(snap.get("clerk") or {})

    @app.get("/api/earnings")
    def earnings() -> JSONResponse:
        snap = build_snapshot(state)
        return JSONResponse(snap.get("earnings") or {})

    @app.get("/api/stocks")
    def stocks() -> JSONResponse:
        return JSONResponse(build_stocks_snapshot(state))

    @app.get("/api/futures")
    def futures() -> JSONResponse:
        return JSONResponse(build_futures_snapshot(state))

    @app.get("/api/crash")
    def crash() -> JSONResponse:
        return JSONResponse(build_crash_snapshot(state))

    @app.get("/api/fed")
    def fed() -> JSONResponse:
        return JSONResponse(build_fed_snapshot(state))

    return app
