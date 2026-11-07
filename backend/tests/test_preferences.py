import copy
import json
from pathlib import Path
import pytest
from app import engine,store,notebooks,provider,market,search,history
from app.schemas import CampaignConfig
from app.compute import calibrate
from fastapi.testclient import TestClient
from app.api import app

@pytest.fixture
def sandbox(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'ROOT',tmp_path);monkeypatch.setattr(notebooks,'ROOT',tmp_path);store.init()
    return TestClient(app)

def cfg(**kw):return CampaignConfig(prompt='Investigate configurable market research.',**{'markets':['BTC','SOL','HYPE'],**kw}).model_dump()

def test_synthetic_llm_campaign_requires_a_provider(sandbox,monkeypatch):
    monkeypatch.setattr(store,'settings',lambda:{})
    payload=cfg()
    del payload['agent_mode']
    response=sandbox.post('/api/campaigns',json=payload)
    assert response.status_code==422
    assert 'provider API key' in response.json()['detail']

@pytest.mark.asyncio
async def test_legacy_campaigns_cannot_start_provider_calls(sandbox):
    config=cfg(model='previously-selected-model')
    response=sandbox.post('/api/campaigns',json={**config,'agent_mode':'deterministic'})
    assert response.status_code==422
    c=engine.create(config)
    del c['config']['agent_mode']
    c.update(status='paused',phase='critic')
    with store.transaction() as db:
        store.save(c,db)
    assert sandbox.get('/api/campaigns/'+c['id']).status_code==200
    assert sandbox.post('/api/campaigns/'+c['id']+'/resume').status_code==409
    with pytest.raises(ValueError,match='Start a new campaign with provider models'):
        await provider.call(c['id'],'old-review','critic',{})


@pytest.mark.asyncio
async def test_synthetic_llm_campaign_routes_all_roles_and_records_traces(sandbox,monkeypatch):
    roles=('initializer','developer','critic','researcher')
    settings={'base_url':'https://example.test/v1','api_key':'test','models':[{'id':role} for role in roles]}
    monkeypatch.setattr(store,'settings',lambda:settings)
    config=cfg(markets=['BTC'],llm_budget_usd=0,agents={role:{'model':role} for role in roles})
    c=engine.create(config)
    await engine.prepare(c)
    assert engine.datasets(c,calibrate=True)[0]['provenance']['synthetic']
    assert history.campaign_input(c)['campaign']['agent_mode']=='llm'
    replies={
        'initializer':{'hypotheses':[{'category':key,'summary':'Test pipeline stage behavior','hypothesis':'Measure the effect of a stage configuration.'} for key,_,_ in engine.CATEGORIES]},
        'developer':{'summary':'Use a constant forecast','cells':{'models':'def stage_models(history,state):\n    state["forecast"]=0.01\n    return state\n'}},
        'critic':{'approved':True},
        'researcher':{'placement':'none'},
    }
    class Response:
        def __init__(self,role):self.role=role
        def raise_for_status(self):pass
        def json(self):
            return {'choices':[{'message':{'content':json.dumps(replies[self.role])}}],
                    'usage':{'prompt_tokens':4000,'completion_tokens':500}}
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,**kwargs):
            assert url=='https://example.test/v1/chat/completions'
            return Response(kwargs['json']['model'])
    monkeypatch.setattr(provider.httpx,'AsyncClient',Client)
    await engine.seed_hypotheses(c)
    n=engine.getnode(c,'h-models')
    await engine.develop(c,n)
    await engine.critic(c,n)
    assert c['phase']=='evaluate'
    n.update(metrics={'sharpe':1.},reward=.1)
    await engine.research(c,n)
    with store.connect() as db:
        traces=[json.loads(row['body']) for row in db.execute("SELECT body FROM outbox WHERE json_extract(body,'$.type')='llm' ORDER BY rowid")]
    assert [item['role'] for item in traces]==list(roles)
    assert all(item['input'] and json.loads(item['output'])==replies[item['role']] for item in traces)


def test_zero_limits_and_annealing(sandbox):
    c=engine.create(cfg(llm_budget_usd=0,llm_token_budget=0,evaluation_budget_seconds=0,evaluation_seconds=0,max_nodes=0,critic_retries=0,markets=['BTC','DOGE','xyz:AAPL']))
    c.update(llm_spent=500,tokens_spent=900000,evaluation_spent=100000,explored=10000)
    assert search.temperature(c)==1
    c['config']['max_nodes']=10000
    assert search.temperature(c)==0
    profile=calibrate(c['config'],{'bars':56,'elapsed_seconds':5,'startup_seconds':2,'replay_seconds':3},3)
    assert profile['evaluation_bars']==573

@pytest.mark.asyncio
@pytest.mark.parametrize('levels,effort',[(['high'],'high'),([], 'none'),([], 'custom-effort'),(['high'],'xhigh')])
async def test_each_agent_uses_its_own_model_with_unlimited_unknown_pricing(sandbox,monkeypatch,levels,effort):
    ids={'developer':'dev-model','researcher':'research-model','critic':'critic-model'}
    settings={'base_url':'https://example.test/v1','api_key':'test','models':[{'id':v,'reasoning_levels':levels,'input_price_per_token':None,'output_price_per_token':None} for v in ids.values()]}
    monkeypatch.setattr(store,'settings',lambda:settings)
    config=cfg(mode='live',llm_budget_usd=0,llm_token_budget=0,agents={r:{'model':v,'reasoning':effort} for r,v in ids.items()})
    provider.validate_agents(config,settings)
    c=engine.create(config);requests=[]
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'choices':[{'message':{'content':'{"approved":true}'}}],'usage':{'prompt_tokens':123,'completion_tokens':10}}
    class Client:
        def __init__(self,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,*args,**kw):requests.append(kw['json']);return Response()
    monkeypatch.setattr(provider.httpx,'AsyncClient',Client)
    for role in ids:await provider.call(c['id'],role,role,{'task':'Test routing'})
    assert [r['model'] for r in requests]==list(ids.values())
    assert all(r['reasoning_effort']==effort for r in requests)
    assert store.load(c['id'])['tokens_spent']==399
    assert store.load(c['id'])['llm_cost_known'] is False

@pytest.mark.asyncio
async def test_live_quotes_capture_fees_funding_and_spread(sandbox,monkeypatch):
    async def resolve(ids,instrument):return [{'id':'BTC','symbol':'BTC','type':'perp','dex':'Hyperliquid'}]
    async def info(body):
        if body['type']=='userFees':return {'userCrossRate':'0.00045'}
        if body['type']=='l2Book':return {'levels':[[{'px':'99'}],[{'px':'101'}]]}
        return [{'universe':[{'name':'BTC'}]},[{'funding':'0.0002'}]]
    monkeypatch.setattr(market,'resolve_markets',resolve);monkeypatch.setattr(market,'info',info)
    snap=await market.execution_snapshot(cfg(mode='live',markets=['BTC']))
    q=snap['quotes'][0]
    assert q['taker_rate']==.00045
    assert q['funding_rate_hourly']==.0002
    assert q['spread_bps']==pytest.approx(200)
    assert q['half_spread_rate']==pytest.approx(.01)

@pytest.mark.asyncio
async def test_reviewed_snapshot_is_pinned_and_market_changes_invalidate_it(sandbox,monkeypatch):
    config=cfg(markets=['DOGE','AVAX'])
    snap=await market.execution_snapshot(config)
    config['execution_snapshot_id']=snap['id']
    async def forbidden(*args):raise AssertionError('Reviewed rates must not be refetched')
    monkeypatch.setattr(market,'execution_snapshot',forbidden)
    c=engine.create(config);await engine.prepare(c)
    c=store.load(c['id'])
    assert c['execution_costs']==snap
    data=engine.datasets(c)
    assert data[0]['execution_quote']==snap['quotes'][0]
    with pytest.raises(ValueError,match='Markets or fee account changed'):
        market.load_execution_snapshot(snap['id'],{**config,'markets':['BTC']})

def test_data_defaults_follow_target_and_preserve_endpoint_contract(sandbox):
    low=sandbox.get('/api/evaluation-defaults?target_seconds=2&market_count=3').json()
    high=sandbox.get('/api/evaluation-defaults?target_seconds=60&market_count=3').json()
    full=sandbox.get('/api/evaluation-defaults?target_seconds=0').json()
    assert low['bars']<high['bars']
    assert full['bars']==4900
    assert low['measured'] is False
    assert sandbox.post('/api/execution-quotes',json=cfg(markets=['DOGE'])).status_code==200

@pytest.mark.asyncio
async def test_unlimited_evaluation_sends_no_deadline_and_tracks_elapsed(sandbox,monkeypatch):
    config=cfg(markets=['BTC'],evaluation_budget_seconds=0,evaluation_seconds=0)
    c=engine.create(config);await engine.prepare(c);c=store.load(c['id'])
    n=engine.getnode(c,'baseline');nb=notebooks.read(c['id'],n['notebook'])
    class Response:
        status_code=200
        def json(self):return {'notebook':nb,'metrics':{'bars':100,'elapsed_seconds':10},'curve':[],'assumptions':[]}
    class Client:
        def __init__(self,**kwargs):assert kwargs['timeout'] is None
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,json):assert json['execution_budget_seconds'] is None;return Response()
    monkeypatch.setattr(engine.httpx,'AsyncClient',Client)
    await engine.run_evaluation(c,n)
    assert store.load(c['id'])['evaluation_spent']>=0


def test_agent_choices_can_be_remembered(sandbox):
    with store.connect() as db:db.execute('INSERT INTO settings VALUES(1,?)',(json.dumps({'base_url':'https://example.test/v1','models':[{'id':'a','reasoning_levels':[]}]}),))
    response=sandbox.put('/api/provider/agents',json={role:{'model':'a','reasoning':'custom-effort'} for role in ['developer','researcher','critic']})
    assert response.status_code==200
    assert sandbox.get('/api/provider').json()['default_agents']['critic']=={'model':'a','reasoning':'custom-effort'}

def test_frozen_funding_cost_overrides_historical_series(sandbox,monkeypatch):
    from app.evaluator import evaluate
    import app.evaluator as evaluator
    class Kernel:
        def __init__(self,nb,**kw):self.nb=nb
        def execute(self):return self.nb
    monkeypatch.setattr(evaluator,'NotebookClient',Kernel)
    config=cfg(markets=['BTC'],bars=160)
    nb=json.loads(Path('/opt/baselines/baseline.ipynb').read_text())
    next(c for c in nb['cells'] if c['id']=='models')['source']='def stage_models(history,state):\n    state["forecast"]=1.\n    return state\n'
    d=market.synthetic('BTC',config,1800000000000)
    for row in d['records']:row['funding']=999.
    d.update(fee={'taker_rate':.00045},slippage_rate=.0001,execution_quote={'funding_rate_hourly':0.})
    payload={'notebook':nb,'config':config,'datasets':[d],'interval_ms':3600000,'sources':[]}
    first=evaluate(payload)
    assert first['metrics']['funding']==0
    d['execution_quote']['funding_rate_hourly']=.001
    second=evaluate(payload)
    assert second['metrics']['funding']>0
    assert second['metrics']['net_income']<first['metrics']['net_income']


@pytest.mark.asyncio
async def test_market_refresh_replaces_cached_catalog(sandbox,monkeypatch):
    import time
    cached={'time':time.time(),'markets':[{'id':'old-market'}]}
    with store.connect() as db:
        db.execute('INSERT INTO cache VALUES(?,?)',('market-universe',json.dumps(cached)))
    requests=[]
    async def info(body):
        requests.append(body['type'])
        if body['type']=='perpDexs':return [None]
        if body['type']=='meta':return {'universe':[{'name':'exchange-market','maxLeverage':10,'szDecimals':2}]}
        if body['type']=='spotMeta':return {'tokens':[],'universe':[]}
        raise AssertionError(body)
    monkeypatch.setattr(market,'info',info)
    assert await market.discover()==cached
    assert requests==[]
    refreshed=await market.discover(force=True)
    assert [m['id'] for m in refreshed['markets']]==['exchange-market']
    assert requests==['perpDexs','meta','spotMeta']
    assert await market.discover()==refreshed


def test_market_endpoint_requests_refresh(sandbox,monkeypatch):
    import app.api as api_module
    seen=[]
    async def discover(force=False):
        seen.append(force)
        return {'markets':[]}
    monkeypatch.setattr(api_module,'discover',discover)
    assert sandbox.get('/api/markets?refresh=true').status_code==200
    assert seen==[True]


def test_campaign_requires_explicit_markets(sandbox):
    response=sandbox.post('/api/campaigns',json={'prompt':'Investigate market momentum'})
    assert response.status_code==422
    assert 'markets' in response.text


@pytest.mark.asyncio
async def test_weekly_candles_include_latest_close_and_align_funding(sandbox, monkeypatch):
    week = 604_800_000
    boundary = (1_800_000_000_000 // week) * week
    candles = [
        {'t': opening, 'T': opening + week - 1, 'o': '100', 'h': '101', 'l': '99', 'c': '100', 'v': '1', 'n': 24}
        for opening in range(boundary - 150 * week, boundary + week, week)
    ]
    del candles[-3]['n']

    async def info(payload):
        if payload['type'] == 'candleSnapshot':
            assert payload['req']['interval'] == '1w'
            assert payload['req']['endTime'] == boundary
            return candles
        assert payload['endTime'] == boundary - 1
        return [
            {'time': boundary - week - 1, 'fundingRate': '0.001'},
            {'time': boundary - week, 'fundingRate': '0.002'},
            {'time': boundary - 1, 'fundingRate': '0.004'},
        ]

    monkeypatch.setattr(market, 'info', info)
    dataset = await market.candles({'id': 'BTC', 'type': 'perp'}, cfg(interval='1w', bars=150), boundary + 1)
    rows = dataset['records']
    assert len(rows) == 150
    assert rows[-1]['available_at'] == boundary
    assert rows[-2]['trade_count'] is None
    assert rows[-1]['trade_count'] == 24
    assert rows[-2]['funding'] == pytest.approx(.001)
    assert rows[-1]['funding'] == pytest.approx(.006)
