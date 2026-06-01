# Simple Append-Only Event Store

A small HTTP service that stores events in an append-only log file and reads them back by ID — even after a crash and restart. Built with Python and FastAPI.

---

## Setup

**Requirements:** Python 3.11+

```bash
# 1. Clone and enter the repo
git clone <your-repo-url>
cd event-store

# 2. Install dependencies
uv sync

# 3. Start the server
uv run uvicorn app.main:app --reload
```

The server starts on `http://localhost:8000`. Interactive API docs are at `http://localhost:8000/docs`.

---

## API — curl examples

### POST /events
Accepts any JSON body. Stamps `id` (UUID v4) and `createdAt` (ISO-8601 UTC). Returns 201 with the full event.

> Replace `python3 -m json.tool` with `jq` if you have jq installed for prettier output.

```bash
curl -s -X POST http://localhost:8000/events \
  -H "Content-Type: application/json" \
  -d '{"user": "ada", "action": "login"}' | python3 -m json.tool
```

```json
{
  "id": "a1b2c3d4-...",
  "createdAt": "2024-06-01T10:00:00.123456+00:00",
  "user": "ada",
  "action": "login"
}
```

### GET /events/:id
Reads the event by ID using a direct byte-seek. Returns 404 if not found.

```bash
# Replace the ID with one returned by POST
curl -s http://localhost:8000/events/a1b2c3d4-... | python3 -m json.tool
```

```bash
# 404 example
curl -s http://localhost:8000/events/does-not-exist
# {"detail": "Event 'does-not-exist' not found."}
```

### GET /stats
Returns the total event count and log file size in bytes.

```bash
curl -s http://localhost:8000/stats | python3 -m json.tool
```

```json
{
  "total": 3,
  "bytes": 466
}
```

---

## Running the tests

```bash
uv run pytest tests/
```

22 tests across three classes:
- `TestEventStoreUnit` — store.py in isolation (append, seek, unicode, offset accuracy)
- `TestHTTPEndpoints` — full HTTP layer via FastAPI TestClient
- `TestCrashRecovery` — write → destroy instance → rebuild → verify every ID readable

---

## Architecture

```
POST /events                        GET /events/:id
     │                                    │
     ▼                                    ▼
┌─────────────────────────────────────────────────────┐
│                     FastAPI (main.py)                │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│                  EventStore (store.py)               │
│                                                      │
│  _index: dict[id → {offset, length}]  ◄── O(1) read │
│                          │                           │
│                          │ seek(offset), read(length)│
│                          ▼                           │
│  ┌────────────────────────────────────────────────┐  │
│  │  events.log  (append-only, one JSON line each) │  │
│  │                                                │  │
│  │  {"id":"a1b2…","createdAt":"…","user":"ada"}\n │  │
│  │  {"id":"c3d4…","createdAt":"…","order":"x"}\n  │  │
│  │  {"id":"e5f6…","createdAt":"…","amount":99}\n  │  │
│  └────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘

Write path (POST /events)
  1. Serialise event → compact JSON + \n
  2. Open events.log in append-binary mode
  3. Record byte offset before write (f.tell())
  4. Write + fsync  ← durable before we update anything
  5. Store _index[id] = { offset, length }
  6. Return 201 with full event

Read path (GET /events/:id)
  1. Look up _index[id] → { offset, length }   O(1)
  2. Open events.log, seek to offset
  3. Read exactly `length` bytes
  4. Decode UTF-8, parse JSON, return

Recovery path (server startup)
  1. Stream events.log line by line
  2. Track running byte offset
  3. Parse each line, store _index[id] = { offset, length }
  4. Print recovered count
```

---

## Core concepts

### Why append-only is safer than overwriting in place

When you overwrite a record in a file, the write might be interrupted halfway — by a crash, a power cut, or an OS buffer flush at the wrong moment. You end up with half the old data and half the new data at the same location. The record is corrupt and there is no way to recover it.

Appending is different. The existing data is never touched. If the process dies mid-write, the worst that can happen is a partial line at the end of the file — the rest of the file is intact. On restart, the recovery code skips any line that isn't valid JSON and keeps going. Every complete event that was written before the crash is still there.

This is the same reason databases use a Write-Ahead Log (WAL): write the intent to an append-only file first, then apply it. If the crash happens before the apply, you replay the log. If it happens after, the log confirms the write was already complete.

### Why the index makes reads fast

`events.log` could contain millions of lines. Without an index, reading a single event by ID means scanning the entire file from the top until you find the right line — O(n), getting slower as the log grows.

The in-memory index stores exactly two numbers per event: the byte offset where the line starts, and how many bytes long it is. A read becomes: seek the file pointer to `offset`, read `length` bytes. That's O(1) regardless of how large the log is or how many events are in it. The trade-off is that the index lives in memory and disappears on crash — but it can always be rebuilt from the log in a single pass on startup, which is exactly what recovery does.

---

## Recovery screenshot

![Recovery log screenshot](./screenshots/recovery.png)

---

## What you struggled with

- The first thing that tripped me up was the byte offset tracking. I knew I needed to record where in the file each event lived, but I wasn't immediately sure how to get that position before writing. I eventually found f.tell(), it returns the current stream position in bytes, so I call it right before the write, and that number becomes the offset I store in the index. Simple in hindsight, but it took me a moment to find the right tool.

- The other struggle was the file mode. I knew I needed to append, but I initially reached for "a" (text append mode). The problem is that text mode can silently transform newline characters, and more importantly, `f.tell()` in text mode doesn't reliably return a byte offset, it returns an opaque value that isn't guaranteed to correspond to actual byte positions. Switching to "ab" (binary append mode) fixed both problems: offsets are exact byte positions, and the file is written exactly as encoded.

---

## What you learned

- The biggest thing was understanding why indexing matters at a fundamental level. Without an index, GET /events/:id would have to scan events.log from the top every time whihc is O(n), getting slower as the log file grows. With the in-memory index storing just two numbers per event (offset and length), every read is a direct seek to the right byte position making it O(1), regardless of how large the file gets. This felt like solveing a DSA problem.

- I also learned how to accept freeform JSON in FastAPI. I'm used to defining a Pydantic schema and annotating the endpoint parameter with it. But when the payload can be literally anything, that doesn't work. I initially tried payload: Annotated\[dict, Body()] which felt like a lot when I could just do `request.json()` to get the json body.

- The ensure_ascii flag in json.dumps() was a small but important discovery. By default it's True, which means any non-ASCII character (Arabic, Japanese, emoji) gets escaped to \uXXXX. The file becomes less readable and the byte lengths change in ways that can be surprising. Setting `ensure_ascii=False` lets the actual UTF-8 characters write to the file as-is, which is both correct and readable. This also motivated writing the tests with some unicode characters to make sure it worked as expected.

- Finally, building this introduced me properly to the Write-Ahead Log (LOG) pattern. I knew databases were crash-safe and uses WAL but I haven't actually looked into it or tried to understand why and how it works. The idea is that you write your intent to an append-only log before applying it, so if the process dies mid-operation, you can replay the log on restart and reconstruct exactly what was committed. This project is a small version of that: the log is the truth, the in-memory index is just a layer on top thtt makes it fast to access, and recovery is just replaying the log to rebuild that layer that allows us to access it fast.

---

## Resources consulted

- [Python Binary I/O docs](https://docs.python.org/3/library/io.html#binary-i-o): understood `"ab"` mode and why binary append is required for reliable byte offsets
- [`io.IOBase.tell()` docs](https://docs.python.org/3/library/io.html#io.IOBase.tell): confirmed that `tell()` returns the current stream position in bytes, which is the offset stored in the index
- [FastAPI lifespan docs](https://fastapi.tiangolo.com/advanced/events/?h=lifespan#lifespan): used this to implement the startup recovery flow correctly
- [Reddit: WAL vs append-only log](https://www.reddit.com/r/AskComputerScience/comments/1dfazpm/what_is_the_difference_between_a_write_ahead_log/): the first comment here gave a clearer intuition for what this is about and made me intrested in what WAL is.

---

## Why this made me a better backend developer

It also gave me a real understanding of why indexing matters. Without the in-memory index, every GET /events/:id would read the entire file from top to bottom looking for a match. Imagine an ID that doesn't exist and you have hundreds of thousands of records — you would wait for the entire file to be scanned and still get a 404 at the end. The index makes that O(1) regardless of how large the log grows.

The trade-off is real though: the index lives in memory, and on startup the entire log has to be replayed to rebuild it. On a huge log file that startup time grows, and the index itself consumes memory proportional to the number of events. But it is absolutely worth it — slow startup once is far better than slow reads on every single request forever.
