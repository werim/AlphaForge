"""Preserved uncertainty may authorize maintenance, never economic completion."""
import json

import pytest

from alphaforge.burnin_campaign import start_or_resume_campaign, check_campaign_completion
from alphaforge.burnin_resolver import resolve_campaign_batch
from alphaforge.reject_lifecycle import preserved_ambiguity_evidence, authorized_ambiguity_ids
from alphaforge.runtime_state import evaluate_runtime_recovery
from alphaforge.storage_policy import compact_stats_only_universe_batch, stats_only_campaign_summary
from test_issue631_reject_recovery import storage_recovery, authenticated_probe
from test_phase8_reject_resolver import COSTS, persist_pending_reject_label


def ambiguous_recovery(tmp_path, monkeypatch):
    import alphaforge.burnin_ops as ops
    engine, policy, conn, cid, run = storage_recovery(tmp_path)
    conn.execute("INSERT INTO order_decisions(decision_id,symbol,mode,decision) VALUES ('amb','S0000USDT','PAPER','REJECTED')")
    persist_pending_reject_label(conn, campaign_id=cid, burnin_run_id=run,
        reject_decision_id='amb', signal_id='amb', symbol='S0000USDT', side='LONG',
        decision_timestamp='2020-09-13T12:26:40Z', timeframe='1m', horizon_bars=1,
        entry=100, stop=90, target=120, execution_cost_assumptions=COSTS,
        regime='TRENDING', reject_reason='LOW_CONFIDENCE',
        source_provenance={'forward_label_subject':'LEGACY_SCANNER_SHADOW_CANDIDATE',
                           'reject_quality_attributable':False})
    result = resolve_campaign_batch(conn, cid, {'S0000USDT':[
        {'timestamp':'2020-09-13T12:27:00Z','high':121,'low':89,
         'source_provenance':{'provider':'BINANCE_READ_ONLY_KLINES','order_submission':'DISABLED'}}]},
        now='2026-01-01T00:00:00Z')
    assert result['ambiguous'] == 1, result
    conn.commit()
    monkeypatch.setattr(ops, '_authoritative_recovery_exposure', lambda *_,**kw:
        evaluate_runtime_recovery(engine, mode='PAPER', campaign_id=cid, force_reconciliation_probe=kw.get('force_reconciliation_probe',False),
                                  reconciliation_probe=authenticated_probe))
    return engine, policy, conn, cid, run


def test_explicit_preserved_ambiguity_maintenance_keeps_economics_and_blocks_continuation(tmp_path, monkeypatch):
    import alphaforge.burnin_ops as ops
    engine, policy, conn, cid, run = ambiguous_recovery(tmp_path, monkeypatch)
    try:
        before = ops._campaign_source_evidence_hash(conn, cid)
        labels = [tuple(r) for r in conn.execute('SELECT * FROM burnin_pending_reject_labels')]
        assert ops.terminalize_zero_exposure_recovery(conn, cid)['status'] == 'FAIL_CLOSED'
        assert authorized_ambiguity_ids(conn, cid, run) == ()
        result = ops.terminalize_zero_exposure_recovery(conn, cid, preserve_ambiguity=True)
        assert result['status'] == 'PASS', result
        assert ops._campaign_source_evidence_hash(conn, cid) == before
        assert authorized_ambiguity_ids(conn, cid, run) == ('prej_amb',)
        assert ops.terminalize_zero_exposure_recovery(conn, cid, preserve_ambiguity=True)['idempotent_replay']
        assert compact_stats_only_universe_batch(engine, policy)['cycles_compacted'] == 2
        assert [tuple(r) for r in conn.execute('SELECT * FROM burnin_pending_reject_labels')] == labels
        outcome = conn.execute('SELECT evidence_complete,hypothetical_gross_r,hypothetical_net_r_after_costs FROM burnin_reject_outcomes').fetchone()
        assert tuple(outcome) == (0, None, None)
        assert 'PENDING_OUTCOME_BACKLOG' in check_campaign_completion(conn,cid)['blockers']
        for resume in (False, True):
            with pytest.raises(RuntimeError, match='MAINTENANCE_ONLY_NO_CONTINUATION'):
                start_or_resume_campaign(conn, cid, resume=resume)
        assert conn.execute('SELECT COUNT(*) FROM burnin_campaign_runs').fetchone()[0] == 1
        assert stats_only_campaign_summary(conn, cid)['qualification_eligible'] is False
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        conn.close(); engine.dispose()


@pytest.mark.parametrize('fault', ['pending','failed','claimed','complete','cross_run','missing_outcome',
    'costs','gaps','incomplete_window','invented_economics','cross_release','cross_payload','missing_decision','live_decision'])
def test_unverified_ambiguity_cannot_authorize_maintenance(tmp_path, monkeypatch, fault):
    import alphaforge.burnin_ops as ops
    engine, _, conn, cid, run = ambiguous_recovery(tmp_path, monkeypatch)
    try:
        if fault in ('pending','failed','claimed'):
            conn.execute('UPDATE burnin_pending_reject_labels SET status=?',({'pending':'PENDING','failed':'FAILED','claimed':'RESOLVING'}[fault],))
        elif fault == 'complete': conn.execute('UPDATE burnin_pending_reject_labels SET evidence_complete=1')
        elif fault == 'cross_run': conn.execute("UPDATE burnin_pending_reject_labels SET burnin_run_id='other'")
        elif fault == 'missing_outcome': conn.execute('DELETE FROM burnin_reject_outcomes')
        elif fault == 'invented_economics': conn.execute('UPDATE burnin_reject_outcomes SET hypothetical_net_r_after_costs=1')
        elif fault == 'cross_release': conn.execute("UPDATE burnin_reject_outcomes SET release_id='other'")
        elif fault == 'missing_decision': conn.execute('DELETE FROM order_decisions')
        elif fault == 'live_decision': conn.execute("UPDATE order_decisions SET mode='LIVE'")
        else:
            payload=json.loads(conn.execute('SELECT payload_json FROM burnin_reject_outcomes').fetchone()[0])
            payload.update({'costs':{'missing_cost_fields':['fee_cost']},'gaps':{'market_gaps':['gap']},
                'incomplete_window':{'window_complete':False},'cross_payload':{'campaign_id':'other'}}[fault])
            conn.execute('UPDATE burnin_reject_outcomes SET payload_json=?',(json.dumps(payload),))
        conn.commit()
        assert ops.terminalize_zero_exposure_recovery(conn,cid,preserve_ambiguity=True)['status'] == 'FAIL_CLOSED'
        assert conn.execute('SELECT campaign_status FROM burnin_campaigns').fetchone()[0] == 'RECOVERY_REQUIRED'
        assert authorized_ambiguity_ids(conn,cid,run) == ()
    finally:
        conn.close(); engine.dispose()


def test_ambiguity_identity_race_rolls_back_and_corruption_revokes_compaction(tmp_path, monkeypatch):
    import alphaforge.burnin_ops as ops
    engine, policy, conn, cid, run = ambiguous_recovery(tmp_path, monkeypatch)
    try:
        def race():
            conn.execute("UPDATE burnin_pending_reject_labels SET source_provenance_json='{}'")
            conn.commit()
        monkeypatch.setattr(ops, '_terminalization_pre_begin_hook', race)
        result=ops.terminalize_zero_exposure_recovery(conn,cid,preserve_ambiguity=True)
        assert result['status']=='FAIL_CLOSED' and 'PRESERVED_AMBIGUITY_CHANGED' in result['failure_reasons']
        monkeypatch.setattr(ops, '_terminalization_pre_begin_hook', lambda: None)
        assert ops.terminalize_zero_exposure_recovery(conn,cid,preserve_ambiguity=True)['status']=='PASS'
        conn.execute("UPDATE burnin_pending_reject_labels SET source_provenance_json='{} changed'");conn.commit()
        assert authorized_ambiguity_ids(conn,cid,run)==()
        assert compact_stats_only_universe_batch(engine,policy)['cycles_compacted']==0
    finally:
        conn.close();engine.dispose()


@pytest.mark.parametrize('fault', ['live_worker','stale_probe','unknown_exposure','missing_provider'])
def test_preserved_ambiguity_does_not_bypass_current_runtime_safety(tmp_path, monkeypatch, fault):
    import alphaforge.burnin_ops as ops
    engine, _, conn, cid, run = ambiguous_recovery(tmp_path, monkeypatch)
    try:
        if fault == 'live_worker': monkeypatch.setattr(ops,'_pid_alive',lambda *_:True)
        if fault == 'unknown_exposure':
            conn.execute('UPDATE runtime_state_snapshots SET unknown_exchange_state=1');conn.commit()
        def probe():
            if fault == 'missing_provider': raise RuntimeError('unavailable')
            value=authenticated_probe()
            if fault == 'stale_probe': value['retrieved_at']='2020-01-01T00:00:00Z'
            return value
        monkeypatch.setattr(ops,'_authoritative_recovery_exposure',lambda *_:
            evaluate_runtime_recovery(engine,mode='PAPER',campaign_id=cid,reconciliation_probe=probe))
        assert ops.terminalize_zero_exposure_recovery(conn,cid,preserve_ambiguity=True)['status']=='FAIL_CLOSED'
        assert authorized_ambiguity_ids(conn,cid,run)==()
        assert conn.execute('SELECT campaign_status FROM burnin_campaigns').fetchone()[0]=='RECOVERY_REQUIRED'
    finally:
        conn.close();engine.dispose()


def test_preserved_ambiguity_authorization_expires_without_reopening_execution(tmp_path,monkeypatch):
    import time
    import alphaforge.burnin_ops as ops
    engine,_,conn,cid,run=ambiguous_recovery(tmp_path,monkeypatch)
    try:
        assert ops.terminalize_zero_exposure_recovery(conn,cid,preserve_ambiguity=True)['status']=='PASS'
        assert authorized_ambiguity_ids(conn,cid,run)
        assert authorized_ambiguity_ids(conn,cid,run,now=time.time()+121)==()
        with pytest.raises(RuntimeError,match='NO_CONTINUATION'):
            start_or_resume_campaign(conn,cid,resume=True)
    finally:
        conn.close();engine.dispose()


@pytest.mark.parametrize('fault',[None,'stale_probe','race'])
def test_expired_maintenance_can_only_renew_with_fresh_unchanged_zero_exposure(tmp_path,monkeypatch,fault):
    import alphaforge.burnin_ops as ops
    engine,policy,conn,cid,run=ambiguous_recovery(tmp_path,monkeypatch)
    try:
        assert ops.terminalize_zero_exposure_recovery(conn,cid,preserve_ambiguity=True)['status']=='PASS'
        # Simulate an expired grant without changing its immutable source identity.
        monkeypatch.setattr(ops,'authorized_ambiguity_ids',lambda *_:())
        if fault=='stale_probe':
            def stale():
                value=authenticated_probe();value['retrieved_at']='2020-01-01T00:00:00Z';return value
            monkeypatch.setattr(ops,'_authoritative_recovery_exposure',lambda *_,**kw:evaluate_runtime_recovery(engine,mode='PAPER',campaign_id=cid,reconciliation_probe=stale,force_reconciliation_probe=kw.get('force_reconciliation_probe',False)))
        if fault=='race':
            def race():
                conn.execute("UPDATE burnin_pending_reject_labels SET source_provenance_json='{}'");conn.commit()
            monkeypatch.setattr(ops,'_terminalization_pre_begin_hook',race)
        result=ops.terminalize_zero_exposure_recovery(conn,cid,preserve_ambiguity=True)
        assert result['status']==('PASS' if fault is None else 'FAIL_CLOSED'),result
        if fault is None:
            assert result['renewed'] is True
            assert authorized_ambiguity_ids(conn,cid,run)
            assert compact_stats_only_universe_batch(engine,policy)['cycles_compacted']==2
        assert conn.execute('SELECT COUNT(*) FROM burnin_campaign_runs').fetchone()[0]==1
        with pytest.raises(RuntimeError,match='NO_CONTINUATION'):start_or_resume_campaign(conn,cid,resume=True)
    finally:
        conn.close();engine.dispose()
