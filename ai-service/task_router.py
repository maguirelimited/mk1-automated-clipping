from __future__ import annotations

from typing import Any


RECOGNISED_TASK_TYPES = {
    "clip_selection",
    "section_candidate_discovery",
    "quality_inspection",
    "edit_plan",
    "metadata",
}

IMPLEMENTED_TASK_TYPES: set[str] = {"section_candidate_discovery"}


class UnknownTaskError(RuntimeError):
    def __init__(self, task_type: str):
        self.task_type = task_type
        self.code = "UNKNOWN_TASK_TYPE"
        self.message = "Unknown task_type."
        super().__init__(self.message)


class TaskNotImplementedError(RuntimeError):
    def __init__(self, task_type: str):
        self.task_type = task_type
        self.code = "TASK_NOT_IMPLEMENTED"
        self.message = "Task type is recognised but not implemented yet."
        super().__init__(self.message)


class AITaskError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 500):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class TaskRouter:
    """Task type dispatch boundary.

    The router dispatches implemented judgement tasks and keeps unrelated task
    types as explicit placeholders. It does not own queues, retries, or job
    truth.
    """

    def recognised_task_types(self) -> set[str]:
        return set(RECOGNISED_TASK_TYPES)

    def implemented_task_types(self) -> set[str]:
        return {"clip_selection", *IMPLEMENTED_TASK_TYPES}

    def route(
        self,
        task_type: str,
        payload: dict[str, Any],
        *,
        settings: Any,
        prompt_text: str,
        schema: dict[str, Any],
        model_client: Any | None = None,
    ) -> dict[str, Any]:
        if task_type not in RECOGNISED_TASK_TYPES:
            raise UnknownTaskError(task_type)
        if task_type == "clip_selection":
            from tasks.clip_selection import run_clip_selection

            return run_clip_selection(
                payload=payload,
                settings=settings,
                prompt_text=prompt_text,
                schema=schema,
                model_client=model_client,
            )
        if task_type == "section_candidate_discovery":
            from tasks.section_candidate_discovery import run_section_candidate_discovery

            return run_section_candidate_discovery(
                payload=payload,
                settings=settings,
                prompt_text=prompt_text,
                schema=schema,
                model_client=model_client,
            )
        if task_type not in self.implemented_task_types():
            raise TaskNotImplementedError(task_type)
        raise TaskNotImplementedError(task_type)
