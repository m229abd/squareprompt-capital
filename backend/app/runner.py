import asyncio
import json
import sys
import time
from fastapi import FastAPI, HTTPException
import os
import signal
import psutil

app=FastAPI(title='Isolated notebook runner')
lock=asyncio.Semaphore(1)

@app.get('/health')
def health():
    return {'status':'ok'}

@app.post('/evaluate')
async def run(payload:dict):
    timeout=payload.get('execution_budget_seconds')
    timeout=float(timeout) if timeout else None
    if timeout is not None and (timeout<1 or timeout>864000):
        raise HTTPException(400,'Invalid timeout')
    async with lock:
        started=time.monotonic()
        process=await asyncio.create_subprocess_exec(sys.executable,'-m','app.run_notebook',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,start_new_session=True)
        try:
            task=process.communicate(json.dumps(payload).encode())
            stdout,stderr=await asyncio.wait_for(task,timeout=max(.001,timeout-(time.monotonic()-started))) if timeout is not None else await task
            if process.returncode:
                raise HTTPException(422,stderr.decode()[-4000:])
            result=json.loads(stdout)
            elapsed=time.monotonic()-started
            metrics=result['metrics']
            metrics['startup_seconds']=metrics.get('startup_seconds',0)+max(0,elapsed-metrics['elapsed_seconds'])
            metrics['elapsed_seconds']=elapsed
            return result
        except asyncio.TimeoutError:
            await terminate_evaluation(process)
            raise HTTPException(422,'Campaign compute allocation exhausted; incomplete evaluation discarded.')


async def terminate_evaluation(process):
    # Jupyter starts a separate session, so killing just the evaluator's process group is insufficient.
    try:
        children=psutil.Process(process.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        children=[]
    for child in reversed(children):
        try: child.kill()
        except psutil.NoSuchProcess: pass
    try: os.killpg(process.pid,signal.SIGKILL)
    except ProcessLookupError: pass
    await process.wait()
    if children:
        await asyncio.to_thread(psutil.wait_procs,children,timeout=2)
