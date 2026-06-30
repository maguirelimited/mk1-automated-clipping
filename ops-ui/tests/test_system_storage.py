from __future__ import annotations

from pathlib import Path

import pytest

from ops_ui.config import Settings
from ops_ui.system import _scan_path, storage_usage


def _make_settings(*, runtime_root: Path, log_root: Path, data_dir: Path) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=5070,
        data_dir=data_dir,
        control_db_path=data_dir / "ops_ui.sqlite3",
        controls_file=data_dir / "controls.json",
        service_timeout_sec=0.01,
        journal_lines=1,
        funnel_run_timeout_sec=1.0,
        stuck_running_sec=7200.0,
        stuck_queued_sec=1800.0,
        stuck_uploading_sec=1800.0,
        services=(),
        runtime_root=runtime_root,
        log_root=log_root,
    )


def test_scan_path_counts_bytes_and_files(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_bytes(b"abc")
    (root / "sub" / "b.txt").write_bytes(b"defgh")
    total_bytes, file_count, exists = _scan_path(root)
    assert exists is True
    assert total_bytes == 8
    assert file_count == 2


def test_scan_path_single_file(tmp_path: Path) -> None:
    target = tmp_path / "db.sqlite3"
    target.write_bytes(b"0123456789")
    assert _scan_path(target) == (10, 1, True)


def test_scan_path_missing_path(tmp_path: Path) -> None:
    assert _scan_path(tmp_path / "nope") == (0, 0, False)


def test_storage_usage_reports_categories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_root = tmp_path / "runtime"
    log_root = tmp_path / "logs"
    data_dir = tmp_path / "ops-data"
    data_dir.mkdir()
    monkeypatch.setenv("OPS_OUTPUT_FUNNEL_DB", str(tmp_path / "absent_funnel.sqlite3"))

    output_dir = runtime_root / "video-automation" / "output"
    output_dir.mkdir(parents=True)
    (output_dir / "clip.mp4").write_bytes(b"x" * 64)
    log_root.mkdir(parents=True)
    (log_root / "app.log").write_bytes(b"y" * 16)

    settings = _make_settings(runtime_root=runtime_root, log_root=log_root, data_dir=data_dir)
    usage = storage_usage(settings, ttl=0.0)

    by_category = {row["category"]: row for row in usage["categories"]}
    assert by_category["Generated clips (public)"]["bytes"] == 64
    assert by_category["Generated clips (public)"]["file_count"] == 1
    assert by_category["Logs"]["bytes"] == 16
    assert by_category["Output-funnel DB"]["exists"] is False
    assert by_category["Source input videos"]["exists"] is False
    assert usage["total_bytes"] == 80
    assert usage["categories"] == sorted(
        usage["categories"], key=lambda row: row["bytes"], reverse=True
    )
