import copy
import json
import math
from pathlib import Path
import pytest
from app import engine,notebooks,search,store
from app.schemas import CampaignConfig
from app.market import synthetic,INTERVAL_MS
from app.evaluator import evaluate

@pytest.fixture
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'ROOT',tmp_path)
    monkeypatch.setattr(notebooks,'ROOT',tmp_path)
    monkeypatch.setattr(notebooks,'BASELINE_DIR',Path('/opt/baselines'))
    store.init()
    return tmp_path

@pytest.fixture
def baseline():
    return json.loads(Path('/opt/baselines/baseline.ipynb').read_text())

def config(**kwargs):
    return CampaignConfig(prompt='Find a robust risk-adjusted momentum strategy.',**{'markets':['BTC','SOL','HYPE'],**kwargs}).model_dump()

def test_exact_twelve_categories_below_baseline(isolated):
    c=engine.create(config())
    categories=[n for n in c['nodes'] if n['kind']=='category']
    assert len(categories)==12
    assert all(n['parent']=='baseline' for n in categories)

def test_annealing_uses_binding_budget(isolated):
    c=engine.create(config())
    assert search.temperature(c)==1
    c['evaluation_spent']=c['config']['evaluation_budget_seconds']*.4
    assert search.temperature(c)==pytest.approx(.6)
    c['llm_spent']=c['config']['llm_budget_usd']
    assert search.temperature(c)==0

def test_reward_backpropagation(isolated):
    c=engine.create(config())
    child=engine.node('test','category-models','models','Change forecast lookback window')
    c['nodes'].append(child)
    search.backpropagate(c,child,-.25)
    assert child['value']==-.25
    assert engine.getnode(c,'category-models')['value']==-.25
    assert engine.getnode(c,'baseline')['visits']==1

def test_pareto_includes_tradeoffs():
    ns=[]
    for nid,ret,dd,sharpe in [('a',.2,.1,2),('b',.1,.02,1.8),('c',.05,.15,1)]:
        n=engine.node(nid,None,None,'A test pipeline variant')
        n.update(status='completed',metrics={'return':ret,'max_drawdown':dd,'sharpe':sharpe})
        ns.append(n)
    assert set(search.pareto(ns))=={'a','b'}

def test_scope_rejects_other_cells(baseline):
    candidate=copy.deepcopy(baseline)
    candidate['cells'][5]['source']='def stage_models(history, state):\n    return state\n'
    with pytest.raises(ValueError,match='outside allowed cell'):
        notebooks.validate(candidate,baseline,'sizing')

@pytest.mark.parametrize('code',['import os','x = open("/data/holdout")','x = history.close.shift(-1)','x = history.close.rolling(4, center=True).mean()','x = history.__class__','x = history.to_numpy()'])
def test_leakage_and_runtime_guards(baseline,code):
    candidate=copy.deepcopy(baseline)
    candidate['cells'][5]['source']=code
    with pytest.raises(ValueError):
        notebooks.validate(candidate)

def test_git_retains_rejected_and_failed(isolated,baseline):
    a=notebooks.commit('campaign','node',0,baseline,'rejected')
    b=notebooks.commit('campaign','node',1,baseline,'failed')
    assert a['sha']!=b['sha']
    assert notebooks.read('campaign',a)==baseline
    assert (isolated/'notebooks'/'.git').exists()
    assert not (isolated/'notebooks'/'campaign'/'.git').exists()

@pytest.mark.asyncio
async def test_holdout_not_exposed_to_development(isolated):
    c=engine.create(config(bars=200,holdout_fraction=.2,embargo_bars=3))
    await engine.prepare(c)
    c=store.load(c['id'])
    development=engine.datasets(c)[0]['records']
    final=engine.datasets(c,holdout=True)[0]['records']
    assert len(development)==157
    # Full causal history is available, with fitting fenced before the embargo.
    held=engine.datasets(c,holdout=True)[0]
    assert held['evaluation_start']==160
    assert final[160]['time']>development[-1]['time']
    assert held['training_cutoff']==development[-1]['available_at']
    assert len(final)==200

@pytest.mark.asyncio
async def test_final_selection_frozen_before_holdout(isolated,monkeypatch):
    c=engine.create(config(max_nodes=1))
    c['phase']='select';c['best']='baseline';c['explored']=1
    b=engine.getnode(c,'baseline');b.update(status='completed',score=1,metrics={'sharpe':1,'return':.1,'max_drawdown':.1})
    with store.transaction() as db:store.save(c,db)
    await engine.step(c['id'])
    frozen=store.load(c['id'])
    assert frozen['selected']=='baseline'
    assert frozen['holdout'] is None
    assert frozen['selection_frozen_at']>0
    assert frozen['phase']=='finalize'

def test_spot_short_rejected():
    with pytest.raises(ValueError,match='Spot supports'):
        config(market_type='spot',direction='short')

def test_prefix_replay_and_costs(baseline,monkeypatch):
    # Bypass kernel startup here; the integration test separately executes a real notebook kernel.
    import app.evaluator as ev
    class Kernel:
        def __init__(self,nb,**kwargs):self.nb=nb
        def execute(self):return self.nb
    monkeypatch.setattr(ev,'NotebookClient',Kernel)
    cfg=config(bars=160,markets=['BTC'])
    data=synthetic('BTC',cfg,1800000000000)
    data['fee']={'taker_rate':.00045};data['slippage_rate']=.0001
    for cell in baseline['cells']:
        if cell['id']=='models':
            cell['source']='def stage_models(history,state):\n    if history["available_at"].max() != state["as_of"]:\n        raise ValueError("Future leaked")\n    state["forecast"] = 1\n    return state\n'
    payload={'notebook':baseline,'config':cfg,'datasets':[data],'interval_ms':INTERVAL_MS['1h'],'timeout':30,'sources':[{'name':'delayed','records':[{'time':0,'available_at':1900000000000,'value':999}]}]}
    out=evaluate(payload)
    assert out['metrics']['fees']>0
    assert out['metrics']['bars']==96
    assert math.isfinite(out['metrics']['sharpe'])
    assert len(out['curve'])==96
    expensive=copy.deepcopy(payload);expensive['datasets'][0]['fee']['taker_rate']=.01
    assert evaluate(expensive)['metrics']['net_income']<out['metrics']['net_income']

@pytest.mark.asyncio
async def test_evaluation_budget_reserves_holdout(isolated,monkeypatch):
    from app.provider import BudgetExhausted
    c=engine.create(config(evaluation_seconds=10,evaluation_budget_seconds=30))
    c['evaluation_spent']=11
    with store.transaction() as db:store.save(c,db)
    with pytest.raises(BudgetExhausted):
        await engine.run_evaluation(c,engine.getnode(c,'baseline'))

@pytest.mark.asyncio
async def test_resume_does_not_repeat_committed_phase(isolated):
    c=engine.create(config(bars=200))
    await engine.step(c['id'])
    after=store.load(c['id'])
    assert after['phase']=='calibrate'
    assert after['dataset_ids']
    assert engine.getnode(after,'baseline')['notebook']
    assert after['step']==1


def test_merge_commit_has_two_real_git_parents(isolated,baseline):
    from dulwich.repo import Repo
    a=notebooks.commit('c','a',0,baseline,'evaluated')
    b=notebooks.commit('c','b',0,baseline,'evaluated')
    merged=notebooks.commit('c','merged',0,baseline,'merged',[a['sha'],b['sha']])
    repo=Repo(str(isolated/'notebooks'))
    assert set(repo[merged['sha'].encode()].parents)=={a['sha'].encode(),b['sha'].encode()}


def test_selection_reaches_frontier_and_ignores_terminal(isolated):
    c=engine.create(config())
    engine.getnode(c,'baseline')['status']='completed'
    a=engine.node('a','category-models','models','Test a shorter prediction window')
    b=engine.node('b','category-risk','risk','Apply a tighter drawdown breaker')
    c['nodes']+=[a,b]
    assert search.select(c) in {'a','b'}
    a['status']='failed'
    assert search.select(c)=='b'

@pytest.mark.asyncio
async def test_managed_fees_apply_live_hip3_metadata(monkeypatch):
    import app.market as market
    async def fake(payload):
        if payload['type']=='userFees':return {'userCrossRate':'0.00045'}
        return {'collateralToken':0,'universe':[{'name':'xyz:TSLA','deployerFeeScale':'1.0','growthMode':'enabled'}]}
    monkeypatch.setattr(market,'info',fake)
    fee=await market.fee_for({'id':'xyz:TSLA','symbol':'TSLA','type':'perp','dex':'xyz'})
    assert fee['taker_rate']==pytest.approx(.00009)
    assert fee['adjustments']['multiplier']==pytest.approx(.2)

def test_cross_asset_prefixes_and_market_global_isolation(baseline,monkeypatch):
    import app.evaluator as ev
    class Kernel:
        def __init__(self,nb,**kwargs):self.nb=nb
        def execute(self):return self.nb
    monkeypatch.setattr(ev,'NotebookClient',Kernel)
    for cell in baseline['cells']:
        if cell['id']=='models':
            cell['source']='''seen = []
def stage_models(history, state):
    seen.append(1)
    if len(seen) > len(history):
        raise ValueError("Cross-market global contamination")
    for other in state["cross_asset"].values():
        if other["available_at"].max() > state["as_of"]:
            raise ValueError("Future cross-asset row leaked")
    for rows in state["sources"].values():
        for row in rows:
            if row["available_at"] > state["as_of"]:
                raise ValueError("Future source leaked")
    state["forecast"] = 0.01
    return state
'''
    cfg=config(bars=160,markets=['BTC','SOL'])
    datasets=[]
    for symbol in cfg['markets']:
        d=synthetic(symbol,cfg,1800000000000)
        d.update(fee={'taker_rate':.00045},slippage_rate=.0001)
        datasets.append(d)
    result=evaluate({'notebook':baseline,'config':cfg,'datasets':datasets,'interval_ms':INTERVAL_MS['1h'],'timeout':30,'sources':[{'name':'late','records':[{'time':0,'available_at':1900000000000,'value':123}]}]})
    assert result['metrics']['bars']==96
    assert len(result['markets'])==2

@pytest.mark.asyncio
async def test_provider_step_is_idempotent_and_settles_cost(isolated,monkeypatch):
    import app.provider as provider
    c=engine.create(config(mode='live',model='test-model'))
    settings={'base_url':'https://provider.test/v1','api_key':'test-only','models':[{'id':'test-model','reasoning_levels':[],'input_price_per_token':.000001,'output_price_per_token':.000002}]}
    monkeypatch.setattr(store,'settings',lambda:settings)
    attempts=[]
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'choices':[{'message':{'content':'{"approved":true,"feedback":"Passed"}'}}],'usage':{'prompt_tokens':100,'completion_tokens':50}}
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,*args,**kwargs):attempts.append(kwargs);return Response()
    monkeypatch.setattr(provider.httpx,'AsyncClient',Client)
    first=await provider.call(c['id'],'same-step','critic',{'task':'Review'})
    second=await provider.call(c['id'],'same-step','critic',{'task':'Review'})
    assert first==second
    assert len(attempts)==1
    latest=store.load(c['id'])
    assert latest['tokens_spent']==150
    assert latest['llm_spent']==pytest.approx(.0002)

@pytest.mark.asyncio
async def test_ambiguous_provider_retry_keeps_reservations(isolated,monkeypatch):
    import app.provider as provider
    import httpx
    c=engine.create(config(mode='live',model='test-model'))
    settings={'base_url':'https://provider.test/v1','api_key':'test-only','models':[{'id':'test-model','reasoning_levels':[],'input_price_per_token':.000001,'output_price_per_token':.000002}]}
    monkeypatch.setattr(store,'settings',lambda:settings)
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,*args,**kwargs):raise httpx.ReadTimeout('Lost response')
    monkeypatch.setattr(provider.httpx,'AsyncClient',Client)
    with pytest.raises(httpx.ReadTimeout):await provider.call(c['id'],'uncertain-step','developer',{'task':'Implement'})
    spent=store.load(c['id'])['llm_spent']
    assert spent>0
    with pytest.raises(httpx.ReadTimeout):await provider.call(c['id'],'uncertain-step','developer',{'task':'Implement'})
    assert store.load(c['id'])['llm_spent']==pytest.approx(2*spent)

@pytest.mark.asyncio
async def test_provider_budget_blocks_network_before_spend(isolated,monkeypatch):
    import app.provider as provider
    c=engine.create(config(mode='live',model='expensive',llm_budget_usd=.01))
    monkeypatch.setattr(store,'settings',lambda:{'base_url':'https://provider.test/v1','models':[{'id':'expensive','input_price_per_token':.01,'output_price_per_token':.01}]})
    with pytest.raises(provider.BudgetExhausted):await provider.call(c['id'],'first-step','developer',{'task':'Implement'})
    assert store.load(c['id'])['llm_spent']==0
