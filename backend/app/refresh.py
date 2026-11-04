import json
import time
import uuid
from temporalio import activity
from . import engine, notebooks, store

@activity.defn(name='create_refresh')
async def create_refresh(payload:dict)->str:
    cid=uuid.uuid5(uuid.NAMESPACE_URL,f"capital:{payload['schedule']}:{payload['index']}").hex[:12]
    try:
        store.load(cid)
        return cid
    except KeyError:
        pass
    parent=store.load(payload['parent'])
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

@activity.defn(name='record_refresh')
async def record_refresh(payload:dict)->None:
    with store.transaction() as db:
        s=json.loads(db.execute('SELECT body FROM schedules WHERE id=?',(payload['schedule'],)).fetchone()['body'])
        if payload['campaign'] not in s['campaigns']:
            s['campaigns'].append(payload['campaign'])
        s['completed_runs']=payload['index']+1
        s['next_at']=time.time()+s['interval_hours']*3600
        db.execute('UPDATE schedules SET body=? WHERE id=?',(json.dumps(s),s['id']))

@activity.defn(name='finish_refresh')
async def finish_refresh(sid:str)->None:
    with store.transaction() as db:
        s=json.loads(db.execute('SELECT body FROM schedules WHERE id=?',(sid,)).fetchone()['body'])
        if s['status']!='stopped':s['status']='completed'
        db.execute('UPDATE schedules SET body=? WHERE id=?',(json.dumps(s),sid))
