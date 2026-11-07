"""Behavioral checks for causal ML fitting, fortnightly updates and holdout fences."""
import copy
import json
import math
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
from app import engine, notebooks, store
from app.evaluator import ORDER, evaluate
from app.market import synthetic, INTERVAL_MS
from app.schemas import CampaignConfig

@pytest.fixture
def baseline():
    return json.loads(Path('/opt/baselines/baseline.ipynb').read_text())

def namespace(baseline):
    notebooks.validate(baseline)
    scope={}
    for cell in baseline['cells']:
        if cell['cell_type']=='code':exec(notebooks.source(cell),scope)
    return scope

def data(interval='1h',bars=1000):
    cfg=CampaignConfig(prompt='Test fortnightly machine learning',markets=['BTC'],bars=bars,interval=interval).model_dump()
    return cfg,synthetic('BTC',cfg,1800000000000)

def step(scope,history,interval='1h',training=None,**overrides):
    state={'as_of':int(history['available_at'].iloc[-1]),'bar_interval_ms':INTERVAL_MS[interval],
        'previous_position':0.,'drawdown':0.,'max_exposure':1.,'sources':{},
        'training_history':history if training is None else training,'execution_costs':{'taker_rate':.00045,'half_spread_rate':.0001,'funding_rate_hourly':.00001},**overrides}
    for category in ORDER:state=scope['stage_'+category](history,state)
    return state

@pytest.mark.parametrize('interval', ['15m','1h','4h','1d','1w'])
def test_retraining_uses_fourteen_elapsed_days_not_bar_count(baseline,interval):
    scope=namespace(baseline)
    period=14*86400000//INTERVAL_MS[interval]
    _,dataset=data(interval,bars=max(150,64+period+2))
    frame=pd.DataFrame(dataset['records'])
    first=step(scope,frame.iloc[:64],interval)
    assert first['model_ready']
    initial=scope['model_cache']['weights'].copy()
    step(scope,frame.iloc[:64+period-1],interval)
    np.testing.assert_array_equal(initial,scope['model_cache']['weights'])
    assert len(scope['model_cache']['events'])==1
    second=step(scope,frame.iloc[:64+period],interval)
    events=second['model_artifact']['fit_events']
    assert len(events)==2
    assert events[1]['fitted_at']-events[0]['fitted_at']==14*86400000
    assert events[1]['trained_through']<=events[1]['fitted_at']


def test_only_matured_targets_are_used_and_short_history_stays_flat(baseline):
    scope=namespace(baseline);_,dataset=data()
    frame=pd.DataFrame(dataset['records'])
    short=step(scope,frame.iloc[:40])
    assert short['target']==0
    assert short['model_ready'] is False
    fitted=step(scope,frame.iloc[:64])
    cache=scope['model_cache']
    assert cache['rows']==36
    assert cache['last_feature_time']==frame.iloc[59]['time']
    assert cache['trained_through']==frame.iloc[63]['available_at']
    assert len(fitted['model_artifact']['weights'])==len(fitted['model_artifact']['features'])
    assert math.isfinite(fitted['forecast'])


def test_holdout_prices_cannot_change_scaler_weights_or_hyperparameters(baseline):
    _,dataset=data();frame=pd.DataFrame(dataset['records']);training=frame.iloc[:157].copy()
    before=namespace(baseline);after=namespace(baseline)
    history=frame.iloc[:600].copy()
    changed=history.copy()
    changed.loc[changed.index>=157,['open','high','low','close']]*=30
    first=step(before,history,training=training)
    second=step(after,changed,training=training)
    for field in ['weights','mean','scale']:
        np.testing.assert_array_equal(before['model_cache'][field],after['model_cache'][field])
    for field in ['alpha','forecast_gain','normalized_error']:
        assert first['model_artifact'][field]==second['model_artifact'][field]
    step(before,frame.iloc[:950],training=training)
    assert len(before['model_cache']['events'])==1
    assert before['model_cache']['trained_through']==int(training['available_at'].iloc[-1])


def test_cost_band_sizing_and_drawdown_breaker(baseline):
    scope=namespace(baseline);_,dataset=data();history=pd.DataFrame(dataset['records'][:64])
    state=step(scope,history)
    signal={'forecast':.002,'forecast_uncertainty':.001,'model_ready':True,'tradable':True,'prediction_horizon':4,'bar_interval_ms':3600000,'execution_costs':{'taker_rate':.00045,'half_spread_rate':.0001}}
    assert scope['stage_signals'](history,copy.deepcopy(signal))['target']==1
    expensive=copy.deepcopy(signal);expensive['execution_costs']['taker_rate']=.01
    assert scope['stage_signals'](history,expensive)['target']==0
    weak={**signal,'forecast':.0005}
    assert scope['stage_signals'](history,weak.copy())['target']==0
    held=scope['stage_signals'](history,{**weak,'previous_position':.1})
    assert scope['stage_sizing'](history,{**held,'volatility':.01})['target']==.1
    assert scope['stage_signals'](history,{**weak,'previous_position':-.1})['target']==0
    assert abs(state['target'])<=.8
    state.update(drawdown=.16,target=1.)
    assert scope['stage_risk'](history,state)['target']==0
    state.update(drawdown=0.,target=1.)
    assert scope['stage_risk'](history,state)['target']==0


def test_liquidity_and_market_factor_cap_exposure_with_missing_data_fallback(baseline):
    scope=namespace(baseline)
    _,dataset=data()
    history=pd.DataFrame(dataset['records'][:64])
    history['trade_count']=100
    history.loc[history.index[-1],'trade_count']=25
    context={'market':'BTC','cross_asset':{'ETH':history},'capital':10000.0}
    sourced=scope['stage_sources'](history,context.copy())
    assert sourced['activity_ratio']==.25
    assert sourced['market_beta']==pytest.approx(1.0)
    sized=scope['stage_sizing'](history,{**sourced,'target':1.0,'volatility':.001})
    normal=scope['stage_sizing'](history,{**sourced,'target':1.0,'volatility':.001,'activity_ratio':1.0})
    assert sized['target']<normal['target']
    hedged=scope['stage_exposure'](history,{**sized,'factor_impulse':-2.0})
    assert 0<hedged['target']<sized['target']
    assert scope['stage_exposure'](history,{**sized,'factor_impulse':2.0})['target']==sized['target']
    stale=scope['stage_sources'](history.drop(columns=['trade_count']),{**context,'cross_asset':{'ETH':history.iloc[:-1]}})
    assert stale['market_beta']==0
    assert math.isfinite(stale['activity_ratio'])
    carry={**sourced,'observed_funding_hourly':.001,'forecast_uncertainty':.01,'volatility':.001}
    long=scope['stage_sizing'](history,{**carry,'target':1.0})
    short=scope['stage_sizing'](history,{**carry,'target':-1.0})
    assert 0<long['target']<abs(short['target'])


def test_session_loss_latches_until_next_session(baseline):
    scope=namespace(baseline)
    _,dataset=data()
    history=pd.DataFrame(dataset['records'][:64])
    session=int(history['available_at'].iloc[-1])//86400000*86400000
    initial={'drawdown':0.0,'capital':10000.0,'as_of':session,'target':.1}
    risk=scope['stage_risk']
    assert risk(history,initial.copy())['target']>0
    assert risk(history,{**initial,'capital':9700.0})['target']==0
    assert risk(history,{**initial,'capital':9900.0})['target']==0
    assert risk(history,{**initial,'capital':9900.0,'as_of':session+86400000})['target']>0


@pytest.mark.asyncio
async def test_real_kernel_exports_fit_artifact_and_scores_full_holdout(baseline,tmp_path,monkeypatch):
    monkeypatch.setattr(store,'ROOT',tmp_path);monkeypatch.setattr(notebooks,'ROOT',tmp_path);store.init()
    cfg,_=data(bars=200)
    campaign=engine.create(cfg);await engine.prepare(campaign);campaign=store.load(campaign['id'])
    datasets=engine.datasets(campaign,holdout=True)
    result=evaluate({'notebook':baseline,'config':cfg,'datasets':datasets,'interval_ms':INTERVAL_MS['1h'],'sources':[]})
    assert result['metrics']['bars']==40
    assert result['metrics']['model_fit_count']==1
    artifact=result['models'][0]
    assert artifact['trained_through']==datasets[0]['training_cutoff']
    assert artifact['rows']>32
    assert len(artifact['weights'])==len(artifact['features'])==12
    assert 0<=artifact['forecast_gain']<=1
    assert all(math.isfinite(v) for v in result['metrics'].values())
    outputs=result['notebook']['cells'][-1]['outputs']
    assert any('models' in o.get('data',{}).get('application/json',{}) for o in outputs)


def test_models_follow_the_notebook_retraining_cell(baseline):
    cell=next(c for c in baseline['cells'] if c['id']=='retraining')
    cell['source']=notebooks.source(cell).replace('14 * 86400000','2 * 86400000')
    scope=namespace(baseline)
    _,dataset=data()
    frame=pd.DataFrame(dataset['records'])
    step(scope,frame.iloc[:64])
    step(scope,frame.iloc[:111])
    assert len(scope['model_cache']['events'])==1
    state=step(scope,frame.iloc[:112])
    artifact=state['model_artifact']
    first,second=artifact['fit_events']
    assert second['fitted_at']-first['fitted_at']==artifact['retrain_interval_ms']==2*86400000
