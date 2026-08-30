from pathlib import Path
from configparser import ConfigParser

from alphaforge.database_defaults import (
    DEFAULT_RUNTIME_DB_RELATIVE_PATH, default_runtime_database_url,
    resolve_runtime_database_url, sqlite_path_from_url, sqlite_url_for_path,
)
from alphaforge.persistence import init_db
from alphaforge import burnin_cli, burnin_ops


def test_fresh_default_bootstraps_only_canonical_location(tmp_path: Path) -> None:
    url = default_runtime_database_url(tmp_path)
    engine = init_db(url)
    engine.dispose()
    assert (tmp_path / DEFAULT_RUNTIME_DB_RELATIVE_PATH).is_file()
    assert not (tmp_path / "alphaforge.db").exists()


def test_override_precedence_and_legacy_compatibility(tmp_path: Path) -> None:
    url_db = tmp_path / "url.db"
    legacy_db = tmp_path / "legacy.db"
    assert sqlite_path_from_url(resolve_runtime_database_url({
        "ALPHAFORGE_DATABASE_URL": f"sqlite+pysqlite:///{url_db}",
        "ALPHAFORGE_DB_PATH": str(legacy_db),
    })) == url_db
    assert sqlite_path_from_url(resolve_runtime_database_url({"ALPHAFORGE_DB_PATH": str(legacy_db)})) == legacy_db


def test_burnin_defaults_match_runtime(monkeypatch) -> None:
    for key in ("ALPHAFORGE_DATABASE_URL", "ALPHAFORGE_DB_URL", "DATABASE_URL", "ALPHAFORGE_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    expected = sqlite_path_from_url(default_runtime_database_url())
    args = type("Args", (), {"db": None})()
    assert Path(burnin_ops._db_path(args)) == expected
    assert Path(burnin_cli._db_path(args)) == expected


def test_alembic_declared_default_matches_runtime() -> None:
    parser = ConfigParser()
    parser.read("alembic.ini")
    declared = parser["alembic"]["sqlalchemy.url"]
    assert Path(sqlite_path_from_url(declared)).as_posix().endswith(DEFAULT_RUNTIME_DB_RELATIVE_PATH.as_posix())


def test_windows_drive_url_is_not_corrupted() -> None:
    assert sqlite_url_for_path(r"C:\AlphaForge\data\runtime\alphaforge_runtime.db") == (
        "sqlite+pysqlite:///C:/AlphaForge/data/runtime/alphaforge_runtime.db"
    )
