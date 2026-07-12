from __future__ import annotations
import argparse, json, os, sqlite3, sys
from pathlib import Path
from sqlalchemy import create_engine
from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign, pause_campaign, get_campaign, qualify_campaign, export_campaign_bundle, bootstrap_campaign_schema, aggregate_campaign
from alphaforge.config import load_config_from_env

def _db_path(args):
    if getattr(args,'db',None): return args.db
    url=load_config_from_env().persistence.database_url
    return url.replace('sqlite:///','').replace('sqlite+pysqlite:///','') if 'sqlite' in url else 'alphaforge.db'

def _print(payload, json_out):
    print(json.dumps(payload,indent=None if json_out else 2,sort_keys=True,default=str) if json_out else _human(payload))

def _human(p):
    if not isinstance(p,dict): return str(p)
    return '\n'.join(f"{k}: {json.dumps(v,default=str) if isinstance(v,(dict,list)) else v}" for k,v in p.items())

def main(argv=None) -> int:
    ap=argparse.ArgumentParser(prog='python -m alphaforge.burnin_cli')
    ap.add_argument('--db'); ap.add_argument('--json',action='store_true')
    sub=ap.add_subparsers(dest='cmd',required=True)
    c=sub.add_parser('create'); c.add_argument('--release-id',required=True); c.add_argument('--duration-days',type=float,required=True); c.add_argument('--symbols',required=True); c.add_argument('--intervals',required=True)
    for name in ('start','resume','status','pause','qualify'):
        s=sub.add_parser(name); s.add_argument('--campaign-id',required=True)
    e=sub.add_parser('export'); e.add_argument('--campaign-id',required=True); e.add_argument('--output-dir',required=True)
    args=ap.parse_args(argv)
    try:
        db=_db_path(args)
        if args.cmd=='export':
            out=export_campaign_bundle(db,args.output_dir,args.campaign_id); _print(out,args.json); return 0
        conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
        try:
            bootstrap_campaign_schema(conn)
            if args.cmd=='create':
                camp=create_campaign(conn,release_id=args.release_id,duration_days=args.duration_days,symbols=[x for x in args.symbols.split(',') if x],intervals=[x for x in args.intervals.split(',') if x]); conn.commit(); _print({'status':'CREATED','campaign_id':camp.campaign_id,'release_id':camp.release_id},args.json); return 0
            if args.cmd=='start': res=start_or_resume_campaign(conn,args.campaign_id); conn.commit(); _print(res,args.json); return 0
            if args.cmd=='resume': res=start_or_resume_campaign(conn,args.campaign_id,resume=True); conn.commit(); _print(res,args.json); return 0
            if args.cmd=='pause': pause_campaign(conn,args.campaign_id); conn.commit(); _print({'status':'PAUSED','campaign_id':args.campaign_id},args.json); return 0
            if args.cmd=='status':
                camp=get_campaign(conn,args.campaign_id)
                if not camp: _print({'status':'NOT_FOUND','campaign_id':args.campaign_id},args.json); return 2
                agg=aggregate_campaign(conn,args.campaign_id); _print({'campaign':camp,'aggregate':agg},args.json); return 0
            if args.cmd=='qualify':
                conn.commit(); engine=create_engine(f"sqlite+pysqlite:///{db}",future=True); res=qualify_campaign(engine,args.campaign_id); engine.dispose(); _print(res,args.json); return 0
        finally: conn.close()
    except KeyError as exc:
        _print({'status':'NOT_FOUND','error':str(exc)},getattr(args,'json',False)); return 2
    except ValueError as exc:
        _print({'status':'FAILED_CLOSED','error':str(exc)},getattr(args,'json',False)); return 3
    except Exception as exc:
        _print({'status':'ERROR','error':f'{exc.__class__.__name__}:{exc}'},getattr(args,'json',False)); return 1

if __name__=='__main__': sys.exit(main())
