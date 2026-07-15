from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ops_ui.store import ControlStore


def test_connect_closes_connection(tmp_path: Path) -> None:
    store = ControlStore(tmp_path / "ops.sqlite3")
    store.init_db()
    conn: sqlite3.Connection | None = None
    with store.connect() as connection:
        conn = connection
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    assert conn is not None
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_get_clip_reviews_batch(tmp_path: Path) -> None:
    store = ControlStore(tmp_path / "ops.sqlite3")
    store.init_db()
    store.set_clip_review("job_a", "clip_1", status="approved")
    store.set_clip_review("job_b", "clip_2", status="rejected")

    reviews = store.get_clip_reviews(
        [
            ("job_a", "clip_1"),
            ("job_b", "clip_2"),
            ("job_c", "clip_9"),
        ]
    )

    assert reviews["job_a::clip_1"]["status"] == "approved"
    assert reviews["job_b::clip_2"]["status"] == "rejected"
    assert "job_c::clip_9" not in reviews
    assert store.get_clip_review("job_a", "clip_1") == reviews["job_a::clip_1"]
