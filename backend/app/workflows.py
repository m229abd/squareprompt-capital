from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
from .history import PHASES

@workflow.defn
class CampaignWorkflow:
    def __init__(self):
        self.paused=False
        self.resume_epoch=0
        self.latest=None

    @workflow.signal
    def pause(self):
        self.paused=True

    @workflow.signal
    def resume(self):
        self.paused=False
        self.resume_epoch+=1

    @workflow.query
    def checkpoint(self):
        return self.latest

    @workflow.run
    async def run(self,request:str|dict):
        # Keep the original command sequence for histories started with a string.
        if isinstance(request,str):
            return await self._legacy(request)
        return await self._run_checkpoints(request)

    async def _legacy(self,cid):
        iteration=0
        while True:
            await workflow.wait_condition(lambda:not self.paused)
            epoch=self.resume_epoch
            try:
                done=await workflow.execute_activity('campaign_step',cid,start_to_close_timeout=timedelta(days=11),heartbeat_timeout=timedelta(seconds=30),retry_policy=RetryPolicy(initial_interval=timedelta(seconds=3),maximum_interval=timedelta(minutes=2),maximum_attempts=0,non_retryable_error_types=['ConfigurationError']))
            except ActivityError:
                # The exact failed phase stays in SQLite. Resume repeats it, not the whole campaign.
                if self.resume_epoch==epoch:
                    self.paused=True
                continue
            if done:
                return cid
            iteration+=1
            if iteration>=150:
                workflow.continue_as_new(cid)

    async def _run_checkpoints(self,request):
        state=request['checkpoint']
        self.latest=state
        for _ in range(150):
            await workflow.wait_condition(lambda:not self.paused)
            epoch=self.resume_epoch
            phase=state['phase']
            try:
                result=await workflow.execute_activity(
                    'campaign_'+phase if phase in PHASES else 'campaign_checkpoint',
                    state,start_to_close_timeout=timedelta(days=11),heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(initial_interval=timedelta(seconds=3),maximum_interval=timedelta(minutes=2),
                                             maximum_attempts=0,non_retryable_error_types=['ConfigurationError']))
            except ActivityError:
                if self.resume_epoch==epoch:
                    self.paused=True
                continue
            state=result['checkpoint']
            self.latest=state
            if result['done']:
                return result['result']
            if result['outcome']=='paused' and self.resume_epoch==epoch:
                self.paused=True
        workflow.continue_as_new({**request,'checkpoint':state})

@workflow.defn
class RefreshWorkflow:
    def __init__(self):
        self.paused=False
        self.stopped=False

    @workflow.signal
    def pause(self):
        self.paused=True

    @workflow.signal
    def resume(self):
        self.paused=False

    @workflow.signal
    def stop(self):
        self.stopped=True
        self.paused=False

    @workflow.run
    async def run(self,config:dict):
        parent=config['campaign']
        index=config.get('starting_index',0)
        while not config['max_runs'] or index<config['max_runs']:
            if self.stopped:
                break
            # Temporal persists the timer across worker restarts and host outages.
            await workflow.sleep(timedelta(hours=config['interval_hours']))
            await workflow.wait_condition(lambda:not self.paused or self.stopped)
            if self.stopped:
                break
            if config.get('history_version')==2:
                request=await workflow.execute_activity('prepare_refresh_campaign',
                    {'schedule':config['id'],'parent':parent,'index':index},start_to_close_timeout=timedelta(minutes=2))
                child=request['campaign']['id']
                result=await workflow.execute_child_workflow(CampaignWorkflow.run,request,id='campaign-'+child,task_queue='capital-research')
                parent=result['campaign_id']
                await workflow.execute_activity('record_refresh',{'schedule':config['id'],'campaign':parent,'index':index,'history_version':2},start_to_close_timeout=timedelta(minutes=2))
            else:
                child=await workflow.execute_activity('create_refresh',{'schedule':config['id'],'parent':parent,'index':index},start_to_close_timeout=timedelta(minutes=2))
                parent=await workflow.execute_child_workflow(CampaignWorkflow.run,child,id='campaign-'+child,task_queue='capital-research')
                await workflow.execute_activity('record_refresh',{'schedule':config['id'],'campaign':child,'index':index},start_to_close_timeout=timedelta(minutes=2))
            index+=1
            if index%50==0 and not self.stopped:
                workflow.continue_as_new({**config,'campaign':parent,'starting_index':index})
        await workflow.execute_activity('finish_refresh',config['id'],start_to_close_timeout=timedelta(minutes=2))
        if config.get('history_version')==2:
            return {'schedule_id':config['id'],'completed_runs':index,'last_campaign_id':parent,'status':'stopped' if self.stopped else 'completed'}
        return config['id']
