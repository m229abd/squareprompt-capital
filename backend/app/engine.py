import asyncio
import copy
import json
import math
import os
import time
import uuid
from pathlib import Path
import httpx
from . import market, notebooks, provider, search, store
from .constants import CATEGORIES, uses_llm
from .limits import reached
from .schemas import CampaignConfig
from .compute import calibrate as calibrate_compute, estimates as compute_estimates


def node(nid,parent,category,summary,hypothesis='',kind='hypothesis',eval_parent='baseline'):
    return {'id':nid,'parent':parent,'category':category,'summary':summary,'hypothesis':hypothesis,'kind':kind,'eval_parent':eval_parent,'status':'queued' if kind!='category' else 'category','reward':None,'score':None,'visits':0,'value':0,'attempt':0,'metrics':None,'notebook':None,'feedback':'','merge_parents':[]}

def create(config,cid=None,initial_status="starting"):
    cfg=CampaignConfig.model_validate(config).model_dump()
    cid=cid or uuid.uuid4().hex[:12]
    nodes=[node('baseline',None,None,'Baseline research pipeline',kind='baseline',eval_parent=None)]
    for key,name,description in CATEGORIES:
        nodes.append(node('category-'+key,'baseline',key,name,description,kind='category'))
    c={'id':cid,'created_at':time.time(),'name':cfg['name'],'config':cfg,'status':initial_status,'phase':'prepare','step':0,'seed':int(cid[:8],16),'nodes':nodes,'active_node':'baseline','llm_spent':0.,'tokens_spent':0,'evaluation_spent':0.,'explored':0,'pareto':[],'best':None,'holdout':None,'error':None,'calibration':None,'dataset_ids':[],'merge_queue':[],'pause_requested':False,'stop_requested':False,'final_reason':None,'selected':None,'model_version':None}
    with store.transaction() as db:
        store.save(c,db)
        store.event(cid,'campaign','Campaign created. Preparing reproducible data and baseline.',db)
    return c

def getnode(c,nid):
    return next(n for n in c['nodes'] if n['id']==nid)

def persist(c,message=None,kind='step'):
    # Budget ledgers can change while an LLM/evaluation activity runs. Never overwrite their newer values.
    with store.transaction() as db:
        current=store.load(c['id'],db)
        for key in ['llm_spent','tokens_spent','evaluation_spent','pause_requested','stop_requested','llm_cost_known','evaluation_reserved']:
            c[key]=current.get(key,True) if key=='llm_cost_known' else current.get(key,0) if key=='evaluation_reserved' else current[key]
        c['step']+=1
        c['error']=None
        if c['status'] not in {'completed','stopped'}:
            c['status']='paused' if c['pause_requested'] else 'running'
        store.save(c,db)
        if c.get('active_node'):
            n=getnode(c,c['active_node'])
            db.execute('INSERT OR IGNORE INTO outbox(id,body) VALUES(?,?)',(f"state:{c['id']}:{c['step']}",json.dumps({'type':'state','campaign':c['id'],'node':n['id'],'step':c['step'],'status':n['status'],'phase':c['phase'],'reward':n['reward'],'summary':n['summary']})))
        if message:
            store.event(c['id'],kind,message,db)

def fail(cid,exc):
    with store.transaction() as db:
        c=store.load(cid,db)
        c['error']=str(exc)[:4000]
        c['status']='attention'
        store.save(c,db)
        store.event(cid,'error',c['error'],db)

async def prepare(c):
    cfg=c['config']
    end_ms=int(c['created_at']*1000)
    datasets=[]
    if cfg['mode']=='demo':
        for symbol in cfg['markets']:
            d=market.synthetic(symbol,cfg,end_ms)
            d['fee']={'taker_rate':.00045,'source':'DEMO assumption','account_specific':False}
            d['slippage_rate']=.0001
            datasets.append(d)
    else:
        chosen=await market.resolve_markets(cfg['markets'],cfg['market_type'])
        for m in chosen:
            d=await market.candles(m,cfg,end_ms)
            datasets.append(d)
    snapshot=market.load_execution_snapshot(cfg['execution_snapshot_id'],cfg) if cfg.get('execution_snapshot_id') else await market.execution_snapshot(cfg)
    if any(q.get('error') for q in snapshot['quotes']): raise ValueError('Execution costs unavailable: '+str(snapshot['quotes']))
    c['execution_costs']=snapshot
    quotes={q['market']:q for q in snapshot['quotes']}
    for d in datasets:
        q=quotes[d['market']['id']]
        d['fee']=q.get('fee_details') or {'taker_rate':q['taker_rate'],'source':q['source'],'fetched_at':q['fetched_at']}
        d['slippage_rate']=q['half_spread_rate']
        d['execution_quote']=q
    # Freeze the campaign dataset and cost snapshot; cache reuse cannot mutate it later.
    c['dataset_ids']=[]
    with store.connect() as db:
        for d in datasets:
            c['dataset_ids'].append(d['id'])
            db.execute('INSERT OR REPLACE INTO cache VALUES(?,?)',(d['id'],json.dumps(d)))
            db.execute('INSERT OR REPLACE INTO cache VALUES(?,?)',(f"snapshot:{c['id']}:{d['id']}",json.dumps(d)))
    baseline_path=notebooks.BASELINE_DIR/cfg['baseline']
    if not baseline_path.is_file():
        baseline_path=store.ROOT/'baselines'/cfg['baseline']
    if baseline_path.name!=cfg['baseline'] or not baseline_path.is_file():
        raise ValueError('Baseline not found in the managed baseline library.')
    baseline=json.loads(baseline_path.read_text())
    notebooks.validate(baseline)
    getnode(c,'baseline')['notebook']=notebooks.commit(c['id'],'baseline',0,baseline,'created')
    c['phase']='calibrate'
    persist(c,'Dataset snapshot pinned; final chronological holdout sealed.','data')

def datasets(c,holdout=False,calibrate=False):
    result=[]
    cfg=c['config']
    with store.connect() as db:
        for did in c['dataset_ids']:
            d=json.loads(db.execute('SELECT body FROM cache WHERE id=?',(f"snapshot:{c['id']}:{did}",)).fetchone()['body'])
            rows=d['records']
            split=int(len(rows)*(1-cfg['holdout_fraction']))
            if holdout:
                # The frozen artifact can fit on pre-embargo data and warm indicators on
                # completed history. The full holdout stays excluded from fitting.
                d['records']=rows
                d['evaluation_start']=split
                d['training_cutoff']=int(rows[split-cfg['embargo_bars']-1]['available_at'])
            else:
                end=split-cfg['embargo_bars']
                if calibrate:
                    end=min(end,120)
                elif c['calibration']:
                    end=min(end,c['calibration']['evaluation_bars'])
                d['records']=rows[:end]
            result.append(d)
    return result

async def run_evaluation(c,n,holdout=False,calibrate=False):
    key=f"eval:{c['id']}:{n['id']}:{n['attempt']}:{'holdout' if holdout else 'calibration' if calibrate else 'development'}"
    with store.connect() as db:
        saved=db.execute('SELECT body FROM cache WHERE id=?',(key,)).fetchone()
    if saved:
        return json.loads(saved['body'])
    # Reserve the final holdout before spending any search evaluation budget.
    with store.transaction() as db:
        latest=store.load(c['id'],db)
        expected,reserve=compute_estimates(latest)
        remaining=latest['config']['evaluation_budget_seconds']-latest['evaluation_spent'] if latest['config']['evaluation_budget_seconds'] else None
        allotted=(remaining if holdout else remaining-reserve) if remaining is not None else None
        if allotted is not None and (allotted<1 or (not holdout and not calibrate and allotted<expected)):
            raise provider.BudgetExhausted('Evaluation budget preserves the final holdout reservation.')
        latest['evaluation_spent']+=allotted or 0
        latest['evaluation_reserved']=allotted or 0
        store.save(latest,db)
    selected_datasets=datasets(c,holdout,calibrate)
    cutoff=max(d['records'][-1]['available_at'] for d in selected_datasets)
    bounded_sources=[{**s,'records':[r for r in s['records'] if r['available_at']<=cutoff]} for s in n.get('sources',[])]
    payload={'notebook':notebooks.read(c['id'],n['notebook']),'config':c['config'],'datasets':selected_datasets,'interval_ms':market.INTERVAL_MS[c['config']['interval']],'execution_budget_seconds':allotted,'sources':bounded_sources}
    started=time.monotonic()
    async with httpx.AsyncClient(timeout=allotted+10 if allotted is not None else None) as client:
        r=await client.post(os.getenv('RUNNER_URL','http://runner:8001')+'/evaluate',json=payload)
    if r.status_code!=200:
        # A received failure has known elapsed time; it must not consume all remaining compute.
        elapsed=min(allotted,time.monotonic()-started) if allotted is not None else time.monotonic()-started
        with store.transaction() as db:
            latest=store.load(c['id'],db)
            latest['evaluation_spent']-=(allotted or 0)-elapsed
            latest['evaluation_reserved']=0
            store.save(latest,db)
        raise ValueError('Notebook evaluation failed: '+r.text[-3000:])
    result=r.json()
    elapsed=min(allotted,time.monotonic()-started) if allotted is not None else time.monotonic()-started
    evaluated_ref=notebooks.commit(c['id'],n['id'],n['attempt'],result.pop('notebook'),'holdout' if holdout else 'calibrated' if calibrate else 'evaluated',base=n['notebook'])
    result['notebook']=evaluated_ref
    with store.transaction() as db:
        # Result and budget settlement commit together. Retry returns the same result.
        latest=store.load(c['id'],db)
        latest['evaluation_spent']-=(allotted or 0)-elapsed
        latest['evaluation_reserved']=0
        store.save(latest,db)
        db.execute('INSERT OR REPLACE INTO cache VALUES(?,?)',(key,json.dumps(result)))
        db.execute('INSERT OR REPLACE INTO outbox(id,body) VALUES(?,?)',(key,json.dumps({'type':'evaluation','campaign':c['id'],'node':n['id'],'holdout':holdout,'calibrate':calibrate,'result':result})))
    return result

def summary(value,fallback):
    words=str(value or fallback).split()
    return ' '.join(words[:8]) if len(words)>=3 else fallback

async def seed_hypotheses(c):
    baseline=notebooks.read(c['id'],getnode(c,'baseline')['notebook'])
    result=await provider.agent(c['id'],'initial-research','initializer',{'task':'initial hypotheses','user_prompt':c['config']['prompt'],'categories':CATEGORIES,'baseline_metrics':getnode(c,'baseline')['metrics'],'baseline_notebook':baseline})
    hypotheses=result.get('hypotheses',[])
    by_category={h['category']:h for h in hypotheses if h.get('category') in {k for k,_,_ in CATEGORIES}}
    if len(by_category)!=12:
        raise ValueError('Pool initializer must return one hypothesis per category.')
    for key,name,_ in CATEGORIES:
        h=by_category[key]
        n=node('h-'+key,'category-'+key,key,summary(h.get('summary'),f'Investigate {name.lower()} improvements'),h['hypothesis'])
        n['base_notebook']=getnode(c,'baseline')['notebook']
        n['sources']=[]
        c['nodes'].append(n)
    c['phase']='select'
    persist(c,'Twelve research branches initialized from the campaign prompt.','research')

async def develop(c,n):
    parent=notebooks.read(c['id'],n.get('base_notebook') or getnode(c,n['eval_parent'])['notebook'])
    merge=n['kind']=='merge'
    payload={'task':'semantic merge' if merge else 'implement hypothesis','hypothesis':n['hypothesis'],'allowed_category':'all category cells' if merge else n['category'],'notebook':parent,'parent_metrics':getnode(c,n['eval_parent']).get('metrics'),'feedback':n.get('feedback'),'user_prompt':c['config']['prompt'],'compute_profile':c['calibration'],'evaluation_target_seconds':c['config']['evaluation_seconds']}
    if merge:
        payload['merge_notebooks']=[notebooks.read(c['id'],getnode(c,p)['notebook']) for p in n['merge_parents']]
    response=await provider.agent(c['id'],f"{n['id']}:developer:{n['attempt']}",'developer',payload)
    try:
        candidate=notebooks.apply_patch(parent,response,n['category'],merge)
    except ValueError as exc:
        # An invalid developer response still produces a preserved failed attempt and bounded repair feedback.
        n['notebook']=notebooks.commit(c['id'],n['id'],n['attempt'],parent,'invalid-response',base=n.get('base_notebook'))
        n['feedback']=str(exc)+' Return a non-empty cells map with replacement Python source.'
        if not c['config']['critic_retries'] or n['attempt']<c['config']['critic_retries']:
            n['attempt']+=1
            c['phase']='develop'
        else:
            n['status']='rejected';c['explored']+=1;c['phase']='select'
            search.backpropagate(c,n,0.)
        persist(c,f"Invalid developer output for {n['id']}; feedback saved.",'rejected')
        return
    n['summary']=summary(response.get('summary'),n['summary'])
    n['notebook']=notebooks.commit(c['id'],n['id'],n['attempt'],candidate,'draft',base=n.get('base_notebook'))
    n['status']='reviewing'
    n['source_requests']=response.get('sources',[])
    c['phase']='critic'
    persist(c,f"Developer saved {n['id']} attempt {n['attempt']}; awaiting review.",'developer')

async def critic(c,n):
    parent=notebooks.read(c['id'],n['base_notebook'])
    candidate=notebooks.read(c['id'],n['notebook'])
    try:
        notebooks.validate(candidate,parent,n['category'],n['kind']=='merge')
        if n['kind']!='merge' and not n['source_requests'] and not notebooks.code_changed(parent,candidate):
            raise ValueError('The proposed edit does not change executable notebook code or add a data source.')
        if n['source_requests'] and n['category']!='sources' and n['kind']!='merge':
            raise ValueError('Additional sources may only be introduced in Sources and ingestion or a merge.')
        verdict=await provider.agent(c['id'],f"{n['id']}:critic:{n['attempt']}",'critic',{'task':'Review exact edit scope, future leakage, train-only fitting, execution correctness, publication latency and source provenance. Return approved boolean.','hypothesis':n['hypothesis'],'allowed_category':'all category cells' if n['kind']=='merge' else n['category'],'parent':parent,'candidate':candidate,'sources':n['source_requests']})
        if verdict.get('approved') is not True:
            raise ValueError(verdict.get('feedback','Critic rejected the candidate'))
        additions=[]
        for request in n['source_requests'][:3]:
            additions.append(await market.download_source(request))
        n['sources']=n.get('sources',[])+additions
        n['feedback']=verdict.get('feedback','Approved')
        n['status']='evaluating'
        c['phase']='evaluate'
        persist(c,f"Critic approved {n['id']}; scope and static leakage checks passed.",'critic')
    except (ValueError, SyntaxError) as exc:
        n['feedback']=str(exc)
        n['notebook']=notebooks.commit(c['id'],n['id'],n['attempt'],candidate,'rejected')
        if not c['config']['critic_retries'] or n['attempt']<c['config']['critic_retries']:
            n['attempt']+=1
            n['status']='developing'
            c['phase']='develop'
        else:
            n['status']='rejected'
            c['explored']+=1
            c['phase']='select'
            search.backpropagate(c,n,0.)
        persist(c,f"Critic rejected {n['id']}: {str(exc)[:250]}",'rejected')

async def evaluate_node(c,n):
    try:
        result=await run_evaluation(c,n)
    except ValueError as exc:
        n['status']='failed'
        n['feedback']=str(exc)
        if n['notebook']:
            n['notebook']=notebooks.commit(c['id'],n['id'],n['attempt'],notebooks.read(c['id'],n['notebook']),'failed')
        search.backpropagate(c,n,0.)
        c['explored']+=1
        c['phase']='select'
        persist(c,f"Evaluation failed for {n['id']}: {str(exc)[:250]}",'failed')
        return
    n['metrics']=result['metrics']
    n['curve']=result['curve']
    n['notebook']=result['notebook']
    n['score']=search.score(n['metrics'],c['config']['objective'])
    parent=getnode(c,n['eval_parent'])
    n['reward']=n['score']-parent['score']
    n['status']='completed' if n['reward']>=0 else 'negative'
    search.backpropagate(c,n,n['reward'])
    c['explored']+=1
    c['pareto']=search.pareto(c['nodes'])
    old_best=c['best']
    if n['score']>getnode(c,old_best)['score']:
        c['best']=n['id']
        # Merge each current Pareto member into the new champion. Merge nodes are evaluated through the same loop.
        if n['kind']!='merge':
            for pid in c['pareto']:
                if pid==n['id']:
                    continue
                mid='m-'+notebooks.fingerprint([n['id'],pid])[:12]
                if any(x['id']==mid for x in c['nodes']):
                    continue
                m=node(mid,pid,None,'Combine complementary Pareto pipeline improvements',f"Combine improvements from {n['id']} and {pid} while preserving causality.",kind='merge',eval_parent=pid)
                m['base_notebook']=n['notebook']
                m['sources']=n.get('sources',[])+[s for s in getnode(c,pid).get('sources',[]) if s['sha256'] not in {v['sha256'] for v in n.get('sources',[])}]
                m['merge_parents']=[pid,n['id']]
                c['nodes'].append(m)
                c['merge_queue'].append(mid)
    if n['kind']=='merge':
        # Keep the evaluated merge attempt in the first-parent history and link the other input.
        refs=[n['notebook']['sha'],getnode(c,n['merge_parents'][0])['notebook']['sha']]
        n['notebook']=notebooks.commit(c['id'],n['id'],n['attempt'],notebooks.read(c['id'],n['notebook']),'merged',refs)
    c['phase']='research' if n['kind']!='merge' else 'select'
    persist(c,f"{n['id']} scored {n['score']:.4f}; parent gain {n['reward']:+.4f}.",'evaluation')

async def research(c,n):
    comparison=getnode(c,n['eval_parent'])
    current_nb=notebooks.read(c['id'],n['notebook'])
    comparison_nb=notebooks.read(c['id'],comparison['notebook'])
    alternatives=[{'summary':x['summary'],'hypothesis':x['hypothesis'],'status':x['status'],'gain':x['reward']}
                  for x in c['nodes'] if x['kind']=='hypothesis' and x['category']==n['category'] and x['id']!=n['id']][-24:]
    h=await provider.agent(c['id'],f"{n['id']}:research",'researcher',{
        'task':'Propose one distinct experiment in the same category. Choose child to build on the evaluated notebook, sibling to try an alternative from its comparison parent, or none if there is no new test. Avoid repeated or tracking-only edits that do not test a hypothesis.',
        'category':n['category'],'hypothesis':n['hypothesis'],'metrics':n['metrics'],'parent_gain':n['reward'],
        'notebook':current_nb,'comparison_parent':{'id':comparison['id'],'metrics':comparison['metrics'],
            'category_source':notebooks.source(next(cell for cell in comparison_nb['cells'] if cell['id']==n['category']))},
        'nearby_hypotheses':alternatives,'user_prompt':c['config']['prompt']})
    placement=h.get('placement')
    hypothesis=h.get('hypothesis')
    c['phase']='select'
    if placement not in ('child','sibling') or not isinstance(hypothesis,str) or not hypothesis.strip():
        persist(c,f"Researcher proposed no further experiment for {n['id']}.",'research')
        return
    target=n if placement=='child' else comparison
    tree_parent=n['id'] if placement=='child' else n['parent']
    fingerprint=lambda text:' '.join(text.casefold().split()).strip(' .!?')
    label=summary(h.get('summary'),summary(hypothesis,f"Test alternative {n['category']} configuration"))
    if any(x['category']==n['category'] and x['kind']=='hypothesis' and (
            fingerprint(x['hypothesis'])==fingerprint(hypothesis) or fingerprint(x['summary'])==fingerprint(label)) for x in c['nodes']):
        persist(c,f"Researcher skipped a repeated proposal for {n['id']}.",'research')
        return
    followup=node('h-'+notebooks.fingerprint([n['id'],'research-'+placement])[:12],tree_parent,n['category'],label,hypothesis.strip(),eval_parent=target['id'])
    followup.update(base_notebook=target['notebook'],sources=copy.deepcopy(target.get('sources',[])),proposed_by=n['id'],proposal_placement=placement)
    c['nodes'].append(followup)
    persist(c,f"Researcher proposed a {placement} of {n['id']}.",'research')

async def finalize(c):
    # Selection is written BEFORE opening the holdout. Retries never select based on holdout results.
    n=getnode(c,c['selected'])
    result=await run_evaluation(c,n,holdout=True)
    c['holdout']={'node':n['id'],'metrics':result['metrics'],'curve':result['curve'],'assumptions':result['assumptions'],'opened_at':time.time(),'selection_frozen_at':c['selection_frozen_at']}
    c['phase']='done'
    c['status']='completed'
    c['active_node']=None
    persist(c,'Final artifact evaluated once on sealed holdout; campaign complete.','holdout')

async def step(cid):
    c=store.load(cid)
    if c['status'] in {'completed','stopped'}:
        return True
    if c['stop_requested'] and not c['best']:
        c['status']='stopped';c['phase']='done';c['active_node']=None
        c['final_reason']='Stopped before the baseline was evaluated; no final model exists.'
        persist(c,c['final_reason'],'campaign')
        return True
    if c['stop_requested'] and c['phase'] not in {'finalize','done','prepare','calibrate','baseline'}:
        c['final_reason']='Stopped by user; selecting best evaluated pipeline.'
        c['selected']=c['best']; c['selection_frozen_at']=time.time(); c['phase']='finalize'
        persist(c,'Stop requested. Search stopped; reserved final holdout will run.','campaign')
        return False
    phase=c['phase']
    if phase in {'seed','develop','critic','research'} and not uses_llm(c['config']):
        raise ValueError(provider.LEGACY_CAMPAIGN_MESSAGE)
    try:
        if phase=='prepare':
            provider.validate_agents(c['config'],store.settings())
            await prepare(c)
        elif phase=='calibrate':
            result=await run_evaluation(c,getnode(c,'baseline'),calibrate=True)
            c['calibration']=calibrate_compute(c['config'],result['metrics'],len(c['dataset_ids']))
            c['phase']='baseline'
            persist(c,'Baseline speed measured; bounded evaluation window selected.','calibration')
        elif phase=='baseline':
            n=getnode(c,'baseline')
            result=await run_evaluation(c,n)
            n.update(metrics=result['metrics'],curve=result['curve'],notebook=result['notebook'],status='completed',score=search.score(result['metrics'],c['config']['objective']),reward=0.)
            c['best']='baseline'; c['pareto']=['baseline']; c['phase']='seed'
            persist(c,'Baseline evaluated; reference score established.','evaluation')
        elif phase=='seed':
            await seed_hypotheses(c)
        elif phase=='select':
            live=store.load(cid)
            expected,reserve=compute_estimates(live)
            limit=reached(c['explored'],c['config']['max_nodes']) or (c['config']['evaluation_budget_seconds'] and live['evaluation_spent']+expected+reserve>c['config']['evaluation_budget_seconds']) or reached(live['llm_spent'],c['config']['llm_budget_usd']) or reached(live['tokens_spent'],c['config']['llm_token_budget'])
            epsilon=search.temperature(live)
            priority_merge=bool(c['merge_queue'])
            nid=None if limit else (c['merge_queue'].pop(0) if priority_merge else search.select(c,epsilon))
            if not nid:
                c['final_reason']='Search limit reached' if limit else 'Hypothesis frontier exhausted'
                c['selected']=c['best']; c['selection_frozen_at']=time.time(); c['phase']='finalize'
                persist(c,'Search ended; final model selection frozen before holdout.','campaign')
            else:
                n=getnode(c,nid)
                # Explicit follow-ups test their chosen parent. Only initial LLM seeds may inherit the champion.
                if n['kind']=='hypothesis' and not n.get('proposed_by') and n['parent']=='category-'+str(n['category']) and c['best']!=n['eval_parent']:
                    n['inherited_from']=c['best']
                    n['base_notebook']=getnode(c,c['best'])['notebook']
                    n['sources']=getnode(c,c['best']).get('sources',[])
                n['status']='developing'; c['active_node']=nid; c['phase']='develop'
                c['selection']={'node':nid,'temperature':epsilon,'mode':'merge' if priority_merge else 'search'}
                persist(c,f"Selected {nid} from the merge queue." if priority_merge else f"Selected {nid} at exploration {epsilon:.3f}.",'selection')
        elif phase=='develop':
            await develop(c,getnode(c,c['active_node']))
        elif phase=='critic':
            await critic(c,getnode(c,c['active_node']))
        elif phase=='evaluate':
            await evaluate_node(c,getnode(c,c['active_node']))
        elif phase=='research':
            await research(c,getnode(c,c['active_node']))
        elif phase=='finalize':
            await finalize(c)
        else:
            raise ValueError('Unknown campaign phase: '+phase)
    except provider.BudgetExhausted as exc:
        c=store.load(cid)
        if c['best'] and c['phase']!='finalize':
            c['selected']=c['best']; c['selection_frozen_at']=time.time(); c['phase']='finalize'; c['final_reason']=str(exc)
            persist(c,str(exc)+' Selecting frozen winner for holdout.','budget')
        else:
            raise
    return store.load(cid)['phase']=='done'
