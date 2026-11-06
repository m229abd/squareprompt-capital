import json
from fastapi.testclient import TestClient
import pytest
from app import api,store,notebooks,engine
from pathlib import Path

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'ROOT',tmp_path)
    monkeypatch.setattr(notebooks,'ROOT',tmp_path)
    store.init()
    # Lifespan dispatch is tested against real Temporal by the smoke run, not in isolated API tests.
    return TestClient(api.app)

def test_provider_key_is_never_returned(client):
    assert client.put('/api/provider',json={'base_url':'https://provider.example/v1','api_key':'private-test-key'}).status_code==200
    response=client.get('/api/provider')
    assert response.status_code==200
    assert response.json()['has_key'] is True
    assert 'private-test-key' not in response.text

def test_invalid_campaign_constraints_are_actionable(client):
    result=client.post('/api/campaigns',json={'markets':['BTC','SOL','HYPE'],'prompt':'Investigate historical momentum','market_type':'spot','direction':'short'})
    assert result.status_code==422
    assert 'Spot supports' in result.text

def test_live_campaign_cannot_silently_use_synthetic_data(client):
    result=client.post('/api/campaigns',json={'markets':['BTC','SOL','HYPE'],'prompt':'Investigate historical momentum','mode':'live','model':'unconfigured'})
    assert result.status_code==422
    assert client.get('/api/campaigns').json()==[]

def test_baseline_schema_is_validated_on_import(client):
    bad=client.post('/api/baselines',json={'notebook':{'cells':[],'nbformat':4,'nbformat_minor':5,'metadata':{}}})
    assert bad.status_code==422
    good=json.loads(Path('/opt/baselines/baseline.ipynb').read_text())
    result=client.post('/api/baselines',json={'notebook':good})
    assert result.status_code==201
    assert result.json()['name'] in [b['name'] for b in client.get('/api/baselines').json()]

def test_holdout_sources_do_not_escape_public_campaign_state(client):
    c=engine.create({'markets':['BTC','SOL','HYPE'],'prompt':'Investigate historical momentum'})
    c['nodes'][0]['sources']=[{'name':'private-future','sha256':'hash','records':[{'time':100,'available_at':200,'value':98765}]}]
    with store.transaction() as db:store.save(c,db)
    result=client.get('/api/campaigns/'+c['id'])
    assert result.status_code==200
    assert '98765' not in result.text

def test_budgets_cannot_be_lowered_mid_campaign(client):
    c=engine.create({'markets':['BTC','SOL','HYPE'],'prompt':'Investigate historical momentum'})
    rejected=client.patch('/api/campaigns/'+c['id']+'/budgets',json={'llm_budget_usd':1})
    assert rejected.status_code==422
    accepted=client.patch('/api/campaigns/'+c['id']+'/budgets',json={'llm_budget_usd':20})
    assert accepted.status_code==200
    assert accepted.json()['config']['llm_budget_usd']==20



def test_overview_keeps_chart_curves_and_only_dataset_metadata(client):
    c=engine.create({'markets':['BTC'],'prompt':'Verify lean dashboard polling'})
    base=engine.getnode(c,'baseline');base['curve']=[{'time':1,'equity':100}]
    winner=engine.node('winner','baseline','models','Increase model regularization strength')
    winner['curve']=[{'time':1,'equity':102}]
    other=engine.node('other','baseline','models','Adjust model regularization strength')
    other['curve']=[{'time':1,'equity':98}]
    c['nodes'] += [winner,other];c['best']='winner'
    with store.transaction() as db:
        store.save(c,db)
        db.execute('INSERT INTO cache VALUES(?,?)',('demo-test',json.dumps({'id':'demo-test','records':[{'close':1}],'bytes':18})))
    body=client.get('/api/overview').json()
    nodes={n['id']:n for n in body['campaigns'][0]['nodes']}
    assert nodes['baseline']['curve']==base['curve']
    assert nodes['winner']['curve']==winner['curve']
    assert 'curve' not in nodes['other']
    assert body['datasets']==[{'id':'demo-test','bytes':18}]
    assert engine.getnode(store.load(c['id']),'other')['curve']==other['curve']


def test_overview_only_loads_selected_campaign_detail(client):
    older=engine.create({'markets':['BTC'],'prompt':'Open this campaign detail'})
    latest=engine.create({'markets':['ETH'],'prompt':'Keep this campaign in the list'})
    for c in (older,latest):
        c['best']='baseline'
        c['holdout']={'metrics':{'return':.1},'curve':[{'time':1,'equity':110}]}
        engine.getnode(c,'baseline').update(metrics={'sharpe':.5},curve=[{'time':1,'equity':102}])
        with store.transaction() as db:store.save(c,db)
    body=client.get('/api/overview',params={'campaign_id':older['id']}).json()
    views={c['id']:c for c in body['campaigns']}
    assert len(views[older['id']]['nodes'])==13
    assert 'curve' in views[older['id']]['nodes'][0]
    assert len(views[latest['id']]['nodes'])==1
    assert views[latest['id']]['nodes'][0]['metrics']['sharpe']==.5
    assert 'curve' not in views[latest['id']]['nodes'][0]
    assert 'curve' not in views[latest['id']]['holdout']


def test_suggested_history_reads_pilot_metadata_and_respects_split(client):
    c=engine.create({'markets':['BTC'],'prompt':'Measure history suggestion without reading replay'})
    c['calibration']={'seconds_per_portfolio_bar':.01,'startup_seconds':3}
    with store.transaction() as db:store.save(c,db)
    r=client.get('/api/evaluation-defaults',params={'target_seconds':15,'holdout_fraction':.3,'embargo_bars':12})
    assert r.status_code==200
    result=r.json()
    assert result['reference_campaign']==c['id']
    assert int(result['bars']*.7)-64-12>=1200
    assert client.get('/api/evaluation-defaults?target_seconds=0').json()['bars']==4900


def test_refresh_requires_an_explicit_interval(client):
    response=client.post('/api/schedules',json={'campaign':'test-campaign'})
    assert response.status_code==422
    assert any(error['loc']==['body','interval_hours'] for error in response.json()['detail'])
