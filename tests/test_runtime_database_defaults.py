from pathlib import Path
from configparser import ConfigParser

from alphaforge.database_defaults import (
    DEFAULT_RUNTIME_DATABASE_URL, DEFAULT_RUNTIME_DB_RELATIVE_PATH,
    default_runtime_database_url, resolve_alembic_database_url,
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


def test_database_url_wins_for_runtime_and_burnin_clis(monkeypatch, tmp_path: Path) -> None:
    canonical = tmp_path / "canonical-url.db"
    legacy = tmp_path / "legacy-path.db"
    monkeypatch.setenv("ALPHAFORGE_DATABASE_URL", f"sqlite+pysqlite:///{canonical}")
    monkeypatch.setenv("ALPHAFORGE_DB_PATH", str(legacy))
    args = type("Args", (), {"db": None})()

    from alphaforge.config import load_config_from_env

    runtime_path = sqlite_path_from_url(load_config_from_env().persistence.database_url)
    assert runtime_path == canonical
    assert Path(burnin_ops._db_path(args)) == canonical
    assert Path(burnin_cli._db_path(args)) == canonical


def test_explicit_db_wins_for_burnin_clis(monkeypatch, tmp_path: Path) -> None:
    explicit = tmp_path / "explicit.db"
    monkeypatch.setenv("ALPHAFORGE_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'url.db'}")
    monkeypatch.setenv("ALPHAFORGE_DB_PATH", str(tmp_path / "legacy.db"))
    args = type("Args", (), {"db": str(explicit)})()

    assert Path(burnin_ops._db_path(args)) == explicit
    assert Path(burnin_cli._db_path(args)) == explicit


def test_alembic_declared_default_matches_runtime() -> None:
    parser = ConfigParser()
    parser.read("alembic.ini")
    declared = parser["alembic"]["sqlalchemy.url"]
    assert Path(sqlite_path_from_url(declared)).as_posix().endswith(DEFAULT_RUNTIME_DB_RELATIVE_PATH.as_posix())


def _temporary_repository(tmp_path: Path, dotenv: str | None = None) -> Path:
    (tmp_path / "src" / "alphaforge").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='dotenv-contract-test'\n")
    if dotenv is not None:
        (tmp_path / ".env").write_text(dotenv, encoding="utf-8")
    return tmp_path


def test_alembic_and_runtime_match_database_url_from_dotenv(monkeypatch, tmp_path: Path) -> None:
    configured = tmp_path / "custom" / "runtime.db"
    root = _temporary_repository(
        tmp_path,
        f"ALPHAFORGE_DATABASE_URL=sqlite+pysqlite:///{configured.as_posix()}\n",
    )
    for key in ("ALPHAFORGE_DATABASE_URL", "ALPHAFORGE_DB_URL", "DATABASE_URL", "ALPHAFORGE_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(root)

    from alphaforge.config import load_config_from_env

    runtime_url = load_config_from_env().persistence.database_url
    alembic_url = resolve_alembic_database_url(DEFAULT_RUNTIME_DATABASE_URL, {}, root)
    assert sqlite_path_from_url(runtime_url) == configured
    assert sqlite_path_from_url(alembic_url) == configured
    assert not (root / "alphaforge.db").exists()


def test_alembic_dotenv_database_url_wins_over_legacy_path(tmp_path: Path) -> None:
    configured = tmp_path / "url-wins.db"
    legacy = tmp_path / "legacy-loses.db"
    root = _temporary_repository(
        tmp_path,
        "\n".join((
            f"ALPHAFORGE_DATABASE_URL=sqlite+pysqlite:///{configured.as_posix()}",
            f"ALPHAFORGE_DB_PATH={legacy.as_posix()}",
        )),
    )
    resolved = resolve_alembic_database_url(DEFAULT_RUNTIME_DATABASE_URL, {}, root)
    assert sqlite_path_from_url(resolved) == configured


def test_alembic_dotenv_bootstrap_preserves_default_and_deliberate_override(tmp_path: Path) -> None:
    default_root = _temporary_repository(tmp_path / "default-root")
    default_url = resolve_alembic_database_url(DEFAULT_RUNTIME_DATABASE_URL, {}, default_root)
    deliberate = f"sqlite+pysqlite:///{(tmp_path / 'operator.db').as_posix()}"
    override_root = _temporary_repository(
        tmp_path / "override-root",
        f"ALPHAFORGE_DATABASE_URL=sqlite+pysqlite:///{(tmp_path / 'dotenv.db').as_posix()}\n",
    )

    assert sqlite_path_from_url(default_url) == default_root / DEFAULT_RUNTIME_DB_RELATIVE_PATH
    assert resolve_alembic_database_url(deliberate, {}, override_root) == deliberate
    assert not (default_root / "alphaforge.db").exists()
    assert not (override_root / "alphaforge.db").exists()


def test_windows_drive_url_is_not_corrupted() -> None:
    assert sqlite_url_for_path(r"C:\AlphaForge\data\runtime\alphaforge_runtime.db") == (
        "sqlite+pysqlite:///C:/AlphaForge/data/runtime/alphaforge_runtime.db"
    )


def test_komutlar_documents_only_valid_db_doctor_form() -> None:
    guide = Path("docs/KOMUTLAR.md").read_text(encoding="utf-8")
    assert "db-doctor diagnose" not in guide
    assert "db-doctor --check-only" in guide
    assert "db-doctor --apply" in guide
    assert "data/runtime/alphaforge_runtime.db" in guide


def test_komutlar_multiday_launch_is_detached_and_uses_canonical_loader() -> None:
    guide = Path("docs/KOMUTLAR.md").read_text(encoding="utf-8")
    assert "--duration-days 7 `\n  --detach" in guide
    assert "load_config_from_env, load_reconciliation_settings" in guide
    assert 'print(f"KEY={bool(recon.api_key.strip())}")' in guide
    assert 'print(f"SECRET={bool(recon.api_secret.strip())}")' in guide
