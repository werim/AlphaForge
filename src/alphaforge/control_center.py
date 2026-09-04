"""Executable adapter for the existing AlphaForge dashboard/Control Center app."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

from alphaforge.dashboard.app import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m alphaforge.control_center", description="Run the PAPER Control Center backend.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", help="Canonical AlphaForge SQLite database path (or use ALPHAFORGE_DB_PATH).")
    parser.add_argument("--project-root", help="AlphaForge project root (or use ALPHAFORGE_PROJECT_ROOT).")
    parser.add_argument("--cors-origin", action="append", default=None, help="Allowed frontend origin; repeat for multiple origins.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    import uvicorn
    if args.db:
        os.environ["ALPHAFORGE_DB_PATH"] = str(Path(args.db).expanduser().resolve())
    if args.project_root:
        os.environ["ALPHAFORGE_PROJECT_ROOT"] = str(Path(args.project_root).expanduser().resolve())
    if args.cors_origin is not None:
        os.environ["ALPHAFORGE_CONTROL_CORS_ORIGINS"] = ",".join(args.cors_origin)
    db = os.getenv("ALPHAFORGE_DB_PATH")
    database_url = f"sqlite+pysqlite:///{Path(db).expanduser().resolve()}" if db else None
    uvicorn.run(create_app(database_url), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    from alphaforge.env_contract import bootstrap_environment
    bootstrap_environment()
    raise SystemExit(main())
