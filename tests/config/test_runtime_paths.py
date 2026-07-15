"""Tests for canonical mutable-state path authority (Prompt 3)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_CONFIG = REPO_ROOT / "scripts" / "config"
sys.path.insert(0, str(SCRIPTS_CONFIG))

from config_manager import ConfigError, ConfigManager  # noqa: E402
from runtime_paths import (  # noqa: E402
    PathAuthorityError,
    assert_pipeline_config_agrees,
    control_state_path_for_env,
    resolve_canonical_paths,
)


def _yaml_paths(repo: Path, token: str) -> dict[str, Path]:
    return {
        "yaml_data_root": (repo / "data" / token).resolve(),
        "yaml_jobs_root": (repo / "jobs" / token).resolve(),
        "yaml_outputs_root": (repo / "outputs" / token).resolve(),
        "yaml_logs_root": (repo / "logs" / token).resolve(),
        "yaml_reports_root": (repo / "reports" / token).resolve(),
        "yaml_database_path": (repo / "database" / f"{token}.db").resolve(),
        "yaml_runs_root": (repo / "runs" / token).resolve(),
    }


class TestPathPrecedence:
    def test_explicit_override_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        repo = tmp_path / "repo"
        for p in _yaml_paths(repo, "dev").values():
            p.parent.mkdir(parents=True, exist_ok=True)
        explicit_data = tmp_path / "explicit-data"
        explicit_data.mkdir()
        monkeypatch.setenv("MK04_DATA_ROOT", str(explicit_data))
        monkeypatch.delenv("MK04_RUNTIME_ROOT", raising=False)
        paths = resolve_canonical_paths(
            environment="development",
            repo_root=repo,
            **_yaml_paths(repo, "dev"),
        )
        assert paths.data_root == explicit_data.resolve()

    def test_runtime_root_overrides_jobs_outputs_hybrid_dev(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        repo = tmp_path / "repo"
        runtime = tmp_path / "varlib" / "dev"
        (runtime / "video-automation").mkdir(parents=True)
        for p in _yaml_paths(repo, "dev").values():
            p.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("MK04_RUNTIME_ROOT", str(runtime))
        for key in (
            "MK04_DATA_ROOT",
            "MK04_JOBS_ROOT",
            "MK04_OUTPUTS_ROOT",
            "MK04_RUNS_ROOT",
            "MK04_REPORTS_ROOT",
            "MK04_DATABASE_PATH",
            "MK04_CONTROL_STATE_FILE",
        ):
            monkeypatch.delenv(key, raising=False)
        paths = resolve_canonical_paths(
            environment="development",
            repo_root=repo,
            **_yaml_paths(repo, "dev"),
        )
        assert paths.jobs_root == (runtime / "video-automation" / "jobs").resolve()
        assert paths.outputs_root == (runtime / "video-automation" / "output").resolve()
        assert paths.data_root == (repo / "data" / "dev").resolve()
        assert paths.runs_root == (repo / "runs" / "dev").resolve()
        assert paths.control_state_file == (repo / "data" / "dev" / "control_state.json").resolve()

    def test_yaml_default_without_runtime(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        repo = tmp_path / "repo"
        for p in _yaml_paths(repo, "dev").values():
            p.parent.mkdir(parents=True, exist_ok=True)
        for key in (
            "MK04_RUNTIME_ROOT",
            "MK04_DATA_ROOT",
            "MK04_JOBS_ROOT",
            "MK04_OUTPUTS_ROOT",
            "MK04_RUNS_ROOT",
        ):
            monkeypatch.delenv(key, raising=False)
        paths = resolve_canonical_paths(
            environment="development",
            repo_root=repo,
            **_yaml_paths(repo, "dev"),
        )
        assert paths.jobs_root == (repo / "jobs" / "dev").resolve()
        assert paths.data_root == (repo / "data" / "dev").resolve()


class TestProductionFailClosed:
    def test_missing_runtime_root_in_deployed_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        repo = Path("/opt/mk04/prod/current")
        monkeypatch.delenv("MK04_RUNTIME_ROOT", raising=False)
        monkeypatch.setenv("MK04_REQUIRE_RUNTIME_PATHS", "1")
        with pytest.raises(PathAuthorityError, match="MK04_RUNTIME_ROOT"):
            resolve_canonical_paths(
                environment="production",
                repo_root=tmp_path,  # code root unused when REQUIRE set
                **_yaml_paths(tmp_path, "prod"),
            )

    def test_runtime_prod_rejects_repo_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        repo = tmp_path / "repo"
        runtime = tmp_path / "varlib" / "prod"
        (runtime / "video-automation").mkdir(parents=True)
        for p in _yaml_paths(repo, "prod").values():
            p.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("MK04_RUNTIME_ROOT", str(runtime))
        monkeypatch.setenv("MK04_LOG_ROOT", str(tmp_path / "varlog" / "prod"))
        for key in (
            "MK04_DATA_ROOT",
            "MK04_JOBS_ROOT",
            "MK04_OUTPUTS_ROOT",
            "MK04_RUNS_ROOT",
            "MK04_REPORTS_ROOT",
            "MK04_DATABASE_PATH",
            "MK04_CONTROL_STATE_FILE",
        ):
            monkeypatch.delenv(key, raising=False)
        paths = resolve_canonical_paths(
            environment="production",
            repo_root=repo,
            **_yaml_paths(repo, "prod"),
        )
        assert paths.data_root == (runtime / "data").resolve()
        assert paths.runs_root == (runtime / "runs").resolve()
        assert "data/prod" not in str(paths.data_root).replace(str(repo), "")
        assert paths.control_state_file == (runtime / "data" / "control_state.json").resolve()
        # Must not equal repository YAML defaults.
        assert paths.jobs_root != (repo / "jobs" / "prod").resolve()
        assert paths.database_path != (repo / "database" / "prod.db").resolve()


class TestConfigManagerHybrid:
    def test_config_manager_dev_hybrid(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # Use real config tree for upload-enabled false etc.
        monkeypatch.chdir(REPO_ROOT)
        runtime = tmp_path / "varlib" / "dev"
        (runtime / "video-automation").mkdir(parents=True)
        monkeypatch.setenv("MK04_RUNTIME_ROOT", str(runtime))
        monkeypatch.setenv("MK04_DATA_ROOT", str(REPO_ROOT / "data" / "dev"))
        monkeypatch.setenv("MK04_RUNS_ROOT", str(REPO_ROOT / "runs" / "dev"))
        for key in ("MK04_JOBS_ROOT", "MK04_OUTPUTS_ROOT", "MK04_REPORTS_ROOT"):
            monkeypatch.delenv(key, raising=False)
        resolved = ConfigManager.load(environment="dev", config_root=REPO_ROOT / "config")
        assert resolved.paths.jobs_root == (runtime / "video-automation" / "jobs").resolve()
        assert resolved.paths.outputs_root == (runtime / "video-automation" / "output").resolve()
        assert resolved.paths.data_root == (REPO_ROOT / "data" / "dev").resolve()
        assert resolved.paths.runs_root == (REPO_ROOT / "runs" / "dev").resolve()
        assert resolved.uploading_enabled is False


class TestPipelineAgreement:
    def test_agreement_ok(self):
        jobs = Path("/var/lib/mk04/prod/video-automation/jobs")
        out = Path("/var/lib/mk04/prod/video-automation/output")
        assert_pipeline_config_agrees(
            config_manager_jobs=jobs,
            config_manager_outputs=out,
            pipeline_jobs=jobs,
            pipeline_outputs=out,
            production=True,
        )

    def test_agreement_mismatch_fails(self):
        with pytest.raises(PathAuthorityError, match="mismatch"):
            assert_pipeline_config_agrees(
                config_manager_jobs=Path("/var/lib/mk04/prod/video-automation/jobs"),
                config_manager_outputs=Path("/var/lib/mk04/prod/video-automation/output"),
                pipeline_jobs=Path("/tmp/wrong/jobs"),
                pipeline_outputs=Path("/var/lib/mk04/prod/video-automation/output"),
                production=True,
            )


class TestControlAgreement:
    def test_control_state_uses_data_root_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        data = tmp_path / "data-dev"
        data.mkdir()
        monkeypatch.setenv("MK04_DATA_ROOT", str(data))
        monkeypatch.delenv("MK04_CONTROL_STATE_FILE", raising=False)
        path = control_state_path_for_env("dev", repo_root=REPO_ROOT)
        assert path == (data / "control_state.json").resolve()

    def test_control_state_explicit_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        target = tmp_path / "control_state.json"
        monkeypatch.setenv("MK04_CONTROL_STATE_FILE", str(target))
        path = control_state_path_for_env("prod", repo_root=REPO_ROOT)
        assert path == target.resolve()


    def test_ai_service_log_dir_uses_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AI_SERVICE_LOG_DIR", "/var/log/mk04/dev/ai-service")
        monkeypatch.delenv("MK04_LOG_ROOT", raising=False)
        import importlib
        import sys

        ai_root = str(REPO_ROOT / "ai-service")
        if ai_root not in sys.path:
            sys.path.insert(0, ai_root)
        import decision_logging

        importlib.reload(decision_logging)
        assert str(decision_logging.DEFAULT_LOG_DIR) == "/var/log/mk04/dev/ai-service"


class TestUploadAuthorityUnchanged:
    def test_prod_yaml_still_disabled(self, monkeypatch: pytest.MonkeyPatch):
        for key in (
            "MK04_RUNTIME_ROOT",
            "MK04_DATA_ROOT",
            "MK04_JOBS_ROOT",
            "MK04_OUTPUTS_ROOT",
            "MK04_REQUIRE_RUNTIME_PATHS",
        ):
            monkeypatch.delenv(key, raising=False)
        resolved = ConfigManager.load(environment="prod", config_root=REPO_ROOT / "config")
        assert resolved.uploading_enabled is False


class TestDeployedProductionClassification:
    """Finalized releases vs pre-finalization ``.staging-*`` snapshots."""

    def test_staging_release_is_non_deployed(self, monkeypatch: pytest.MonkeyPatch):
        from runtime_paths import (
            _deployed_production_context,
            _is_finalized_production_release_dir,
        )

        monkeypatch.delenv("MK04_REQUIRE_RUNTIME_PATHS", raising=False)
        monkeypatch.delenv("MK04_CODE_ROOT", raising=False)
        staging = Path("/opt/mk04/prod/releases/.staging-20260714T120000Z_deadbeef")
        assert _is_finalized_production_release_dir(staging) is False
        assert _deployed_production_context(staging) is False

    def test_finalized_release_is_deployed(self, monkeypatch: pytest.MonkeyPatch):
        from runtime_paths import (
            _deployed_production_context,
            _is_finalized_production_release_dir,
        )

        monkeypatch.delenv("MK04_REQUIRE_RUNTIME_PATHS", raising=False)
        monkeypatch.delenv("MK04_CODE_ROOT", raising=False)
        finalized = Path("/opt/mk04/prod/releases/20260714T120000Z_deadbeef")
        assert _is_finalized_production_release_dir(finalized) is True
        assert _deployed_production_context(finalized) is True

    def test_current_resolving_to_finalized_is_deployed(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from runtime_paths import (
            _deployed_production_context,
            _is_finalized_production_release_dir,
        )

        monkeypatch.delenv("MK04_REQUIRE_RUNTIME_PATHS", raising=False)
        monkeypatch.delenv("MK04_CODE_ROOT", raising=False)
        current = Path("/opt/mk04/prod/current")
        if not current.exists():
            pytest.skip("no live /opt/mk04/prod/current on this host")
        active = current.resolve()
        assert not active.name.startswith(".")
        assert _is_finalized_production_release_dir(active) is True
        assert _deployed_production_context(active) is True
        assert _deployed_production_context(Path("/opt/mk04/prod/current")) is True

    def test_finalized_without_runtime_root_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("MK04_RUNTIME_ROOT", raising=False)
        monkeypatch.delenv("MK04_REQUIRE_RUNTIME_PATHS", raising=False)
        monkeypatch.delenv("MK04_CODE_ROOT", raising=False)
        for p in _yaml_paths(tmp_path, "prod").values():
            p.parent.mkdir(parents=True, exist_ok=True)
        finalized = Path("/opt/mk04/prod/releases/20260714T120000Z_deadbeef")
        with pytest.raises(PathAuthorityError, match="MK04_RUNTIME_ROOT"):
            resolve_canonical_paths(
                environment="production",
                repo_root=finalized,
                **_yaml_paths(tmp_path, "prod"),
            )

    def test_staging_yaml_validation_does_not_select_release_local_as_live_runtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Hermetic staging may use YAML defaults; must not require live runtime."""
        monkeypatch.delenv("MK04_RUNTIME_ROOT", raising=False)
        monkeypatch.delenv("MK04_REQUIRE_RUNTIME_PATHS", raising=False)
        monkeypatch.delenv("MK04_CODE_ROOT", raising=False)
        for p in _yaml_paths(tmp_path, "prod").values():
            p.parent.mkdir(parents=True, exist_ok=True)
        staging = Path("/opt/mk04/prod/releases/.staging-20260714T120000Z_deadbeef")
        paths = resolve_canonical_paths(
            environment="production",
            repo_root=staging,
            **_yaml_paths(tmp_path, "prod"),
        )
        assert paths.data_root == (tmp_path / "data" / "prod").resolve()
        assert paths.jobs_root == (tmp_path / "jobs" / "prod").resolve()
        assert "/var/lib/mk04/prod" not in str(paths.data_root)
        assert "/opt/mk04/prod/releases/" not in str(paths.data_root)

    def test_previously_failing_loads_pass_under_staging_shaped_classification(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The two staging-validation failures must pass when repo_root is .staging-*."""
        import runtime_paths as rp

        staging = Path("/opt/mk04/prod/releases/.staging-20260714T120000Z_deadbeef")
        real = rp._deployed_production_context

        def _as_if_staging(_repo_root: Path) -> bool:
            return real(staging)

        monkeypatch.setattr(rp, "_deployed_production_context", _as_if_staging)
        for key in (
            "MK04_RUNTIME_ROOT",
            "MK04_DATA_ROOT",
            "MK04_JOBS_ROOT",
            "MK04_OUTPUTS_ROOT",
            "MK04_REQUIRE_RUNTIME_PATHS",
            "MK04_CODE_ROOT",
        ):
            monkeypatch.delenv(key, raising=False)

        # Mirrors tests/config/test_config_manager.py::TestRealConfigTree::test_loads_prod_from_real_config
        resolved = ConfigManager.load(
            environment="prod",
            funnel_id="business",
            platform_id="youtube",
            config_root=REPO_ROOT / "config",
        )
        assert resolved.environment == "production"
        assert resolved.uploading_enabled is False

        # Mirrors TestUploadAuthorityUnchanged::test_prod_yaml_still_disabled
        resolved2 = ConfigManager.load(environment="prod", config_root=REPO_ROOT / "config")
        assert resolved2.uploading_enabled is False

    def test_checkout_remains_non_deployed(self, monkeypatch: pytest.MonkeyPatch):
        from runtime_paths import _deployed_production_context

        monkeypatch.delenv("MK04_REQUIRE_RUNTIME_PATHS", raising=False)
        monkeypatch.delenv("MK04_CODE_ROOT", raising=False)
        assert _deployed_production_context(REPO_ROOT) is False
