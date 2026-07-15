"""Helpers for private, root-bounded runtime artifact directories and files."""

from __future__ import annotations

import os
from pathlib import Path
import re


_SAFE_SUFFIX_PATTERN = re.compile(r"^\.[A-Za-z0-9]{1,16}$")


def safe_upload_suffix(file_name: str, *, fallback: str = ".bin") -> str:
    suffix = Path(str(file_name or "")).suffix
    return suffix.lower() if _SAFE_SUFFIX_PATTERN.fullmatch(suffix) else fallback


def ensure_private_directory(
    path: str | Path,
    *,
    allowed_root: str | Path | None = None,
    exist_ok: bool = True,
) -> Path:
    """Create a private directory and reject symlink escapes from its root."""

    candidate = Path(path).expanduser()
    resolved_root: Path | None = None
    if allowed_root is not None:
        root = Path(allowed_root).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        resolved_root = root.resolve(strict=True)
        _chmod_private_directory(resolved_root)
    candidate.mkdir(parents=True, exist_ok=exist_ok)
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("private_artifact_path_not_directory")
    if resolved_root is not None and resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError("private_artifact_path_outside_root")
    _chmod_private_directory(resolved)
    return resolved


def write_private_bytes(directory: str | Path, file_name: str, content: bytes) -> Path:
    """Exclusively create a private file below an isolated directory."""

    name = Path(str(file_name))
    if name.is_absolute() or name.name != str(file_name) or str(file_name) in {"", ".", ".."}:
        raise ValueError("invalid_private_artifact_filename")
    root = ensure_private_directory(directory)
    target = root / str(file_name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    harden_private_file(target)
    return target


def harden_private_file(path: str | Path) -> None:
    try:
        os.chmod(Path(path), 0o600)
    except OSError:
        # Windows ACLs inherit from the isolated parent; chmod is best effort.
        return


def _chmod_private_directory(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        return
