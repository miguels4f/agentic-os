"""Crash-safe publication shared by lifecycle records and executable revisions."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


def fault(point: str) -> None:
    """Abrupt process death at a named publication boundary (acceptance testing)."""
    if os.environ.get("FOUNDRY_CRASH_AT") == point:
        os._exit(86)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic(path: Path, data: bytes) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("publication path must not be a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if os.environ.get("FOUNDRY_CRASH_AT") == "partial_file_written":
                midpoint = len(data) // 2
                stream.write(data[:midpoint])
                stream.flush()
                os.fsync(stream.fileno())
                fault("partial_file_written")
                stream.write(data[midpoint:])
            else:
                stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        fault("file_written")
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def immutable(path: Path, data: bytes) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("revision path must not be a symbolic link")
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError("existing immutable revision failed its integrity check")
    else:
        atomic(path, data)


def publish_state(root: Path, data: bytes) -> None:
    revision = root / "revisions" / f"{hashlib.sha256(data).hexdigest()}.json"
    immutable(revision, data)
    fault("state_revision")
    atomic(root / "state.json", data)
    fault("state_committed")


def publish_build(root: Path, bundle, materialize) -> dict:
    """Stage all files, then expose a new version without replacing its predecessor."""
    digest = hashlib.sha256(json.dumps(bundle.files(), sort_keys=True).encode()).hexdigest()
    destination = root / f"generated-{digest}"
    if destination.is_symlink():
        raise ValueError("generated revision must not be a symbolic link")
    if not destination.exists():
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=root)) / "bundle"
        materialize(bundle, staging)
        sync_directory(staging)
        os.rename(staging, destination)
        sync_directory(root)
        fault("build_published")
    actual = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in destination.iterdir() if path.is_file()}
    expected = {name: hashlib.sha256(content.encode()).hexdigest()
                for name, content in bundle.files().items()}
    if destination.is_symlink() or actual != expected:
        raise ValueError("generated revision failed its integrity check")
    return {"directory": destination.name, "bundle_id": bundle.bundle_id,
            "runtime_version": bundle.manifest["runtime_version"],
            "manifest_sha256": actual["manifest.json"], "file_sha256": actual}
