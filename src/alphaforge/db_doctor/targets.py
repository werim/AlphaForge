"""Resolve configured database targets without opening or creating them."""
from __future__ import annotations
import ntpath, os, re
from pathlib import Path
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
ENV_TARGETS=("ALPHAFORGE_DB_PATH","ALPHAFORGE_BURNIN_DATABASE_PATH","ALPHAFORGE_DATABASE_URL","ALPHAFORGE_SQLITE_PATH")
_WIN=re.compile(r"^[A-Za-z]:[\\/]")
def _windows_identity(value:str)->str:
    return ntpath.normcase(ntpath.normpath(value.replace("/","\\")))
def parse_target(raw:str)->dict:
    raw=str(raw); dialect="sqlite"; database=raw; is_url="://" in raw
    if is_url:
        try:
            url=make_url(raw); dialect=url.get_backend_name(); database=url.database
        except ArgumentError:
            return {"raw_value":raw,"dialect":"unknown","canonical_identity":None,"canonical_path":None,"parse_error":"invalid SQLAlchemy URL"}
    canonical=None
    if dialect=="sqlite" and database not in (None,"",":memory:"):
        database=str(database)
        if _WIN.match(database): canonical="windows:"+_windows_identity(database)
        else: canonical="filesystem:"+str(Path(database).expanduser().resolve(strict=False))
    elif dialect=="sqlite" and database==":memory:": canonical="sqlite::memory:"
    elif is_url:
        canonical=f"{dialect}://{url.host or ''}:{url.port or ''}/{url.database or ''}"
    return {"raw_value":raw,"dialect":dialect,"canonical_identity":canonical,"canonical_path":canonical.split(":",1)[1] if canonical and canonical.startswith("filesystem:") else None,"parse_error":None}
def resolve_database_targets(explicit=None,environ=None):
    env=os.environ if environ is None else environ; candidates=[]
    def add(source,value): candidates.append({"source":source,**parse_target(value)})
    if explicit is not None: add("CLI --db",explicit)
    for name in ENV_TARGETS:
        if env.get(name): add(name,env[name])
    identities={c["canonical_identity"] for c in candidates if c["canonical_identity"]}; dialects={c["dialect"] for c in candidates}
    return {"candidates":candidates,"canonical_identities":sorted(identities),"dialects":sorted(dialects),"conflict":len(identities)>1 or len(dialects)>1 or any(c["parse_error"] for c in candidates),"selected":candidates[0] if len(identities)==1 and len(dialects)==1 else None}
