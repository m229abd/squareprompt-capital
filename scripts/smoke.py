"""Run inside the API container: python /app/scripts/smoke.py (or via Compose exec stdin)."""
import json
import time
import httpx

with httpx.Client(base_url='http://localhost:8000/api',timeout=30) as client:
    created=client.post('/campaigns',json={'name':'Cross-market momentum discovery','prompt':'Find robust momentum with realistic costs and volatility-aware risk. This is a synthetic end-to-end workflow verification.','mode':'demo','markets':['BTC','SOL','HYPE'],'bars':480,'max_nodes':16,'evaluation_seconds':20,'evaluation_budget_seconds':600})
    created.raise_for_status()
    cid=created.json()['id']
    print('Campaign:',cid,flush=True)
    prior=None
    for _ in range(180):
        c=client.get('/campaigns/'+cid).json()
        current=(c['phase'],c['explored'],c['status'])
        if current!=prior:print(current,flush=True)
        prior=current
        if c['status']=='attention':raise RuntimeError(c['error'])
        if c['status']=='completed':
            assert c['holdout'] and c['selected']
            assert c['holdout']['metrics']['bars']==96
            assert len([n for n in c['nodes'] if n['kind']=='category' and n['parent']=='baseline'])==12
            for n in c['nodes']:
                if n['reward'] is not None and n['kind']!='baseline':
                    p=next(p for p in c['nodes'] if p['id']==n['eval_parent'])
                    assert abs(n['reward']-(n['score']-p['score']))<1e-8
            print(json.dumps({'campaign':cid,'winner':c['selected'],'experiments':c['explored'],'holdout':c['holdout']['metrics'],'git':next(n for n in c['nodes'] if n['id']==c['selected'])['notebook']['sha']},indent=2),flush=True)
            break
        time.sleep(2)
    else:raise TimeoutError('Campaign did not finish in six minutes')
