"""Opt-in durable signed-transport budget for one sealed PAPER attempt."""
from contextlib import contextmanager, closing
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from threading import Event
from urllib.parse import parse_qs, urlsplit

PERMISSION = ("api.binance.com", "/sapi/v1/account/apiRestrictions")
EXPOSURE = {("fapi.binance.com", "/fapi/v3/positionRisk"),
            ("fapi.binance.com", "/fapi/v1/openOrders")}
TRADES = ("fapi.binance.com", "/fapi/v1/userTrades")
ALLOWED = {"permission": {PERMISSION}, "admission": EXPOSURE,
           **{phase: EXPOSURE | {TRADES} for phase in (
               "worker_preflight", "worker_recovery", "startup_recovery", "startup", "periodic")},
           "shutdown": set()}
ENV_KEYS = ("AF_SIGNED_GRANT_PATH", "AF_SIGNED_GRANT_SHA256", "AF_SIGNED_LEDGER_PATH", "AF_SIGNED_ATTEMPT")
_ACTIVE = ContextVar("af_signed_budget_phase", default=None)
_SCHEDULED = ContextVar("af_scheduled_signed_phase", default=None)
_STOPPED = Event()


class SignedReadDenied(RuntimeError):
    pass


def deny():
    raise SignedReadDenied("SIGNED_READ_BUDGET_DENIED")


def positive(value):
    return type(value) is int and value > 0


class Budget:
    def __init__(self, path, digest, ledger, attempt):
        try:
            raw = Path(path).read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                deny()
            grant = json.loads(raw)
            phases = grant["phases"]
            if (not isinstance(attempt, str) or not attempt or grant["attempt"] != attempt
                    or grant["mode"] != "PAPER" or grant["live"] is not False
                    or not positive(grant["lifetime_ns"]) or not positive(grant["max_requests"])
                    or set(phases) != set(ALLOWED)):
                deny()
            for name, rule in phases.items():
                routes = {tuple(route) for route in rule["routes"]}
                if (not routes <= ALLOWED[name]
                        or any(type(rule[k]) is not int or rule[k] < 0 for k in (
                            "starts", "requests", "requests_per_start", "attempts_per_operation",
                            "min_interval_ns", "window_ns"
                        ))):
                    deny()
                if name == "shutdown" or rule["starts"] == 0:
                    if routes or rule["starts"] or rule["requests"]:
                        deny()
                    continue
                if not routes or not all(positive(rule[k]) for k in (
                    "starts", "requests", "requests_per_start", "attempts_per_operation", "window_ns"
                )):
                    deny()
                if name != "periodic" and rule["starts"] != 1:
                    deny()
                if name == "permission" and any(rule[k] != 1 for k in (
                    "requests", "requests_per_start", "attempts_per_operation"
                )):
                    deny()
                if name == "periodic" and not positive(rule["min_interval_ns"]):
                    deny()
                if rule["attempts_per_operation"] > 3:
                    deny()
        except (OSError, ValueError, TypeError, KeyError):
            deny()
        self.phases = phases
        self.grant = grant
        self.digest = digest
        self.ledger = Path(ledger).absolute()
        self.attempt = attempt

    def initialize(self):
        # Exclusive creation prevents reset/restart. Children open an existing ledger only.
        fd = os.open(self.ledger, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(self.ledger)) as conn, conn:
            conn.executescript("""
            CREATE TABLE authority(digest TEXT, attempt TEXT, epoch INTEGER, closed INTEGER, ledger_path TEXT);
            CREATE TABLE phases(phase TEXT PRIMARY KEY, first_ns INTEGER, last_ns INTEGER, starts INTEGER, used INTEGER);
            CREATE TABLE debits(phase TEXT, start INTEGER, host TEXT, path TEXT, symbol TEXT);
            CREATE INDEX debit_scope ON debits(phase,start,host,path,symbol);
            """)
            conn.execute("INSERT INTO authority VALUES (?, ?, ?, 0, ?)",
                         (self.digest, self.attempt, time.monotonic_ns(), str(self.ledger)))

    @contextmanager
    def transaction(self, closing_budget=False):
        try:
            with closing(sqlite3.connect(self.ledger.as_uri() + "?mode=rw", uri=True, timeout=0.2)) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute("SELECT digest, attempt, epoch, closed, ledger_path FROM authority").fetchall()
                if len(rows) != 1 or rows[0][:2] != (self.digest, self.attempt):
                    deny()
                _, _, epoch, closed, path = rows[0]
                if path != str(self.ledger) or closed not in (0, 1):
                    deny()
                now = time.monotonic_ns()
                if not closing_budget and (_STOPPED.is_set() or closed or not epoch <= now < epoch + self.grant["lifetime_ns"]):
                    deny()
                yield conn, now
        except (sqlite3.Error, OSError, OverflowError):
            deny()

    def start(self, phase):
        if phase not in self.phases or phase == "shutdown":
            deny()
        rule = self.phases[phase]
        with self.transaction() as (conn, now):
            row = conn.execute("SELECT first_ns,last_ns,starts,used FROM phases WHERE phase=?", (phase,)).fetchone()
            first, last, count, used = row or (now, None, 0, 0)
            if (count >= rule["starts"] or now >= first + rule["window_ns"]
                    or (last is not None and now - last < rule["min_interval_ns"])):
                deny()
            conn.execute("INSERT OR REPLACE INTO phases VALUES (?,?,?,?,?)", (phase, first, now, count + 1, used))
        return count + 1

    def debit(self, phase, start, method, url):
        parsed = urlsplit(url)
        route = (parsed.hostname, parsed.path)
        query = parse_qs(parsed.query)
        symbol = query.get("symbol", [""])
        if (method != "GET" or parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.fragment
                or list(route) not in self.phases[phase]["routes"]
                or len(query.get("signature", [])) != 1 or len(symbol) != 1
                or bool(symbol[0]) != (route == TRADES)):
            deny()
        rule = self.phases[phase]
        with self.transaction() as (conn, now):
            row = conn.execute("SELECT first_ns,starts,used FROM phases WHERE phase=?", (phase,)).fetchone()
            if row is None or row[1] != start or now >= row[0] + rule["window_ns"]:
                deny()
            total = conn.execute("SELECT coalesce(max(rowid),0) FROM debits").fetchone()[0]
            phase_total = row[2]
            per_start = conn.execute("SELECT count(*) FROM debits WHERE phase=? AND start=?", (phase, start)).fetchone()[0]
            attempts = conn.execute(
                "SELECT count(*) FROM debits WHERE phase=? AND start=? AND host=? AND path=? AND symbol=?",
                (phase, start, *route, symbol[0]),
            ).fetchone()[0]
            if (total >= self.grant["max_requests"] or phase_total >= rule["requests"]
                    or per_start >= rule["requests_per_start"] or attempts >= rule["attempts_per_operation"]):
                deny()
            conn.execute("INSERT INTO debits VALUES (?,?,?,?,?)", (phase, start, *route, symbol[0]))
            conn.execute("UPDATE phases SET used=used+1 WHERE phase=?", (phase,))

    def transfer_to_mount(self, mount):
        """Retire the single writable admission copy before enabling the worker copy."""
        mount = Path(mount)
        if mount.resolve(strict=True) != mount or not mount.is_dir():
            deny()
        destination = mount / "signed-read-ledger.sqlite"
        if destination == self.ledger:
            deny()
        with self.transaction() as (source, _):
            # Snapshot and retire under the same write lock. A failed or
            # interrupted transfer consumes the handoff without issuing a new budget.
            snapshot = "\n".join(source.iterdump())
            source.execute("UPDATE authority SET closed=2")

        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(destination)) as target, target:
            # The initial copy still names the old ledger path, so it cannot
            # be spent from the mount until it is explicitly enabled below.
            target.executescript(snapshot)
            target.execute("UPDATE authority SET closed=3, ledger_path=?", (str(destination),))
        with closing(sqlite3.connect(destination.as_uri() + "?mode=rw", uri=True)) as target, target:
            target.execute("BEGIN IMMEDIATE")
            row = target.execute("SELECT digest, attempt, closed, ledger_path FROM authority").fetchall()
            if row != [(self.digest, self.attempt, 3, str(destination))]:
                deny()
            target.execute("UPDATE authority SET closed=0")
        return destination


    def close(self):
        _STOPPED.set()
        with self.transaction(closing_budget=True) as (conn, _):
            conn.execute("UPDATE authority SET closed=1")


def configured(required=False):
    values = tuple(os.getenv(key) for key in ENV_KEYS)
    if not any(values) and not required:
        return None
    if not all(values):
        deny()
    return Budget(*values)


@contextmanager
def signed_phase(name):
    budget = configured()
    if budget is None:
        yield
        return
    token = _ACTIVE.set((budget, name, budget.start(name)))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


@dataclass
class ScheduledPhase:
    name: str
    reserved_at: float | None = None


@contextmanager
def scheduled_signed_phase(name):
    """Label a runtime cycle without reserving quota outside its error boundary."""
    phase = ScheduledPhase(name)
    token = _SCHEDULED.set(phase)
    try:
        yield phase
    finally:
        _SCHEDULED.reset(token)


@contextmanager
def open_scheduled_signed_phase():
    phase = _SCHEDULED.get()
    if phase is None:
        yield
        return
    with signed_phase(phase.name):
        if _ACTIVE.get() is not None:
            phase.reserved_at = time.monotonic()
        yield


def debit_signed(method, url):
    owner = _ACTIVE.get()
    if owner is None:
        if configured() is not None:
            deny()
        return
    budget, phase, start = owner
    budget.debit(phase, start, method, url)


def scoped_probe(phase, probe):
    def run():
        with signed_phase(phase):
            return probe()
    return run


def close_signed_budget():
    budget = configured()
    if budget is not None:
        budget.close()
