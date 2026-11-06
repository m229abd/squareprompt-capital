import json
import time
import uuid
from temporalio import activity
from temporalio.exceptions import ApplicationError
from . import engine, history, notebooks, provider, store
from .constants import uses_llm

@activity.defn(name='create_refresh')
async def create_refresh(payload:dict)->str:
    cid=uuid.uuid5(uuid.NAMESPACE_URL,f"capital:{payload['schedule']}:{payload['index']}").hex[:12]
    try:
        store.load(cid)
        return cid
    except KeyError:
        pass
    parent=store.load(payload['parent'])
    if not uses_llm(parent['config']):
        raise ApplicationError(provider.LEGACY_CAMPAIGN_MESSAGE,type='ConfigurationError',non_retryable=True)
    winner=engine.getnode(parent,parent['selected'])
    nb=notebooks.read(parent['id'],winner['notebook'])
    name=notebooks.fingerprint(nb)[:16]+'.ipynb'
    path=store.ROOT/'baselines';path.mkdir(exist_ok=True)
    (path/name).write_text(json.dumps(nb))
    cfg=parent['config'].copy()
    cfg['baseline']=name
    cfg['execution_snapshot_id']=''
    cfg['registry_name']=cfg.get('registry_name') or 'squareprompt-'+parent['id']
    cfg['name']=parent['name'].split(' · refresh ')[0]+f" · refresh {payload['index']+1}"
    return engine.create(cfg,cid,initial_status='scheduled')['id']

@activity.defn(name='prepare_refresh_campaign')
async def prepare_refresh_campaign(payload:dict)->dict:
    cid=await create_refresh(payload)
    return history.campaign_input(store.load(cid))


@activity.defn(name='record_refresh')
async def record_refresh(payload:dict)->dict|None:
    with store.transaction() as db:
        s=json.loads(db.execute('SELECT body FROM schedules WHERE id=?',(payload['schedule'],)).fetchone()['body'])
        if payload['campaign'] not in s['campaigns']:
            s['campaigns'].append(payload['campaign'])
        s['completed_runs']=payload['index']+1
        s['next_at']=time.time()+s['interval_hours']*3600
        db.execute('UPDATE schedules SET body=? WHERE id=?',(json.dumps(s),s['id']))
    if payload.get('history_version')==2:
        return {'schedule_id':s['id'],'campaign_id':payload['campaign'],'completed_runs':s['completed_runs'],'next_at':s['next_at']}

@activity.defn(name='finish_refresh')
async def finish_refresh(sid:str)->None:
    with store.transaction() as db:
        s=json.loads(db.execute('SELECT body FROM schedules WHERE id=?',(sid,)).fetchone()['body'])
        if s['status']!='stopped':s['status']='completed'
        db.execute('UPDATE schedules SET body=? WHERE id=?',(json.dumps(s),sid))
