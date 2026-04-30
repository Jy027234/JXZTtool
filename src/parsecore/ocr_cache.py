"""Page-level OCR result cache.

Cache key: sha256(file_bytes) + page_number + provider_tag + str(options_hash).
Storage   : ``var/cache/ocr/`` as tiny JSON files (one per key, TTL-evicted).

The cache is disabled when:
- ``ocr_cache_dir`` is falsy or the target directory cannot be created.
- TTL is 0.

This module is intentionally small; it only caches the extracted text string
for a single page, not the full layout structure.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any


_DEFAULT_CACHE_DIR = Path("var") / "cache" / "ocr"
_DEFAULT_TTL_SECONDS = 7 * 24 * 3600  # 7 days


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
    ) -> None:
        self._cache_dir: Path | None
        if cache_dir is None:
            self._cache_dir = _DEFAULT_CACHE_DIR
        elif not cache_dir:
            self._cache_dir = None  # disabled
        else:
            self._cache_dir = Path(cache_dir)
        self._ttl = float(ttl_seconds)

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
        if not self.enabled:
            return None
        k = _key(file_bytes, file_path, page_number, provider_tag, options_repr)
        path = self._path(k)
        try:
            raw = path.read_text(encoding="utf-8")
            entry: dict[str, Any] = json.loads(raw)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        if self._ttl > 0 and time.time() - entry.get("ts", 0) > self._ttl:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        return entry.get("text")

    def put(
        self,
        *,
        file_path: str,
        page_number: int,
        provider_tag: str,
        text: str,
        options_repr: str = "",
        file_bytes: bytes | None = None,
    ) -> None:
        """Write OCR text to cache; silently ignores write failures."""
        if not self.enabled:
            return
        k = _key(file_bytes, file_path, page_number, provider_tag, options_repr)
        path = self._path(k)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"ts": time.time(), "text": text}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass  # cache write failure is not fatal

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
                    if now - data.get("ts", 0) > self._ttl:
                        entry_path.unlink(missing_ok=True)
                        count += 1
                except (OSError, json.JSONDecodeError):
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
