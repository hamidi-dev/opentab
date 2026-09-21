import sqlite3
import tempfile
from pathlib import Path

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
