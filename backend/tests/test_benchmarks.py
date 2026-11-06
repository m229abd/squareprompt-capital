import json
import pytest
from fastapi.testclient import TestClient
from app import engine,store
from app.api import app
from app.benchmarks import development_benchmarks


def config(**overrides):
    return {'initial_capital':100.,'direction':'both','max_gross_exposure':1.,**overrides}


def dataset(kind='perp',fee=0.,spread=0.,funding=0.):
    return {'market':{'type':kind},'fee':{'taker_rate':fee},'slippage_rate':spread,
            'execution_quote':{'funding_rate_hourly':funding},'records':[
                {'time':0,'open':50.,'close':100.},
                {'time':1,'open':100.,'close':120.},
                {'time':2,'open':120.,'close':130.}]}


def test_fixed_quantity_excludes_warmup_in_mixed_portfolio():
    result=development_benchmarks(config(),[dataset(),dataset('spot')],3600000,[1,2])
    assert [r['equity'] for r in result['buy_hold']]==[120.,130.]
    assert set(result)=={'buy_hold'}


def test_entry_exit_costs_and_signed_funding_match_passive_notional():
    result=development_benchmarks(config(),[dataset(fee=.008,spread=.002,funding=.01)],3600000,[1,2])
    q=1/1.01
    entry_fee=q*100*.01
    exit_fee=q*130*.01
    funding=q*(100+120)*.01
    assert result['buy_hold'][-1]['equity']==pytest.approx(100+q*30-entry_fee-exit_fee-funding)
    with pytest.raises(ValueError,match='scoring window'):
        development_benchmarks(config(),[dataset()],3600000,[1,3])


def test_benchmark_endpoint_uses_frozen_development_and_caches(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'ROOT',tmp_path);store.init()
    c=engine.create({'prompt':'Verify passive benchmark alignment','markets':['BTC']})
    baseline=engine.getnode(c,'baseline')
    baseline['curve']=[{'time':1,'equity':10000.},{'time':2,'equity':10000.}]
    with store.transaction() as db:store.save(c,db)
    def development_only(c,holdout=False,calibrate=False):
        assert not holdout and not calibrate
        return [dataset()]
    monkeypatch.setattr(engine,'datasets',development_only)
    client=TestClient(app)
    url=f"/api/campaigns/{c['id']}/benchmarks"
    first=client.get(url);assert first.status_code==200
    assert [r['time'] for r in first.json()['buy_hold']]==[1,2]
    with store.connect() as db:
        assert json.loads(db.execute('SELECT body FROM cache WHERE id=?',(f"benchmarks-v2:{c['id']}",)).fetchone()[0])==first.json()
    monkeypatch.setattr(engine,'datasets',lambda *args:pytest.fail('Cache should avoid reloading candles'))
    assert client.get(url).json()==first.json()


def test_buy_hold_is_available_after_calibration_before_baseline(tmp_path,monkeypatch):
    from app.compute import WARMUP
    monkeypatch.setattr(store,'ROOT',tmp_path);store.init()
    c=engine.create({'prompt':'Check benchmark during baseline replay','markets':['BTC']})
    client=TestClient(app)
    url=f"/api/campaigns/{c['id']}/benchmarks"
    assert client.get(url).status_code==409
    c['calibration']={'evaluation_bars':WARMUP+2}
    c['phase']='baseline'
    with store.transaction() as db:store.save(c,db)
    d=dataset()
    d['records']=[{'time':i-WARMUP+1,'open':50.,'close':50.} for i in range(WARMUP)] + [
        {'time':1,'open':100.,'close':120.},{'time':2,'open':120.,'close':130.}]
    monkeypatch.setattr(engine,'datasets',lambda c:[d])
    response=client.get(url)
    assert response.status_code==200
    assert set(response.json())=={'buy_hold'}
    assert [p['time'] for p in response.json()['buy_hold']]==[1,2]
    assert response.json()['buy_hold'][-1]['equity']==pytest.approx(13000.)


@pytest.mark.asyncio
async def test_sandbox_spot_sleeves_are_identified_in_mixed_campaign(tmp_path,monkeypatch):
    from app import market
    monkeypatch.setattr(store,'ROOT',tmp_path);store.init()
    c=engine.create({'prompt':'Verify mixed market benchmark inputs','markets':['BTC','@107'],'market_type':'both'})
    cfg=c['config']
    snapshot=await market.execution_snapshot(cfg)
    assert [q['instrument'] for q in snapshot['quotes']]==['perp','spot']
    assert snapshot['quotes'][1]['funding_rate_hourly']==0
    assert market.synthetic('@107',cfg,1800000000000)['market']['type']=='spot'
