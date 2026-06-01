import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

LOG_FILE = "events.log"


@dataclass
class IndexEntry:
    offset: int  # byte position where this line starts in the file
    length: int  # byte length of the line (not counting the trailing \n)


class EventStore:
    def __init__(self, log_path: str = LOG_FILE):
        self.log_path = log_path
        self._index: dict[str, IndexEntry] = {}
        self._lock = threading.Lock()
        self._recover()

    def _recover(self) -> None:
        """Stream the log and rebuild the in-memory index from byte offsets."""
        if not os.path.exists(self.log_path):
            print("[store] No log file found — starting fresh.")
            return

        recovered = 0
        offset = 0

        with open(self.log_path, "rb") as f:
            for raw_line in f:
                line_bytes = len(raw_line)
                stripped = raw_line.rstrip(b"\n")

                if stripped:
                    try:
                        event = json.loads(stripped)
                        event_id = event.get("id")
                        if event_id:
                            self._index[event_id] = IndexEntry(
                                offset=offset,
                                length=len(stripped),
                            )
                            recovered += 1
                    except json.JSONDecodeError:
                        pass  # skip corrupt lines, never crash

                offset += line_bytes

        print(f"[store] Recovery complete — {recovered} event(s) restored from log.")

    def append(self, payload: dict) -> dict[str, Any]:
        """
        Serialise event as UTF-8 JSON, append to log, fsync, update index.
        """
        event = {
            "id": str(uuid.uuid4()),
            "createdAt": datetime.now(UTC).isoformat(),
            **payload,
        }
        event_id = event.get("id")
        if not event_id:
            raise ValueError("Event must contain an 'id' field.")

        line = json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"
        encoded = line.encode("utf-8")
        data_length = len(encoded) - 1

        with self._lock:
            with open(self.log_path, "ab") as f:
                offset = f.tell()
                f.write(encoded)
                f.flush()
                os.fsync(f.fileno())

            self._index[event_id] = IndexEntry(offset=offset, length=data_length)

        return event

    def get(self, event_id: str) -> dict | None:
        """
        Seek directly to the event's byte offset and read exactly `length` bytes.
        """
        entry = self._index.get(event_id)
        if entry is None:
            return None

        with open(self.log_path, "rb") as f:
            f.seek(entry.offset)
            raw = f.read(entry.length)

        return json.loads(raw.decode("utf-8"))

    def stats(self) -> dict:
        size = os.path.getsize(self.log_path) if os.path.exists(self.log_path) else 0
        return {
            "total": len(self._index),
            "bytes": size,
        }
