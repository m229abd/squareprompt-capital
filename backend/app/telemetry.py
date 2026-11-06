import hashlib
import json
import os
import time
from uuid import UUID
from . import store, notebooks


def opik_trace_id(ledger_id,start):
    """A stable UUIDv7 keyed by the original event time and outbox ID."""
    millis=int(start*1000)
    entropy=int.from_bytes(hashlib.sha256(ledger_id.encode()).digest()[:10],'big')
    value=(millis<<80) | (7<<76) | (((entropy>>62)&0xfff)<<64) | (2<<62) | (entropy&((1<<62)-1))
    return str(UUID(int=value))


def flush(limit=20):
    with store.connect() as db:
        rows=db.execute("SELECT id,body FROM outbox WHERE sent=0 ORDER BY json_extract(body, '$.type')='llm',rowid LIMIT ?",(limit,)).fetchall()
    for row in rows:
        item=json.loads(row['body'])
        try:
            if item['type']=='state':
                from tensorboardX import SummaryWriter
                with SummaryWriter(str(store.ROOT/'tensorboard'/item['campaign']/item['node'])) as writer:
                    writer.add_text('state/status',item['status'],item['step'])
                    writer.add_text('state/phase',item['phase'],item['step'])
                    writer.add_text('hypothesis',item['summary'],item['step'])
                    if item.get('reward') is not None:
                        writer.add_scalar('reward/parent_gain',item['reward'],item['step'])
            elif item['type']=='llm':
                import httpx
                from datetime import datetime, timezone
                start=item['time']
                body={'id':opik_trace_id(row['id'],start),'project_name':os.getenv('OPIK_PROJECT_NAME','squareprompt-capital'),'name':item['role'],'start_time':datetime.fromtimestamp(start,timezone.utc).isoformat(),'end_time':datetime.fromtimestamp(start+item.get('elapsed',0),timezone.utc).isoformat(),'input':{'messages':item['input']},'output':{'response':item.get('output'),'error':item.get('error')},'metadata':{'campaign':item['campaign'],'model':item['model'],'ledger_id':row['id'],'cost_usd':item.get('cost_usd'),'usage':item.get('usage')},'tags':[item['campaign'],item['role']],'source':'sdk'}
                response=httpx.post(os.getenv('OPIK_URL_OVERRIDE','http://opik-backend:8080').rstrip('/')+'/v1/private/traces',json=body,headers={'workspaceName':os.getenv('OPIK_WORKSPACE','default')},timeout=5)
                if response.status_code!=409:
                    response.raise_for_status()
            elif item['type']=='evaluation':
                from tensorboardX import SummaryWriter
                import mlflow
                result=item['result']
                cid=item['campaign']; nid=item['node']
                stage='holdout' if item['holdout'] else 'calibration' if item['calibrate'] else 'development'
                with SummaryWriter(str(store.ROOT/'tensorboard'/cid/nid/stage)) as writer:
                    for key,value in result['metrics'].items():
                        writer.add_scalar(key,value,0)
                    for model in result.get('models',[]):
                        for index,event in enumerate(model.get('fit_events',[])):
                            writer.add_scalar(f"models/{model['market']}/training_rows",event['rows'],index)
                            writer.add_scalar(f"models/{model['market']}/alpha",event['alpha'],index)
                            writer.add_text(f"models/{model['market']}/fit",json.dumps(event),index)
                    writer.add_text('notebook_commit',result['notebook']['sha'],0)
                mlflow.set_tracking_uri(os.getenv('MLFLOW_TRACKING_URI','http://mlflow:5000'))
                mlflow.set_experiment('squareprompt-'+cid)
                client=mlflow.MlflowClient()
                # Reuse a tagged run if retry follows a crash during artifact logging.
                exp=client.get_experiment_by_name('squareprompt-'+cid)
                matches=client.search_runs([exp.experiment_id],filter_string=f"tags.ledger_id = '{row['id']}'")
                run_id=matches[0].info.run_id if matches else None
                with mlflow.start_run(run_id=run_id,run_name=f'{nid}/{stage}') as run:
                    mlflow.set_tags({'node':nid,'campaign':cid,'stage':stage,'git_commit':result['notebook']['sha'],'ledger_id':row['id']})
                    mlflow.log_metrics(result['metrics'])
                    mlflow.log_artifact(str(notebooks.repo(cid)/result['notebook']['path']),'notebooks')
                    if result.get('models'):mlflow.log_dict({'models':result['models']},'models.json')
                    mlflow.log_dict({'curve':result['curve'],'assumptions':result['assumptions']},'evaluation.json')
                    if item['holdout']:
                        name=store.load(cid)['config'].get('registry_name') or 'squareprompt-'+cid
                        try:
                            client.create_registered_model(name)
                        except Exception:
                            pass
                        versions=list(client.search_model_versions(f"name='{name}'"))
                        version=next((v for v in versions if v.run_id==run.info.run_id),None)
                        if version is None:
                            version=client.create_model_version(name,source=f'runs:/{run.info.run_id}/notebooks',run_id=run.info.run_id,description='Research notebook artifact. Promotion requires paper/live execution validation.')
                        client.set_registered_model_alias(name,'research-winner',version.version)
                        with store.transaction() as db:
                            c=store.load(cid,db); c['model_version']={'name':name,'version':version.version,'run_id':run.info.run_id}; store.save(c,db)
            with store.connect() as db:
                db.execute('UPDATE outbox SET sent=1,error=NULL WHERE id=?',(row['id'],))
        except Exception as exc:
            with store.connect() as db:
                db.execute('UPDATE outbox SET error=? WHERE id=?',(str(exc)[:1000],row['id']))
