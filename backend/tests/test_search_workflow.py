import json
from pathlib import Path

import pytest
from dulwich.repo import Repo
from app import engine, notebooks, provider, search, store
from app.api import public


@pytest.fixture
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'ROOT',tmp_path)
    monkeypatch.setattr(notebooks,'ROOT',tmp_path)
    store.init()


@pytest.fixture
def baseline():
    return json.loads(Path('/opt/baselines/baseline.ipynb').read_text())


def campaign(**kwargs):
    return engine.create({'prompt':'Investigate robust hypothesis search', 'markets':['BTC'], **kwargs})


def test_notebook_branches_fork_at_their_input_and_keep_attempts(isolated,baseline):
    root=notebooks.commit('demo','baseline',0,baseline,'evaluated')
    a=notebooks.commit('demo','a',0,baseline,'draft',base=root)
    b=notebooks.commit('demo','b',0,baseline,'draft',base=root)
    assert notebooks.commit('demo','a',0,baseline,'draft',base=root)==a
    rejected=notebooks.commit('demo','a',0,baseline,'rejected')
    retry=notebooks.commit('demo','a',1,baseline,'draft',base=root)
    with Repo(str(notebooks.repo('demo'))) as repo:
        ancestry=lambda ref: repo[ref['sha'].encode()].parents
        assert ancestry(a)==ancestry(b)==[root['sha'].encode()]
        assert ancestry(rejected)==[a['sha'].encode()]
        assert ancestry(retry)==[rejected['sha'].encode()]
        assert repo.refs[b'refs/heads/campaigns/demo/a']==retry['sha'].encode()
        assert repo.refs[b'refs/heads/campaigns/demo/b']==b['sha'].encode()
    (notebooks.repo('demo')/a['path']).unlink()
    assert notebooks.read('demo',a)==baseline


def test_policy_excludes_reserved_compute_and_keeps_decision(isolated):
    c=campaign(evaluation_budget_seconds=100)
    c['evaluation_spent']=20
    temperature=search.temperature(c)
    c['selection']={'node':'h-models','temperature':temperature,'mode':'search'}
    c.update(phase='evaluate',evaluation_spent=100,evaluation_reserved=80)
    visible=public(c)
    assert visible['evaluation_spent']==20
    assert visible['temperature']==temperature==pytest.approx(.8)
    assert visible['selection']['temperature']==temperature


def test_zero_is_greedy_and_one_reaches_the_whole_frontier(isolated):
    c=campaign()
    engine.getnode(c,'baseline').update(status='completed',visits=40)
    engine.getnode(c,'category-models').update(visits=20,value=10)
    engine.getnode(c,'category-signals').update(visits=1,value=-1)
    c['nodes']+= [engine.node('a','category-models','models','Increase ridge model regularization'),engine.node('b','category-signals','signals','Increase forecast entry threshold')]
    choices=set()
    for seed in range(20):
        c['seed']=seed
        assert search.select(c,0)=='a'
        choices.add(search.select(c,1))
    assert choices=={'a','b'}


@pytest.mark.asyncio
async def test_seed_uses_pool_initializer_and_keeps_legacy_fallback(isolated,baseline,monkeypatch):
    roles={'developer':{'model':'d'},'researcher':{'model':'r','reasoning':'low'},'critic':{'model':'c'},'initializer':{'model':'i','reasoning':'custom'}}
    c=campaign(mode='live',agents=roles)
    engine.getnode(c,'baseline')['notebook']=notebooks.commit(c['id'],'baseline',0,baseline,'created')
    async def agent(cid,key,role,payload):
        assert role=='initializer'
        assert provider.agent_selection(store.load(cid)['config'],role)=={'model':'i','reasoning':'custom'}
        return {'hypotheses':[{'category':key,'summary':'Test specific stage hypothesis','hypothesis':'Test a specific stage configuration.'} for key,_,_ in engine.CATEGORIES]}
    monkeypatch.setattr(provider,'agent',agent)
    await engine.seed_hypotheses(c)
    assert sum(n['kind']=='hypothesis' for n in c['nodes'])==12
    del c['config']['agents']['initializer']
    assert provider.agent_selection(c['config'],'initializer')=={'model':'r','reasoning':'low'}


@pytest.mark.parametrize('placement',['child','sibling','none',None])
@pytest.mark.asyncio
async def test_researcher_placement_pins_notebook_and_reward_parent(isolated,baseline,monkeypatch,placement):
    c=campaign(mode='live')
    root=engine.getnode(c,'baseline')
    root.update(status='completed',notebook=notebooks.commit(c['id'],'baseline',0,baseline,'evaluated'),sources=[{'name':'baseline source'}])
    current=engine.node('observed','category-signals','signals','Widen forecast uncertainty entry band','Try a wider threshold.')
    current.update(status='negative',reward=-.25,metrics={'sharpe':.1},notebook=notebooks.commit(c['id'],'observed',0,baseline,'evaluated',base=root['notebook']),sources=[{'name':'current source'}])
    champion=engine.node('champion','category-models','models','Improve ridge penalty tuning')
    champion.update(status='completed',notebook=notebooks.commit(c['id'],'champion',0,baseline,'evaluated',base=root['notebook']))
    c['nodes'] += [current,champion]
    c['best']='champion'
    c['phase']='research'
    with store.transaction() as db:store.save(c,db)
    async def agent(cid,key,role,payload):
        assert role=='researcher'
        assert payload['comparison_parent']['id']=='baseline'
        return {'placement':placement,'summary':'Try smaller forecast uncertainty band','hypothesis':'Set the uncertainty band to five percent.'}
    monkeypatch.setattr(provider,'agent',agent)
    await engine.research(c,current)
    c=store.load(c['id'])
    children=[n for n in c['nodes'] if n.get('proposed_by')=='observed']
    if placement in ('none',None):
        assert children==[]
        return
    assert len(children)==1
    proposal=children[0]
    expected=current if placement=='child' else root
    assert proposal['parent']==('observed' if placement=='child' else 'category-signals')
    assert proposal['eval_parent']==expected['id']
    assert proposal['base_notebook']==expected['notebook']
    assert proposal['sources']==expected['sources']
    await engine.research(c,current)
    assert sum(n.get('proposed_by')=='observed' for n in store.load(c['id'])['nodes'])==1
    await engine.step(c['id'])
    selected=engine.getnode(store.load(c['id']),proposal['id'])
    assert selected['status']=='developing'
    assert selected['base_notebook']==expected['notebook']
    assert not selected.get('inherited_from')


def test_comment_only_patch_does_not_change_executable_code(baseline):
    cell=next(c for c in baseline['cells'] if c['id']=='signals')
    annotated=notebooks.apply_patch(baseline,{'cells':{'signals':'# Updated heading\n'+notebooks.source(cell)}},'signals')
    assert not notebooks.code_changed(baseline,annotated)
    edited=notebooks.apply_patch(baseline,{'cells':{'signals':notebooks.source(cell)+'\nentry_scale = 0.2\n'}},'signals')
    assert notebooks.code_changed(baseline,edited)
