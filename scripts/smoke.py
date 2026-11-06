"""Run inside the API container: python /app/scripts/smoke.py (or via Compose exec stdin)."""
import json
import os
import time
import httpx

with httpx.Client(base_url='http://localhost:8000/api',timeout=30) as client:
    settings=client.get('/provider').raise_for_status().json()
    agents=settings.get('default_agents',{})
    if not settings.get('has_key') or any(not agents.get(role,{}).get('model') for role in ('developer','researcher','critic')):
        raise SystemExit('Configure your provider and use Remember role choices in the campaign wizard before running this smoke test.')
    created=client.post('/campaigns',json={'name':'Cross-market momentum discovery','prompt':'Find robust momentum with realistic costs and volatility-aware risk. This is a synthetic end-to-end workflow verification.','mode':'demo','agents':agents,'llm_budget_usd':float(os.getenv('SMOKE_LLM_BUDGET_USD','10')),'markets':['BTC','SOL','HYPE'],'bars':480,'max_nodes':2,'evaluation_seconds':20,'evaluation_budget_seconds':600})
    if not created.is_success:
        raise SystemExit(f'Campaign could not launch ({created.status_code}). {created.json().get("detail")}')
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
