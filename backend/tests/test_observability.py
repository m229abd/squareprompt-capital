import json
from uuid import UUID

import pytest

from app import engine, history, store, telemetry, worker


@pytest.fixture
def campaign(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'ROOT',tmp_path)
    store.init()
    return engine.create({'name':'Test observability','markets':['BTC'],'prompt':'private brief not for Temporal history'})


def test_temporal_input_and_winner_are_compact(campaign):
    campaign['config']['api_key']='not for Temporal'
    campaign['nodes'][0].update(metrics={'sharpe':1.2,'curve':['large payload']},notebook={'sha':'abc','branch':'campaigns/test/baseline','path':'private file'})
    campaign['selected']='baseline'
    campaign['holdout']={'metrics':{'bars':40,'return':.02},'curve':['holdout raw data']}
    request=history.campaign_input(campaign)
    result=history.campaign_result(campaign,request['checkpoint'])
    assert request['campaign']['markets']==['BTC']
    assert result['winner']['metrics']=={'sharpe':1.2}
    assert result['winner']['notebook']=={'sha':'abc','branch':'campaigns/test/baseline'}
    assert result['holdout']=={'bars':40,'return':.02}
    encoded=json.dumps([request,result])
    assert all(value not in encoded for value in ('api_key','private brief','large payload','private file','holdout raw data'))


@pytest.mark.asyncio
async def test_checkpoint_retry_does_not_advance_a_second_stage(campaign,monkeypatch):
    request=history.campaign_input(campaign)['checkpoint']
    calls=[]
    async def advance(cid):
        calls.append(cid)
        c=store.load(cid)
        c.update(phase='calibrate',step=1,status='running')
        with store.transaction() as db:store.save(c,db)
    monkeypatch.setattr(worker,'execute_step',advance)
    first=await worker.execute_checkpoint(request)
    recovered=await worker.execute_checkpoint(request)
    assert len(calls)==1
    assert first['outcome']=='advanced'
    assert recovered['outcome']=='recovered'
    assert first['checkpoint']==recovered['checkpoint']
    assert first['checkpoint']['phase']=='calibrate'


def test_opik_id_is_stable_uuidv7_with_original_timestamp():
    start=1793900000.123
    trace=telemetry.opik_trace_id('campaign:developer:0',start)
    identifier=UUID(trace)
    assert identifier.version==7
    assert identifier.int>>80==int(start*1000)
    assert trace==telemetry.opik_trace_id('campaign:developer:0',start)
    assert trace!=telemetry.opik_trace_id('campaign:developer:1',start)
