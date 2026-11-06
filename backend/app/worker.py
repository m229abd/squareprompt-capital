import asyncio
import os
import httpx
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker
from . import engine, history, store, telemetry
from .workflows import CampaignWorkflow, RefreshWorkflow
from .refresh import create_refresh, prepare_refresh_campaign, record_refresh, finish_refresh

async def execute_step(cid):
    async def heartbeat():
        while True:
            with store.connect() as db:
                row=db.execute("""SELECT json_extract(body,'$.phase') AS phase,
                                  json_extract(body,'$.step') AS step,
                                  json_extract(body,'$.active_node') AS node
                                  FROM campaigns WHERE id=?""",(cid,)).fetchone()
            activity.heartbeat({'campaign_id':cid,**dict(row),'attempt':activity.info().attempt})
            await asyncio.sleep(5)
    task=asyncio.create_task(heartbeat())
    try:
        if store.load(cid).get('pause_requested'):
            return False
        return await engine.step(cid)
    except (ValueError,KeyError,SyntaxError) as exc:
        engine.fail(cid,exc)
        raise ApplicationError(str(exc),type='ConfigurationError',non_retryable=True) from exc
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {400,401,403,404,422}:
            engine.fail(cid,exc)
            raise ApplicationError(str(exc),type='ConfigurationError',non_retryable=True) from exc
        raise
    finally:
        task.cancel()

@activity.defn(name='campaign_step')
async def campaign_step(cid:str)->bool:
    return await execute_step(cid)


async def execute_checkpoint(request):
    cid=request['campaign_id']
    before=history.checkpoint(store.load(cid))
    if before['step']<request['step']:
        raise ApplicationError('Saved campaign checkpoint is behind the workflow input.',type='ConfigurationError',non_retryable=True)
    if before['step']>request['step']:
        outcome='recovered'
    elif before['status']=='paused':
        outcome='paused'
    else:
        await execute_step(cid)
        outcome='advanced'
    campaign=store.load(cid)
    state=history.checkpoint(campaign)
    if state['step']==before['step'] and state['status']=='paused':
        outcome='paused'
    done=state['phase']=='done'
    result={'done':done,'outcome':outcome,'performed_phase':request['phase'],'checkpoint':state}
    if done:
        result['result']=history.campaign_result(campaign,state)
    return result


def checkpoint_activity(phase):
    async def run(request:dict)->dict:
        return await execute_checkpoint(request)
    return activity.defn(name=f'campaign_{phase}')(run)


CHECKPOINT_ACTIVITIES=[checkpoint_activity(phase) for phase in (*history.PHASES,'checkpoint')]

async def main():
    store.init()
    while True:
        try:
            client=await Client.connect(os.getenv('TEMPORAL_ADDRESS','temporal:7233'))
            break
        except Exception:
            await asyncio.sleep(3)
    async def telemetry_loop():
        while True:
            await asyncio.to_thread(telemetry.flush)
            await asyncio.sleep(10)
    telemetry_task=asyncio.create_task(telemetry_loop())
    try:
        worker=Worker(client,task_queue='capital-research',workflows=[CampaignWorkflow,RefreshWorkflow],activities=[campaign_step,*CHECKPOINT_ACTIVITIES,create_refresh,prepare_refresh_campaign,record_refresh,finish_refresh],max_concurrent_activities=1)
        await worker.run()
    finally:
        telemetry_task.cancel()

if __name__=='__main__':
    asyncio.run(main())
