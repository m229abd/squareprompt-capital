CATEGORIES = [
 ('sources', 'Sources and ingestion', 'Real-time/historical feeds, order books, news/events and cross-asset inputs'),
 ('quality', 'Quality and alignment', 'Quality checks, synchronization and when information becomes available'),
 ('storage', 'Storage and provenance', 'Feature store, point-in-time datasets and data lineage'),
 ('state', 'Market state', 'Regimes, microstructure, liquidity, correlations and anomalies'),
 ('models', 'Predictive models', 'Return/volatility forecasts, probabilities, calibration, quantiles and uncertainty'),
 ('signals', 'Signal decisions', 'Signal generation, regime-aware selection, ensembles, timing and conflict resolution'),
 ('sizing', 'Sizing and allocation', 'Position sizes, allocation optimization and capital budgets'),
 ('exposure', 'Exposure and hedging', 'Factor/correlation exposure and hedges'),
 ('risk', 'Trading risk controls', 'Pre-trade checks, limits, drawdown breakers, stress tests and kill switch'),
 ('retraining', 'Retraining', 'Training windows, schedules and update methods'),
 ('drift', 'Drift monitoring', 'Changes in inputs or forecast performance and how to respond'),
 ('registry', 'Model registry', 'Model versions, promotion and saved artifacts'),
]


def uses_llm(config):
    # Older campaigns opted into provider calls through live-data mode.
    return (config.get('agent_mode') or ('llm' if config['mode']=='live' else None))=='llm'
