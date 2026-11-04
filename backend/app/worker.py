import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
import httpx
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker
from . import engine, store, telemetry
from .workflows import CampaignWorkflow, RefreshWorkflow
from .refresh import create_refresh, record_refresh, finish_refresh

@activity.defn(name='campaign_step')
async def campaign_step(cid:str)->bool:
    async def heartbeat():
        while True:
            activity.heartbeat(store.load(cid)['phase'])
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
        worker=Worker(client,task_queue='capital-research',workflows=[CampaignWorkflow,RefreshWorkflow],activities=[campaign_step,create_refresh,record_refresh,finish_refresh],max_concurrent_activities=1)
        await worker.run()
    finally:
        telemetry_task.cancel()

if __name__=='__main__':
    asyncio.run(main())
