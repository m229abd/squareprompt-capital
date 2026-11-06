import math
import random
from .limits import fraction

TERMINAL = {'completed','negative','rejected','failed'}

def temperature(c):
    cfg=c['config']
    progress=max(fraction(c['llm_spent'],cfg['llm_budget_usd']), fraction(c['tokens_spent'],cfg['llm_token_budget']), fraction(max(0,c['evaluation_spent']-c.get('evaluation_reserved',0)),cfg['evaluation_budget_seconds']), fraction(c['explored'],cfg['max_nodes']))
    return max(0., min(1., 1-progress))

def score(metrics, objective):
    return {'sharpe':metrics['sharpe'], 'net_income':metrics['net_income'], 'return_drawdown':metrics['return']-2*metrics['max_drawdown']}[objective]

def pareto(nodes):
    eligible=[n for n in nodes if n.get('metrics') and n['kind']!='category' and n['status'] in {'completed','negative'}]
    def dominates(a,b):
        am,bm=a['metrics'],b['metrics']
        return am['return']>=bm['return'] and am['max_drawdown']<=bm['max_drawdown'] and am['sharpe']>=bm['sharpe'] and (am['return']>bm['return'] or am['max_drawdown']<bm['max_drawdown'] or am['sharpe']>bm['sharpe'])
    return [n['id'] for n in eligible if not any(dominates(o,n) for o in eligible if o['id']!=n['id'])]

def select(c, epsilon=None):
    """Mix uniform frontier sampling with reward-guided tree descent."""
    nodes={n['id']:n for n in c['nodes']}
    frontier=[n for n in c['nodes'] if n['status']=='queued' and n['kind']!='category']
    if not frontier:
        return None
    epsilon=temperature(c) if epsilon is None else epsilon
    rng=random.Random(c['seed']+c['step'])
    if rng.random()<epsilon:
        return rng.choice(frontier)['id']
    paths=set()
    for node in frontier:
        cur=node
        while cur:
            paths.add(cur['id'])
            cur=nodes.get(cur.get('parent'))
    current=nodes['baseline']
    while current['status']!='queued':
        children=[n for n in c['nodes'] if n.get('parent')==current['id'] and n['id'] in paths]
        if not children:
            return None
        means=[n.get('value',0)/max(1,n.get('visits',0)) for n in children]
        scale=max(1e-8,max(abs(x) for x in means))
        def rank(n):
            visits=n.get('visits',0)
            mean=n.get('value',0)/max(1,visits)
            bonus=math.sqrt(2*math.log(max(2,current.get('visits',1)))/max(1,visits))
            return (mean/scale+epsilon*bonus,n['id'])
        current=max(children,key=rank)
    return current['id']

def backpropagate(c, node, reward):
    nodes={n['id']:n for n in c['nodes']}
    seen=set()
    while node and node['id'] not in seen:
        seen.add(node['id'])
        node['visits']=node.get('visits',0)+1
        node['value']=node.get('value',0)+reward
        node=nodes.get(node.get('parent'))
