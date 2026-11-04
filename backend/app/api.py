import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError
from . import engine, notebooks, provider, search, store
from .constants import CATEGORIES
from .market import discover
from .schemas import CampaignConfig, ProviderConfig
from .workflows import CampaignWorkflow, RefreshWorkflow

async def temporal():
    return await Client.connect(os.getenv('TEMPORAL_ADDRESS','temporal:7233'))

async def dispatch(cid):
    client=await temporal()
    try:
        await client.start_workflow(CampaignWorkflow.run,cid,id='campaign-'+cid,task_queue='capital-research')
    except WorkflowAlreadyStartedError:
        pass

@asynccontextmanager
async def lifespan(app):
    store.init()
    async def reconcile():
        while True:
            with store.connect() as db:
                campaigns=[json.loads(r['body']) for r in db.execute('SELECT body FROM campaigns')]
            for c in campaigns:
                if c['status']=='starting':
                    try:
                        await dispatch(c['id'])
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

@app.get('/api/overview')
def overview():
    with store.connect() as db:
        campaigns=[public(json.loads(r['body'])) for r in db.execute('SELECT body FROM campaigns ORDER BY updated DESC')]
        events=[dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 16')]
        cache=[json.loads(r['body']) for r in db.execute("SELECT body FROM cache WHERE id LIKE 'candles-%' OR id LIKE 'demo-%'")]
        outbox=[dict(r) for r in db.execute('SELECT id,sent,error FROM outbox WHERE sent=0 LIMIT 10')]
    return {'campaigns':campaigns,'events':events,'datasets':[{k:v for k,v in d.items() if k!='records'} for d in cache],'telemetry':outbox,'categories':[{'id':k,'name':n,'description':d} for k,n,d in CATEGORIES]}

@app.get('/api/campaigns')
def campaigns():
    return overview()['campaigns']

@app.post('/api/campaigns',status_code=201)
async def create_campaign(config:CampaignConfig):
    if config.mode=='live':
        cfg=store.settings()
        try:
            provider.validate_agents(config.model_dump(),cfg)
        except ValueError as exc:
            raise HTTPException(422,str(exc))
    if config.execution_snapshot_id:
        from .market import load_execution_snapshot
        try: load_execution_snapshot(config.execution_snapshot_id,config.model_dump())
        except ValueError as exc: raise HTTPException(422,str(exc))
    c=engine.create(config.model_dump())
    # The durable dispatch reconciler recovers an API crash between database write and workflow start.
    try:
        await asyncio.wait_for(dispatch(c['id']),timeout=5)
    except Exception:
        pass
    return public(c)

@app.get('/api/campaigns/{cid}')
def campaign(cid:str):
    return public(require(cid))

@app.post('/api/campaigns/{cid}/{action}')
async def control(cid:str,action:str):
    if action not in {'pause','resume','stop'}:
        raise HTTPException(404,'Unknown action')
    c=require(cid)
    if c['status'] in {'completed','stopped'}:
        raise HTTPException(409,'Completed campaigns are immutable. Start a new campaign for further research.')
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
    return FileResponse(notebooks.repo(cid)/n['notebook']['path'],filename=f'{cid}-{nid}.ipynb',media_type='application/x-ipynb+json')

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
async def schedule_refresh(body:dict):
    parent=require(body['campaign'])
    if parent['status']!='completed' or not parent['selected']:
        raise HTTPException(422,'Complete the initial campaign before scheduling interval research refreshes.')
    interval=int(body.get('interval_hours',24));runs=int(body.get('max_runs',3))
    if not 1<=interval<=720 or not 0<=runs<=100:
        raise HTTPException(422,'Interval must be 1–720 hours and runs must be 0–100 (0 means unlimited).')
    sid=uuid.uuid4().hex[:12]
    schedule={'id':sid,'campaign':parent['id'],'interval_hours':interval,'max_runs':runs,'completed_runs':0,'campaigns':[],'status':'active','next_at':time.time()+interval*3600,'reserved_llm_ceiling':parent['config']['llm_budget_usd']*runs or None,'reserved_eval_ceiling':parent['config']['evaluation_budget_seconds']*runs or None}
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
def evaluation_defaults(target_seconds:int=15,market_count:int=1,mode:str='demo'):
    import math
    if target_seconds<0 or not 1<=market_count<=200:raise HTTPException(422,'Invalid target or market count')
    with store.connect() as db: campaigns=[json.loads(r['body']) for r in db.execute('SELECT body FROM campaigns ORDER BY updated DESC')]
    reference=next((c for c in campaigns if c.get('calibration',{} ) and c['calibration'].get('seconds_per_portfolio_bar') and c['config']['mode']==mode),None)
    if target_seconds==0:bars=4900
    elif reference:
        p=reference['calibration'];cost=p['seconds_per_portfolio_bar']*(market_count/max(1,len(reference['config']['markets'])))**2
        steps=max(20,int(max(0,target_seconds-p.get('startup_seconds',0))/cost))
        bars=math.ceil((64+steps+3)/.8)
    else:bars=round(720*target_seconds/15/max(1,market_count/3))
    return {'bars':min(4900,max(150,int(bars))),'interval':'1h','holdout_fraction':.2,'embargo_bars':3,'measured':bool(reference),'reference_campaign':reference['id'] if reference else None,'note':'Suggested from a previous pilot; this campaign remeasures inside Docker.' if reference else 'Initial estimate; this campaign measures actual compute before selecting its scoring window.'}

@app.put('/api/provider/agents')
def remember_agents(body:dict):
    from .schemas import AgentConfig
    if set(body)!={'developer','researcher','critic'}:raise HTTPException(422,'Set all three agent roles.')
    cfg=store.settings()
    selected={r:AgentConfig.model_validate(v).model_dump() for r,v in body.items()}
    for role,choice in selected.items():
        model=next((m for m in cfg.get('models',[]) if m['id']==choice['model']),None)
        if not model:raise HTTPException(422,f'{role}: choose a discovered model.')
    cfg['default_agents']=selected
    with store.connect() as db:db.execute('INSERT OR REPLACE INTO settings VALUES(1,?)',(json.dumps(cfg),))
    return {'saved':True}
