from __future__ import annotations
import os
from pathlib import Path
from urllib.parse import unquote,urlparse
ENV_TARGETS=("ALPHAFORGE_DB_PATH","ALPHAFORGE_BURNIN_DATABASE_PATH","ALPHAFORGE_DATABASE_URL","ALPHAFORGE_SQLITE_PATH")
def _path(value):
    if "://" in value:
        p=urlparse(value.replace("sqlite+pysqlite://","sqlite://",1))
        if p.scheme!="sqlite": return None
        value=unquote(p.path)
    return str(Path(value).expanduser().resolve(strict=False))
def resolve_database_targets(explicit=None,environ=None):
    env=os.environ if environ is None else environ; candidates=[]
    if explicit is not None: candidates.append({"source":"CLI --db","value":str(explicit),"canonical_path":_path(str(explicit))})
    for n in ENV_TARGETS:
        if env.get(n): candidates.append({"source":n,"value":env[n],"canonical_path":_path(env[n])})
    paths=sorted({x["canonical_path"] for x in candidates if x["canonical_path"]})
    return {"candidates":candidates,"canonical_paths":paths,"conflict":len(paths)>1,"dialects":["sqlite"]}
