"""Bounded research summaries for Temporal payloads and heartbeat details."""

from .constants import uses_llm

PHASES=('prepare','calibrate','baseline','seed','select','develop','critic','evaluate','research','finalize')
LIMITS=('llm_budget_usd','llm_token_budget','evaluation_budget_seconds','evaluation_seconds','max_nodes','critic_retries')
METRICS=('bars','return','net_income','max_drawdown','sharpe','fees','funding','elapsed_seconds','model_fit_count')


def pick(data,keys):
    return {key:data[key] for key in keys if key in data and data[key] is not None}


def node_summary(node):
    result=pick(node,('id','kind','category','status','attempt','eval_parent','reward','score','proposal_placement','proposed_by'))
    result['summary']=str(node.get('summary',''))[:160]
    if node.get('metrics'):
        result['metrics']=pick(node['metrics'],METRICS)
    if node.get('notebook'):
        result['notebook']=pick(node['notebook'],('sha','branch'))
    return result


def checkpoint(campaign):
    result=pick(campaign,('phase','step','status','explored','best','selected','final_reason'))
    result['campaign_id']=campaign['id']
    nodes=campaign.get('nodes',[])
    active=next((node for node in nodes if node['id']==campaign.get('active_node')),None)
    if active:
        result['node']=node_summary(active)
    selection=campaign.get('selection')
    if selection:
        result['selection']=pick(selection,('node','mode','temperature'))
    if campaign.get('calibration'):
        result['development_window']=pick(campaign['calibration'],('evaluation_bars','target_seconds','estimated_evaluation_seconds'))
    result['budget_used']={'llm_usd':campaign.get('llm_spent',0.),'tokens':campaign.get('tokens_spent',0),
                           'evaluation_seconds':max(0.,campaign.get('evaluation_spent',0.)-campaign.get('evaluation_reserved',0.))}
    return result


def campaign_input(campaign):
    config=campaign['config']
    context=pick(config,('mode','market_type','direction','objective','interval','bars','holdout_fraction','baseline'))
    if uses_llm(config):
        context['agent_mode']='llm'
    context.update(id=campaign['id'],name=campaign['name'],markets=config['markets'],limits=pick(config,LIMITS))
    agents={role:pick(choice,('model','reasoning')) for role,choice in config.get('agents',{}).items() if choice.get('model')}
    if agents and context.get('agent_mode')=='llm':
        context['agents']=agents
    return {'history_version':2,'campaign':context,'checkpoint':checkpoint(campaign)}


def campaign_result(campaign,state):
    result={**state}
    winner=next((node for node in campaign['nodes'] if node['id']==campaign.get('selected')),None)
    if winner:
        result['winner']=node_summary(winner)
    if campaign.get('holdout'):
        result['holdout']=pick(campaign['holdout']['metrics'],METRICS)
    return result
