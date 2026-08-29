from __future__ import annotations
import hashlib, os, sqlite3
from pathlib import Path

def collect_identity(path: Path) -> dict:
    canonical = path.expanduser().resolve(strict=False)
    exists = canonical.is_file()
    result = {"canonical_path": str(canonical), "exists": exists, "sqlite_version": sqlite3.sqlite_version}
    if exists:
        stat = canonical.stat()
        with canonical.open("rb") as handle:
            prefix = handle.read(4096)
        result["file_identity"] = {"device": stat.st_dev, "inode": stat.st_ino, "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns, "header_sha256": hashlib.sha256(prefix).hexdigest(),
            "wal_exists": Path(str(canonical) + "-wal").exists(), "shm_exists": Path(str(canonical) + "-shm").exists()}
    return result

