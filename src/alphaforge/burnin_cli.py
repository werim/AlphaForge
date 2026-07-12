from __future__ import annotations
import argparse, asyncio, json, os, sqlite3, sys, time
from sqlalchemy import create_engine
from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign, pause_campaign, get_campaign, qualify_campaign, export_campaign_bundle, bootstrap_campaign_schema, aggregate_campaign, BurnInCampaignRunner, launch_campaign_worker, mark_worker_started, mark_worker_failed, resolve_campaign_batch, detect_stale_worker, check_campaign_completion
from alphaforge.config import load_config_from_env

def _db_path(args):
    if getattr(args,'db',None): return args.db
    url=load_config_from_env().persistence.database_url
    return url.replace('sqlite:///','').replace('sqlite+pysqlite:///','') if 'sqlite' in url else 'alphaforge.db'

def _print(payload, json_out): print(json.dumps(payload,indent=None if json_out else 2,sort_keys=True,default=str) if json_out else _human(payload))
def _human(p): return '\n'.join(f"{k}: {json.dumps(v,default=str) if isinstance(v,(dict,list)) else v}" for k,v in p.items()) if isinstance(p,dict) else str(p)

def main(argv=None) -> int:
    ap=argparse.ArgumentParser(prog='python -m alphaforge.burnin_cli'); ap.add_argument('--db'); ap.add_argument('--json',action='store_true')
    sub=ap.add_subparsers(dest='cmd',required=True)
    c=sub.add_parser('create'); c.add_argument('--release-id',required=True); c.add_argument('--duration-days',type=float,required=True); c.add_argument('--symbols',required=True); c.add_argument('--intervals',required=True)
    st=sub.add_parser('start'); st.add_argument('--campaign-id',required=True); g=st.add_mutually_exclusive_group(required=True); g.add_argument('--foreground',action='store_true'); g.add_argument('--detach',action='store_true')
    rs=sub.add_parser('resume'); rs.add_argument('--campaign-id',required=True); g2=rs.add_mutually_exclusive_group(required=True); g2.add_argument('--foreground',action='store_true'); g2.add_argument('--detach',action='store_true')
    for name in ('status','pause','qualify','complete','recover-check'):
        s=sub.add_parser(name); s.add_argument('--campaign-id',required=True)
    r=sub.add_parser('resolve'); r.add_argument('--campaign-id',required=True)
    w=sub.add_parser('worker'); w.add_argument('--campaign-id',required=True)
    e=sub.add_parser('export'); e.add_argument('--campaign-id',required=True); e.add_argument('--output-dir',required=True)
    args=ap.parse_args(argv); db=_db_path(args)
    try:
        if args.cmd=='export': out=export_campaign_bundle(db,args.output_dir,args.campaign_id); _print(out,args.json); return 0
        engine=create_engine(f"sqlite+pysqlite:///{db}",future=True)
        if args.cmd=='worker':
            try: asyncio.run(BurnInCampaignRunner(engine,args.campaign_id).run_foreground()); return 0
            finally: engine.dispose()
        with sqlite3.connect(db) as conn:
            conn.row_factory=sqlite3.Row; bootstrap_campaign_schema(conn)
            if args.cmd=='create':
                camp=create_campaign(conn,release_id=args.release_id,duration_days=args.duration_days,symbols=[x for x in args.symbols.split(',') if x],intervals=[x for x in args.intervals.split(',') if x]); conn.commit(); _print({'status':'CREATED','campaign_id':camp.campaign_id,'release_id':camp.release_id},args.json); return 0
            if args.cmd in {'start','resume'}:
                if args.detach:
                    # Allocate/recover lineage first, launch worker, then mark RUNNING only if subprocess is alive.
                    res=start_or_resume_campaign(conn,args.campaign_id,resume=args.cmd=='resume'); conn.commit(); proc=launch_campaign_worker(db,args.campaign_id); time.sleep(0.2)
                    if proc.poll() is not None:
                        mark_worker_failed(conn,args.campaign_id,f'WORKER_EXITED:{proc.returncode}'); conn.commit(); _print({'status':'FAILED','campaign_id':args.campaign_id,'returncode':proc.returncode},args.json); return 1
                    mark_worker_started(conn,args.campaign_id,pid=proc.pid,runtime_status='DETACHED'); conn.commit(); _print({'status':'RUNNING','campaign_id':args.campaign_id,'pid':proc.pid,**res},args.json); return 0
                conn.commit(); asyncio.run(BurnInCampaignRunner(engine,args.campaign_id).run_foreground()); return 0
            if args.cmd=='pause': pause_campaign(conn,args.campaign_id); conn.commit(); _print({'status':'PAUSED','campaign_id':args.campaign_id},args.json); return 0
            if args.cmd=='status':
                camp=get_campaign(conn,args.campaign_id)
                if not camp: _print({'status':'NOT_FOUND','campaign_id':args.campaign_id},args.json); return 2
                stale=detect_stale_worker(conn,args.campaign_id); conn.commit(); _print({'campaign':get_campaign(conn,args.campaign_id),'aggregate':aggregate_campaign(conn,args.campaign_id),'worker':stale},args.json); return 0
            if args.cmd=='recover-check': res=detect_stale_worker(conn,args.campaign_id); conn.commit(); _print(res,args.json); return 0
            if args.cmd=='resolve': res=resolve_campaign_batch(conn,args.campaign_id); conn.commit(); _print(res,args.json); return 0
            if args.cmd=='complete': res=check_campaign_completion(conn,args.campaign_id); conn.commit(); _print(res,args.json); return 0
            if args.cmd=='qualify':
                conn.commit(); res=qualify_campaign(engine,args.campaign_id); _print(res,args.json); return 0
    except KeyError as exc: _print({'status':'NOT_FOUND','error':str(exc)},getattr(args,'json',False)); return 2
    except ValueError as exc: _print({'status':'FAILED_CLOSED','error':str(exc)},getattr(args,'json',False)); return 3
    except KeyboardInterrupt: return 130
    except Exception as exc: _print({'status':'ERROR','error':f'{exc.__class__.__name__}:{exc}'},getattr(args,'json',False)); return 1
    return 1
if __name__=='__main__': sys.exit(main())
