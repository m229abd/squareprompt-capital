"""Compute calibration changes workload; target duration never terminates a candidate."""
import math

WARMUP=64
MIN_SCORED_BARS=20

def calibrate(config, metrics, markets):
    available=int(config['bars']*(1-config['holdout_fraction']))-config['embargo_bars']
    scored=max(1,int(metrics['bars']))
    startup=max(0.,metrics.get('startup_seconds',0.))
    replay=max(.001,metrics.get('replay_seconds',metrics['elapsed_seconds']-startup))
    # One portfolio step replays every allowed market. Subtract fixed startup overhead first.
    seconds_per_bar=replay/scored
    target=config['evaluation_seconds']
    count=available-WARMUP if target==0 else max(MIN_SCORED_BARS,math.floor(max(0.,target-startup)/seconds_per_bar))
    total=min(available,WARMUP+count)
    estimate=startup+(total-WARMUP)*seconds_per_bar
    holdout_bars=config['bars']-int(config['bars']*(1-config['holdout_fraction']))
    return {'policy':'adaptive-v2','seconds':metrics['elapsed_seconds'],'startup_seconds':startup,
            'seconds_per_portfolio_bar':seconds_per_bar,'bars_per_second':markets/seconds_per_bar,
            'evaluation_bars':total,'estimated_evaluation_seconds':estimate,'target_seconds':target,
            'holdout_reserve_seconds':max(2.,2*(startup+holdout_bars*seconds_per_bar)),
            'note':'Hardware-calibrated workload, frozen for comparable scores. Target duration is an estimate, not a cutoff.'}

def estimates(c):
    profile=c.get('calibration') or {}
    cfg=c['config']
    expected=profile.get('estimated_evaluation_seconds',cfg['evaluation_seconds'])
    reserve=profile.get('holdout_reserve_seconds',cfg['evaluation_seconds'])
    # Update scheduling estimates as candidate complexity changes, without changing the scoring window.
    for n in c['nodes']:
        m=n.get('metrics')
        if m:
            expected=max(expected,m.get('elapsed_seconds',expected))
    winner=next((n for n in c['nodes'] if n['id']==(c.get('selected') or c.get('best'))),None)
    if winner and winner.get('metrics'):
        m=winner['metrics']; startup=m.get('startup_seconds',0.)
        scored=cfg['bars']-int(cfg['bars']*(1-cfg['holdout_fraction']))
        reserve=max(reserve,2*(startup+max(.001,m.get('elapsed_seconds',expected)-startup)*scored/max(1,m.get('bars',1))))
    return max(1.,expected),max(2.,reserve)
