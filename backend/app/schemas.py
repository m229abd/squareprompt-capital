from typing import Literal
from pydantic import BaseModel, Field, model_validator

class AgentConfig(BaseModel):
    model: str = ''
    reasoning: str | None = Field(default=None, max_length=128)

class CampaignConfig(BaseModel):
    name: str = Field(default='Untitled research campaign', min_length=1, max_length=120)
    prompt: str = Field(min_length=10, max_length=20000)
    mode: Literal['demo','live'] = 'demo'
    markets: list[str] = Field(min_length=1, max_length=200)
    market_type: Literal['perp','spot','both'] = 'perp'
    direction: Literal['both','long','short'] = 'both'
    objective: Literal['sharpe','net_income','return_drawdown'] = 'sharpe'
    llm_budget_usd: float = Field(default=10, ge=0, le=10000)
    llm_token_budget: int = Field(default=100000, ge=0, le=100000000)
    evaluation_budget_seconds: int = Field(default=600, ge=0, le=864000)
    evaluation_seconds: int = Field(default=15, ge=0, le=3600)
    max_nodes: int = Field(default=24, ge=0, le=2000)
    critic_retries: int = Field(default=2, ge=0, le=10)
    bars: int = Field(default=720, ge=150, le=4900)
    interval: Literal['1m','3m','5m','15m','30m','1h','2h','4h','8h','12h','1d','3d','1w'] = '1h'
    holdout_fraction: float = Field(default=0.2, ge=0.1, le=0.4)
    embargo_bars: int = Field(default=3, ge=1, le=50)
    initial_capital: float = Field(default=10000, gt=0, le=100000000)
    max_gross_exposure: float = Field(default=1, gt=0, le=1)
    model: str = ''
    reasoning: str | None = Field(default=None, max_length=128)
    agents: dict[Literal['developer','researcher','critic','initializer'], AgentConfig] = Field(default_factory=dict)
    execution_snapshot_id: str = ''
    wallet_address: str = ''
    baseline: str = 'baseline.ipynb'
    registry_name: str = ''

    @model_validator(mode='after')
    def valid_constraints(self):
        if self.market_type == 'spot' and self.direction == 'short':
            raise ValueError('Spot supports long/flat. Select perpetuals for short exposure.')
        if int(self.bars*(1-self.holdout_fraction))-self.embargo_bars<84:
            raise ValueError('Development history needs at least 84 bars after the split and embargo.')
        if self.bars-int(self.bars*(1-self.holdout_fraction))<20:
            raise ValueError('Final holdout needs at least 20 bars.')
        return self

class ProviderConfig(BaseModel):
    base_url: str
    api_key: str = ''


class RefreshScheduleRequest(BaseModel):
    campaign: str
    interval_hours: int = Field(ge=1, le=720)
    max_runs: int = Field(default=3, ge=0, le=100)
