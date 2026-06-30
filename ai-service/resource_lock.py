from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


AI_BUSY_CODE = "AI_BUSY"
AI_BUSY_MESSAGE = "Local AI model is already processing another heavy task. Retry later."


class ResourceBusyError(RuntimeError):
    """Raised when a heavy local-model task is requested while the lock is held.

    This is a controlled, non-fatal signal. `ai-service` does not queue, sleep,
    or retry. Callers (for example `video-automation`) own retries and job truth.
    """

    def __init__(self, message: str = AI_BUSY_MESSAGE):
        self.code = AI_BUSY_CODE
        self.message = message
        self.status_code = 503
        super().__init__(message)


class LocalModelResourceLock:
    """One-at-a-time, non-blocking guard around heavy local-model generation.

    MK1 runs a single local model. Concurrent heavy tasks would contend for the
    same GPU/CPU memory, so heavy work is serialised. The lock is in-process
    only: it does not span multiple service processes and is intentionally not a
    queue.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        """Acquire without blocking. Returns False immediately if already held."""
        return self._lock.acquire(blocking=False)

    def release(self) -> None:
        try:
            self._lock.release()
        except RuntimeError:
            # Releasing an unlocked lock should never crash the request path.
            pass

    def is_held(self) -> bool:
        acquired = self._lock.acquire(blocking=False)
        if acquired:
            self._lock.release()
            return False
        return True

    @contextmanager
    def guard(self) -> Iterator[None]:
        """Run a heavy task while holding the lock.

        Raises :class:`ResourceBusyError` immediately if the lock is already
        held. The lock is always released in the ``finally`` block, so it is
        freed on success, on a controlled task error, on a validation failure,
        and on any unexpected exception.
        """
        if not self.try_acquire():
            raise ResourceBusyError()
        try:
            yield
        finally:
            self.release()


# Process-wide singleton. Heavy model-backed endpoints share this one lock.
MODEL_RESOURCE_LOCK = LocalModelResourceLock()
