#!/usr/bin/env python3
"""GTA baseline funnel acceptance — simplified Ops UI create → sync → test run."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OPS_UI = REPO / "ops-ui"
sys.path.insert(0, str(OPS_UI))

os.environ.setdefault("OPS_UI_DATA_DIR", str(REPO / "ops-ui" / "data"))
os.environ.setdefault("OPS_FUNNEL_REGISTRY_DIR", str(REPO / "ops-ui" / "data" / "funnel_registry"))
os.environ.setdefault("OPS_UI_AUTH_DISABLED", "1")

FUNNEL_ID = "gta_clips_001"
# Local dev: placeholder channel URL for registry/sync acceptance.
# Replace with a real GTA channel or playlist before production ingestion.
SOURCE_URL = "https://www.youtube.com/@DEV_PLACEHOLDER_GTA_CHANNEL/videos"


def _csrf(client, path: str) -> str:
    html = client.get(path).get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    return html.split(marker, 1)[1].split('"', 1)[0] if marker in html else ""


def _edit_post_from_form(form: dict, **overrides: str) -> dict[str, str]:
    from ops_ui.funnel_management.schema import ALLOWED_PLATFORMS

    data: dict[str, str] = {
        "funnel_id": form["funnel_id"],
        "display_name": form["display_name"],
        "description": form["description"],
        "category": form["category"],
        "status": form["status"],
        "environment": form["environment"],
        "operator_note": form["operator_note"],
        "created_at": form["created_at"],
        "template_source": form["template_source"],
        "acquisition_source_type": form["acquisition_source_type"],
        "min_duration_minutes": form["min_duration_minutes"],
        "max_duration_minutes": form["max_duration_minutes"],
        "max_downloads_per_run": form["max_downloads_per_run"],
        "pipeline_profile": form["pipeline_profile"],
        "ai_rule_profile": form["ai_rule_profile"],
        "max_clips": form["max_clips"],
        "min_clip_duration_sec": form["min_clip_duration_sec"],
        "max_clip_duration_sec": form["max_clip_duration_sec"],
        "max_overlap_sec": form["max_overlap_sec"],
        "filename_prefix": form["filename_prefix"],
        "delivery_mode": form["delivery_mode"],
        "posting_mode": form["posting_mode"],
        "config_manager_funnel_id": form["config_manager_funnel_id"],
        "source_count": str(len(form["sources"])),
        "route_count": "0",
    }
    if form.get("enabled"):
        data["enabled"] = "on"
    for index, source in enumerate(form["sources"]):
        prefix = f"source_{index}_"
        for key, value in source.items():
            if key in {"active", "hydrate_missing_duration", "remove"}:
                if value:
                    data[f"{prefix}{key}"] = "on"
            elif key not in {"active", "hydrate_missing_duration", "remove"}:
                data[f"{prefix}{key}"] = value
    for platform in sorted(ALLOWED_PLATFORMS):
        if form["platforms"].get(platform):
            data[f"platform_{platform}"] = "on"
    data.update(overrides)
    return data


def _http_json(url: str, *, method: str = "GET", payload: dict | None = None, timeout: float = 30.0) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return resp.status, json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            parsed = {"error": raw}
        return exc.code, parsed
    except urllib.error.URLError as exc:
        return 0, {"error": str(exc)}


def _wait_for_job(job_id: str, *, timeout_sec: float = 600.0) -> dict:
    deadline = time.time() + timeout_sec
    last: dict = {}
    while time.time() < deadline:
        status, body = _http_json(f"http://127.0.0.1:5150/jobs/{job_id}")
        last = body if isinstance(body, dict) else {"raw": body}
        state = str(last.get("state") or last.get("status") or "").lower()
        if state in {"completed", "failed", "cancelled", "error"}:
            return last
        time.sleep(5)
    return last


def _find_clip_outputs(job_id: str) -> list[Path]:
    jobs_root = REPO / "jobs" / "dev"
    if not jobs_root.is_dir():
        return []
    matches: list[Path] = []
    for path in jobs_root.rglob("*"):
        if path.suffix.lower() in {".mp4", ".webm", ".mkv"} and job_id in str(path):
            matches.append(path)
    return matches


def main() -> int:
    from ops_ui.app import create_app
    from ops_ui.config import load_settings
    from ops_ui.funnel_management.edit import edit_form_from_funnel
    from ops_ui.funnel_management.readiness_summary import build_simple_funnel_status, processing_blocker_messages
    from ops_ui.funnel_management.registry import FunnelRegistry
    from ops_ui.funnel_management.sync import FunnelSynchronizer
    from ops_ui.funnel_management.sync_workflow import resolve_sync_paths
    from ops_ui.funnels import build_funnel_validator, load_canonical_funnel_detail
    from ops_ui.store import ControlStore

    settings = load_settings()
    registry_dir = settings.data_dir / "funnel_registry"
    registry_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, object] = {
        "A_funnel_created": {},
        "B_runtime_sync": {},
        "C_processing_readiness": {},
        "D_test_run": {},
        "E_output_clip": {},
        "F_ui_issues": [],
        "G_next_fixes": [],
    }

    existing = registry_dir / f"{FUNNEL_ID}.json"
    if existing.exists():
        backup = existing.with_suffix(".json.bak")
        backup.write_text(existing.read_text(encoding="utf-8"), encoding="utf-8")
        existing.unlink()
        print(f"Backed up and removed prior registry entry -> {backup}")

    client = create_app(settings).test_client()

    create_data = {
        "template_id": "baseline_stream_clips",
        "funnel_id": FUNNEL_ID,
        "display_name": "GTA Clips 001",
        "category": "gaming",
        "source_type": "youtube_channel",
        "source_urls": SOURCE_URL,
        "description": "GTA / gaming clips baseline acceptance test",
        "csrf_token": _csrf(client, "/funnels/new"),
    }
    resp = client.post("/funnels/new", data=create_data, follow_redirects=False)
    if resp.status_code != 302:
        report["A_funnel_created"] = {"ok": False, "error": resp.get_data(as_text=True)[:1500]}
        _print_report(report)
        return 1

    registry = FunnelRegistry(registry_dir)
    funnel = registry.get_funnel(FUNNEL_ID)
    saved = json.loads((registry_dir / f"{FUNNEL_ID}.json").read_text(encoding="utf-8"))
    report["A_funnel_created"] = {
        "ok": True,
        "template_source": saved["identity"]["template_source"],
        "status": saved["identity"]["status"],
        "enabled": saved["identity"]["enabled"],
        "environment": saved["identity"]["environment"],
        "posting_enabled": saved["distribution"]["posting_enabled"],
        "ai_rule_profile": saved["processing"]["ai_rules"]["ai_rule_profile"],
        "prompt_managed": saved["processing"]["ai_rules"]["prompt_managed"],
        "config_manager_preset": saved["mappings"].get("config_manager_preset_id"),
        "source_count": len(saved["acquisition"]["sources"]),
    }
    print("A. Funnel created via simplified POST /funnels/new")

    # Dev accommodation: lower min duration so local test video can pass source-input validation later.
    form = edit_form_from_funnel(funnel)
    edit_data = _edit_post_from_form(
        form,
        min_duration_minutes="1",
        max_duration_minutes="30",
        source_0_url=SOURCE_URL,
    )
    edit_data["csrf_token"] = _csrf(client, f"/funnels/{FUNNEL_ID}/edit")
    resp = client.post(f"/funnels/{FUNNEL_ID}/edit", data=edit_data, follow_redirects=False)
    if resp.status_code != 302:
        report["F_ui_issues"].append("Edit for dev min_duration failed")
    funnel = registry.get_funnel(FUNNEL_ID)

    validator = build_funnel_validator()
    before = validator.validate_funnel(funnel)

    env_paths = resolve_sync_paths("dev")
    synchronizer = FunnelSynchronizer(env_paths.to_target_paths())
    plan = synchronizer.build_plan(funnel)
    if not plan.ok:
        report["B_runtime_sync"] = {"ok": False, "errors": [e for e in plan.errors]}
        _print_report(report)
        return 1

    sync_resp = client.post(
        f"/funnels/{FUNNEL_ID}/sync",
        data={"environment": "dev", "confirm_understand": "on", "csrf_token": _csrf(client, f"/funnels/{FUNNEL_ID}/sync")},
        follow_redirects=True,
    )
    if b"Synced successfully" not in sync_resp.data and sync_resp.status_code not in {200, 302}:
        report["B_runtime_sync"] = {"ok": False, "body": sync_resp.get_data(as_text=True)[:1500]}
        _print_report(report)
        return 1

    paths = {
        "source_input": REPO / "source-input" / "input_service" / "config" / "funnels.json",
        "video_json": REPO / "video-automation" / "config" / "funnels" / f"{FUNNEL_ID}.json",
        "ai_registry": REPO / "ai-service" / "config" / "funnel_rule_registry.json",
        "config_yaml": REPO / "config" / "funnels" / f"{FUNNEL_ID}.yaml",
    }
    source_has_entry = any(
        isinstance(e, dict) and e.get("funnel_id") == FUNNEL_ID
        for e in json.loads(paths["source_input"].read_text(encoding="utf-8"))
    )
    report["B_runtime_sync"] = {
        "ok": True,
        "source_input_entry": source_has_entry,
        "video_json": paths["video_json"].is_file(),
        "ai_registry_alias": json.loads(paths["ai_registry"].read_text(encoding="utf-8")).get("aliases", {}).get(FUNNEL_ID),
        "config_yaml": paths["config_yaml"].is_file(),
    }
    print("B. Runtime sync applied via POST /funnels/<id>/sync")

    funnel = registry.get_funnel(FUNNEL_ID)
    after = validator.validate_funnel(funnel)
    store = ControlStore(settings.control_db_path)
    store.init_db()
    detail = load_canonical_funnel_detail(FUNNEL_ID, settings, store, ingestion_paused=False, registry_dir=registry_dir)
    simple = detail["simple_status"]
    report["C_processing_readiness"] = {
        "processing_ready": after.processing_ready,
        "sync_ready": after.sync_ready,
        "posting_ready": after.posting_ready,
        "posting_enabled": funnel.distribution.posting_enabled,
        "runnable": after.runnable,
        "test_run_available": simple["test_run_available"],
        "blockers": processing_blocker_messages(after),
        "simple_status": simple,
    }
    print("C. Processing readiness:", "ready" if after.processing_ready else "not ready")

    # Ensure test video is long enough for min_duration=1 minute after edit+sync re-check
    test_video = REPO / "input" / "test_server_video.mp4"
    if test_video.is_file():
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(test_video)],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            duration = float(proc.stdout.strip())
        except ValueError:
            duration = 0.0
        if duration < 60:
            subprocess.run(
                ["bash", str(REPO / "input" / "generate_test_server_video.sh")],
                env={**os.environ, "TEST_SERVER_VIDEO_DURATION_SEC": "65"},
                check=True,
                cwd=str(REPO),
            )

    # Start minimal services for processing test if not already up
    status, health = _http_json("http://127.0.0.1:5150/healthz")
    services_started = False
    if status == 0:
        deploy = REPO / "deploy" / "scripts"
        for script in ("run-video-automation.sh", "run-ai-service.sh", "run-input-service.sh", "run-ops-ui.sh"):
            subprocess.Popen(["bash", str(deploy / script), "dev"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            services_started = True
        for _ in range(30):
            status, health = _http_json("http://127.0.0.1:5150/healthz")
            if status == 200:
                break
            time.sleep(2)

    # Try UI Run test (source-input /run-funnel) first
    run_resp = client.post("/funnels/run", data={"funnel_id": FUNNEL_ID, "next": "funnel_detail"}, follow_redirects=True)
    run_via_source = "Funnel" in run_resp.get_data(as_text=True)
    report["D_test_run"] = {"ui_run_post_attempted": True, "flash_page_returned": run_via_source}

    # Processing path: POST /jobs with local test video (mirrors handoff after acquisition)
    job_id: str | None = None
    if test_video.is_file():
        status, body = _http_json(
            "http://127.0.0.1:5150/jobs",
            method="POST",
            payload={"video": str(test_video), "funnel_id": FUNNEL_ID},
            timeout=60.0,
        )
        report["D_test_run"]["video_automation_status"] = status
        report["D_test_run"]["video_automation_body"] = body
        job_id = str(body.get("job_id") or "")

    clip_paths: list[str] = []
    if job_id:
        final = _wait_for_job(job_id, timeout_sec=900.0)
        report["D_test_run"]["final_job_state"] = final.get("state") or final.get("status")
        clip_paths = [str(p) for p in _find_clip_outputs(job_id)]
        status, outputs = _http_json(f"http://127.0.0.1:5150/jobs/{job_id}/outputs")
        report["E_output_clip"] = {
            "job_id": job_id,
            "outputs_http_status": status,
            "outputs": outputs,
            "clip_files_on_disk": clip_paths,
        }
        # Ops Outputs UI page smoke
        if status == 0:
            ops_status = 0
        else:
            ops_status, _ = _http_json("http://127.0.0.1:5170/ops/outputs")
        report["E_output_clip"]["ops_outputs_page_status"] = ops_status
    else:
        report["D_test_run"]["error"] = "Could not create video-automation job"
        report["G_next_fixes"].append("Start video-automation on :5150 and retry POST /jobs")

    if not after.processing_ready:
        report["G_next_fixes"].append("Fix processing blockers: " + ", ".join(processing_blocker_messages(after)))
    if SOURCE_URL.startswith("https://www.youtube.com/@DEV_PLACEHOLDER"):
        report["F_ui_issues"].append(
            "Source URL is a dev placeholder — Run test via source-input will not ingest real GTA content until replaced."
        )
    if not clip_paths:
        report["G_next_fixes"].append("No clip output found — check ai-service/Ollama and video-automation job logs")

    _print_report(report)
    ok = bool(report["A_funnel_created"].get("ok")) and bool(report["B_runtime_sync"].get("ok"))
    ok = ok and bool(report["C_processing_readiness"].get("processing_ready"))
    ok = ok and bool(clip_paths)
    return 0 if ok else 2


def _print_report(report: dict[str, object]) -> None:
    print("\n=== ACCEPTANCE REPORT ===")
    for key in ("A_funnel_created", "B_runtime_sync", "C_processing_readiness", "D_test_run", "E_output_clip", "F_ui_issues", "G_next_fixes"):
        print(f"\n{key}:")
        print(json.dumps(report.get(key, {}), indent=2, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
