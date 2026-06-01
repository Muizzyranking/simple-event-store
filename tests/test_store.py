import json
import os
import uuid

from fastapi.testclient import TestClient


def make_store(path: str):
    """Return a fresh EventStore pointed at the given path."""
    from app.services import EventStore

    return EventStore(log_path=path)


def make_app(path: str):
    """
    Return a TestClient whose EventStore uses a temp log file.
    We monkey-patch the module-level `store` variable before each test.
    """
    import app.main as app_module
    from app.services import EventStore

    app_module.store = EventStore(log_path=path)
    return TestClient(app_module.app)


class TestEventStoreUnit:
    def test_append_and_get_roundtrip(self, tmp_path):
        """Written event can be read back with identical content."""
        log = str(tmp_path / "events.log")
        s = make_store(log)

        event = {
            "id": str(uuid.uuid4()),
            "createdAt": "2024-01-01T00:00:00+00:00",
            "msg": "hello",
        }
        s.append(event)

        result = s.get(event["id"])
        assert result == event

    def test_get_unknown_id_returns_none(self, tmp_path):
        """get() returns None for an id that was never written."""
        log = str(tmp_path / "events.log")
        s = make_store(log)
        assert s.get("does-not-exist") is None

    def test_append_updates_index(self, tmp_path):
        """Index has an entry with correct offset/length after append."""
        log = str(tmp_path / "events.log")
        s = make_store(log)

        event = {"id": "abc-123", "createdAt": "2024-01-01T00:00:00+00:00", "x": 1}
        s.append(event)

        entry = s._index.get("abc-123")
        assert entry is not None
        assert entry.offset == 0  # first line starts at byte 0
        assert entry.length > 0

    def test_multiple_appends_correct_offsets(self, tmp_path):
        """Each event's offset points to its own line — not the previous one."""
        log = str(tmp_path / "events.log")
        s = make_store(log)

        ids = []
        for i in range(5):
            eid = str(uuid.uuid4())
            ids.append(eid)
            s.append({"id": eid, "createdAt": "2024-01-01T00:00:00+00:00", "i": i})

        offsets = [s._index[eid].offset for eid in ids]
        # Offsets must be strictly increasing
        assert offsets == sorted(offsets)
        assert len(set(offsets)) == 5  # all distinct

    def test_stats_total_and_bytes(self, tmp_path):
        """stats() returns the correct event count and a non-zero byte size."""
        log = str(tmp_path / "events.log")
        s = make_store(log)

        for i in range(3):
            s.append(
                {
                    "id": str(uuid.uuid4()),
                    "createdAt": "2024-01-01T00:00:00+00:00",
                    "n": i,
                }
            )

        st = s.stats()
        assert st["total"] == 3
        assert st["bytes"] > 0

    def test_stats_bytes_matches_file_size(self, tmp_path):
        """stats()['bytes'] equals the actual file size on disk."""
        log = str(tmp_path / "events.log")
        s = make_store(log)
        s.append({"id": str(uuid.uuid4()), "createdAt": "now", "data": "x"})

        assert s.stats()["bytes"] == os.path.getsize(log)

    def test_log_is_append_only(self, tmp_path):
        """Writing two events never shrinks the file."""
        log = str(tmp_path / "events.log")
        s = make_store(log)

        s.append({"id": "a", "createdAt": "now"})
        size_after_first = os.path.getsize(log)

        s.append({"id": "b", "createdAt": "now"})
        size_after_second = os.path.getsize(log)

        assert size_after_second > size_after_first

    def test_log_format_one_json_per_line(self, tmp_path):
        """Each line in the log is valid JSON and ends with exactly one newline."""
        log = str(tmp_path / "events.log")
        s = make_store(log)

        for i in range(3):
            s.append({"id": str(i), "createdAt": "now"})

        with open(log, encoding="utf-8") as f:
            lines = f.readlines()

        assert len(lines) == 3
        for line in lines:
            assert line.endswith("\n")
            parsed = json.loads(line)  # must not raise
            assert "id" in parsed

    def test_unicode_payload_roundtrip(self, tmp_path):
        """Events containing unicode characters survive write → read intact."""
        log = str(tmp_path / "events.log")
        s = make_store(log)

        event = {
            "id": str(uuid.uuid4()),
            "createdAt": "2024-01-01T00:00:00+00:00",
            "greeting": "héllo wörld 🌍",
            "arabic": "مرحبا",
        }
        s.append(event)

        result = s.get(event["id"])
        assert result is not None
        assert result["greeting"] == event["greeting"]
        assert result["arabic"] == event["arabic"]


class TestHTTPEndpoints:
    def test_post_returns_201_with_full_event(self, tmp_path):
        """POST /events → 201, body contains id, createdAt, and caller's fields."""
        client = make_app(str(tmp_path / "events.log"))
        resp = client.post("/events", json={"user": "ada", "action": "login"})

        assert resp.status_code == 201
        body = resp.json()
        assert "id" in body
        assert "createdAt" in body
        assert body["user"] == "ada"
        assert body["action"] == "login"

    def test_post_generates_unique_ids(self, tmp_path):
        """Two POSTs produce two different UUIDs."""
        client = make_app(str(tmp_path / "events.log"))
        id1 = client.post("/events", json={"n": 1}).json()["id"]
        id2 = client.post("/events", json={"n": 2}).json()["id"]
        assert id1 != id2

    def test_get_returns_posted_event(self, tmp_path):
        """GET /events/:id returns exactly what POST stored."""
        client = make_app(str(tmp_path / "events.log"))
        posted = client.post("/events", json={"order": "babbage-engine"}).json()

        fetched = client.get(f"/events/{posted['id']}").json()
        assert fetched == posted

    def test_get_unknown_id_returns_404(self, tmp_path):
        """GET /events/:id with an unknown id → 404."""
        client = make_app(str(tmp_path / "events.log"))
        resp = client.get("/events/totally-made-up-id")
        assert resp.status_code == 404

    def test_stats_reflects_writes(self, tmp_path):
        """GET /stats total increments with each POST."""
        client = make_app(str(tmp_path / "events.log"))

        for i in range(4):
            client.post("/events", json={"i": i})

        st = client.get("/stats").json()
        assert st["total"] == 4
        assert st["bytes"] > 0

    def test_post_freeform_payload(self, tmp_path):
        """POST accepts deeply nested arbitrary JSON."""
        client = make_app(str(tmp_path / "events.log"))
        payload = {"nested": {"list": [1, 2, 3], "flag": True, "score": 9.9}}
        resp = client.post("/events", json=payload)

        assert resp.status_code == 201
        body = resp.json()
        assert body["nested"]["list"] == [1, 2, 3]
        assert body["nested"]["flag"] is True

    def test_health(self, tmp_path):
        client = make_app(str(tmp_path / "events.log"))
        assert client.get("/health").json() == {"status": "ok"}


class TestCrashRecovery:
    def test_index_rebuilt_after_restart(self, tmp_path):
        """
        Core recovery test.
        Write N events → discard the store instance (simulates crash) →
        create a new store pointing at the same log → every id is still readable.
        """
        log = str(tmp_path / "events.log")

        s1 = make_store(log)
        written = []
        for i in range(5):
            event = {
                "id": str(uuid.uuid4()),
                "createdAt": "2024-01-01T00:00:00+00:00",
                "sequence": i,
            }
            s1.append(event)
            written.append(event)

        del s1

        s2 = make_store(log)

        for event in written:
            recovered = s2.get(event["id"])
            assert recovered == event, f"Event {event['id']} not recovered correctly"

    def test_recovery_count_is_correct(self, tmp_path, capsys):
        """Recovery log message reports the exact number of events in the file."""
        log = str(tmp_path / "events.log")

        s1 = make_store(log)
        for i in range(7):
            s1.append({"id": str(uuid.uuid4()), "createdAt": "now", "i": i})
        del s1

        make_store(log)  # triggers recovery
        captured = capsys.readouterr()
        assert "7" in captured.out

    def test_recovery_stats_match(self, tmp_path):
        """stats() on a recovered store reflects the correct total and byte count."""
        log = str(tmp_path / "events.log")

        s1 = make_store(log)
        for i in range(3):
            s1.append({"id": str(uuid.uuid4()), "createdAt": "now", "i": i})
        original_stats = s1.stats()
        del s1

        s2 = make_store(log)
        recovered_stats = s2.stats()

        assert recovered_stats["total"] == original_stats["total"]
        assert recovered_stats["bytes"] == original_stats["bytes"]

    def test_recovery_with_unicode_events(self, tmp_path):
        """Unicode payloads survive crash → recovery → read without corruption."""
        log = str(tmp_path / "events.log")

        event = {
            "id": str(uuid.uuid4()),
            "createdAt": "now",
            "text": "日本語テスト 🚀",
        }

        s1 = make_store(log)
        s1.append(event)
        del s1

        s2 = make_store(log)
        result = s2.get(event["id"])
        assert result is not None
        assert result["text"] == event["text"]

    def test_partial_write_does_not_corrupt_recovery(self, tmp_path):
        """
        A corrupt line in the log is skipped; valid events around it are recovered.
        """
        log = str(tmp_path / "events.log")

        # Write two good events manually, inject a corrupt line in between
        good1 = {"id": "good-1", "createdAt": "now", "n": 1}
        good2 = {"id": "good-2", "createdAt": "now", "n": 2}

        with open(log, "ab") as f:
            f.write((json.dumps(good1) + "\n").encode("utf-8"))
            f.write(b"THIS IS NOT JSON\n")  # corrupt line
            f.write((json.dumps(good2) + "\n").encode("utf-8"))

        s = make_store(log)
        assert s.get("good-1") is not None
        assert s.get("good-2") is not None
        assert s.stats()["total"] == 2  # corrupt line not counted
