from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent


def _resolve_log_dir() -> Path:
    """Environment-scoped AI decision log directory when deployed."""
    raw = os.environ.get("AI_SERVICE_LOG_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    log_root = os.environ.get("MK04_LOG_ROOT", "").strip()
    if log_root:
        return Path(log_root).expanduser() / "ai-service"
    return BASE_DIR / "logs"


DEFAULT_LOG_DIR = _resolve_log_dir()
DEFAULT_LOG_PATH = DEFAULT_LOG_DIR / "ai_decisions.jsonl"
DEFAULT_ARTIFACT_DIR = DEFAULT_LOG_DIR / "artifacts"
DEFAULT_PREVIEW_STRING_LIMIT = 180
DEFAULT_PREVIEW_COLLECTION_LIMIT = 12
DEFAULT_PREVIEW_DEPTH = 4


@dataclass(frozen=True)
class DecisionLogResult:
    ok: bool
    input_artifact_path: str | None
    output_artifact_path: str | None
    warning: dict[str, str] | None = None


class DecisionLogger:
    def __init__(
        self,
        *,
        log_path: Path | None = None,
        artifact_dir: Path | None = None,
        preview_string_limit: int = DEFAULT_PREVIEW_STRING_LIMIT,
    ):
        self.log_path = log_path or DEFAULT_LOG_PATH
        self.artifact_dir = artifact_dir or DEFAULT_ARTIFACT_DIR
        self.preview_string_limit = preview_string_limit

    def write(
        self,
        *,
        request_payload: dict[str, Any],
        response_payload: dict[str, Any],
    ) -> DecisionLogResult:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            request_id = _safe_artifact_id(str(response_payload.get("request_id") or "unknown"))
            input_artifact = self.artifact_dir / f"{request_id}_input.json"
            output_artifact = self.artifact_dir / f"{request_id}_output.json"

            _write_json(input_artifact, request_payload)
            _write_json(output_artifact, response_payload)

            entry = self._metadata_entry(
                request_payload=request_payload,
                response_payload=response_payload,
                input_artifact=input_artifact,
                output_artifact=output_artifact,
            )
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=True, default=str) + "\n")

            return DecisionLogResult(
                ok=True,
                input_artifact_path=str(input_artifact),
                output_artifact_path=str(output_artifact),
            )
        except Exception as exc:
            return DecisionLogResult(
                ok=False,
                input_artifact_path=None,
                output_artifact_path=None,
                warning={
                    "code": "DECISION_LOG_WRITE_FAILED",
                    "message": f"AI result returned but decision log could not be written: {exc}",
                },
            )

    def _metadata_entry(
        self,
        *,
        request_payload: dict[str, Any],
        response_payload: dict[str, Any],
        input_artifact: Path,
        output_artifact: Path,
    ) -> dict[str, Any]:
        result = response_payload.get("result")
        return {
            "request_id": response_payload.get("request_id"),
            "job_id": response_payload.get("job_id"),
            "task_type": response_payload.get("task_type"),
            "funnel_id": response_payload.get("funnel_id"),
            "input_hash": response_payload.get("input_hash"),
            "output_hash": response_payload.get("output_hash"),
            "model_used": response_payload.get("model_used"),
            "provider": response_payload.get("provider"),
            "prompt_version": response_payload.get("prompt_version"),
            "schema_version": response_payload.get("schema_version"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": response_payload.get("status"),
            "error": response_payload.get("error"),
            "input_preview": build_preview(request_payload, string_limit=self.preview_string_limit),
            "output_preview": build_preview(response_payload, string_limit=self.preview_string_limit),
            "input_artifact_path": str(input_artifact),
            "output_artifact_path": str(output_artifact),
            "ai_result": build_preview(result, string_limit=self.preview_string_limit),
            "final_decision": None,
            "performance": None,
        }


def build_preview(
    value: Any,
    *,
    string_limit: int = DEFAULT_PREVIEW_STRING_LIMIT,
    collection_limit: int = DEFAULT_PREVIEW_COLLECTION_LIMIT,
    depth: int = DEFAULT_PREVIEW_DEPTH,
) -> Any:
    if depth <= 0:
        return _summarize_leaf(value, string_limit=string_limit)
    if isinstance(value, str):
        return _preview_string(value, string_limit=string_limit)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= collection_limit:
                out["__truncated__"] = f"{len(value) - collection_limit} more key(s)"
                break
            if str(key).lower() == "transcript" and isinstance(item, str):
                out[str(key)] = _preview_transcript(item, string_limit=string_limit)
            else:
                out[str(key)] = build_preview(
                    item,
                    string_limit=string_limit,
                    collection_limit=collection_limit,
                    depth=depth - 1,
                )
        return out
    if isinstance(value, list):
        items = [
            build_preview(
                item,
                string_limit=string_limit,
                collection_limit=collection_limit,
                depth=depth - 1,
            )
            for item in value[:collection_limit]
        ]
        if len(value) > collection_limit:
            items.append({"__truncated__": f"{len(value) - collection_limit} more item(s)"})
        return items
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=True, default=str)
        handle.write("\n")


def _safe_artifact_id(request_id: str) -> str:
    allowed = []
    for char in request_id:
        if char.isalnum() or char in {"_", "-", "."}:
            allowed.append(char)
        else:
            allowed.append("_")
    safe = "".join(allowed).strip("._-")
    return safe[:128] or "unknown"


def _preview_transcript(value: str, *, string_limit: int) -> dict[str, Any]:
    return {
        "type": "transcript",
        "length": len(value),
        "preview": _preview_string(value, string_limit=min(string_limit, 80)),
    }


def _preview_string(value: str, *, string_limit: int) -> str:
    if len(value) <= string_limit:
        return value
    return value[:string_limit] + f"...<truncated {len(value) - string_limit} chars>"


def _summarize_leaf(value: Any, *, string_limit: int) -> Any:
    if isinstance(value, str):
        return _preview_string(value, string_limit=string_limit)
    if isinstance(value, dict):
        return {"type": "object", "keys": len(value)}
    if isinstance(value, list):
        return {"type": "list", "items": len(value)}
    return value
