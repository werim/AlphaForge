import json, sqlite3
from sqlalchemy import create_engine
from alphaforge.burnin_campaign import create_campaign, start_or_resume_campaign, pause_campaign, get_campaign, aggregate_campaign, export_campaign_bundle
from alphaforge.burnin import persist_burnin_observation


def test_campaign_create_start_resume_pause_and_export(tmp_path):
    db=tmp_path/'a.db'; conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='rel1',duration_days=7,symbols=['BTCUSDT'],intervals=['1h'])
    conn.commit()
    first=start_or_resume_campaign(conn,camp.campaign_id); conn.commit()
    persist_burnin_observation(conn,observation_id='o1',burnin_run_id=first['burnin_run_id'],release_id='rel1',execution_mode='PAPER',decision='REJECTED',source_provenance={'provider':'PAPER'})
    second=start_or_resume_campaign(conn,camp.campaign_id,resume=True); conn.commit()
    assert first['burnin_run_id'] != second['burnin_run_id']
    assert get_campaign(conn,camp.campaign_id)['restart_count'] == 1
    pause_campaign(conn,camp.campaign_id); conn.commit()
    assert get_campaign(conn,camp.campaign_id)['campaign_status'] == 'PAUSED'
    agg=aggregate_campaign(conn,camp.campaign_id)
    assert agg['metrics']['sample_count'] == 1
    conn.close()
    out=export_campaign_bundle(db,tmp_path/'out',camp.campaign_id)
    assert (tmp_path/'out'/f"burnin_campaign_{camp.campaign_id}"/'manifest.json').exists()
    assert out['manifest']['source_run_ids'] == [first['burnin_run_id'], second['burnin_run_id']]


def test_config_drift_pauses_campaign(tmp_path):
    conn=sqlite3.connect(tmp_path/'a.db'); conn.row_factory=sqlite3.Row
    camp=create_campaign(conn,release_id='rel1',duration_days=7,symbols=['BTCUSDT'],intervals=['1h'])
    conn.commit()
    try:
        start_or_resume_campaign(conn,camp.campaign_id,config_hash='different')
    except ValueError as exc:
        assert 'CONFIG_DRIFT' in str(exc)
    else:
        raise AssertionError('expected drift')
    assert get_campaign(conn,camp.campaign_id)['campaign_status'] == 'PAUSED'
