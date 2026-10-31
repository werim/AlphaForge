"""Focused regression coverage for trusted GitHub delivery verifier."""
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verifier", HERE / "scripts/verify_codex_delivery.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def _job(*, mutation="success", full="success"):
    return [{
        "name": "test", "conclusion": "success",
        "steps": [
            {"name": "Full regression suite", "conclusion": full},
            {"name": "Protected safety mutation gate", "conclusion": mutation},
            {"name": "Run offline backtest", "conclusion": "success"},
            {"name": "Verify backtest outputs", "conclusion": "success"},
            {"name": "Cross-surface invariant gate", "conclusion": "skipped"},
        ],
    }]


def _run(*, sha="a" * 40, conclusion="success", event="workflow_dispatch"):
    return {
        "id": 123, "event": event, "head_sha": sha,
        "status": "completed", "conclusion": conclusion, "run_attempt": 1,
    }


def test_exact_sha_success_is_ci_only_not_semantic_proof():
    verdict = verifier.evaluate(
        _run(), _job(), "EXACT_HEAD_SHA",
        [{"filename": "src/alphaforge/feature.py"},
         {"filename": "tests/test_feature.py"}],
    )
    assert verdict["status"] == "CI_EVIDENCE_VERIFIED"
    report = verifier.format_report(
        {"number": 12, "head": {"sha": "a" * 40},
         "base": {"repo": {"full_name": "werim/AlphaForge"}}},
        _run(), verdict,
    )
    assert "NOT independently verified" in report
    assert "No branch/code/CI was changed" in report


def test_missing_or_skipped_protected_mutation_is_not_pass():
    for state in ("skipped", "failure", "cancelled", None):
        verdict = verifier.evaluate(
            _run(), _job(mutation=state), "EXACT_HEAD_SHA",
            [{"filename": "scripts/example.py"}, {"filename": "tests/test_example.py"}],
        )
        assert verdict["status"] == "INCOMPLETE_CI_GATES"
        assert "Protected safety mutation gate" in verdict["missing"]


def test_stale_or_synthetic_sha_cannot_be_exaggerated():
    result = verifier.evaluate(
        _run(), _job(), "DIFFERENT_TREE", [{"filename": "docs/note.md"}]
    )
    assert result["status"] == "INCOMPLETE_SOURCE_IDENTITY"


def test_green_run_without_regression_change_needs_review():
    verdict = verifier.evaluate(
        _run(), _job(), "EXACT_HEAD_SHA",
        [{"filename": "src/alphaforge/feature.py"}],
    )
    assert verdict["status"] == "REVIEW_REQUIRED_NO_CHANGED_REGRESSION"


def test_failed_ci_is_not_pass():
    verdict = verifier.evaluate(
        _run(conclusion="failure"), _job(), "EXACT_HEAD_SHA", []
    )
    assert verdict["status"] == "CI_FAILED_OR_NOT_SUCCESSFUL"


def test_pr_ci_requires_protected_gates_but_uses_actual_base_contract():
    jobs = _job()
    jobs[0]["steps"].extend([
        {"name": "System verification routing gate", "conclusion": "success"},
        {"name": "Routed PR verification", "conclusion": "success"},
        {"name": "System audit fabric gate", "conclusion": "success"},
    ])
    jobs[0]["steps"][4]["conclusion"] = "success"
    outcome = verifier.evaluate(
        _run(event="pull_request"), jobs, "EXACT_HEAD_SHA",
        [{"filename": "docs/note.md"}],
    )
    assert outcome["status"] == "CI_EVIDENCE_VERIFIED"


def test_unauthorized_comment_does_not_trigger_audit():
    class Fake:
        repo = "werim/AlphaForge"
        def get(self, path):
            assert path.endswith("/pulls/12")
            return {"state": "open", "user": {"login": "owner"},
                    "head": {"repo": {"full_name": self.repo},
                             "ref": "codex/test", "sha": "a" * 40}}
    payload = {
        "issue": {"number": 12, "pull_request": {"url": "ignored"}},
        "comment": {"body": "TASK: #1131H.1 RESULT: DONE",
                    "user": {"login": "untrusted"}, "author_association": "NONE"},
    }
    assert verifier.handle(Fake(), "issue_comment", payload) == []


def test_non_final_comment_and_no_pr_payload_do_nothing():
    class Never:
        def get(self, *args, **kwargs):
            raise AssertionError("No API call should be made")
    payload = {"issue": {"number": 12}, "comment": {"body": "TASK: #1131H.1 RESULT: DONE"}}
    assert verifier.handle(Never(), "issue_comment", payload) == []


def test_opt_in_scope_requires_same_repo_and_codex_or_marker():
    pr = {"state": "open", "head": {"ref": "codex/work",
          "repo": {"full_name": "werim/AlphaForge"}}}
    assert verifier.eligible(pr, "werim/AlphaForge")
    pr["head"]["repo"]["full_name"] = "someone/fork"
    assert not verifier.eligible(pr, "werim/AlphaForge")


# End-to-end transport/source fakes execute the trusted verifier, never PR code.
from copy import deepcopy
from io import BytesIO
import json
from urllib.error import HTTPError
import pytest


def _pr():
    return dict(number=12,state='open',head=dict(sha='a'*40,ref='codex/work',repo=dict(full_name='werim/AlphaForge')),
                base=dict(ref='main',repo=dict(full_name='werim/AlphaForge')))


class DevAPI:
    repo = 'werim/AlphaForge'

    def __init__(self):
        self.run = {**_run(event='push'), 'name': 'Tests', 'head_branch': 'dev',
                    'head_repository': {'full_name': self.repo}}
        self.head = 'a' * 40
        self.tree = 'b' * 40
        self.checkout = self.head
        self.branch_reads = 0
        self.move_head = False
        self.new_attempt = False
        self.mutation = 'success'
        self.full = 'success'

    def get(self, path):
        if path.endswith('/branches/dev'):
            self.branch_reads += 1
            head = 'c' * 40 if self.move_head and self.branch_reads > 1 else self.head
            return {'commit': {'sha': head}}
        if '/actions/workflows/test.yml/runs?' in path:
            return {'workflow_runs': [deepcopy(self.run)]}
        if path.endswith('/git/commits/' + self.head):
            return {'sha': self.head, 'tree': {'sha': self.tree}}
        raise AssertionError(path)

    def pages(self, path, key=None):
        if '/pulls?state=open' in path:
            return iter([])
        if path.endswith('/jobs'):
            return iter(self.jobs())
        if path.endswith('/commits/' + self.head):
            assert key == 'files'
            return iter([{'filename': 'scripts/verify_codex_delivery.py'},
                         {'filename': 'tests/test_verify_codex_delivery.py'}])
        raise AssertionError(path)

    def jobs(self):
        job = _job(mutation=self.mutation, full=self.full)[0]
        job['id'] = 99
        job['steps'].append({'name': 'Run actions/checkout@v4', 'conclusion': 'success',
                            'started_at': '2026-10-28T00:00:00Z',
                            'completed_at': '2026-10-28T00:00:01Z'})
        return [job]

    def log(self, job_id):
        assert job_id == 99
        if self.new_attempt:
            self.run = {**self.run, 'run_attempt': 2, 'status': 'in_progress', 'conclusion': None}
        return ('2026-10-28T00:00:00.100Z [command]/usr/bin/git log -1 --format=%H\n'
                '2026-10-28T00:00:00.200Z ' + self.checkout + '\n')


def test_dev_push_produces_receipt_without_an_open_pr_or_comment_write():
    api = DevAPI()
    reports = verifier.handle(api, 'workflow_run', {'workflow_run': deepcopy(api.run)})
    assert len(reports) == 1
    report = reports[0]
    assert 'RESULT: CI_EVIDENCE_VERIFIED' in report
    assert 'EXACT_HEAD_SHA' in report and api.head in report and api.tree in report
    assert 'attempt 1' in report and '/actions/runs/123' in report
    assert 'Comment delivery: NOT_APPLICABLE_BRANCH_RECEIPT' in report


@pytest.mark.parametrize('defect,expected', [
    ('failed', 'CI_FAILED_OR_NOT_SUCCESSFUL'),
    ('skipped_mutation', 'INCOMPLETE_CI_GATES'),
    ('skipped_full', 'INCOMPLETE_CI_GATES'),
    ('wrong_checkout', 'INCOMPLETE_SOURCE_IDENTITY'),
    ('wrong_tree', 'INCOMPLETE_SOURCE_IDENTITY'),
    ('moved_head', 'STALE_EVIDENCE_NOT_VERIFIED'),
    ('new_attempt', 'STALE_EVIDENCE_NOT_VERIFIED'),
])
def test_dev_push_never_promotes_failed_missing_or_stale_evidence(defect, expected):
    api = DevAPI()
    if defect == 'failed': api.run['conclusion'] = 'failure'
    elif defect == 'skipped_mutation': api.mutation = 'skipped'
    elif defect == 'skipped_full': api.full = 'skipped'
    elif defect == 'wrong_checkout': api.checkout = 'd' * 40
    elif defect == 'wrong_tree': api.tree = 'unavailable'
    elif defect == 'moved_head': api.move_head = True
    elif defect == 'new_attempt': api.new_attempt = True
    reports = verifier.handle(api, 'workflow_run', {'workflow_run': deepcopy(api.run)})
    assert len(reports) == 1
    assert 'RESULT: ' + expected in reports[0]
    assert 'RESULT: CI_EVIDENCE_VERIFIED' not in reports[0]


@pytest.mark.parametrize('field,value', [
    ('head_branch', 'main'), ('event', 'workflow_dispatch'),
    ('head_repository', {'full_name': 'untrusted/fork'}), ('status', 'in_progress'),
])
def test_dev_branch_receipts_ignore_other_or_unfinished_sources(field, value):
    api = DevAPI()
    api.run[field] = value
    assert verifier.handle(api, 'workflow_run', {'workflow_run': api.run}) == []


def test_workflow_routes_dev_push_without_changing_trusted_checkout_or_permissions():
    workflow = (HERE / '.github/workflows/codex-delivery-verification.yml').read_text()
    assert "github.event.workflow_run.event == 'push'" in workflow
    assert "github.event.workflow_run.head_branch == 'dev'" in workflow
    assert 'ref: main' in workflow and 'persist-credentials: false' in workflow
    assert 'pull_request_target' not in workflow and 'secrets.' not in workflow


@pytest.mark.parametrize('field,value', [('id', None), ('id', True),
                                        ('run_attempt', None), ('run_attempt', 0),
                                        ('run_attempt', True)])
def test_dev_receipt_requires_explicit_run_and_attempt(field, value):
    api = DevAPI()
    api.run[field] = value
    reports = verifier.handle(api, 'workflow_run', {'workflow_run': api.run})
    assert len(reports) == 1 and 'RESULT: INCOMPLETE_CI_IDENTITY' in reports[0]
    assert api.branch_reads == 0


def test_dev_receipt_writes_existing_artifact_and_summary_without_a_pr(monkeypatch, tmp_path):
    api = DevAPI()
    event = tmp_path / 'event.json'
    event.write_text(json.dumps({'workflow_run': api.run}))
    receipt, summary = tmp_path / 'receipt.json', tmp_path / 'summary.md'
    for key, value in {'GITHUB_EVENT_NAME': 'workflow_run', 'GITHUB_EVENT_PATH': str(event),
                       'GITHUB_REPOSITORY': api.repo, 'GH_TOKEN': 'not-a-real-token',
                       'DELIVERY_RECEIPTS_PATH': str(receipt),
                       'GITHUB_STEP_SUMMARY': str(summary)}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(verifier, 'GitHub', lambda *args: api)
    assert verifier.main() == 0
    saved = json.loads(receipt.read_text())
    assert saved['schema'] == 'alpha-delivery-receipts-v1'
    assert len(saved['reports']) == 1
    assert saved['reports'][0] in summary.read_text()
    assert 'RESULT: CI_EVIDENCE_VERIFIED' in saved['reports'][0]


class API:
    repo='werim/AlphaForge'
    def __init__(self):
        self.pr=_pr(); self.run={**_run(), 'created_at':'2026-10-28T00:00:00Z'}
        self.comments=[]; self.writes=[]; self.forbid=False
    def get(self,path):
        if '/pulls/12' in path: return deepcopy(self.pr)
        if '/actions/workflows/test.yml/runs?' in path: return dict(workflow_runs=[self.run])
        raise AssertionError(path)
    def pages(self,path,key=None):
        if '/comments' in path: return iter(self.comments)
        if path.endswith('/jobs'): return iter(self.jobs())
        if path.endswith('/files'): return iter([dict(filename='tests/test_feature.py')])
        if '/pulls?state=open' in path:return iter([deepcopy(self.pr)])
        raise AssertionError(path)
    def jobs(self):
        job=_job()[0];job['id']=99
        job['steps'].append(dict(name='Run actions/checkout@v4',conclusion='success',
            started_at='2026-10-28T00:00:00Z',completed_at='2026-10-28T00:00:01Z'))
        return [job]
    def log(self,job_id):
        assert job_id==99
        return ('2026-10-28T00:00:00.100Z [command]/usr/bin/git log -1 --format=%H\n'
                '2026-10-28T00:00:00.200Z '+'a'*40+'\n')
    def post(self,path,body):
        if self.forbid:
            raise HTTPError(path,403,'Forbidden',{'X-GitHub-Request-Id':'request-123'},
                            BytesIO(b'{"message":"Resource not accessible by integration"}'))
        self.writes.append(('POST',path,body))
        self.comments.append(dict(id=7,body=body['body'],user=dict(login='github-actions[bot]')))
    def patch(self,path,body):
        self.writes.append(('PATCH',path,body));self.comments[0]['body']=body['body']


def test_forbidden_comment_preserves_summary_receipt_and_no_permission_escalation(monkeypatch,tmp_path):
    api=API();api.forbid=True
    event=tmp_path/'event.json';event.write_text('{"inputs":{"pr_number":"12"}}')
    summary=tmp_path/'summary.md';receipt=tmp_path/'receipt.json'
    for key,value in dict(GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_EVENT_PATH=str(event),
        GITHUB_REPOSITORY=api.repo,GH_TOKEN='not-a-real-token',GITHUB_STEP_SUMMARY=str(summary),
        DELIVERY_RECEIPTS_PATH=str(receipt)).items():monkeypatch.setenv(key,value)
    monkeypatch.setattr(verifier,'GitHub',lambda *args:api)
    assert verifier.main()==0
    saved=json.loads(receipt.read_text())
    assert len(saved['reports'])==1
    assert 'CI_EVIDENCE_VERIFIED' in saved['reports'][0]
    assert 'COMMENT_WRITE_FORBIDDEN' in summary.read_text() and 'request-123' in summary.read_text()
    assert 'NOT independently verified' in summary.read_text()
    assert api.writes==[]
    workflow=(HERE/'.github/workflows/codex-delivery-verification.yml').read_text()
    assert 'ref: main' in workflow and 'pull_request_target' not in workflow
    assert 'persist-credentials: false' in workflow and 'issues: write' in workflow
    assert 'pull-requests: read' in workflow and 'secrets.' not in workflow
    assert 'Preserve verification receipts' in workflow


def test_duplicate_delivery_updates_only_same_head_bot_receipt():
    api=API()
    args=(api,'workflow_dispatch',dict(inputs=dict(pr_number='12')))
    first=verifier.handle(*args)
    second=verifier.handle(*args)
    assert first[0].endswith("Comment delivery: CREATED")
    assert second[0].endswith("Comment delivery: UNCHANGED")
    assert first[0].split("Comment delivery:")[0]==second[0].split("Comment delivery:")[0]
    assert len(api.writes)==1
    api.run['conclusion']='failure'
    updated=verifier.handle(*args)
    assert updated[0].endswith("Comment delivery: UPDATED")
    assert len(api.writes)==2 and api.writes[-1][0]=='PATCH'
    assert 'CI_FAILED_OR_NOT_SUCCESSFUL' in api.comments[0]['body']


def test_head_moves_during_log_inspection_never_publishes():
    api=API(); original=api.log
    def moved(job):
        result=original(job);api.pr['head']['sha']='b'*40;return result
    api.log=moved
    result=verifier.handle(api,'workflow_dispatch',dict(inputs=dict(pr_number='12')))
    assert 'STALE_HEAD_NOT_PUBLISHED' in result[0]
    assert 'CI_EVIDENCE_VERIFIED' not in result[0]
    assert api.writes==[]


def test_new_pending_or_failed_run_supersedes_prior_success():
    api=API();original=api.get
    for state,conclusion in [('in_progress',None),('completed','failure')]:
        newer={**_run(), 'id':124,'status':state,'conclusion':conclusion,'created_at':'2026-10-28T00:01:00Z'}
        api.get=lambda path: dict(workflow_runs=[api.run,newer]) if '/runs?' in path else original(path)
        run=verifier.run_for_sha(api,api.pr)
        assert run['id']==124
        verdict=verifier.evaluate(run,api.jobs(),'EXACT_HEAD_SHA',[])
        assert verdict['status']!='CI_EVIDENCE_VERIFIED'


def test_actual_tested_tree_is_required_even_when_metadata_matches_head():
    api=API()
    assert verifier.code_identity(api,api.pr,api.run)=='UNAVAILABLE_TESTED_CHECKOUT'
    assert verifier.tested_checkout(api,api.jobs())=='a'*40
    assert verifier.code_identity(api,api.pr,api.run,'a'*40)=='EXACT_HEAD_SHA'
    run={**api.run,'event':'pull_request'}
    commits={'a'*40:dict(tree=dict(sha='c'*40)),
             'b'*40:dict(tree=dict(sha='c'*40),parents=[dict(sha='a'*40)])}
    api.get=lambda path: commits[path.rsplit('/',1)[-1]]
    assert verifier.code_identity(api,api.pr,run,'b'*40)=='TREE_EQUIVALENT_MERGE'
    commits['b'*40]['tree']['sha']='d'*40
    assert verifier.code_identity(api,api.pr,run,'b'*40)=='DIFFERENT_TREE'
    commits['b'*40]['parents']=[]
    assert verifier.code_identity(api,api.pr,run,'b'*40)=='UNRELATED_CHECKOUT'


def test_checkout_log_outside_checkout_step_or_unavailable_cannot_qualify():
    api=API(); original=api.log(99)
    for log in (original.replace('00:00:00.','00:00:02.'),original+original,'', 'bad timestamp'):
        api.log=lambda _:log
        assert verifier.tested_checkout(api,api.jobs()) is None
    api.log=lambda _: (_ for _ in ()).throw(HTTPError('',403,'Forbidden',{},None))
    assert verifier.tested_checkout(api,api.jobs()) is None


def test_redirect_never_forwards_auth_or_uses_http():
    req=verifier.Request('https://api.github.com/logs',headers={'Authorization':'Bearer secret'})
    redirect=verifier.SafeRedirect().redirect_request(req,None,302,'',{},'https://blob.example/log')
    assert redirect.get_header('Authorization') is None
    with pytest.raises(ValueError):verifier.SafeRedirect().redirect_request(req,None,302,'',{},'http://blob.example/log')


def test_newer_ci_appearing_during_evaluation_is_not_published():
    api=API();original=api.log
    def changed(job):
        result=original(job)
        api.run={**api.run,'run_attempt':2,'status':'in_progress','conclusion':None}
        return result
    api.log=changed
    result=verifier.handle(api,'workflow_dispatch',dict(inputs=dict(pr_number='12')))
    assert api.writes==[]
    assert 'STALE_CI_NOT_PUBLISHED' in result[0] and 'CI_EVIDENCE_VERIFIED' not in result[0]


def test_comment_lookup_exhaustion_does_not_create_duplicate():
    api=API();api.pages=lambda *args,**kwargs: (_ for _ in ()).throw(RuntimeError('GITHUB_PAGINATION_INCOMPLETE'))
    assert verifier.publish(api,api.pr,'report','a'*40)=='COMMENT_LOOKUP_INCOMPLETE_NOT_PUBLISHED'
    assert api.writes==[]


def test_pagination_is_bounded_and_never_silently_complete():
    api=verifier.GitHub('werim/AlphaForge','unused');calls=[]
    def get(path):calls.append(path);return [dict(id=i) for i in range(100)]
    api.get=get
    with pytest.raises(RuntimeError,match='PAGINATION_INCOMPLETE'):list(api.pages('/comments'))
    assert len(calls)==3


def test_checkout_log_fractional_last_second_is_inside_api_step_bounds():
    api=API();jobs=api.jobs();jobs[0]['steps'][-1]['completed_at']='2026-10-28T00:00:00Z'
    assert verifier.tested_checkout(api,jobs)=='a'*40


def test_checkout_log_utf8_bom_is_supported():
    api=API();log=api.log(99);api.log=lambda _:"\ufeff"+log.replace('.100Z','.1001234Z').replace('.200Z','.2001234Z')
    assert verifier.tested_checkout(api,api.jobs())=='a'*40


def test_unformatted_multiline_artifact_output_cannot_spoof_checkout():
    api=API();log=api.log(99)
    api.log=lambda _:log+'artifacts/backtest/output.csv\n'
    assert verifier.tested_checkout(api,api.jobs())=='a'*40
    api.log=lambda _:log.replace('2026-10-28T00:00:00.200Z','unformatted\n2026-10-28T00:00:00.200Z')
    assert verifier.tested_checkout(api,api.jobs()) is None


def test_head_move_during_comment_lookup_is_rechecked_before_write():
    api=API();original=api.pages
    def pages(path,key=None):
        rows=original(path,key)
        if path.endswith('/comments'):api.pr['head']['sha']='b'*40
        return rows
    api.pages=pages
    result=verifier.handle(api,'workflow_dispatch',dict(inputs=dict(pr_number='12')))
    assert api.writes==[]
    assert 'STALE_HEAD_NOT_PUBLISHED' in result[0] and 'CI_EVIDENCE_VERIFIED' not in result[0]
