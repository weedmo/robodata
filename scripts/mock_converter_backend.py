#!/usr/bin/env python3
"""Mock converter backend for UI live-feedback QA.

Serves synthetic /api/converter/status + /api/converter/logs WS stream
so we can verify the new ConvertPage live UI (Recording N/M · elapsed,
pulse border, ghost fill, failure flash, Scanning pill, Activity LIVE)
without touching the real converter. Also handles POST /start and
related endpoints so clicking Convert/Start in the UI is a no-op.

Run:
    python3 scripts/mock_converter_backend.py

Then from frontend/:
    VITE_BACKEND_URL=http://localhost:8888 npm run dev
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Mutable so /status reflects progress as WS stream fires.
TASKS = [
    {
        "cell_task": "cellMOCK/pick_and_place",
        "total": 12,
        "done": 5,
        "pending": 7,
        "failed": 0,
        "retry": 0,
        "validation": {
            "quick": {"status": "not_run", "summary": "Not validated", "checked_at": None},
            "full": {"status": "not_run", "summary": "Not validated", "checked_at": None},
        },
    },
    {
        "cell_task": "cellMOCK/sort_objects",
        "total": 8,
        "done": 0,
        "pending": 8,
        "failed": 0,
        "retry": 0,
        "validation": {
            "quick": {"status": "not_run", "summary": "Not validated", "checked_at": None},
            "full": {"status": "not_run", "summary": "Not validated", "checked_at": None},
        },
    },
]


def _summary() -> str:
    done = sum(t["done"] for t in TASKS)
    total = sum(t["total"] for t in TASKS)
    return f"{done}/{total} converted"


@app.get("/api/cells/sources")
def sources():
    # Using 'lerobot' triggers CONVERTER_SOURCE gate in appChrome.ts
    # so the Converter indicator dot becomes visible in the top nav.
    return [
        {"name": "lerobot", "path": "/tmp/mock-lerobot", "cell_count": 0, "active": True}
    ]


@app.get("/api/cells/sources/{name}/cells")
def cells(name: str):
    return []


@app.get("/api/converter/status")
def status():
    return {
        "container_state": "running",
        "docker_available": True,
        "exit_code": None,
        "oom_killed": False,
        "finished_at": None,
        "tasks": TASKS,
        "summary": _summary(),
    }


@app.post("/api/converter/build")
@app.post("/api/converter/start")
@app.post("/api/converter/stop")
def noop_action():
    return {"status": "ok"}


@app.post("/api/converter/validate/{mode}")
def noop_validate(mode: str):
    return {"status": "ok", "mode": mode}


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def _emit_event(ws: WebSocket, ev: dict):
    await ws.send_text(json.dumps(ev))


async def _event_stream(ws: WebSocket):
    task = "cellMOCK/pick_and_place"
    total = 12

    # Phase 1: scan only → UI shows "Scanning…" pill on hero
    await _emit_event(ws, {"type": "scan", "ts": _ts(), "tasks": 2, "pending": 15})
    await asyncio.sleep(3)

    # Phase 2: converting kicks in → Scanning pill vanishes, card activates
    await _emit_event(ws, {"type": "converting", "ts": _ts(), "task": task, "count": 7})
    await asyncio.sleep(1)

    # Phase 3: iterate through remaining 7 recordings (index 6..12)
    for idx in range(6, total + 1):
        serial = f"R_{idx:03d}"

        await _emit_event(ws, {
            "type": "recording_start", "ts": _ts(),
            "recording": f"{task}/{serial}",
            "index": idx, "total": total,
        })
        # Elapsed timer should tick visibly (~5s → "00:05")
        await asyncio.sleep(5)

        if idx == 8:
            # Trigger failure flash. Failure flash lasts 500ms per event; we
            # emit a short burst 300ms apart so the QA screenshot window is
            # wide enough to reliably capture the red border.
            for _ in range(4):
                await _emit_event(ws, {
                    "type": "failed", "ts": _ts(),
                    "recording": f"{task}/{serial}",
                    "error_code": "E_QUALITY",
                    "reason": "HZ ratio below threshold (mock)",
                })
                await asyncio.sleep(0.3)
            TASKS[0]["failed"] += 1
            TASKS[0]["pending"] -= 1
        else:
            await _emit_event(ws, {
                "type": "converted", "ts": _ts(),
                "recording": f"{task}/{serial}",
                "frames": 1800 + idx * 7,
                "duration": 3.9 + (idx % 3) * 0.3,
            })
            TASKS[0]["done"] += 1
            TASKS[0]["pending"] -= 1

        await asyncio.sleep(1)

    # Phase 4: finalizing → done
    await _emit_event(ws, {"type": "finalizing", "ts": _ts(), "task": task})
    await asyncio.sleep(3)
    await _emit_event(ws, {"type": "finalized", "ts": _ts(), "task": task})


@app.websocket("/api/converter/logs")
async def logs_ws(websocket: WebSocket):
    await websocket.accept()
    try:
        await _event_stream(websocket)
        # Keep connection open silently after scripted run
        while True:
            await asyncio.sleep(60)
    except WebSocketDisconnect:
        return


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8888, log_level="warning")
