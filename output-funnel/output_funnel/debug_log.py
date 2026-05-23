from __future__ import annotations

import json
import time
from typing import Any

_DEBUG_LOG_PATH = "/Users/anthonymaguire/VAmk0.4/.cursor/debug-8aae3e.log"
_SESSION_ID = "8aae3e"


def agent_debug_log(
    *,
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, Any] | None = None,
    run_id: str = "pre-fix",
) -> None:
    # #region agent log
    try:
        payload = {
            "sessionId": _SESSION_ID,
            "timestamp": int(time.time() * 1000),
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data or {},
            "runId": run_id,
        }
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")
    except Exception:
        pass
    # #endregion
