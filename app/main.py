from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.services import EventStore

store: EventStore


@asynccontextmanager
async def lifespan(_: FastAPI):
    global store
    store = EventStore()
    yield


app = FastAPI(
    title="Append-Only Event Store",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Routes
# =============
@app.post("/events", status_code=status.HTTP_201_CREATED)
async def append_event(request: Request):
    """
    Accept any JSON body.
    Stamp { id: UUIDv4, createdAt: ISO-8601 UTC }.
    Append to log. Return 201 with the full event.
    """
    try:
        payload: dict[str, Any] = await request.json()
    except Exception as e:
        raise HTTPException(
            status_code=400, detail="Request body must be valid JSON."
        ) from e

    try:
        event = store.append(payload)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse(content=event, status_code=201)


@app.get("/events/{event_id}")
async def get_event(event_id: str):
    """
    Look up id in the in-memory index.
    """
    event = store.get(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"Event '{event_id}' not found.")
    return event


@app.get("/stats")
async def get_stats():
    """Return { total, bytes } from the in-memory index and log file size."""
    return store.stats()


@app.get("/health")
async def health():
    return {"status": "ok"}
