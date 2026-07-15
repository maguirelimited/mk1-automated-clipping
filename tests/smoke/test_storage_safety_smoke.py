"""Storage Safety & Integration smoke (Phase 12).

Validates interaction between retention, classification, apply safety, disk
pressure, scheduling, log rotation, database backup, and Operations UI loaders.
Does not add storage features — proves the subsystem is safe end-to-end.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STORAGE_TESTS = REPO_ROOT / "tests" / "storage"
OPS_UI_ROOT = REPO_ROOT / "ops-ui"
OPS_DIR = REPO_ROOT / "scripts" / "ops"
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCRIPTS_CONFIG = SCRIPTS_DIR / "config"

for path in (STORAGE_TESTS, SCRIPTS_CONFIG, SCRIPTS_DIR):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

import pytest

from storage.database_backup import (
    STATUS_FAIL,
    STATUS_SUCCESS,
    load_latest_backup_record,
    run_database_backup,
)
from storage.disk_pressure import (
    DiskPressureLevel,
    can_start_new_job,
    evaluate_disk_pressure,
    record_disk_pressure_block,
)
from storage.log_rotation import (
    STATUS_FAIL as ROTATION_FAIL,
    STATUS_SUCCESS as ROTATION_SUCCESS,
    load_latest_rotation_record,
    rotate_active_log,
    run_log_rotation,
)
from storage.retention_apply import RetentionApplyExecutor, run_retention_apply
from storage.retention_planner import RetentionPlanner, run_retention_dry_run
from storage.retention_report import RetentionFileDecision, load_plan_report
from storage.retention_schedule import (
    STATUS_FAIL as SCHEDULE_FAIL,
    STATUS_SUCCESS as SCHEDULE_SUCCESS,
    load_latest_scheduled_retention,
    run_scheduled_retention,
)

from smoke_harness import (
    FIXED_NOW,
    build_resolved,
    make_sqlite_db,
    populate_safety_scenario,
    set_schedule,
    touch_age,
    usage_percent,
)

def _all_paths_exist(*paths: Path) -> bool:
    return all(p.is_file() for p in paths)


class TestEndToEndRetention:
    def test_dry_run_identifies_eligible_without_deleting(self, tmp_path: Path, monkeypatch):
        scenario = populate_safety_scenario(tmp_path)
        resolved = build_resolved(tmp_path)
        before = {
            p: p.stat().st_size
            for p in (
                scenario.eligible_temp,
                scenario.protected_active,
                scenario.protected_database,
            )
        }

        def _fail_unlink(*_args, **_kwargs):
            raise AssertionError("dry-run must not delete files")

        monkeypatch.setattr(Path, "unlink", _fail_unlink)

        report, path = run_retention_dry_run(
            resolved,
            now=FIXED_NOW,
            report_dir=tmp_path / "reports/dev/retention",
        )
        assert path.is_file()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["mode"] == "dry-run"
        assert report.eligible_count >= 1
        assert scenario.eligible_temp.exists()
        for p, size in before.items():
            assert p.stat().st_size == size

    def test_apply_deletes_only_eligible_and_writes_report(self, tmp_path: Path):
        scenario = populate_safety_scenario(tmp_path)
        resolved = build_resolved(tmp_path)

        plan_report, _ = run_retention_dry_run(
            resolved,
            now=FIXED_NOW,
            report_dir=tmp_path / "reports/dev/retention",
        )
        protected_before = _all_paths_exist(
            scenario.protected_active,
            scenario.protected_failed,
            scenario.protected_unknown,
            scenario.protected_database,
            scenario.protected_outside_root,
        )
        assert protected_before

        apply_report, apply_path = run_retention_apply(
            resolved,
            plan_report,
            now=FIXED_NOW,
            report_dir=tmp_path / "reports/dev/retention",
        )
        assert not scenario.eligible_temp.exists()
        assert scenario.protected_active.exists()
        assert scenario.protected_failed.exists()
        assert scenario.protected_unknown.exists()
        assert scenario.protected_database.exists()
        assert scenario.protected_outside_root.exists()
        assert apply_report.successful_deletions >= 1
        assert apply_report.bytes_reclaimed >= scenario.eligible_size
        assert apply_path.is_file()
        apply_payload = json.loads(apply_path.read_text(encoding="utf-8"))
        assert apply_payload["mode"] == "apply"
        assert apply_payload["source_plan_id"] == plan_report.retention_run_id


class TestSafetyGuarantees:
    def test_planner_buckets_match_hard_rules(self, tmp_path: Path):
        scenario = populate_safety_scenario(tmp_path)
        dev_report = RetentionPlanner(build_resolved(tmp_path), now=FIXED_NOW).plan_dry_run()
        prod_report = RetentionPlanner(
            build_resolved(tmp_path, "prod"), now=FIXED_NOW
        ).plan_dry_run()

        by_path = {f.path: f for f in dev_report.eligible_files + dev_report.protected_files}
        assert str(scenario.protected_active.resolve()) in by_path
        assert by_path[str(scenario.protected_active.resolve())].reason == "active_job"

        fail_rows = [f for f in dev_report.protected_files if "job_fail" in f.path]
        assert any(f.reason == "failed_job" for f in fail_rows)

        assert len(dev_report.unknown_files) >= 1
        assert dev_report.unknown_files[0].reason == "unknown_artifact_type"

        db_rows = [f for f in dev_report.protected_files if f.artifact_type == "database"]
        assert db_rows and db_rows[0].reason == "protected_type"

        outside = [f for f in dev_report.protected_files if f.artifact_type == "run_record"]
        assert outside and outside[0].reason == "outside_allowed_root"

        finals = [f for f in prod_report.protected_files if f.artifact_type == "final_clip"]
        assert any(f.reason == "final_clip_default_protected" for f in finals)

        upload_rows = [
            f
            for f in dev_report.protected_files
            if "job_upload" in f.path and f.artifact_type == "final_clip"
        ]
        assert any(f.reason == "upload_not_confirmed" for f in upload_rows)

    def test_apply_rejects_symlink_targets(self, tmp_path: Path):
        scenario = populate_safety_scenario(tmp_path)
        resolved = build_resolved(tmp_path)
        link = scenario.symlink_link
        decision = RetentionFileDecision(
            path=str(link),
            artifact_type="temporary_file",
            disposition="eligible",
            reason="expired_temporary_file",
            size_bytes=link.lstat().st_size,
            job_id="job_sym",
            current_state="completed",
        )
        from storage.retention_report import RetentionPlanReport

        plan = RetentionPlanReport(
            retention_run_id="symlink_smoke_plan",
            environment="development",
            mode="dry-run",
            policy_version="storage.retention.v1",
            retention_enabled=True,
            started_at="2026-07-04T12:00:00Z",
            finished_at="2026-07-04T12:00:00Z",
            eligible_files=[decision],
        )
        report = RetentionApplyExecutor(resolved, plan, now=FIXED_NOW).execute()
        assert scenario.symlink_real.exists()
        assert report.deletions[0].skip_reason == "symlink_detected"

    def test_retention_disabled_refuses_deletion(self, tmp_path: Path):
        populate_safety_scenario(tmp_path, retention_enabled=False)
        resolved = build_resolved(tmp_path)
        plan = RetentionPlanner(resolved, now=FIXED_NOW).plan_dry_run()
        assert plan.retention_enabled is False
        assert plan.eligible_count == 0

        apply_report = RetentionApplyExecutor(resolved, plan, now=FIXED_NOW).execute()
        assert apply_report.successful_deletions == 0
        assert any("storage.retention.enabled is false" in e for e in apply_report.errors)


class TestDiskPressureIntegration:
    @pytest.mark.parametrize(
        ("percent", "expected"),
        [
            (50.0, DiskPressureLevel.NORMAL),
            (85.0, DiskPressureLevel.WARNING),
            (92.0, DiskPressureLevel.URGENT),
            (96.0, DiskPressureLevel.CRITICAL),
            (99.0, DiskPressureLevel.REJECT_NEW_JOBS),
        ],
    )
    def test_threshold_classification(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        percent: float,
        expected: DiskPressureLevel,
    ) -> None:
        populate_safety_scenario(tmp_path)
        resolved = build_resolved(tmp_path)
        monkeypatch.setattr(
            "storage.disk_pressure.shutil.disk_usage",
            lambda _p: usage_percent(percent),
        )
        status = evaluate_disk_pressure(resolved)
        assert status.level == expected

    def test_reject_new_jobs_records_block(self, tmp_path: Path, monkeypatch):
        populate_safety_scenario(tmp_path)
        (tmp_path / "data" / "prod" / "storage").mkdir(parents=True, exist_ok=True)
        gate = can_start_new_job(
            "prod",
            build_resolved(tmp_path, "prod"),
            disk_usage_fn=lambda _p: usage_percent(99.0),
        )
        assert gate.allowed is False
        assert "reject" in (gate.reason or "").lower()

        record_disk_pressure_block(
            environment="production",
            status=gate.status,
            reason=gate.reason or "disk pressure",
            repo_root=tmp_path,
            data_root=tmp_path / "data" / "prod",
            trigger="smoke_test",
            run_id="run_smoke_block",
        )
        block_file = tmp_path / "data" / "prod" / "storage" / "disk_pressure_blocks.jsonl"
        assert block_file.is_file()
        line = block_file.read_text(encoding="utf-8").strip().splitlines()[-1]
        payload = json.loads(line)
        assert payload["run_id"] == "run_smoke_block"
        assert "reject" in (payload.get("reason") or "").lower()

    def test_run_pipeline_blocks_on_disk_pressure(self, tmp_path: Path, monkeypatch):
        if str(OPS_DIR) not in sys.path:
            sys.path.insert(0, str(OPS_DIR))

        import execution_lock as el
        import run_pipeline as rp
        import run_records as rr

        populate_safety_scenario(tmp_path)
        (tmp_path / "data" / "prod").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(el, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rp, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rr, "REPO_ROOT", tmp_path)
        resolved = build_resolved(tmp_path, "prod")

        monkeypatch.setattr(
            rp,
            "validate_config",
            lambda _env: (rp.EXIT_SUCCESS, "config ok", resolved),
        )
        monkeypatch.setattr(rp, "write_config_snapshot", lambda *_a, **_k: None)
        monkeypatch.setattr(
            rp,
            "check_boot_readiness",
            lambda _env: (rp.EXIT_SUCCESS, "boot readiness READY"),
        )
        monkeypatch.setattr(
            "storage.disk_pressure.shutil.disk_usage",
            lambda _p: usage_percent(99.0),
        )

        code = rp.run_pipeline("prod", funnel_id="business", trigger="scheduled")
        assert code == rp.EXIT_SUCCESS
        run_dirs = list((tmp_path / "runs" / "prod").iterdir())
        assert len(run_dirs) == 1
        record = json.loads((run_dirs[0] / "run_record.json").read_text(encoding="utf-8"))
        assert record["status"] == "SKIPPED"
        assert "reject" in (record.get("failure_reason") or "").lower()


class TestScheduledRetentionIntegration:
    def test_scheduled_dry_run_records_without_deleting(self, tmp_path: Path):
        populate_safety_scenario(tmp_path)
        set_schedule(tmp_path, environment="prod", enabled=True, mode="dry_run")
        resolved = build_resolved(tmp_path, "prod")
        records_dir = tmp_path / "data" / "prod" / "storage"
        records_dir.mkdir(parents=True, exist_ok=True)

        result = run_scheduled_retention(
            resolved,
            records_dir=records_dir,
            now=FIXED_NOW,
        )
        assert result.status == SCHEDULE_SUCCESS
        assert result.mode == "dry_run"
        assert result.report_path
        assert Path(result.report_path).is_file()
        latest = load_latest_scheduled_retention(records_dir=records_dir)
        assert latest["status"] == SCHEDULE_SUCCESS
        assert latest["mode"] == "dry_run"
        assert (tmp_path / "database" / "dev.db").is_file()

    def test_scheduled_apply_respects_safety(self, tmp_path: Path):
        populate_safety_scenario(tmp_path)
        set_schedule(
            tmp_path,
            environment="dev",
            enabled=True,
            mode="apply",
            retention_enabled=True,
        )
        resolved = build_resolved(tmp_path)
        records_dir = tmp_path / "data" / "dev" / "storage"
        records_dir.mkdir(parents=True, exist_ok=True)

        result = run_scheduled_retention(
            resolved,
            records_dir=records_dir,
            now=FIXED_NOW,
        )
        assert result.status == SCHEDULE_SUCCESS
        assert result.mode == "apply"
        latest = load_latest_scheduled_retention(records_dir=records_dir)
        assert latest["mode"] == "apply"
        assert (tmp_path / "database" / "dev.db").is_file()

    def test_planner_failure_recorded(self, tmp_path: Path):
        populate_safety_scenario(tmp_path)
        set_schedule(tmp_path, environment="prod", enabled=True, mode="dry_run")
        resolved = build_resolved(tmp_path, "prod")
        records_dir = tmp_path / "data" / "prod" / "storage"

        result = run_scheduled_retention(
            resolved,
            records_dir=records_dir,
            dry_run_fn=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("planner boom")),
        )
        assert result.status == SCHEDULE_FAIL
        latest = load_latest_scheduled_retention(records_dir=records_dir)
        assert latest["status"] == SCHEDULE_FAIL
        assert (tmp_path / "database" / "dev.db").is_file()


class TestLogRotationIntegration:
    def test_rotation_preserves_active_log_and_retention_expires_rotated(
        self, tmp_path: Path
    ):
        populate_safety_scenario(tmp_path)
        resolved = build_resolved(tmp_path)
        logs_root = tmp_path / "logs" / "dev"
        logs_root.mkdir(parents=True, exist_ok=True)
        active = logs_root / "service.log"
        active.write_bytes(b"x" * 200)
        rotate_active_log(active, max_bytes=100, backup_count=3, compress=True)
        assert active.is_file()
        assert active.stat().st_size == 0
        rotated = logs_root / "service.log.1"
        assert rotated.is_file()
        touch_age(rotated, days=40)

        plan = RetentionPlanner(resolved, now=FIXED_NOW).plan_dry_run()
        rotated_rows = [
            f for f in plan.eligible_files if "service.log.1" in f.path
        ]
        assert rotated_rows
        assert rotated_rows[0].artifact_type == "service_log"
        assert rotated_rows[0].retention_days == 30

    def test_rotation_failure_recorded(self, tmp_path: Path):
        populate_safety_scenario(tmp_path)
        resolved = build_resolved(tmp_path)
        records_dir = tmp_path / "data" / "dev" / "storage"
        logs_root = tmp_path / "logs" / "dev"
        logs_root.mkdir(parents=True, exist_ok=True)
        active = logs_root / "fail.log"
        active.write_bytes(b"x" * 200)

        def boom(_path: Path) -> None:
            raise OSError("truncate denied")

        with pytest.raises(RuntimeError, match="active log preserved"):
            rotate_active_log(
                active,
                max_bytes=10,
                backup_count=2,
                compress=False,
                truncate_fn=boom,
            )
        assert active.read_bytes() == b"x" * 200

        def failing_rotate(_path, **_kwargs):
            raise OSError("rotate boom")

        result = run_log_rotation(
            resolved,
            records_dir=records_dir,
            logs_root=logs_root,
            rotate_fn=failing_rotate,
        )
        assert result.status == ROTATION_FAIL
        latest = load_latest_rotation_record(records_dir=records_dir)
        assert latest["status"] == ROTATION_FAIL


class TestDatabaseBackupIntegration:
    def test_backup_creation_and_live_db_protection(self, tmp_path: Path):
        populate_safety_scenario(tmp_path)
        db_path = tmp_path / "database" / "dev.db"
        make_sqlite_db(db_path)
        resolved = build_resolved(tmp_path)
        records_dir = tmp_path / "data" / "dev" / "storage"
        backup_dir = tmp_path / "backups" / "dev" / "database"

        result = run_database_backup(
            resolved,
            now=FIXED_NOW,
            records_dir=records_dir,
            backup_dir=backup_dir,
        )
        assert result.status == STATUS_SUCCESS
        backup = Path(result.backup_path)
        assert backup.is_file()
        assert db_path.is_file()
        latest = load_latest_backup_record(records_dir=records_dir)
        assert latest["status"] == STATUS_SUCCESS
        assert latest["integrity_ok"] is True

        plan = RetentionPlanner(resolved, now=FIXED_NOW).plan_dry_run()
        db_rows = [f for f in plan.protected_files if f.artifact_type == "database"]
        assert db_rows and db_rows[0].reason == "protected_type"

    def test_failed_backup_recorded_without_deleting_live_db(self, tmp_path: Path):
        populate_safety_scenario(tmp_path)
        db_path = tmp_path / "database" / "dev.db"
        make_sqlite_db(db_path)
        resolved = build_resolved(tmp_path)
        records_dir = tmp_path / "data" / "dev" / "storage"
        backup_dir = tmp_path / "backups" / "dev" / "database"

        first = run_database_backup(
            resolved,
            now=FIXED_NOW,
            records_dir=records_dir,
            backup_dir=backup_dir,
        )
        assert first.status == STATUS_SUCCESS
        previous = Path(first.backup_path)

        def boom(_src: Path, _dest: Path) -> None:
            raise RuntimeError("disk full")

        second = run_database_backup(
            resolved,
            now=datetime_with_hour(1),
            records_dir=records_dir,
            backup_dir=backup_dir,
            create_fn=boom,
        )
        assert second.status == STATUS_FAIL
        assert previous.is_file()
        assert db_path.is_file()
        latest = load_latest_backup_record(records_dir=records_dir)
        assert latest["status"] == STATUS_FAIL


def datetime_with_hour(offset: int):
    return FIXED_NOW + timedelta(hours=offset)


class TestOperationsUIIntegration:
    def test_storage_context_reflects_backend_records(self, tmp_path: Path, monkeypatch):
        populate_safety_scenario(tmp_path)
        make_sqlite_db(tmp_path / "database" / "dev.db")
        set_schedule(tmp_path, environment="dev", enabled=True, mode="dry_run")
        resolved = build_resolved(tmp_path)
        records_dir = tmp_path / "data" / "dev" / "storage"
        records_dir.mkdir(parents=True, exist_ok=True)

        run_retention_dry_run(
            resolved,
            now=FIXED_NOW,
            report_dir=tmp_path / "reports/dev/retention",
        )
        run_scheduled_retention(
            resolved,
            records_dir=records_dir,
            now=FIXED_NOW,
        )
        run_database_backup(
            resolved,
            now=FIXED_NOW,
            records_dir=records_dir,
            backup_dir=tmp_path / "backups" / "dev" / "database",
        )
        logs_root = tmp_path / "logs" / "dev"
        logs_root.mkdir(parents=True, exist_ok=True)
        (logs_root / "ui.log").write_bytes(b"x" * 200)
        run_log_rotation(
            resolved,
            records_dir=records_dir,
            logs_root=logs_root,
        )

        if str(OPS_UI_ROOT) not in sys.path:
            sys.path.insert(0, str(OPS_UI_ROOT))

        from ops_ui.config import ServiceConfig, Settings
        from ops_ui.storage_ui import build_storage_context

        def _load(_token: str):
            return build_resolved(tmp_path, "dev")

        monkeypatch.setattr("ops_ui.storage_ui._load_resolved", _load)
        monkeypatch.setattr(
            "storage.disk_pressure.shutil.disk_usage",
            lambda _p: usage_percent(42.0, total=100 * 1024**3),
        )

        settings = Settings(
            host="127.0.0.1",
            port=5070,
            data_dir=tmp_path / "data" / "dev",
            control_db_path=tmp_path / "ops.sqlite3",
            controls_file=tmp_path / "controls.json",
            service_timeout_sec=0.01,
            journal_lines=1,
            funnel_run_timeout_sec=1.0,
            stuck_running_sec=7200.0,
            stuck_queued_sec=1800.0,
            stuck_uploading_sec=1800.0,
            environment="dev",
            services=(
                ServiceConfig(
                    key="source-input",
                    label="source-input",
                    base_url="http://127.0.0.1:9",
                    systemd_unit="mk04-source-input.service",
                ),
            ),
        )

        ctx = build_storage_context(
            settings,
            shell={
                "shell_env_token": "dev",
                "shell_health_data": {},
                "shell_status_data": {},
                "shell_connected": True,
                "shell_environment_label": "DEVELOPMENT",
                "shell_is_production": False,
            },
        )
        assert ctx.get("storage_error") is None
        assert ctx["storage_disk"]["available"] is True
        assert ctx["storage_disk"]["level"] == "NORMAL"
        assert ctx["storage_retention"]["available"] is True
        assert ctx["storage_retention"]["status"] in {"SUCCESS", "success", "SKIPPED"}
        assert ctx["storage_backup"]["available"] is True
        assert ctx["storage_backup"]["status"] == "SUCCESS"
        assert ctx["storage_log_rotation"]["available"] is True
        assert ctx["storage_links"]


class TestRegressionGuards:
    def test_plan_roundtrip_before_apply(self, tmp_path: Path):
        populate_safety_scenario(tmp_path)
        resolved = build_resolved(tmp_path)
        plan, plan_path = run_retention_dry_run(
            resolved,
            now=FIXED_NOW,
            report_dir=tmp_path / "reports/dev/retention",
        )
        loaded = load_plan_report(plan_path)
        assert loaded.retention_run_id == plan.retention_run_id
        assert loaded.mode == "dry-run"

    def test_storage_ui_does_not_import_apply_executor(self):
        source = (OPS_UI_ROOT / "ops_ui" / "storage_ui.py").read_text(encoding="utf-8")
        assert "RetentionApplyExecutor" not in source
        assert "RetentionPlanner" not in source
        assert "run_retention_apply" not in source

    def test_smoke_modules_present(self):
        required = [
            REPO_ROOT / "scripts" / "storage" / "retention_planner.py",
            REPO_ROOT / "scripts" / "storage" / "retention_apply.py",
            REPO_ROOT / "scripts" / "storage" / "disk_pressure.py",
            REPO_ROOT / "scripts" / "storage" / "retention_schedule.py",
            REPO_ROOT / "scripts" / "storage" / "log_rotation.py",
            REPO_ROOT / "scripts" / "storage" / "database_backup.py",
            REPO_ROOT / "scripts" / "ops" / "run-scheduled-retention.sh",
            REPO_ROOT / "scripts" / "ops" / "run-log-rotation.sh",
            REPO_ROOT / "scripts" / "ops" / "run-database-backup.sh",
            OPS_UI_ROOT / "ops_ui" / "storage_ui.py",
        ]
        missing = [str(p) for p in required if not p.is_file()]
        assert not missing, f"missing storage smoke prerequisites: {missing}"
