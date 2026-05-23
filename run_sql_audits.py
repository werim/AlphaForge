#!/usr/bin/env python3
"""Run SQL diagnostics in a folder and export one CSV per result query.

This utility is intentionally SQL-first and read-only. A `.sql` file may
contain one or more result-producing statements, normally SELECT or WITH ...
SELECT. Every statement is validated before any statement in that file is
executed; files containing mutation or setup statements are rejected without
partial execution. Multi-query files produce numbered CSV outputs.

Use `--db auto` to discover a local SQLite runtime database automatically.
Discovery favors PAPER/runtime database names, then the most recently modified
valid SQLite database candidate.

Every run also writes `all_reports_summary.csv`, a single inventory of each
executed SQL statement, execution status, row/column counts, output CSV path,
and DB metadata.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_RESULT_QUERY_PREFIXES = ("select", "with", "pragma", "explain")
_SQLITE_SUFFIXES = (".sqlite", ".sqlite3", ".db")
_DB_ENV_KEYS = (
    "ALPHAFORGE_DB_PATH",
    "ALPHAFORGE_SQLITE_DB",
    "PAPER_RUNTIME_DB",
    "PAPER_RUNTIME_DB_PATH",
    "RUNTIME_DB_PATH",
    "SQLITE_DB_PATH",
)
_KNOWN_DB_PATHS = (
    "data/paper_runtime.sqlite",
    "data/paper_runtime.db",
    "data/runtime.sqlite",
    "data/runtime.db",
    "data/alphaforge.sqlite",
    "data/alphaforge.db",
    "paper_runtime.sqlite",
    "paper_runtime.db",
    "runtime.sqlite",
    "runtime.db",
    "alphaforge.sqlite",
    "alphaforge.db",
)
_EXCLUDED_DIR_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
}
_PREFERRED_NAME_PARTS = (
    "paper_runtime",
    "paper",
    "runtime",
    "alphaforge",
    "order",
    "decision",
    "trade",
)
_SUMMARY_COLUMNS = [
    "sql_file",
    "statement_index",
    "statement_count",
    "status",
    "rows",
    "columns",
    "output_csv",
    "error_type",
    "error",
    "db_path",
    "db_size_bytes",
    "db_mtime_epoch",
]


@dataclass(slots=True)
class AuditResult:
    sql_file: str
    statement_index: int
    statement_count: int
    status: str
    rows: int
    columns: int
    output_csv: str
    error_type: str = ""
    error: str = ""


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


def _sql_without_comments(sql: str) -> str:
    """Remove comments for classification while preserving quoted literals."""
    output: list[str] = []
    i = 0
    in_single = False
    in_double = False
    in_backtick = False
    in_bracket = False
    while i < len(sql):
        char = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if in_single:
            output.append(char)
            if char == "'":
                if nxt == "'":
                    output.append(nxt)
                    i += 1
                else:
                    in_single = False
        elif in_double:
            output.append(char)
            if char == '"':
                if nxt == '"':
                    output.append(nxt)
                    i += 1
                else:
                    in_double = False
        elif in_backtick:
            output.append(char)
            if char == "`":
                in_backtick = False
        elif in_bracket:
            output.append(char)
            if char == "]":
                in_bracket = False
        elif char == "-" and nxt == "-":
            i += 2
            while i < len(sql) and sql[i] not in "\r\n":
                i += 1
            output.append("\n")
            continue
        elif char == "/" and nxt == "*":
            i += 2
            while i + 1 < len(sql) and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i += 1 if i < len(sql) else 0
            output.append(" ")
        else:
            output.append(char)
            if char == "'":
                in_single = True
            elif char == '"':
                in_double = True
            elif char == "`":
                in_backtick = True
            elif char == "[":
                in_bracket = True
        i += 1
    return "".join(output)


def split_sql_statements(sql: str) -> list[str]:
    """Split SQLite statements on unquoted semicolons outside comments."""
    statements: list[str] = []
    current: list[str] = []
    i = 0
    in_single = False
    in_double = False
    in_backtick = False
    in_bracket = False
    in_line_comment = False
    in_block_comment = False
    while i < len(sql):
        char = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        current.append(char)
        if in_line_comment:
            if char in "\r\n":
                in_line_comment = False
        elif in_block_comment:
            if char == "*" and nxt == "/":
                current.append(nxt)
                i += 1
                in_block_comment = False
        elif in_single:
            if char == "'":
                if nxt == "'":
                    current.append(nxt)
                    i += 1
                else:
                    in_single = False
        elif in_double:
            if char == '"':
                if nxt == '"':
                    current.append(nxt)
                    i += 1
                else:
                    in_double = False
        elif in_backtick:
            if char == "`":
                in_backtick = False
        elif in_bracket:
            if char == "]":
                in_bracket = False
        elif char == "-" and nxt == "-":
            current.append(nxt)
            i += 1
            in_line_comment = True
        elif char == "/" and nxt == "*":
            current.append(nxt)
            i += 1
            in_block_comment = True
        elif char == "'":
            in_single = True
        elif char == '"':
            in_double = True
        elif char == "`":
            in_backtick = True
        elif char == "[":
            in_bracket = True
        elif char == ";":
            statement = "".join(current).strip()
            if _sql_without_comments(statement).strip(" ;\t\r\n"):
                statements.append(statement)
            current = []
        i += 1

    tail = "".join(current).strip()
    if _sql_without_comments(tail).strip(" ;\t\r\n"):
        statements.append(tail)
    return statements


def looks_like_result_query(sql: str) -> bool:
    """Return whether a single statement is an allowed read-only query."""
    stripped = _sql_without_comments(sql).lstrip().lower()
    return stripped.startswith(_RESULT_QUERY_PREFIXES)


def is_sqlite_database(path: Path) -> bool:
    """Validate that a file is openable as SQLite without mutating it."""
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA schema_version").fetchone()
        return True
    except sqlite3.Error:
        return False


def candidate_name_score(path: Path) -> int:
    """Prefer runtime/PAPER-looking DBs over unrelated SQLite files."""
    lowered = str(path).lower()
    score = 0
    for index, part in enumerate(_PREFERRED_NAME_PARTS):
        if part in lowered:
            score += (len(_PREFERRED_NAME_PARTS) - index) * 10
    if "test" in lowered or "fixture" in lowered:
        score -= 100
    if "backup" in lowered or "old" in lowered:
        score -= 25
    return score


def iter_db_candidates(search_root: Path) -> Iterable[Path]:
    """Yield DB candidates from env, known paths, and recursive file search."""
    seen: set[Path] = set()
    for env_key in _DB_ENV_KEYS:
        value = os.getenv(env_key)
        if not value:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = search_root / path
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            yield resolved
    for relative in _KNOWN_DB_PATHS:
        resolved = (search_root / relative).resolve()
        if resolved not in seen:
            seen.add(resolved)
            yield resolved
    for suffix in _SQLITE_SUFFIXES:
        for path in search_root.rglob(f"*{suffix}"):
            if any(part in _EXCLUDED_DIR_PARTS for part in path.parts):
                continue
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield resolved


def discover_db(search_root: Path) -> Path:
    """Find the best local SQLite DB candidate for audit execution."""
    search_root = search_root.resolve()
    valid_candidates = [path for path in iter_db_candidates(search_root) if is_sqlite_database(path)]
    if not valid_candidates:
        raise FileNotFoundError(
            "No valid SQLite DB found. Pass --db /path/to/file.sqlite or set one of: "
            + ", ".join(_DB_ENV_KEYS)
        )
    valid_candidates.sort(
        key=lambda path: (
            candidate_name_score(path),
            path.stat().st_mtime,
            -len(path.parts),
            str(path),
        ),
        reverse=True,
    )
    return valid_candidates[0]


def resolve_db_path(db_arg: str, search_root: Path) -> Path:
    """Resolve explicit DB path or auto-discover one."""
    if db_arg.lower() == "auto":
        db_path = discover_db(search_root)
        print(f"AUTO DB {db_path}")
        return db_path
    return Path(db_arg).expanduser()


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


def write_error_csv(csv_path: Path, sql_file: str, error: Exception, statement_index: int = 0) -> None:
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sql_file", "statement_index", "error_type", "error"])
        writer.writerow([sql_file, statement_index or "", type(error).__name__, str(error)])


def _statement_csv_path(sql_path: Path, out_dir: Path, index: int, count: int) -> Path:
    suffix = "" if count == 1 else f"__{index:02d}"
    return out_dir / f"{safe_report_name(sql_path)}{suffix}.csv"


def run_sql_to_csv(conn: sqlite3.Connection, sql_path: Path, out_dir: Path) -> tuple[list[AuditResult], int]:
    sql = read_sql(sql_path)
    if not sql:
        out_csv = _statement_csv_path(sql_path, out_dir, 1, 1)
        write_status_csv(out_csv, "SKIPPED_EMPTY_SQL")
        return [AuditResult(sql_path.name, 0, 0, "SKIPPED_EMPTY_SQL", 0, 1, str(out_csv))], 0

    statements = split_sql_statements(sql)
    if not statements:
        out_csv = _statement_csv_path(sql_path, out_dir, 1, 1)
        write_status_csv(out_csv, "SKIPPED_EMPTY_SQL")
        return [AuditResult(sql_path.name, 0, 0, "SKIPPED_EMPTY_SQL", 0, 1, str(out_csv))], 0

    invalid = [index for index, statement in enumerate(statements, start=1) if not looks_like_result_query(statement)]
    if invalid:
        raise ValueError(
            "SQL audit files may contain only result queries starting with SELECT, WITH, PRAGMA, or EXPLAIN. "
            f"Refusing file before execution because statement(s) {invalid} are not allowed."
        )

    count = len(statements)
    results: list[AuditResult] = []
    failures = 0
    for index, statement in enumerate(statements, start=1):
        out_csv = _statement_csv_path(sql_path, out_dir, index, count)
        try:
            cursor = conn.execute(statement)
            if cursor.description is None:
                write_status_csv(out_csv, "SQL_EXECUTED_WITH_NO_RESULT_SET")
                results.append(AuditResult(sql_path.name, index, count, "NO_RESULT_SET", 0, 1, str(out_csv)))
                continue
            rows = cursor.fetchall()
            columns = [description[0] for description in cursor.description]
            write_rows_csv(out_csv, columns, rows)
            results.append(AuditResult(sql_path.name, index, count, "OK", len(rows), len(columns), str(out_csv)))
        except Exception as exc:
            failures += 1
            error_csv = out_dir / f"{safe_report_name(sql_path)}__{index:02d}__ERROR.csv"
            write_error_csv(error_csv, sql_path.name, exc, index)
            results.append(
                AuditResult(sql_path.name, index, count, "ERROR", 1, 4, str(error_csv), type(exc).__name__, str(exc))
            )
    return results, failures


def write_summary_csv(summary_path: Path, db_path: Path, results: list[AuditResult]) -> None:
    db_stat = db_path.stat()
    with summary_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_SUMMARY_COLUMNS)
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "sql_file": result.sql_file,
                    "statement_index": result.statement_index,
                    "statement_count": result.statement_count,
                    "status": result.status,
                    "rows": result.rows,
                    "columns": result.columns,
                    "output_csv": result.output_csv,
                    "error_type": result.error_type,
                    "error": result.error,
                    "db_path": str(db_path),
                    "db_size_bytes": db_stat.st_size,
                    "db_mtime_epoch": db_stat.st_mtime,
                }
            )


def run_all_sql_audits(db_path: Path, sql_dir: Path, out_dir: Path, summary_name: str) -> int:
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite DB not found: {db_path}")
    if not sql_dir.exists():
        raise FileNotFoundError(f"SQL directory not found: {sql_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    sql_files = list(iter_sql_files(sql_dir))
    if not sql_files:
        print(f"No .sql files found in {sql_dir}")
        write_summary_csv(out_dir / summary_name, db_path, [])
        return 0

    failures = 0
    results: list[AuditResult] = []
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for sql_path in sql_files:
            try:
                file_results, file_failures = run_sql_to_csv(conn, sql_path, out_dir)
                results.extend(file_results)
                failures += file_failures
                for result in file_results:
                    label = f" statement {result.statement_index}/{result.statement_count}" if result.statement_count > 1 else ""
                    print(f"{result.status} {sql_path.name}{label} -> {result.output_csv}")
            except Exception as exc:
                failures += 1
                error_csv = out_dir / f"{safe_report_name(sql_path)}__ERROR.csv"
                write_error_csv(error_csv, sql_path.name, exc)
                results.append(AuditResult(sql_path.name, 0, 0, "ERROR", 1, 4, str(error_csv), type(exc).__name__, str(exc)))
                print(f"ERROR {sql_path.name} -> {error_csv}: {exc}")

    summary_path = out_dir / summary_name
    write_summary_csv(summary_path, db_path, results)
    print(f"SUMMARY -> {summary_path}")
    return failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run each read-only SQL statement in a folder against a SQLite DB and export CSV reports."
    )
    parser.add_argument("--db", default="auto", help="SQLite database path, or 'auto' to discover a local runtime DB. Default: auto")
    parser.add_argument("--sql-dir", default=Path("sql"), type=Path, help="Directory containing .sql audit files")
    parser.add_argument("--out-dir", default=Path("reports/sql_csv"), type=Path, help="Directory where CSV reports will be written")
    parser.add_argument("--summary-name", default="all_reports_summary.csv", help="Aggregate summary CSV filename written inside --out-dir")
    parser.add_argument("--search-root", default=Path("."), type=Path, help="Root directory used for --db auto discovery. Default: current directory")
    parser.add_argument("--allow-failures", action="store_true", help="Exit 0 even if one or more SQL statements fail")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = resolve_db_path(args.db, args.search_root)
    failures = run_all_sql_audits(db_path, args.sql_dir, args.out_dir, args.summary_name)
    if failures and not args.allow_failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
