from __future__ import annotations

import os
import time
from pathlib import Path

from ops_ui.app import _parse_ttl, create_app
from ops_ui.config import ServiceConfig, Settings
from ops_ui.system import CommandResult, cleanup_preview, run_retention_cleanup


DAY = 86400.0


def _settings(tmp_path: Path, *, runtime_root: Path | None = None, code_root: Path | None = None) -> Settings:
    kwargs: dict = {}
    if code_root is not None:
        kwargs["code_root"] = code_root
    return Settings(
        host="127.0.0.1",
        port=5070,
        data_dir=tmp_path,
        control_db_path=tmp_path / "ops.sqlite3",
        controls_file=tmp_path / "controls.json",
        service_timeout_sec=0.01,
        journal_lines=1,
        funnel_run_timeout_sec=1.0,
        stuck_running_sec=100.0,
        stuck_queued_sec=50.0,
        stuck_uploading_sec=50.0,
        environment="dev",
        runtime_root=runtime_root or (tmp_path / "runtime"),
        **kwargs,
        services=(
            ServiceConfig(key="source-input", label="source-input", base_url="http://127.0.0.1:9", systemd_unit="a"),
            ServiceConfig(key="video-automation", label="video-automation", base_url="http://127.0.0.1:9", systemd_unit="b"),
            ServiceConfig(key="output-funnel", label="output-funnel", base_url="http://127.0.0.1:9", systemd_unit="c"),
        ),
    )


def _write(path: Path, *, age_days: float, content: bytes = b"x" * 100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    stamp = time.time() - age_days * DAY
    os.utime(path, (stamp, stamp))


def _age_dir(path: Path, *, age_days: float) -> None:
    stamp = time.time() - age_days * DAY
    os.utime(path, (stamp, stamp))


def _build_tree(runtime_root: Path) -> None:
    va = runtime_root / "video-automation"
    # Fresh job — nothing should be deleted.
    _write(va / "jobs" / "job_fresh" / "input_src.mp4", age_days=0)
    _write(va / "jobs" / "job_fresh" / "clips" / "clip1.mp4", age_days=0)
    _write(va / "jobs" / "job_fresh" / "report.json", age_days=0)
    # Aged media but folder kept (report.json is fresh -> not whole-aged).
    _write(va / "jobs" / "job_media" / "input_src.mp4", age_days=20)
    _write(va / "jobs" / "job_media" / "clips" / "clip_old.mp4", age_days=20)
    _write(va / "jobs" / "job_media" / "report.json", age_days=0)
    # Whole-aged folder (everything older than metadata TTL).
    _write(va / "jobs" / "job_whole" / "input_src.mp4", age_days=30)
    _write(va / "jobs" / "job_whole" / "clips" / "c.mp4", age_days=30)
    _write(va / "jobs" / "job_whole" / "report.json", age_days=30)
    # Public/scratch/orphan roots.
    _write(va / "output" / "old.mp4", age_days=20)
    _write(va / "output" / "new.mp4", age_days=0)
    _write(va / "temp" / "old_dir" / "inner.bin", age_days=20)
    _age_dir(va / "temp" / "old_dir", age_days=20)
    _write(va / "temp" / "fresh.bin", age_days=0)
    _write(va / "input" / "orphan_old.mp4", age_days=20)
    _write(va / "input" / "orphan_new.mp4", age_days=0)
    # Preserved category — must never appear in the preview.
    _write(va / "analytics" / "feedback.jsonl", age_days=30)


def _build_source_input_tree(runtime_root: Path) -> None:
    si = runtime_root / "source-input"
    # tmp/ leaked downloads (per-funnel subdirs). Aged subdir swept, fresh kept.
    _write(si / "tmp" / "funnel_a" / "leaked.part", age_days=20)
    _age_dir(si / "tmp" / "funnel_a", age_days=20)
    _write(si / "tmp" / "funnel_b" / "fresh.part", age_days=0)
    # inputs/rejected/ media + .reason.txt sidecars (per-funnel subdirs).
    _write(si / "inputs" / "rejected" / "funnel_a" / "bad.mp4", age_days=20)
    _write(si / "inputs" / "rejected" / "funnel_a" / "bad.mp4.reason.txt", age_days=20)
    _age_dir(si / "inputs" / "rejected" / "funnel_a", age_days=20)
    _write(si / "inputs" / "rejected" / "funnel_b" / "recent.mp4", age_days=0)
    # PRESERVED: state (seen_urls + ledger) and inputs/ready, even when aged.
    _write(si / "state" / "seen_urls.json", age_days=30)
    _write(si / "state" / "input_jobs" / "job1.json", age_days=30)
    _write(si / "inputs" / "ready" / "funnel_a" / "source.mp4", age_days=30)


def _by_category(preview: dict) -> dict[str, dict]:
    return {row["category"]: row for row in preview["categories"]}


def test_cleanup_preview_counts_and_exclusions(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    _build_tree(runtime_root)
    settings = _settings(tmp_path, runtime_root=runtime_root)

    preview = cleanup_preview(settings, media_days=5, metadata_days=14)
    cats = _by_category(preview)

    # Only aged media inside the not-fully-aged folder counts here (1 each),
    # NOT the fresh job and NOT the whole-aged job (counted once below).
    assert cats["Per-job source copies (input_*)"]["file_count"] == 1
    assert cats["Per-job clip mirrors"]["file_count"] == 1
    assert cats["Output clips"]["file_count"] == 1
    assert cats["Temp/scratch"]["file_count"] == 1
    assert cats["Orphan input"]["file_count"] == 1
    # Whole-aged job folder counts every file inside it (3 files).
    assert cats["Whole aged job folders"]["file_count"] == 3

    assert preview["total_file_count"] == 8
    assert preview["total_bytes"] > 0
    assert preview["media_days"] == 5
    assert preview["metadata_days"] == 14

    # Preserved analytics never shows up.
    assert "feedback.jsonl" not in str(preview)


def test_cleanup_preview_source_input_categories(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    _build_source_input_tree(runtime_root)
    settings = _settings(tmp_path, runtime_root=runtime_root)

    preview = cleanup_preview(settings, media_days=5, metadata_days=14)
    cats = _by_category(preview)

    # tmp/: one aged per-funnel subdir swept (1 file), fresh subdir kept.
    assert cats["Source-input temp"]["file_count"] == 1
    assert cats["Source-input temp"]["bytes"] > 0
    # rejected/: one aged per-funnel subdir swept incl. media + .reason.txt
    # sidecar (2 files), fresh subdir kept.
    assert cats["Source-input rejected"]["file_count"] == 2
    assert cats["Source-input rejected"]["bytes"] > 0

    # Preserved state + ledger + inputs/ready never appear as deletable.
    blob = str(preview)
    assert "seen_urls.json" not in blob
    assert "input_jobs" not in blob
    assert str(runtime_root / "source-input" / "state") not in [c["path"] for c in preview["categories"]]
    assert str(runtime_root / "source-input" / "inputs" / "ready") not in [
        c["path"] for c in preview["categories"]
    ]


def test_cleanup_preview_missing_roots_returns_zero(tmp_path: Path) -> None:
    settings = _settings(tmp_path, runtime_root=tmp_path / "does-not-exist")
    preview = cleanup_preview(settings, media_days=5, metadata_days=14)
    assert preview["total_file_count"] == 0
    assert preview["total_bytes"] == 0
    assert len(preview["categories"]) == 8


def test_parse_ttl_validation() -> None:
    assert _parse_ttl("7", "media") == (7, None)
    value, err = _parse_ttl("0", "media")
    assert value == 0 and err and "non-positive" in err
    value, err = _parse_ttl("abc", "metadata")
    assert value == 0 and err and "Invalid" in err
    value, err = _parse_ttl("", "media")
    assert err is not None


def test_run_retention_cleanup_rejects_non_positive(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    result = run_retention_cleanup(settings, media_days=0, metadata_days=14)
    assert result.ok is False
    assert ">= 1" in result.message


def test_run_retention_cleanup_invokes_without_sudo(tmp_path: Path, monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    captured: dict = {}

    def _fake_run(args, *, timeout, env=None):
        captured["args"] = list(args)
        captured["env"] = dict(env or {})
        return CommandResult(True, "[tag] summary removed=0 bytes=0 dry_run=0", 0)

    monkeypatch.setattr("ops_ui.system._run", _fake_run)
    runtime_root = tmp_path / "runtime"
    settings = _settings(tmp_path, runtime_root=runtime_root, code_root=repo_root)

    result = run_retention_cleanup(settings, media_days=5, metadata_days=14)
    assert result.ok is True

    # No sudo elevation: invoked directly as the owning user.
    assert "sudo" not in captured["args"]
    assert captured["args"][0] == "bash"
    assert captured["args"][1].endswith("deploy/scripts/retention-sweeper.sh")
    assert captured["args"][2] == "dev"
    # Runtime root + env propagate reliably (no sudo to strip them).
    assert captured["env"]["MK04_RUNTIME_ROOT"] == str(runtime_root)
    assert captured["env"]["MK04_ENV"] == "dev"


def test_cleanup_preview_route_renders(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    _build_tree(runtime_root)
    app = create_app(_settings(tmp_path, runtime_root=runtime_root))
    response = app.test_client().post("/cleanup/preview", data={"media_days": "5", "metadata_days": "14"})
    assert response.status_code == 200
    assert b"Storage cleanup" in response.data
    assert b"Whole aged job folders" in response.data
    assert b"would be freed" in response.data


def test_cleanup_run_without_confirm_does_not_execute(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple] = []

    def _fake(*args, **kwargs):  # pragma: no cover - should never run
        calls.append((args, kwargs))
        return CommandResult(True, "summary removed=1 bytes=1", 0)

    monkeypatch.setattr("ops_ui.app.run_retention_cleanup", _fake)
    app = create_app(_settings(tmp_path))
    response = app.test_client().post("/cleanup/run", data={"media_days": "5", "metadata_days": "14"})
    assert response.status_code == 200  # re-shows preview, no redirect
    assert calls == []  # sweeper never invoked


def test_cleanup_run_with_confirm_invokes_sweeper(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict] = []

    def _fake(settings, *, media_days, metadata_days, **kwargs):
        calls.append({"media_days": media_days, "metadata_days": metadata_days})
        return CommandResult(True, "[tag] summary removed=2 bytes=4096 media_days=5 metadata_days=14 dry_run=0", 0)

    monkeypatch.setattr("ops_ui.app.run_retention_cleanup", _fake)
    app = create_app(_settings(tmp_path))
    response = app.test_client().post(
        "/cleanup/run",
        data={"media_days": "5", "metadata_days": "14", "confirm": "1"},
    )
    assert response.status_code == 302  # redirect back to recovery
    assert calls == [{"media_days": 5, "metadata_days": 14}]
