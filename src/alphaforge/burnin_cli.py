from __future__ import annotations
import argparse, json, os, sqlite3, sys, subprocess, time, traceback
from pathlib import Path
from sqlalchemy import create_engine
from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign, pause_campaign, get_campaign, qualify_campaign, export_campaign_bundle, bootstrap_campaign_schema, aggregate_campaign, BurnInCampaignRunner, BinanceReadOnlyCandleProvider, DEFAULT_PHASE8_PAPER_SLIPPAGE_BPS, terminalize_active_campaign_run, event
from alphaforge.config import load_config_from_env
from alphaforge.persistence import init_db

def _db_path(args):
    if getattr(args,'db',None): return str(Path(args.db).expanduser().resolve())
    url=load_config_from_env().persistence.database_url
    return url.replace('sqlite:///','').replace('sqlite+pysqlite:///','') if 'sqlite' in url else 'alphaforge.db'

def _print(payload, json_out):
    print(json.dumps(payload,indent=None if json_out else 2,sort_keys=True,default=str) if json_out else _human(payload))

def _human(p):
    if not isinstance(p,dict): return str(p)
    return '\n'.join(f"{k}: {json.dumps(v,default=str) if isinstance(v,(dict,list)) else v}" for k,v in p.items())


def _mark_worker_failed(db: str, campaign_id: str, message: str) -> None:
    conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    try:
        bootstrap_campaign_schema(conn)
        conn.execute("UPDATE burnin_campaigns SET campaign_status='FAILED', last_error=? WHERE campaign_id=?", (message, campaign_id)); conn.commit()
    finally: conn.close()

def _launch_detached_worker(db: str, campaign_id: str) -> dict[str, object]:
    cmd=[sys.executable, '-m', 'alphaforge.burnin_cli', '--db', db, 'worker', '--campaign-id', campaign_id]
    root=Path("artifacts") / "burnin" / campaign_id; root.mkdir(parents=True, exist_ok=True)
    stdout=(root / "worker.stdout.log").open("ab", buffering=0); stderr=(root / "worker.stderr.log").open("ab", buffering=0)
    try: proc=subprocess.Popen(cmd, stdout=stdout, stderr=stderr, env={**os.environ, "ALPHAFORGE_BURNIN_DATABASE_PATH": str(Path(db).expanduser().resolve())})
    finally: stdout.close(); stderr.close()
    time.sleep(0.2)
    if proc.poll() is not None:
        _mark_worker_failed(db, campaign_id, f'WORKER_STARTUP_EXITED:{proc.returncode}')
        raise RuntimeError(f'WORKER_STARTUP_EXITED:{proc.returncode}')
    conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    try:
        bootstrap_campaign_schema(conn)
        now=__import__('alphaforge.burnin').burnin.utc_now()
        conn.execute("UPDATE burnin_campaigns SET worker_pid=?, worker_started_at=?, campaign_status='RUNNING' WHERE campaign_id=?", (proc.pid, now, campaign_id)); conn.commit()
    finally: conn.close()
    return {'status':'DETACHED','campaign_id':campaign_id,'worker_pid':proc.pid,'db':db}

def main(argv=None) -> int:
    ap=argparse.ArgumentParser(prog='python -m alphaforge.burnin_cli')
    ap.add_argument('--db'); ap.add_argument('--json',action='store_true')
    sub=ap.add_subparsers(dest='cmd',required=True)
    c=sub.add_parser('create'); c.add_argument('--release-id',required=True); c.add_argument('--duration-days',type=float,required=True); c.add_argument('--symbols',required=True); c.add_argument('--intervals',required=True)
    for name in ('start','resume','status','pause','qualify'):
        s=sub.add_parser(name); s.add_argument('--campaign-id',required=True); s.add_argument('--foreground', action='store_true'); s.add_argument('--detach', action='store_true')
    e=sub.add_parser('export'); e.add_argument('--campaign-id',required=True); e.add_argument('--output-dir',required=True)
    w=sub.add_parser('worker'); w.add_argument('--campaign-id',required=True); w.add_argument('--once', action='store_true')
    args=ap.parse_args(argv)
    try:
        db=_db_path(args)
        if args.cmd == 'create' and not Path(db).expanduser().exists():
            # Campaign creation is the only CLI path that can prove this is a
            # fresh canonical database. All existing paths remain fail-closed.
            init_db(f"sqlite+pysqlite:///{Path(db).expanduser().resolve()}").dispose()
        if args.cmd=='export':
            out=export_campaign_bundle(db,args.output_dir,args.campaign_id); _print(out,args.json); return 0
        if args.cmd=='worker':
            engine=init_db(f"sqlite+pysqlite:///{db}")
            try:
                runner=BurnInCampaignRunner(engine,args.campaign_id,BinanceReadOnlyCandleProvider())
                if args.once:
                    with engine.begin() as conn: bootstrap_campaign_schema(conn)
                    res=runner.resolver_tick(); _print(res,args.json); return 0 if res.get('status') in {'OK','PAUSED'} else 1
                import asyncio; res=asyncio.run(runner.run_foreground())
                conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
                try:
                    campaign=get_campaign(conn, args.campaign_id)
                    if campaign and campaign.get("campaign_status") == "PAUSED" and campaign.get("worker_pid") == os.getpid():
                        conn.execute("UPDATE burnin_campaigns SET worker_pid=NULL, worker_started_at=NULL WHERE campaign_id=?", (args.campaign_id,))
                        event(conn, args.campaign_id, "WORKER_PAUSED_EXITED", burnin_run_id=campaign.get("active_run_id"), details={"worker_pid": os.getpid()})
                        conn.commit()
                finally: conn.close()
                _print(res,args.json); return 0
            except BaseException as exc:
                conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
                try:
                    bootstrap_campaign_schema(conn)
                    campaign=get_campaign(conn, args.campaign_id) or {}
                    root=Path("artifacts") / "burnin" / args.campaign_id
                    detail={"exception_type": exc.__class__.__name__, "message": str(exc), "traceback": traceback.format_exc(), "campaign_id": args.campaign_id, "burnin_run_id": campaign.get("active_run_id"), "worker_pid": campaign.get("worker_pid"), "stdout_log_path": str(root / "worker.stdout.log"), "stderr_log_path": str(root / "worker.stderr.log")}
                    terminalize_active_campaign_run(conn, args.campaign_id, run_status="FAILED", campaign_status="FAILED", reason="WORKER_UNCAUGHT_EXCEPTION", event_type="WORKER_UNCAUGHT_EXCEPTION", details=detail)
                    conn.commit()
                finally: conn.close()
                raise
            finally: engine.dispose()
        conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
        try:
            bootstrap_campaign_schema(conn)
            if args.cmd=='create':
                camp=create_campaign(conn,release_id=args.release_id,duration_days=args.duration_days,symbols=[x for x in args.symbols.split(',') if x],intervals=[x for x in args.intervals.split(',') if x],runtime_config=load_config_from_env().runtime,paper_slippage_bps=DEFAULT_PHASE8_PAPER_SLIPPAGE_BPS); conn.commit(); _print({'status':'CREATED','campaign_id':camp.campaign_id,'release_id':camp.release_id},args.json); return 0
            if args.cmd in {'start','resume'}:
                if not (args.foreground or args.detach):
                    _print({'status':'FAILED_CLOSED','error':'WORKER_MODE_REQUIRED'},args.json); return 3
                res=start_or_resume_campaign(conn,args.campaign_id,resume=(args.cmd=='resume')); conn.commit()
                if args.detach:
                    out=_launch_detached_worker(db,args.campaign_id); _print({**res, **out},args.json); return 0
                engine=init_db(f"sqlite+pysqlite:///{db}")
                try:
                    runner=BurnInCampaignRunner(engine,args.campaign_id,BinanceReadOnlyCandleProvider())
                    import asyncio; out=asyncio.run(runner.run_foreground()); _print({**res, **out},args.json); return 0
                finally: engine.dispose()
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
