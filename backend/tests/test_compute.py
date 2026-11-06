import asyncio
import json
import os
import sys
import psutil
import pytest
from app.compute import calibrate,estimates
from app.schemas import CampaignConfig

def test_faster_compute_increases_workload_after_fixed_startup():
    cfg=CampaignConfig(markets=['BTC','SOL','HYPE'],prompt='Investigate adaptive compute',bars=1000,evaluation_seconds=12).model_dump()
    fast=calibrate(cfg,{'bars':56,'elapsed_seconds':4,'startup_seconds':2,'replay_seconds':2},3)
    slow=calibrate(cfg,{'bars':56,'elapsed_seconds':10,'startup_seconds':2,'replay_seconds':8},3)
    assert fast['evaluation_bars']==64+280
    assert slow['evaluation_bars']==64+70
    assert fast['estimated_evaluation_seconds']==pytest.approx(12)
    assert slow['estimated_evaluation_seconds']==pytest.approx(12)
    assert fast['holdout_reserve_seconds']>0

def test_unattainable_target_selects_minimum_honestly():
    cfg=CampaignConfig(markets=['BTC','SOL','HYPE'],prompt='Investigate adaptive compute',evaluation_seconds=2).model_dump()
    profile=calibrate(cfg,{'bars':56,'elapsed_seconds':12,'startup_seconds':10,'replay_seconds':2},2)
    assert profile['evaluation_bars']==84
    assert profile['estimated_evaluation_seconds']>cfg['evaluation_seconds']

def test_slower_model_updates_estimate_without_changing_protocol():
    cfg=CampaignConfig(markets=['BTC','SOL','HYPE'],prompt='Investigate adaptive compute').model_dump()
    profile=calibrate(cfg,{'bars':56,'elapsed_seconds':4,'startup_seconds':2,'replay_seconds':2},2)
    original=profile['evaluation_bars']
    c={'config':cfg,'calibration':profile,'best':'n','nodes':[{'id':'n','metrics':{'bars':80,'elapsed_seconds':30,'startup_seconds':2}}]}
    expected,reserve=estimates(c)
    assert expected>=30
    assert reserve>=2*(2+28*(720-576)/80)
    assert profile['evaluation_bars']==original

@pytest.mark.asyncio
async def test_cleanup_kills_child_with_its_own_session():
    from app.runner import terminate_evaluation
    code='import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(120)"],start_new_session=True); print(p.pid,flush=True); time.sleep(120)'
    parent=await asyncio.create_subprocess_exec(sys.executable,'-c',code,stdout=asyncio.subprocess.PIPE,start_new_session=True)
    pid=int(await parent.stdout.readline())
    assert os.getpgid(pid)!=os.getpgid(parent.pid)
    await terminate_evaluation(parent)
    try: assert psutil.Process(pid).status()==psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess: pass

@pytest.mark.asyncio
async def test_candidate_can_complete_past_target(tmp_path,monkeypatch):
    from app import engine,store,notebooks
    monkeypatch.setattr(store,'ROOT',tmp_path)
    monkeypatch.setattr(notebooks,'ROOT',tmp_path)
    store.init()
    c=engine.create(CampaignConfig(markets=['BTC','SOL','HYPE'],prompt='Investigate adaptive compute',evaluation_seconds=2,evaluation_budget_seconds=60,bars=200).model_dump())
    await engine.prepare(c)
    c=store.load(c['id'])
    baseline=engine.getnode(c,'baseline')
    nb=notebooks.read(c['id'],baseline['notebook'])
    clock=[0.]
    monkeypatch.setattr(engine.time,'monotonic',lambda:clock[0])
    class Response:
        status_code=200
        def json(self):return {'notebook':nb,'metrics':{'elapsed_seconds':5,'bars':56},'curve':[],'assumptions':[]}
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,json):
            assert 'timeout' not in json
            assert json['execution_budget_seconds']==58
            clock[0]=5
            return Response()
    monkeypatch.setattr(engine.httpx,'AsyncClient',Client)
    result=await engine.run_evaluation(c,baseline)
    assert result['metrics']['elapsed_seconds']>c['config']['evaluation_seconds']
    assert store.load(c['id'])['evaluation_spent']==pytest.approx(5)
