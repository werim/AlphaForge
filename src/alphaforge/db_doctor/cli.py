from __future__ import annotations
import argparse
from pathlib import Path
from .diagnostics import diagnose
from .planner import plan
from .repairs import repair
from .report import render
from .verifier import certify

def main(argv=None):
    parser=argparse.ArgumentParser(prog="python -m alphaforge.db_doctor")
    parser.add_argument("--db",required=True); parser.add_argument("--json",action="store_true")
    parser.add_argument("command",choices=("diagnose","plan","repair","certify"))
    args=parser.parse_args(argv); path=Path(args.db)
    payload={"diagnose":diagnose,"plan":plan,"repair":repair,"certify":certify}[args.command](path)
    print(render(payload,args.json)); return 0 if payload.get("status") in {"HEALTHY","NO_ACTION","READY","REPAIRED","DATABASE_CERTIFIED"} else 2

