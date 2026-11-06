import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from functools import lru_cache
from threading import Lock
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError
from . import engine, history, notebooks, provider, search, store
from .constants import CATEGORIES, uses_llm
from .market import discover
from .schemas import CampaignConfig, ProviderConfig, RefreshScheduleRequest
from .workflows import CampaignWorkflow, RefreshWorkflow

async def temporal():
    return await Client.connect(os.getenv('TEMPORAL_ADDRESS','temporal:7233'))

async def dispatch(cid):
    client=await temporal()
    try:
        await client.start_workflow(CampaignWorkflow.run,history.campaign_input(store.load(cid)),id='campaign-'+cid,task_queue='capital-research')
    except WorkflowAlreadyStartedError:
        pass

@asynccontextmanager
async def lifespan(app):
    store.init()
    async def reconcile():
        while True:
            with store.connect() as db:
                starting=[r['id'] for r in db.execute("SELECT id FROM campaigns WHERE json_extract(body,'$.status')='starting'")]
            for cid in starting:
                try:
                    await dispatch(cid)
                except Exception:
                    pass
            with store.connect() as db:
                schedules=[json.loads(r['body']) for r in db.execute('SELECT body FROM schedules')]
            for s in schedules:
                if s['status']=='active':
                    try:
                        client=await temporal()
                        await client.start_workflow(RefreshWorkflow.run,s,id='refresh-'+s['id'],task_queue='capital-research')
                    except Exception:
                        pass
            await asyncio.sleep(5)
    task=asyncio.create_task(reconcile())
    yield
    task.cancel()

app=FastAPI(title='Squareprompt Capital',version='0.1.0',lifespan=lifespan)

def require(cid):
    try:
        return store.load(cid)
    except KeyError:
        raise HTTPException(404,'Campaign not found')

def public(c):
    c['temperature']=search.temperature(c)
    c['evaluation_spent']=max(0,c['evaluation_spent']-c.get('evaluation_reserved',0))
    for n in c['nodes']:
        if 'sources' in n:
            n['sources']=[{k:v for k,v in s.items() if k!='records'} for s in n['sources']]
        if n['id'] not in {c.get('best'),'baseline'}:
            n.pop('curve',None)
    return c

@app.get('/api/health')
async def health():
    with store.connect() as db:
        pending=db.execute('SELECT count(*) FROM outbox WHERE sent=0').fetchone()[0]
    return {'status':'ok','pending_telemetry':pending,'version':'0.1.0'}

@app.get('/api/observability/opik')
async def opik_status():
    try:
        async with httpx.AsyncClient(timeout=2,trust_env=False) as client:
            response=await client.get('http://opik-ui:5174/api/health-check')
            online=response.status_code==200
    except httpx.RequestError:
        online=False
    return {'online':online,'url':'http://localhost:5174'}


_projection_lock=Lock()

@lru_cache(maxsize=128)
def campaign_card(root,cid,updated):
    with store.connect() as db:
        row=db.execute("""
            SELECT json_remove(c.body,'$.nodes','$.holdout.curve','$.execution_costs') AS body,
                (SELECT json_remove(n.value,'$.curve','$.sources','$.source_requests')
                 FROM json_each(c.body,'$.nodes') AS n
                 WHERE json_extract(n.value,'$.id')=json_extract(c.body,'$.best') LIMIT 1) AS best_node
            FROM campaigns AS c WHERE c.id=?
        """,(cid,)).fetchone()
    card=json.loads(row['body'])
    card['nodes']=[json.loads(row['best_node'])] if row['best_node'] else []
    return public(card)


@lru_cache(maxsize=3)
def campaign_detail(root,cid,updated):
    with store.connect() as db:
        row=db.execute("""
            SELECT json_set(c.body,'$.nodes',json((
                SELECT json_group_array(json_set(
                    CASE WHEN json_extract(n.value,'$.id') IN ('baseline',json_extract(c.body,'$.best'))
                         THEN n.value ELSE json_remove(n.value,'$.curve') END,
                    '$.sources',json((SELECT json_group_array(json_remove(s.value,'$.records'))
                                      FROM json_each(n.value,'$.sources') AS s))
                ))
                FROM json_each(c.body,'$.nodes') AS n
            ))) AS body FROM campaigns AS c WHERE c.id=?
        """,(cid,)).fetchone()
    return public(json.loads(row['body']))


def campaign_index(db,selected=None,*,include_detail=True):
    entries=db.execute('SELECT id,updated FROM campaigns ORDER BY updated DESC').fetchall()
    ids={r['id'] for r in entries}
    selected=(selected if selected in ids else entries[0]['id']) if include_detail and entries else None
    # Serialize cold projection reads across tabs; reuse the projection until the row changes.
    with _projection_lock:
        root=str(store.ROOT)
        return [(campaign_detail if row['id']==selected else campaign_card)(root,row['id'],row['updated']) for row in entries]


@lru_cache(maxsize=1)
def dataset_catalog(root,revision):
    with store.connect() as db:
        return [json.loads(row['body']) for row in db.execute(
            "SELECT json_remove(body,'$.records') AS body FROM cache WHERE id LIKE 'candles-%' OR id LIKE 'demo-%'")]


@app.get('/api/overview')
def overview(campaign_id:str|None=None):
    with store.connect() as db:
        campaigns=campaign_index(db,campaign_id)
        events=[dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 16')]
        revision=tuple(tuple(row) for row in db.execute(
            "SELECT id,rowid FROM cache WHERE id LIKE 'candles-%' OR id LIKE 'demo-%' ORDER BY id"))
        with _projection_lock:
            cache=dataset_catalog(str(store.ROOT),revision)
        outbox=[dict(r) for r in db.execute('SELECT id,sent,error FROM outbox WHERE sent=0 LIMIT 10')]
    return JSONResponse({'campaigns':campaigns,'events':events,'datasets':cache,'telemetry':outbox,'categories':[{'id':k,'name':n,'description':d} for k,n,d in CATEGORIES]})


@app.get('/api/campaigns')
def campaigns():
    with store.connect() as db:
        return JSONResponse(campaign_index(db,include_detail=False))

@app.post('/api/campaigns',status_code=201)
async def create_campaign(config:CampaignConfig):
    settings=config.model_dump()
    try:
        provider.validate_agents(settings,store.settings())
    except ValueError as exc:
        raise HTTPException(422,str(exc))
    if config.execution_snapshot_id:
        from .market import load_execution_snapshot
        try: load_execution_snapshot(config.execution_snapshot_id,settings)
        except ValueError as exc: raise HTTPException(422,str(exc))
    c=engine.create(settings)
    # The durable dispatch reconciler recovers an API crash between database write and workflow start.
    try:
        await asyncio.wait_for(dispatch(c['id']),timeout=5)
    except Exception:
        pass
    return public(c)

@app.get('/api/campaigns/{cid}')
def campaign(cid:str):
    with store.connect() as db:
        row=db.execute('SELECT updated FROM campaigns WHERE id=?',(cid,)).fetchone()
    if row is None:
        raise HTTPException(404,'Campaign not found')
    with _projection_lock:
        detail=campaign_detail(str(store.ROOT),cid,row['updated'])
    return JSONResponse(detail)

@app.get('/api/campaigns/{cid}/benchmarks')
def campaign_benchmarks(cid:str):
    from .benchmarks import development_benchmarks
    from .market import INTERVAL_MS
    from .compute import WARMUP
    key=f'benchmarks-v2:{cid}'
    with store.connect() as db:
        cached=db.execute('SELECT body FROM cache WHERE id=?',(key,)).fetchone()
        if cached:
            return json.loads(cached['body'])
        row=db.execute("""SELECT json_extract(body,'$.config') AS config,
                               json_extract(body,'$.dataset_ids') AS datasets,
                               json_extract(body,'$.calibration') AS calibration,
                               json_extract(body,'$.nodes[0].curve') AS curve
                        FROM campaigns WHERE id=?""",(cid,)).fetchone()
    if row is None:
        raise HTTPException(404,'Campaign not found')
    c={'id':cid,'config':json.loads(row['config']),'dataset_ids':json.loads(row['datasets']),
       'calibration':json.loads(row['calibration']) if row['calibration'] else None}
    times=[point['time'] for point in json.loads(row['curve'] or '[]')]
    if not times and not c['calibration']:
        raise HTTPException(409,'Buy & hold is available after the development window is calibrated.')
    try:
        datasets=engine.datasets(c)
        if not times and datasets:
            times=[point['time'] for point in datasets[0]['records'][WARMUP:]]
        result=development_benchmarks(c['config'],datasets,INTERVAL_MS[c['config']['interval']],times)
    except (KeyError,TypeError,ValueError) as exc:
        raise HTTPException(409,f'Development benchmarks unavailable: {exc}') from exc
    with store.connect() as db:
        db.execute('INSERT OR IGNORE INTO cache VALUES(?,?)',(key,json.dumps(result,allow_nan=False)))
    return result

@app.post('/api/campaigns/{cid}/{action}')
async def control(cid:str,action:str):
    if action not in {'pause','resume','stop'}:
        raise HTTPException(404,'Unknown action')
    c=require(cid)
    if c['status'] in {'completed','stopped'}:
        raise HTTPException(409,'Completed campaigns are immutable. Start a new campaign for further research.')
    if action=='resume' and not uses_llm(c['config']):
        raise HTTPException(409,provider.LEGACY_CAMPAIGN_MESSAGE)
    client=await temporal()
    handle=client.get_workflow_handle('campaign-'+cid)
    with store.transaction() as db:
        c=store.load(cid,db)
        if action=='pause':
            c['pause_requested']=True
            c['status']='paused'
        elif action=='resume':
            c['pause_requested']=False
            c['status']='running'
            c['error']=None
        else:
            c['stop_requested']=True
            c['pause_requested']=False
        store.save(c,db)
        store.event(cid,'control',f'{action.capitalize()} requested. In-flight work completes at the next durable boundary.',db)
    await handle.signal('pause' if action=='pause' else 'resume')
    return public(store.load(cid))

@app.patch('/api/campaigns/{cid}/budgets')
def topup(cid:str,body:dict):
    require(cid)
    with store.transaction() as db:
        c=store.load(cid,db)
        if c['status'] in {'completed','stopped'}:
            raise HTTPException(409,'Completed campaigns are immutable')
        cfg=c['config'].copy()
        for key in ['llm_budget_usd','llm_token_budget','evaluation_budget_seconds','evaluation_seconds','max_nodes']:
            if key in body:
                if cfg[key]==0 and body[key]!=0 or body[key]!=0 and body[key]<cfg[key]:
                    raise HTTPException(422,'Budgets may only be increased during a campaign')
                cfg[key]=body[key]
        c['config']=CampaignConfig.model_validate(cfg).model_dump()
        store.save(c,db)
    return public(c)

@app.get('/api/campaigns/{cid}/nodes/{nid}/notebook')
def download_notebook(cid:str,nid:str):
    c=require(cid)
    try:
        n=engine.getnode(c,nid)
    except StopIteration:
        raise HTTPException(404,'Node not found')
    if not n.get('notebook'):
        raise HTTPException(404,'Notebook not created yet')
    return Response(json.dumps(notebooks.read(cid,n['notebook']),indent=1,ensure_ascii=False),media_type='application/x-ipynb+json',headers={'Content-Disposition':f'attachment; filename="{cid}-{nid}.ipynb"'})

@app.get('/api/campaigns/{cid}/events')
def events(cid:str):
    require(cid)
    with store.connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM events WHERE campaign=? ORDER BY id DESC LIMIT 200',(cid,))]

@app.get('/api/calls')
def calls():
    with store.connect() as db:
        return [{'id':r['id'],'campaign':r['campaign'],'role':r['role'],'status':r['status'],**json.loads(r['body'])} for r in db.execute('SELECT * FROM calls ORDER BY rowid DESC LIMIT 100')]

@app.get('/api/provider')
def provider_settings():
    cfg=store.settings()
    return {**cfg,'api_key':'','has_key':bool(cfg.get('api_key'))}

@app.put('/api/provider')
def save_provider(body:ProviderConfig):
    from urllib.parse import urlparse
    parsed=urlparse(body.base_url)
    if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(422,'Enter an HTTP(S) provider base URL without credentials or query parameters')
    if parsed.scheme=='http' and parsed.hostname not in {'localhost','127.0.0.1','host.docker.internal'}:
        raise HTTPException(422,'Remote providers must use HTTPS')
    cfg=store.settings()
    if cfg.get('base_url')!=body.base_url.rstrip('/'):
        cfg['models']=[]
        cfg.pop('default_agents',None)
    cfg['base_url']=body.base_url.rstrip('/')
    if body.api_key:
        cfg['api_key']=body.api_key
    with store.connect() as db:
        db.execute('INSERT OR REPLACE INTO settings VALUES(1,?)',(json.dumps(cfg),))
    return {'saved':True}

@app.post('/api/provider/models')
async def models():
    try:
        return await provider.discover_models()
    except (ValueError,httpx.HTTPError) as exc:
        raise HTTPException(422,str(exc))

@app.put('/api/provider/pricing')
def pricing(body:dict):
    cfg=store.settings()
    model=next((m for m in cfg.get('models',[]) if m['id']==body.get('model')),None)
    if model is None:
        raise HTTPException(404,'Discover the model before setting verified pricing')
    for key in ['input_price_per_token','output_price_per_token']:
        val=float(body[key])
        if not 0<=val<=1:
            raise HTTPException(422,'Pricing must be USD per token, between 0 and 1')
        model[key]=val
    model['price_source']='user-verified'
    with store.connect() as db:
        db.execute('INSERT OR REPLACE INTO settings VALUES(1,?)',(json.dumps(cfg),))
    return {'saved':True}

@app.get('/api/markets')
async def markets(refresh:bool=False):
    try:
        return await discover(force=refresh)
    except Exception as exc:
        raise HTTPException(502,'Market discovery failed: '+str(exc))

@app.get('/api/baselines')
def baselines():
    return [{'name':p.name,'bytes':p.stat().st_size} for p in [*notebooks.BASELINE_DIR.glob('*.ipynb'),*(store.ROOT/'baselines').glob('*.ipynb')]]

@app.post('/api/baselines',status_code=201)
def upload_baseline(body:dict):
    try:
        nb=body['notebook']
        notebooks.validate(nb)
        name=notebooks.fingerprint(nb)[:16]+'.ipynb'
        # User baselines persist in the managed data volume rather than changing the image.
        path=store.ROOT/'baselines'
        path.mkdir(exist_ok=True)
        (path/name).write_text(json.dumps(nb))
        return {'name':name}
    except Exception as exc:
        raise HTTPException(422,str(exc))


@app.get('/api/schedules')
def schedules():
    with store.connect() as db:
        return [json.loads(r['body']) for r in db.execute('SELECT body FROM schedules ORDER BY rowid DESC')]

@app.post('/api/schedules',status_code=201)
async def schedule_refresh(body:RefreshScheduleRequest):
    parent=require(body.campaign)
    if parent['status']!='completed' or not parent['selected']:
        raise HTTPException(422,'Complete the initial campaign before scheduling interval research refreshes.')
    try:
        provider.validate_agents(parent['config'],store.settings())
    except ValueError as exc:
        raise HTTPException(422,str(exc))
    interval=body.interval_hours;runs=body.max_runs
    sid=uuid.uuid4().hex[:12]
    schedule={'history_version':2,'id':sid,'campaign':parent['id'],'interval_hours':interval,'max_runs':runs,'completed_runs':0,'campaigns':[],'status':'active','next_at':time.time()+interval*3600,'reserved_llm_ceiling':parent['config']['llm_budget_usd']*runs or None,'reserved_eval_ceiling':parent['config']['evaluation_budget_seconds']*runs or None}
    with store.transaction() as db:
        db.execute('INSERT INTO schedules VALUES(?,?)',(sid,json.dumps(schedule)))
    try:
        client=await temporal()
        await client.start_workflow(RefreshWorkflow.run,schedule,id='refresh-'+sid,task_queue='capital-research')
    except Exception as exc:
        # The record persists; the API reconciler also dispatches refresh schedules.
        pass
    return schedule

@app.post('/api/schedules/{sid}/{action}')
async def schedule_control(sid:str,action:str):
    if action not in {'pause','resume','stop'}:raise HTTPException(404,'Unknown schedule action')
    with store.transaction() as db:
        row=db.execute('SELECT body FROM schedules WHERE id=?',(sid,)).fetchone()
        if not row:raise HTTPException(404,'Schedule not found')
        s=json.loads(row['body'])
        s['status']={'pause':'paused','resume':'active','stop':'stopped'}[action]
        db.execute('UPDATE schedules SET body=? WHERE id=?',(json.dumps(s),sid))
    client=await temporal()
    await client.get_workflow_handle('refresh-'+sid).signal(action)
    return s


@app.post('/api/execution-quotes')
async def execution_quotes(config:CampaignConfig):
    from .market import execution_snapshot
    try: return await execution_snapshot(config.model_dump())
    except (ValueError,httpx.HTTPError) as exc: raise HTTPException(422,str(exc))

@app.get('/api/evaluation-defaults')
def evaluation_defaults(target_seconds:int=Query(15,ge=0,le=3600),market_count:int=Query(1,ge=1,le=200),
                        mode:str='demo',holdout_fraction:float=Query(.2,ge=.1,le=.4),
                        embargo_bars:int=Query(3,ge=1,le=50)):
    from .compute import suggested_history
    if mode not in {'demo','live'}:
        raise HTTPException(422,'Unknown campaign mode')
    reference=None
    if target_seconds:
        with store.connect() as db:
            reference=db.execute("""
                SELECT id,json_extract(body,'$.calibration.seconds_per_portfolio_bar') AS bar_seconds,
                          COALESCE(json_extract(body,'$.calibration.startup_seconds'),0) AS startup_seconds,
                          json_array_length(body,'$.config.markets') AS market_count
                FROM campaigns
                WHERE json_extract(body,'$.config.mode')=? AND bar_seconds>0
                ORDER BY updated DESC LIMIT 1
            """,(mode,)).fetchone()
    bars=suggested_history(target_seconds,market_count,holdout_fraction,embargo_bars,reference)
    note=('Maximum requested history; all selected development bars are used.' if not target_seconds else
          'Time-based suggestion from a previous pilot. This campaign measures its own speed.' if reference else
          'Time-based estimate. The baseline pilot measures this campaign’s speed.')
    return {'bars':bars,'interval':'1h','holdout_fraction':.2,'embargo_bars':3,'measured':reference is not None,
            'reference_campaign':reference['id'] if reference else None,'note':note}

@app.put('/api/provider/agents')
def remember_agents(body:dict):
    from .schemas import AgentConfig
    required={'developer','researcher','critic'}
    if not required<=set(body)<=required|{'initializer'}:raise HTTPException(422,'Set Developer, Researcher and Critic; Pool initialization is optional.')
    cfg=store.settings()
    selected={r:AgentConfig.model_validate(v).model_dump() for r,v in body.items()}
    for role,choice in selected.items():
        if role=='initializer' and not choice['model']:continue
        model=next((m for m in cfg.get('models',[]) if m['id']==choice['model']),None)
        if not model:raise HTTPException(422,f'{role}: choose a discovered model.')
    cfg['default_agents']={**cfg.get('default_agents',{}),**selected}
    with store.connect() as db:db.execute('INSERT OR REPLACE INTO settings VALUES(1,?)',(json.dumps(cfg),))
    return {'saved':True}
