"""Safe, read-only job artifact resolver (Operations & Observability Phase 4).

Discovers existing artifacts under an environment-scoped job directory.
Does not generate reports, modify files, embed JSON contents, or follow
user-supplied paths.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .index import (
    _env_token,
    _execution_context,
    _find_job_dir,
    _is_safe_id,
    _jobs_root_for,
    _read_report,
)
from .models import ArtifactReference, LogReference
from .schemas import CONTRACT_SCHEMA_VERSION

# Singleton artifacts: (artifact_type, candidate relative paths under job_dir).
# First existing candidate wins; if none exist, the first path is the expected
# missing reference.
_SINGLETON_ARTIFACTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("transcript", ("transcript.json", "transcript_payload.json")),
    ("raw_candidate_pool", ("raw_candidate_pool.json",)),
    ("processing_report", ("processing_report.json",)),
    (
        "selection_result",
        (
            "post_processing/selection/selection_result.json",
            "selection.json",
        ),
    ),
    (
        "post_processing_report",
        (
            "post_processing/reports/post_processing_report.json",
            "post_processing_report.json",
        ),
    ),
    ("job_log", ("job.log", "pipeline.log")),
)

_CLIP_EXTENSIONS = frozenset({".mp4", ".mov", ".webm", ".mkv", ".m4v"})
_METADATA_SUFFIX = "_metadata_writer_v1.json"
_MAX_MULTI_ARTIFACTS = 100

# Stable display order for UI/tests (deterministic, no frontend sorting required).
_ARTIFACT_TYPE_ORDER: dict[str, int] = {
    "transcript": 0,
    "raw_candidate_pool": 1,
    "processing_report": 2,
    "selection_result": 3,
    "post_processing_report": 4,
    "clip_metadata": 5,
    "output_clip": 6,
    "job_log": 7,
}


def _utc_iso_from_mtime(path: Path) -> str | None:
    try:
        ts = path.stat().st_mtime
    except OSError:
        return None
    return (
        datetime.fromtimestamp(ts, tz=UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _size_bytes(path: Path) -> int | None:
    try:
        return int(path.stat().st_size)
    except OSError:
        return None


def _job_rel(token: str, job_id: str, *parts: str) -> str:
    segments = ["jobs", token, job_id, *[p for p in parts if p]]
    return "/".join(segments)


def _resolve_under_job(job_dir: Path, relative: str) -> Path | None:
    """Resolve a relative path under job_dir; reject traversal."""
    text = (relative or "").replace("\\", "/").strip().lstrip("/")
    if not text or ".." in text.split("/"):
        return None
    target = (job_dir / text).resolve()
    try:
        target.relative_to(job_dir.resolve())
    except ValueError:
        return None
    return target


def _singleton_ref(
    *,
    artifact_type: str,
    candidates: tuple[str, ...],
    job_dir: Path,
    token: str,
    job_id: str,
    run_id: str | None,
) -> ArtifactReference:
    for relative in candidates:
        path = _resolve_under_job(job_dir, relative)
        if path is not None and path.is_file():
            return ArtifactReference(
                artifact_type=artifact_type,
                path=_job_rel(token, job_id, *relative.split("/")),
                exists=True,
                environment=token,
                job_id=job_id,
                run_id=run_id,
                created_at=_utc_iso_from_mtime(path),
                size_bytes=_size_bytes(path),
                detail=None,
            )
    expected = candidates[0]
    return ArtifactReference.missing(
        artifact_type,
        path=_job_rel(token, job_id, *expected.split("/")),
        environment=token,
        job_id=job_id,
        run_id=run_id,
        detail="not found",
    )


def _list_files(directory: Path, *, predicate) -> list[Path]:
    if not directory.is_dir():
        return []
    try:
        entries = [p for p in directory.iterdir() if p.is_file() and predicate(p)]
    except OSError:
        return []
    entries.sort(key=lambda p: p.name)
    return entries[:_MAX_MULTI_ARTIFACTS]


def _multi_refs(
    *,
    artifact_type: str,
    relative_dir: str,
    job_dir: Path,
    token: str,
    job_id: str,
    run_id: str | None,
    predicate,
    missing_detail: str,
) -> list[ArtifactReference]:
    safe_dir = _resolve_under_job(job_dir, relative_dir)
    if safe_dir is None:
        return [
            ArtifactReference.missing(
                artifact_type,
                path=_job_rel(token, job_id, *relative_dir.split("/")),
                environment=token,
                job_id=job_id,
                run_id=run_id,
                detail=missing_detail,
            )
        ]

    files = _list_files(safe_dir, predicate=predicate)
    if not files:
        return [
            ArtifactReference.missing(
                artifact_type,
                path=_job_rel(token, job_id, *relative_dir.split("/")),
                environment=token,
                job_id=job_id,
                run_id=run_id,
                detail=missing_detail,
            )
        ]

    refs: list[ArtifactReference] = []
    for path in files:
        rel = f"{relative_dir.rstrip('/')}/{path.name}"
        refs.append(
            ArtifactReference(
                artifact_type=artifact_type,
                path=_job_rel(token, job_id, *rel.split("/")),
                exists=True,
                environment=token,
                job_id=job_id,
                run_id=run_id,
                created_at=_utc_iso_from_mtime(path),
                size_bytes=_size_bytes(path),
                detail=None,
            )
        )
    return refs


def _is_clip_file(path: Path) -> bool:
    return path.suffix.lower() in _CLIP_EXTENSIONS


def _is_metadata_file(path: Path) -> bool:
    name = path.name
    return name.endswith(_METADATA_SUFFIX) or name.endswith("_metadata.json")


def _job_log_refs(
    *,
    job_dir: Path,
    token: str,
    job_id: str,
    run_id: str | None,
) -> tuple[ArtifactReference, LogReference]:
    artifact = _singleton_ref(
        artifact_type="job_log",
        candidates=("job.log", "pipeline.log"),
        job_dir=job_dir,
        token=token,
        job_id=job_id,
        run_id=run_id,
    )
    # Also accept a single *.log in the job root if named candidates are absent.
    if not artifact.exists:
        logs = _list_files(job_dir, predicate=lambda p: p.suffix.lower() == ".log")
        if logs:
            path = logs[0]
            artifact = ArtifactReference(
                artifact_type="job_log",
                path=_job_rel(token, job_id, path.name),
                exists=True,
                environment=token,
                job_id=job_id,
                run_id=run_id,
                created_at=_utc_iso_from_mtime(path),
                size_bytes=_size_bytes(path),
                detail=None,
            )

    log_ref = LogReference(
        source="job",
        path=artifact.path if artifact.exists else None,
        job_id=job_id,
        run_id=run_id,
        detail=None if artifact.exists else "job log not found",
    )
    return artifact, log_ref


def resolve_job_artifacts(
    mk04_env_token: str,
    job_id: str,
) -> dict[str, Any] | None:
    """Resolve artifacts for a job.

    Returns a payload dict, or None when the job cannot be resolved.
    Missing individual artifacts are included with exists=False.
    """
    if not _is_safe_id(job_id):
        return None

    token = _env_token(mk04_env_token)
    jobs_root = _jobs_root_for(token)
    job_dir = _find_job_dir(jobs_root, job_id)
    if job_dir is None:
        return None

    report = _read_report(job_dir)
    if report is None:
        return None

    ctx = _execution_context(report, job_dir)
    run_id = None
    if isinstance(ctx, dict):
        run_id = str(ctx.get("run_id") or "").strip() or None
    resolved_job_id = str(report.get("job_id") or job_id)

    artifacts: list[ArtifactReference] = []
    for artifact_type, candidates in _SINGLETON_ARTIFACTS:
        if artifact_type == "job_log":
            continue
        artifacts.append(
            _singleton_ref(
                artifact_type=artifact_type,
                candidates=candidates,
                job_dir=job_dir,
                token=token,
                job_id=resolved_job_id,
                run_id=run_id,
            )
        )

    # Clip metadata (may be zero or many).
    metadata_refs = _multi_refs(
        artifact_type="clip_metadata",
        relative_dir="post_processing/metadata",
        job_dir=job_dir,
        token=token,
        job_id=resolved_job_id,
        run_id=run_id,
        predicate=_is_metadata_file,
        missing_detail="no clip metadata files",
    )
    artifacts.extend(metadata_refs)

    # Output clips from primary clips/ and post_processing/clips/.
    clip_refs: list[ArtifactReference] = []
    for relative_dir in ("clips", "post_processing/clips"):
        clip_refs.extend(
            _multi_refs(
                artifact_type="output_clip",
                relative_dir=relative_dir,
                job_dir=job_dir,
                token=token,
                job_id=resolved_job_id,
                run_id=run_id,
                predicate=_is_clip_file,
                missing_detail="no output clips",
            )
        )
    # Collapse dual "missing" placeholders into one when both dirs are empty.
    existing_clips = [r for r in clip_refs if r.exists]
    if existing_clips:
        artifacts.extend(existing_clips)
    else:
        artifacts.append(
            ArtifactReference.missing(
                "output_clip",
                path=_job_rel(token, resolved_job_id, "clips"),
                environment=token,
                job_id=resolved_job_id,
                run_id=run_id,
                detail="no output clips",
            )
        )

    job_log_artifact, log_ref = _job_log_refs(
        job_dir=job_dir,
        token=token,
        job_id=resolved_job_id,
        run_id=run_id,
    )
    artifacts.append(job_log_artifact)

    artifacts.sort(
        key=lambda a: (
            _ARTIFACT_TYPE_ORDER.get(a.artifact_type, 99),
            a.path or "",
        )
    )

    return {
        "environment": token,
        "job_id": resolved_job_id,
        "artifacts": [a.to_dict() for a in artifacts],
        "logs": [log_ref.to_dict()],
        "count": len(artifacts),
        "present_count": sum(1 for a in artifacts if a.exists),
        "missing_count": sum(1 for a in artifacts if not a.exists),
        "schema_version": CONTRACT_SCHEMA_VERSION,
    }


def list_artifact_references(
    mk04_env_token: str,
    job_id: str,
) -> list[ArtifactReference] | None:
    """Return ArtifactReference models, or None if the job is missing."""
    payload = resolve_job_artifacts(mk04_env_token, job_id)
    if payload is None:
        return None
    refs: list[ArtifactReference] = []
    for item in payload["artifacts"]:
        ref = ArtifactReference.from_dict(item)
        if ref is not None:
            refs.append(ref)
    return refs
