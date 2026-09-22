"""Page-level OCR result cache.

Cache key: sha256(file_bytes) + page_number + provider_tag + str(options_hash).
Storage   : ``var/cache/ocr/`` as tiny JSON files (one per key, TTL-evicted).

The cache is disabled when:
- ``ocr_cache_dir`` is falsy or the target directory cannot be created.
- TTL is 0.

The cache stores the extracted text plus optional OCR line locators.  Keeping
the line records is required for citation bbox fidelity on warm-cache parses.
Legacy text-only entries remain readable, but callers that require locators
can treat them as a cache miss and refresh them.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any


_DEFAULT_CACHE_DIR = Path("var") / "cache" / "ocr"
_DEFAULT_TTL_SECONDS = 7 * 24 * 3600  # 7 days
#: Default page-entry bound.  20k pages is roughly 80 large scanned documents;
#: enough to be useful, small enough that the directory cannot run away.
_DEFAULT_MAX_ENTRIES = 20000

#: Result-affecting OCR settings that must be part of the cache key.
#:
#: A cache key that omits the provider or its options serves one engine's output
#: as another engine's result, which is a silent correctness bug rather than a
#: performance one: switching ``providers.ocr.provider`` or its ``options`` would
#: keep returning the previous engine's text.
_OCR_IDENTITY_KEYS: tuple[str, ...] = (
    "provider",
    "base_url",
    "model",
    "lang",
    "language",
    "engine",
    "options",
    "det_model",
    "rec_model",
    "cls_model",
)


def ocr_cache_identity(settings: Any = None) -> tuple[str, str]:
    """Return ``(provider_tag, options_repr)`` for an OCR provider configuration.

    ``provider_tag`` names the engine; ``options_repr`` is a stable hash of the
    settings that change its output.  Both are required in the cache key.
    """

    if settings is None:
        return "unknown", ""

    provider = str(
        _setting(settings, "provider") or _setting(settings, "name") or "unknown"
    ).strip().lower()
    provider = provider or "unknown"
    payload: dict[str, Any] = {}
    for key in _OCR_IDENTITY_KEYS:
        if key in {"provider", "name"}:
            continue
        value = _setting(settings, key)
        if value in (None, "", (), []):
            continue
        payload[key] = value
    if not payload:
        return provider, ""
    return provider, hashlib.sha256(_stable_repr(payload).encode("utf-8")).hexdigest()[:16]


def _setting(settings: Any, name: str) -> Any:
    if isinstance(settings, dict):
        return settings.get(name)
    return getattr(settings, name, None)


def _stable_repr(value: Any) -> str:
    if isinstance(value, dict):
        return json.dumps(
            {str(key): _plain(item) for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))},
            ensure_ascii=False,
            sort_keys=False,
        )
    return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True)


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _entry_timestamp(entry: Any) -> float | None:
    """Return a valid cache timestamp, or ``None`` for a damaged entry."""

    if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
        return None
    timestamp = entry.get("ts")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return None
    try:
        timestamp = float(timestamp)
    except (OverflowError, ValueError):
        return None
    return timestamp if math.isfinite(timestamp) else None


def _discard(path: Path) -> None:
    """Best-effort removal for corrupt or expired cache files."""

    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _key(
    file_bytes: bytes | None,
    file_path: str,
    page_number: int,
    provider_tag: str,
    options_repr: str,
) -> str:
    """Derive a stable cache key string."""
    if file_bytes is not None:
        file_hash = hashlib.sha256(file_bytes).hexdigest()[:16]
    else:
        # Fall back to path + mtime for large files we don't want to hash fully.
        try:
            stat = os.stat(file_path)
            file_hash = hashlib.sha256(
                f"{file_path}:{stat.st_size}:{stat.st_mtime}".encode()
            ).hexdigest()[:16]
        except OSError:
            file_hash = hashlib.sha256(file_path.encode()).hexdigest()[:16]
    raw = f"{file_hash}:{page_number}:{provider_tag}:{options_repr}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class PageOcrCache:
    """Disk-backed page-level OCR text cache.

    Parameters
    ----------
    cache_dir:
        Directory where cache entries are stored.  Created on first write.
    ttl_seconds:
        Entries older than this are treated as expired.  Set to 0 to disable.
    """

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._cache_dir: Path | None
        if cache_dir is None:
            self._cache_dir = _DEFAULT_CACHE_DIR
        elif not cache_dir:
            self._cache_dir = None  # disabled
        else:
            self._cache_dir = Path(cache_dir)
        self._ttl = float(ttl_seconds)
        # 0 disables the bound.  The bound exists because the page cache had only
        # a TTL: a long-running deployment with many distinct documents grows the
        # directory without limit until the TTL expires.
        self._max_entries = max(0, int(max_entries))

    @property
    def enabled(self) -> bool:
        return self._cache_dir is not None and self._ttl > 0

    def _path(self, key: str) -> Path:
        assert self._cache_dir is not None
        return self._cache_dir / key[:2] / f"{key}.json"

    def get(
        self,
        *,
        file_path: str,
        page_number: int,
        provider_tag: str,
        options_repr: str = "",
        file_bytes: bytes | None = None,
    ) -> str | None:
        """Return cached OCR text or ``None`` on miss/expiry."""
        entry = self.get_entry(
            file_path=file_path,
            page_number=page_number,
            provider_tag=provider_tag,
            options_repr=options_repr,
            file_bytes=file_bytes,
        )
        if entry is None:
            return None
        text = entry.get("text")
        return text if isinstance(text, str) else None

    def get_entry(
        self,
        *,
        file_path: str,
        page_number: int,
        provider_tag: str,
        options_repr: str = "",
        file_bytes: bytes | None = None,
    ) -> dict[str, Any] | None:
        """Return a complete cache entry or ``None`` on miss/expiry."""
        if not self.enabled:
            return None
        k = _key(file_bytes, file_path, page_number, provider_tag, options_repr)
        path = self._path(k)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, UnicodeDecodeError):
            _discard(path)
            return None
        except OSError:
            return None
        timestamp = _entry_timestamp(entry)
        if timestamp is None:
            _discard(path)
            return None
        if self._ttl > 0 and time.time() - timestamp > self._ttl:
            _discard(path)
            return None
        return entry

    def put(
        self,
        *,
        file_path: str,
        page_number: int,
        provider_tag: str,
        text: str,
        lines: list[dict[str, Any]] | None = None,
        options_repr: str = "",
        file_bytes: bytes | None = None,
    ) -> None:
        """Write OCR text and optional line locators; ignore write failures.

        The write is atomic (temp file + ``os.replace``): a reader must never see
        a half-written entry, because the reader treats unparsable JSON as a miss
        and would silently re-run OCR while the writer is still flushing.
        """

        if not self.enabled:
            return
        k = _key(file_bytes, file_path, page_number, provider_tag, options_repr)
        path = self._path(k)
        payload = json.dumps(
            {"ts": time.time(), "text": text, "lines": lines or []},
            ensure_ascii=False,
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{k}.",
                suffix=".tmp",
                delete=False,
            )
            temp_path = Path(handle.name)
            try:
                with handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, path)
            except BaseException:
                temp_path.unlink(missing_ok=True)
                raise
        except OSError:
            pass  # cache write failure is not fatal
        else:
            self._enforce_capacity()

    def _enforce_capacity(self) -> int:
        """Drop the oldest entries when the entry bound is exceeded."""

        if self._max_entries <= 0 or self._cache_dir is None:
            return 0
        try:
            entries = list(self._cache_dir.rglob("*.json"))
        except OSError:
            return 0
        overflow = len(entries) - self._max_entries
        if overflow <= 0:
            return 0
        aged: list[tuple[float, Path]] = []
        for entry_path in entries:
            try:
                aged.append((entry_path.stat().st_mtime, entry_path))
            except OSError:
                continue
        aged.sort()
        removed = 0
        for _mtime, entry_path in aged[:overflow]:
            try:
                entry_path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
        return removed

    def stats(self) -> dict[str, Any]:
        """Entry count and bound, for observability."""

        if self._cache_dir is None:
            return {"enabled": False, "entries": 0, "max_entries": self._max_entries}
        try:
            entries = len(list(self._cache_dir.rglob("*.json")))
        except OSError:
            entries = 0
        return {
            "enabled": self.enabled,
            "entries": entries,
            "max_entries": self._max_entries,
            "ttl_seconds": self._ttl,
        }

    def evict_expired(self) -> int:
        """Remove all entries that have exceeded their TTL.  Returns evicted count."""
        if not self.enabled or self._cache_dir is None:
            return 0
        count = 0
        now = time.time()
        try:
            for entry_path in self._cache_dir.rglob("*.json"):
                try:
                    raw = entry_path.read_text(encoding="utf-8")
                    data = json.loads(raw)
                    timestamp = _entry_timestamp(data)
                    if timestamp is None:
                        _discard(entry_path)
                    elif now - timestamp > self._ttl:
                        entry_path.unlink(missing_ok=True)
                        count += 1
                except (json.JSONDecodeError, UnicodeDecodeError):
                    _discard(entry_path)
                except OSError:
                    pass
        except OSError:
            pass
        return count


# Module-level default cache instance (lazy initialised).
_default_cache: PageOcrCache | None = None


def get_default_cache() -> PageOcrCache:
    global _default_cache
    if _default_cache is None:
        _default_cache = PageOcrCache()
    return _default_cache
