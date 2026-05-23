#!/usr/bin/env python3
"""Run every SQL diagnostic in a folder and export one CSV per query.

This utility is intentionally SQL-first and read-oriented. It expects each
`.sql` file to contain a single result-producing statement, normally SELECT or
WITH ... SELECT. Files that execute successfully but return no result set still
produce a small status CSV. Failures are isolated per file and written as
`<sql_name>__ERROR.csv` so one broken diagnostic does not hide the rest.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from pathlib import Path
from typing import Iterable


_RESULT_QUERY_PREFIXES = ("select", "with", "pragma", "explain")


def safe_report_name(sql_file: Path) -> str:
    """Convert a SQL filename into a stable CSV-safe report stem."""
    stem = sql_file.stem.lower().strip()
    normalized = re.sub(r"[^a-z0-9_]+", "_", stem).strip("_")
    return normalized or "sql_report"


def iter_sql_files(sql_dir: Path) -> Iterable[Path]:
    """Return SQL files in deterministic order."""
    return sorted(path for path in sql_dir.glob("*.sql") if path.is_file())


def read_sql(sql_path: Path) -> str:
    """Read a SQL file using UTF-8 with BOM tolerance."""
    return sql_path.read_text(encoding="utf-8-sig").strip()


def looks_like_result_query(sql: str) -> bool:
    """Best-effort guard against multi-statement mutation scripts."""
    stripped = sql.lstrip().lower()
    return stripped.startswith(_RESULT_QUERY_PREFIXES)


def write_rows_csv(csv_path: Path, columns: list[str], rows: list[sqlite3.Row]) -> None:
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row[column] for column in columns])


def write_status_csv(csv_path: Path, status: str) -> None:
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["status"])
        writer.writerow([status])


def write_error_csv(csv_path: Path, sql_file: str, error: Exception) -> None:
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sql_file", "error_type", "error"])
        writer.writerow([sql_file, type(error).__name__, str(error)])


def run_sql_to_csv(conn: sqlite3.Connection, sql_path: Path, out_dir: Path) -> Path:
    sql = read_sql(sql_path)
    out_csv = out_dir / f"{safe_report_name(sql_path)}.csv"

    if not sql:
        write_status_csv(out_csv, "SKIPPED_EMPTY_SQL")
        return out_csv

    if not looks_like_result_query(sql):
        raise ValueError(
            "SQL audit files should be single result queries starting with "
            "SELECT, WITH, PRAGMA, or EXPLAIN. Refusing to execute mutation script."
        )

    cursor = conn.execute(sql)
    if cursor.description is None:
        write_status_csv(out_csv, "SQL_EXECUTED_WITH_NO_RESULT_SET")
        return out_csv

    rows = cursor.fetchall()
    columns = [description[0] for description in cursor.description]
    write_rows_csv(out_csv, columns, rows)
    return out_csv


def run_all_sql_audits(db_path: Path, sql_dir: Path, out_dir: Path) -> int:
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite DB not found: {db_path}")
    if not sql_dir.exists():
        raise FileNotFoundError(f"SQL directory not found: {sql_dir}")

    out_dir.mkdir(parents=True, exist_ok=True)
    sql_files = list(iter_sql_files(sql_dir))

    if not sql_files:
        print(f"No .sql files found in {sql_dir}")
        return 0

    failures = 0
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for sql_path in sql_files:
            try:
                csv_path = run_sql_to_csv(conn, sql_path, out_dir)
                print(f"OK {sql_path.name} -> {csv_path}")
            except Exception as exc:  # keep audits independent
                failures += 1
                error_csv = out_dir / f"{safe_report_name(sql_path)}__ERROR.csv"
                write_error_csv(error_csv, sql_path.name, exc)
                print(f"ERROR {sql_path.name} -> {error_csv}: {exc}")

    return failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run each .sql file in a folder against a SQLite DB and export one CSV per SQL file."
    )
    parser.add_argument("--db", required=True, type=Path, help="SQLite database path")
    parser.add_argument("--sql-dir", default=Path("sql"), type=Path, help="Directory containing .sql audit files")
    parser.add_argument(
        "--out-dir",
        default=Path("reports/sql_csv"),
        type=Path,
        help="Directory where CSV reports will be written",
    )
    parser.add_argument(
        "--allow-failures",
        action="store_true",
        help="Exit 0 even if one or more SQL files fail",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    failures = run_all_sql_audits(args.db, args.sql_dir, args.out_dir)
    if failures and not args.allow_failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
