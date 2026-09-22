import json
import sqlite3
import tempfile
from pathlib import Path

from opentab import diagnostics as debug
from opentab.persistence import usage_cache


def _row(rid, session, tokens=1):
    return (
        (rid, str(rid), session, "assistant", 10, 20, rid),
        (rid, str(rid), session, "assistant", 10, "p/m", 0.0, tokens, 2, 0, 0, 0),
    )


def test_scalar_sidecar_transactional_updates_deletes_and_scoped_reads():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "usage.sqlite3")
        payload = {
            "version": 1,
            "identity": ["source", 1, 2],
            "built_at": 1000,
            "rows": [_row(1, "one"), _row(2, "two")],
        }
        usage_cache.write(path, payload)
        assert usage_cache.read(path) == payload
        assert usage_cache.read(path, ["one"])["rows"] == [_row(1, "one")]
        reader = sqlite3.connect(path)
        reader.execute("create table writes(rid)")
        reader.execute(
            "create trigger updated after update on usage begin insert into writes values(new.rid); end"
        )
        reader.commit()
        payload["rows"] = [_row(1, "one"), _row(2, "two", 3), _row(3, "three")]
        usage_cache.write(path, payload)
        assert reader.execute("select rid from writes").fetchall() == [(2,)]
        payload["rows"] = [_row(3, "one")]
        usage_cache.write(path, payload)
        assert usage_cache.read(path) == payload
        assert usage_cache.read(path, ["two"])["rows"] == []
        reader.close()


def test_scalar_sidecar_failed_write_rolls_back_metadata_and_rows():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "usage.sqlite3")
        payload = {
            "version": 1,
            "identity": ["source", 1, 2],
            "built_at": 1000,
            "rows": [_row(1, "one")],
        }
        usage_cache.write(path, payload)
        invalid = {**payload, "built_at": 2000, "rows": [_row(2, "two", object())]}
        try:
            usage_cache.write(path, invalid)
        except sqlite3.Error:
            pass
        else:
            raise AssertionError("unsupported value accepted")
        assert usage_cache.read(path) == payload


def test_scalar_sidecar_debug_reports_scoped_reuse_and_actual_changed_rows():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "private-cache.sqlite3")
        log = Path(tmp) / "debug.jsonl"
        payload = {
            "version": 1,
            "identity": ["private-source", 1, 2],
            "built_at": 1000,
            "rows": [_row(1, "private-one"), _row(2, "private-two")],
        }
        with debug.session(filename=str(log)):
            usage_cache.write(path, payload)
            usage_cache.write(path, payload)
            payload["rows"] = [_row(1, "private-one", 3)]
            usage_cache.write(path, payload)
            assert usage_cache.read(path, iter(["private-one"])) == payload
        records = [json.loads(line) for line in log.read_text().splitlines()]
        writes = [r for r in records if r["event"] == "usage.sidecar_written"]
        assert [(r["upserted"], r["deleted"], r["unchanged"]) for r in writes] == [
            (2, 0, 0),
            (0, 0, 2),
            (1, 1, 0),
        ]
        scoped = next(r for r in records if r["event"] == "usage.sidecar_rows.end")
        assert scoped["sessions"] == scoped["rows"] == 1
        assert "private-" not in log.read_text() and tmp not in log.read_text()
