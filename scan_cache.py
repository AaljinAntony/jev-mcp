"""Mtime-keyed caches for workspace scanning.

`load_jev_settings` already re-reads config files only when an mtime changes
(`jev_engine.py:295-361`). The file scanners had no equivalent, so every tool
call re-walked the skill tree with `rglob` and forked a `git ls-files`
subprocess. This module applies the same signature-keyed pattern to both.

The cache holds **paths**, never file content: a signature that only notices
directory-level changes is therefore safe, because the bytes of a candidate are
re-read per call by `candidates.read_text_cache` / `read_head`. Editing the text
of an existing `SKILL.md` is invisible here *by design* and shows up in the
previews instead.
"""

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

#: Sliding window before an entry is re-derived even if its signature is
#: unchanged. Bounds worst-case staleness (mtime granularity, coarse clocks)
#: without paying for a re-scan on every call.
DEFAULT_TTL_S = 5.0


@dataclass
class _Entry:
    value: Any
    signature: tuple
    at: float


class ScanCache:
    """Signature- and TTL-keyed cache. Thread-safe; process-local."""

    def __init__(self, ttl_s: float = DEFAULT_TTL_S) -> None:
        self._ttl = ttl_s
        self._lock = threading.Lock()
        self._entries: dict = {}

    def get(self, key, signature_fn: Callable[[], Any]) -> Any:
        """Return the cached value, or None when absent, stale, or TTL-expired.

        `signature_fn` returns a cheap-to-compare description of the current
        on-disk state (mtimes, sizes, a git HEAD). Cheap beats exact: a
        directory mtime changes when entries are added or removed, which covers
        the overwhelming majority of edits.

        `signature_fn` runs outside the lock, so the entry read at the top may be
        evicted by the time the signature comes back. Every write below therefore
        re-checks membership instead of assuming it, and re-reads the value under
        the lock: `self._entries[key].at = now` on a key another thread had already
        popped raised `KeyError` out of the cache and out of the tool call.
        """
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if now - entry.at > self._ttl:
                self._entries.pop(key, None)
                return None
        try:
            signature = signature_fn()
        except OSError:
            return None
        with self._lock:
            current = self._entries.get(key)
            if current is None or current is not entry:
                return None      # evicted or replaced while the signature ran
            if signature != entry.signature:
                self._entries.pop(key, None)
                return None
            # Sliding TTL: an actively used entry is not expired while in use.
            entry.at = now
            return entry.value

    def put(self, key, signature: tuple, value: Any) -> None:
        with self._lock:
            self._entries[key] = _Entry(value, signature, time.monotonic())

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
