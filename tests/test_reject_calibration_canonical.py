import json, sqlite3
from alphaforge.burnin import BurnInRun, bootstrap_burnin_schema, canonical_decision_sql, persist_burnin_observation, persist_burnin_run
from alphaforge.burnin_campaign import bootstrap_campaign_schema, build_phase8_campaign_identity, execution_threshold_calibration
from alphaforge.burnin_resolver import persist_pending_reject_label
from alphaforge.runtime import RuntimeConfig
from alphaforge.multi_timeframe import build_execution_context, build_regime_context, build_setup_context


def test_5732_physical_rows_are_5263_canonical_and_diagnostics_remain():
    c=sqlite3.connect(':memory:'); bootstrap_burnin_schema(c)
    persist_burnin_run(c, BurnInRun('r','rel',git_commit='g',config_hash='c',strategy_config_hash='s',universe_hash='u',source_provenance={'provider':'PAPER'}))
    for i in range(5263):
        persist_burnin_observation(c,observation_id=f'c{i}',burnin_run_id='r',release_id='rel',execution_mode='PAPER',decision='REJECTED',metrics={'reject_decision_id':f'r{i}'})
    for i in range(469):
        persist_burnin_observation(c,observation_id=f'd{i}',burnin_run_id='r',release_id='rel',execution_mode='PAPER',decision='REJECTED',metrics={'reject_decision_id':f'r{i}'},observation_kind='DIAGNOSTIC')
    assert c.execute('select count(*) from burnin_observations').fetchone()[0] == 5732
    assert c.execute(f"select count(*) from burnin_observations o where {canonical_decision_sql('o')}").fetchone()[0] == 5263
    assert c.execute("select count(*) from burnin_observations where json_extract(metrics_json,'$.observation_kind')='DIAGNOSTIC'").fetchone()[0] == 469


def test_layer_thresholds_are_independent_and_identity_sensitive():
    candles=[]
    for i,p in enumerate([100,100,100,100,100.04,100.05,100.06,100.07,100.08,100.09,100.10,100.11,100.12,100.13,100.14,100.15,100.16,100.17,100.18,100.19]):
        candles.append({'close':p,'high':p,'low':p,'close_ts':i+1})
    assert build_regime_context(candles,'1h',direction_threshold=0)['direction'] != build_regime_context(candles,'1h',direction_threshold=.1)['direction']
    assert build_setup_context(candles,'15m',direction_threshold=0)['direction'] != build_setup_context(candles,'15m',direction_threshold=.1)['direction']
    market={'spread_pct':0,'expected_slippage_pct':0,'latency_ms':0,'liquidity_score':1}
    assert build_execution_context(candles,'1m',market,direction_threshold=0)['trigger']
    base=dict(regime_direction_threshold=.0005,setup_direction_threshold=.0005)
    a=build_phase8_campaign_identity(RuntimeConfig(**base,execution_direction_threshold=.0005),symbols=['BTC'],intervals=['1m'],paper_source_exchanges=['binance'])
    b=build_phase8_campaign_identity(RuntimeConfig(**base,execution_direction_threshold=.0004),symbols=['BTC'],intervals=['1m'],paper_source_exchanges=['binance'])
    assert a['strategy_config_hash'] != b['strategy_config_hash']


def test_pending_label_exactly_once_and_calibration_excludes_incomplete():
    c=sqlite3.connect(':memory:'); c.row_factory=sqlite3.Row; bootstrap_campaign_schema(c)
    persist_burnin_run(c,BurnInRun('r','rel',git_commit='g',config_hash='c',strategy_config_hash='s',universe_hash='u',source_provenance={'provider':'PAPER'}))
    c.execute("insert into burnin_campaigns(campaign_id,release_id,campaign_status,created_at,config_hash,strategy_config_hash,universe_hash,git_commit,source_provenance_json,symbols_json,intervals_json,schema_version) values('camp','rel','RUNNING','x','c','s','u','g','{}','[]','[]','v')")
    kw=dict(campaign_id='camp',burnin_run_id='r',reject_decision_id='rid',signal_id='s',symbol='BTC',side='LONG',decision_timestamp='2026-01-01T00:00:00Z',entry=100,stop=90,target=120,horizon_seconds=60,execution_cost_assumptions={},regime='TRENDING',reject_reason='MTF_EXECUTION_NOT_CONFIRMED',source_provenance={'mtf':{'execution':{'ma_delta_strength':.00015}}})
    assert persist_pending_reject_label(c,**kw)==persist_pending_reject_label(c,**kw)
    assert c.execute('select count(*) from burnin_pending_reject_labels').fetchone()[0]==1
    c.execute("insert into burnin_reject_outcomes(reject_outcome_id,burnin_run_id,release_id,reject_reason,symbol,regime,decision_time,forward_label,would_tp,would_sl,ambiguous,hypothetical_net_r_after_costs,avoided_loss,missed_profit,evidence_complete,payload_json,schema_version) values('rout_rid','r','rel','MTF_EXECUTION_NOT_CONFIRMED','BTC','TRENDING','x','TP_BEFORE_SL',1,0,0,.4,0,.4,1,?, 'v')",(json.dumps({'window_complete':True,'reject_correct':False}),))
    out=execution_threshold_calibration(c,'camp'); bucket=next(x for x in out if x['bucket']=='0.0001-0.0002')
    assert bucket['count']==1 and bucket['TP_BEFORE_SL']==1 and bucket['reject_correct_pct']==0
    c.execute("update burnin_reject_outcomes set payload_json='{}'")
    assert sum(x['count'] for x in execution_threshold_calibration(c,'camp'))==0
