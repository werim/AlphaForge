"""Authoritative runtime database defaults and override resolution."""
from __future__ import annotations
from pathlib import Path, PureWindowsPath
import re
from typing import Mapping
from sqlalchemy.engine import make_url

DEFAULT_RUNTIME_DB_RELATIVE_PATH = Path("data/runtime/alphaforge_runtime.db")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

def default_runtime_db_path(root: Path | None = None) -> Path:
    return ((root or REPOSITORY_ROOT) / DEFAULT_RUNTIME_DB_RELATIVE_PATH).resolve()

def sqlite_url_for_path(path: str | Path) -> str:
    raw = str(path)
    if re.match(r"^[A-Za-z]:[\\/]", raw):
        return f"sqlite+pysqlite:///{PureWindowsPath(raw).as_posix()}"
    return f"sqlite+pysqlite:///{Path(path).expanduser().resolve().as_posix()}"

def default_runtime_database_url(root: Path | None = None) -> str:
    return sqlite_url_for_path(default_runtime_db_path(root))

def resolve_runtime_database_url(env: Mapping[str, str], root: Path | None = None) -> str:
    configured = next((str(env[name]).strip() for name in ("ALPHAFORGE_DATABASE_URL", "ALPHAFORGE_DB_URL", "DATABASE_URL") if str(env.get(name, "")).strip()), None)
    if configured:
        url = make_url(configured)
        if not url.get_backend_name().startswith("sqlite") or url.database == ":memory:": return configured
        path = Path(url.database or "").expanduser()
        if not path.is_absolute(): path = (root or REPOSITORY_ROOT) / path
        return url.set(database=str(path.resolve())).render_as_string(hide_password=False)
    legacy = str(env.get("ALPHAFORGE_DB_PATH", "")).strip()
    if legacy:
        path = Path(legacy).expanduser()
        if not path.is_absolute(): path = (root or REPOSITORY_ROOT) / path
        return sqlite_url_for_path(path)
    return default_runtime_database_url(root)

def sqlite_path_from_url(database_url: str) -> Path | None:
    url = make_url(database_url)
    if not url.get_backend_name().startswith("sqlite") or not url.database or url.database == ":memory:": return None
    return Path(url.database).expanduser().resolve()
