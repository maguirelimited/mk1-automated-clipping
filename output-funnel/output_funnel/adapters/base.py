from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from output_funnel.models import PublishResult


class PlatformAdapter(ABC):
    platform: str

    @abstractmethod
    def publish(self, upload_job: dict[str, Any], source_clip: dict[str, Any], profile: dict[str, Any]) -> PublishResult:
        raise NotImplementedError
