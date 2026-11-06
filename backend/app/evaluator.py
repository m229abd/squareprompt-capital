"""Trusted evaluator. Notebook functions receive prefixes, never the full evaluation frame."""
import builtins
import copy
import json
import math
import time
import numpy as np
import pandas as pd
import nbformat
from nbclient import NotebookClient
from .notebooks import validate, source

ORDER=['sources','quality','storage','retraining','state','models','signals','sizing','exposure','risk','drift','registry']

def evaluate(payload):
    started=time.monotonic()
    nb=validate(payload['notebook'])
    # Execute all cells in an actual Jupyter kernel before the trusted chronological replay.
    executed=NotebookClient(nbformat.from_dict(copy.deepcopy(nb)),timeout=None,startup_timeout=max(1,int(payload.get('execution_budget_seconds') or 864000)),kernel_name='python3').execute()
    startup_seconds=time.monotonic()-started
    replay_started=time.monotonic()
    cfg=payload['config']
    results=[]
    model_artifacts=[]
    curves=[]
    all_returns=[]
    bars_per_year=365*24*3600000/payload['interval_ms']
    market_count=len(payload['datasets'])
    frames={d['market']['id']:pd.DataFrame(d['records']) for d in payload['datasets']}
    for dataset in payload['datasets']:
        np.random.seed(0)
        namespace={'__builtins__':{k:getattr(builtins,k) for k in ['__import__','abs','all','any','bool','dict','enumerate','float','int','isinstance','len','list','map','max','min','range','round','set','sorted','str','sum','tuple','zip','ValueError','Exception']}}
        for cell in nb['cells']:
            if cell['cell_type']=='code':
                exec(compile(source(cell),cell['id'],'exec'),namespace)
        records=dataset['records']
        frame=pd.DataFrame(records)
        capital=cfg['initial_capital']/market_count
        equity=capital
        peak=capital
        previous=0.
        net=[]
        turnover=0.
        fees_total=0.
        funding_total=0.
        curve=[]
        start_index=int(dataset.get('evaluation_start',payload.get('warmup',64)))
        training_cutoff=dataset.get('training_cutoff')
        if len(frame)<start_index+20:
            raise ValueError('Insufficient replay bars after warmup')
        for i in range(start_index-1,len(frame)-1):
            history=frame.iloc[:i+1].copy(deep=True)
            decision_time=int(frame.iloc[i]['available_at'])
            # Only observations published by the decision time are passed to the notebook.
            sources={s['name']:[copy.deepcopy(r) for r in s['records'] if r['available_at']<=decision_time] for s in payload.get('sources',[])}
            context={'target':0.,'capital':equity,'peak':peak,'drawdown':1-equity/peak,'previous_position':previous,'market':dataset['market']['id'],'sources':sources,'max_exposure':cfg['max_gross_exposure'],'cross_asset':{symbol:df[df['available_at']<=decision_time].copy(deep=True) for symbol,df in frames.items()}}
            context['bar_interval_ms']=payload['interval_ms']
            context['execution_costs']={'taker_rate':dataset['fee']['taker_rate'],
                'half_spread_rate':dataset['slippage_rate'],
                'funding_rate_hourly':dataset.get('execution_quote',{}).get('funding_rate_hourly',0.) if dataset['market']['type']=='perp' else 0.}
            context['training_history']=(history if training_cutoff is None else history[history['available_at']<=training_cutoff]).copy(deep=True)
            for category in ORDER:
                fn=namespace.get('stage_'+category)
                if not callable(fn):
                    raise ValueError(f'Missing stage_{category}(history, state)')
                context=fn(history,context)
                if not isinstance(context,dict):
                    raise ValueError(f'{category}: stage must return state dict')
            target=float(context.get('target',0))
            if not math.isfinite(target):
                raise ValueError('Non-finite target exposure')
            target=max(-cfg['max_gross_exposure'],min(cfg['max_gross_exposure'],target))
            if dataset['market']['type']=='spot' or cfg['direction']=='long':
                target=max(0,target)
            if cfg['direction']=='short':
                target=min(0,target)
            # Signal from last completed close executes at the NEXT bar open.
            nxt=frame.iloc[i+1]
            opening=float(nxt['open'])
            change=abs(target-previous)
            execution_cost=change*(dataset['fee']['taker_rate']+dataset['slippage_rate'])
            gross=target*(float(nxt['close'])/opening-1)
            # Gap P&L belongs to the prior position held across the boundary.
            gap=previous*(opening/float(frame.iloc[i]['close'])-1)
            funding_rate=(dataset['execution_quote']['funding_rate_hourly']*payload['interval_ms']/3600000 if dataset.get('execution_quote') else float(nxt.get('funding',0)))
            funding=target*funding_rate if dataset['market']['type']=='perp' else 0.
            r=max(-1.,gross+gap-execution_cost-funding)
            fees_total+=equity*change*dataset['fee']['taker_rate']
            funding_total+=equity*funding
            turnover+=change
            equity*=1+r
            peak=max(peak,equity)
            previous=target
            net.append(r)
            curve.append({'time':int(nxt['time']),'equity':equity,'drawdown':1-equity/peak})
        if context.get('model_artifact'):
            model_artifacts.append({'market':dataset['market']['id'],**copy.deepcopy(context['model_artifact'])})
        # Close final positions; include exit fees and slippage.
        exit_cost=abs(previous)*(dataset['fee']['taker_rate']+dataset['slippage_rate'])
        fees_total+=equity*abs(previous)*dataset['fee']['taker_rate']
        equity*=1-exit_cost
        net[-1]=(1+net[-1])*(1-exit_cost)-1
        curve[-1]['equity']=equity
        curve[-1]['drawdown']=1-equity/peak
        results.append({'market':dataset['market']['id'],'initial':capital,'equity':equity,'fees':fees_total,'funding':funding_total,'turnover':turnover})
        all_returns.append(net)
        curves.append(curve)
    lengths={len(x) for x in all_returns}
    if len(lengths)!=1 or any([x['time'] for x in curves[0]]!=[x['time'] for x in curve] for curve in curves):
        raise ValueError('Markets must have exactly aligned evaluation windows')
    # Independent equal-capital sleeves. Aggregate portfolio returns from actual aggregate equity.
    combined=np.sum([[r['equity'] for r in curve] for curve in curves],axis=0)
    rets=np.diff(np.r_[cfg['initial_capital'],combined])/np.r_[cfg['initial_capital'],combined[:-1]]
    peak=np.maximum.accumulate(np.r_[cfg['initial_capital'],combined])[1:]
    dd=1-combined/peak
    std=float(np.std(rets,ddof=1))
    sharpe=float(np.mean(rets)/std*math.sqrt(bars_per_year)) if std>1e-12 else 0.
    # Avoid infinities and misleading huge values in JSON and objective arithmetic.
    sharpe=max(-100.,min(100.,sharpe))
    metrics={'net_income':float(combined[-1]-cfg['initial_capital']),'return':float(combined[-1]/cfg['initial_capital']-1),'max_drawdown':float(max(dd)),'sharpe':sharpe,'bars':len(rets),'elapsed_seconds':time.monotonic()-started,'startup_seconds':startup_seconds,'replay_seconds':time.monotonic()-replay_started,'model_fit_count':sum(len(m.get('fit_events',[])) for m in model_artifacts),'fees':sum(r['fees'] for r in results),'funding':sum(r['funding'] for r in results)}
    equity_curve=[{'time':curves[0][i]['time'],'equity':float(value),'drawdown':float(dd[i])} for i,value in enumerate(combined)]
    executed['cells'][-1]['outputs'].append(nbformat.v4.new_output('display_data',data={'text/plain':json.dumps(metrics,indent=2),'application/json':metrics}))
    if model_artifacts:
        executed['cells'][-1]['outputs'].append(nbformat.v4.new_output('display_data',data={'text/plain':json.dumps(model_artifacts,indent=2),'application/json':{'models':model_artifacts}}))
    return {'models':model_artifacts,'metrics':metrics,'curve':equity_curve,'markets':results,'notebook':dict(executed),'assumptions':['Equal-capital independent sleeves; gross exposure capped at 1x.','Final holdout supplies a fixed pre-embargo training_history; the strategy must honor this fit boundary.','Next-open fills; snapshotted fee tier, hourly funding and observed spread held constant across replay.','No intrabar order-book queue, market impact or liquidation simulation.']}
