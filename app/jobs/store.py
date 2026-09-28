"""Temporary on-disk job storage.

Each job is a private directory holding the uploaded file(s), the generated catalog and meta.json.

    created    <root>/<id>/
    download   atomically renamed to <id>.claimed (a concurrent second download loses the race), streamed;
               deleted when the whole file was sent, renamed back if the transfer was cut off.
    discarded  deleted on DELETE.
    expired    the sweeper deletes jobs older than the TTL, claimed dirs orphaned by a crash,
               and half-written dirs from requests that died mid-upload.
"""

import json
import logging
import os
import secrets
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

META = "meta.json"
OUTPUT = "output.xlsx"
CLAIMED_SUFFIX = ".claimed"
ORPHAN_GRACE_SECONDS = 600


@dataclass
class JobMeta:
    job_id: str
    owner: str
    created_at: float
    expires_at: float
    input_files: list[dict] = field(default_factory=list)  # [{"name": ..., "bytes": ...}]
    output_file: str = ""
    output_bytes: int = 0
    summary: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class JobStore:
    def __init__(self, root: Path, ttl_seconds: int):
        self.root = root
        self.ttl_seconds = ttl_seconds
        root.mkdir(parents=True, exist_ok=True)

    # ---------- lifecycle ----------

    def create(self, owner: str) -> tuple[JobMeta, Path]:
        job_id = secrets.token_urlsafe(18)  # 144 bits: unguessable, and still owner-checked on every access
        path = self.root / job_id
        path.mkdir(mode=0o700)
        now = time.time()
        return JobMeta(job_id=job_id, owner=owner, created_at=now, expires_at=now + self.ttl_seconds), path

    def commit(self, meta: JobMeta) -> None:
        """Writes meta.json last: a directory without it is an incomplete job and invisible to readers."""
        tmp = self.root / meta.job_id / (META + ".tmp")
        tmp.write_text(json.dumps(asdict(meta)), encoding="utf-8")
        os.replace(tmp, self.root / meta.job_id / META)

    def get(self, job_id: str, owner: str) -> JobMeta | None:
        if not _valid_id(job_id):
            return None
        meta = self._read_meta(self.root / job_id)
        if meta is None or meta.owner != owner or meta.expires_at <= time.time():
            return None
        return meta

    def list(self, owner: str) -> list[JobMeta]:
        now = time.time()
        jobs = [m for p in self._job_dirs() if (m := self._read_meta(p)) and m.owner == owner and m.expires_at > now]
        return sorted(jobs, key=lambda m: m.created_at, reverse=True)

    def count_active(self, owner: str) -> int:
        return len(self.list(owner))

    def claim(self, job_id: str, owner: str) -> tuple[JobMeta, Path] | None:
        """Takes exclusive ownership of a job. None if missing, foreign, expired or already claimed."""
        meta = self.get(job_id, owner)
        if meta is None:
            return None
        claimed = self.root / f"{job_id}{CLAIMED_SUFFIX}"
        try:
            os.rename(self.root / job_id, claimed)  # atomic; the loser of a concurrent claim gets OSError
        except OSError:
            return None
        return meta, claimed

    def release(self, meta: JobMeta, claimed: Path) -> bool:
        """Un-claims a job whose download didn't complete, so the user can retry before it expires."""
        if meta.expires_at <= time.time():
            self.delete(claimed)
            return False
        try:
            os.rename(claimed, self.root / meta.job_id)
            return True
        except OSError:
            log.warning("Could not release claimed job %s; deleting it", meta.job_id)
            self.delete(claimed)
            return False

    def delete(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)
        if path.exists():
            log.warning("Could not fully delete %s; the sweeper will retry", path.name)

    def discard(self, job_id: str, owner: str) -> bool:
        claimed = self.claim(job_id, owner)
        if claimed is None:
            return False
        self.delete(claimed[1])
        return True

    # ---------- housekeeping ----------

    def sweep(self) -> list[str]:
        """Deletes expired and orphaned job directories; returns their names."""
        removed, now = [], time.time()
        for path in self.root.iterdir():
            if not path.is_dir():
                continue
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if path.name.endswith(CLAIMED_SUFFIX):
                expired = age > ORPHAN_GRACE_SECONDS  # a live download finishes long before this
            elif meta := self._read_meta(path):
                expired = meta.expires_at <= now
            else:
                expired = age > ORPHAN_GRACE_SECONDS  # request died before commit()
            if expired:
                self.delete(path)
                removed.append(path.name)
        return removed

    def check_writable(self) -> None:
        probe = self.root / f".probe-{secrets.token_hex(4)}"
        probe.write_bytes(b"")
        probe.unlink()

    def _job_dirs(self):
        return (p for p in self.root.iterdir() if p.is_dir() and not p.name.endswith(CLAIMED_SUFFIX))

    @staticmethod
    def _read_meta(path: Path) -> JobMeta | None:
        try:
            return JobMeta(**json.loads((path / META).read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None


def _valid_id(job_id: str) -> bool:
    return 0 < len(job_id) <= 64 and all(c.isalnum() or c in "-_" for c in job_id)
