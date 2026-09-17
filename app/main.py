"""app/main.py — FastAPI application entry point."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from app.decoder import decode_loop, vessel_store, track_store
from app.sdr_reader import read_sdr

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self) -> None:
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.add(ws)
        logger.info("WebSocket connected; total=%d", len(self.active))

    def disconnect(self, ws: WebSocket) -> None:
        self.active.discard(ws)
        logger.info("WebSocket disconnected; total=%d", len(self.active))

    async def broadcast(self, vessels: dict, tracks: dict) -> None:
        if not self.active:
            return
        try:
            payload = json.dumps(
                {"vessels": vessels, "tracks": {m: list(t) for m, t in tracks.items()}},
                default=str,
            )
        except Exception as exc:
            logger.warning("JSON serialisation error: %s", exc)
            return
        dead = set()
        for ws in list(self.active):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Application lifespan — start background tasks
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    queue: asyncio.Queue = asyncio.Queue()
    t1 = asyncio.create_task(read_sdr(queue), name="sdr_reader")
    t2 = asyncio.create_task(decode_loop(queue, manager.broadcast), name="decoder")
    logger.info("Background tasks started")
    try:
        yield
    finally:
        t1.cancel()
        t2.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/vessels")
async def get_vessels():
    return {
        "vessels": vessel_store,
        "tracks": {m: list(t) for m, t in track_store.items()},
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    # Push current state immediately so the UI isn't blank on connect
    try:
        if vessel_store:
            payload = json.dumps(
                {"vessels": vessel_store, "tracks": {m: list(t) for m, t in track_store.items()}},
                default=str,
            )
            await ws.send_text(payload)
    except Exception as exc:
        logger.warning("Initial snapshot error: %s", exc)

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("WebSocket receive error: %s", exc)
    finally:
        manager.disconnect(ws)
