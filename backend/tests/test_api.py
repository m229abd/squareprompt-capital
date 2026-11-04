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
