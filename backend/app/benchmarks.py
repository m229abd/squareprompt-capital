"""Buy-and-hold reference on the exact scored development timestamps."""


def passive_curve(rows, capital, exposure, cost_rate, funding_rate):
    """Hold a fixed quantity from the first open to the final close."""
    opening=float(rows[0]['open'])
    # Size the entry against equity after paying its own execution cost.
    notional=capital*exposure/(1+exposure*cost_rate)
    quantity=notional/opening
    cash=capital-quantity*opening-notional*cost_rate
    values=[]
    depleted=False
    for row in rows:
        if not depleted:
            cash-=quantity*float(row['open'])*funding_rate(row)
            equity=cash+quantity*float(row['close'])
            depleted=equity<=0
        values.append(0. if depleted else equity)
    values[-1]=max(0.,values[-1]-quantity*float(rows[-1]['close'])*cost_rate)
    return values


def development_benchmarks(config,datasets,interval_ms,times):
    if not datasets or not times:
        raise ValueError('Baseline has no scored development bars.')
    totals=[0.]*len(times)
    capital=config['initial_capital']/len(datasets)
    for d in datasets:
        # Match the recorded baseline curve exactly. This also serves older campaigns.
        rows=d['records'][-len(times):]
        if [r['time'] for r in rows]!=times:
            raise ValueError('Frozen market data does not match the baseline scoring window.')
        perpetual=d['market']['type']=='perp'
        quote=d.get('execution_quote')
        def funding_rate(row):
            if not perpetual:return 0.
            if quote:return quote['funding_rate_hourly']*interval_ms/3600000
            return float(row.get('funding',0.))
        cost_rate=d['fee']['taker_rate']+d['slippage_rate']
        curve=passive_curve(rows,capital,config['max_gross_exposure'],cost_rate,funding_rate)
        totals=[a+b for a,b in zip(totals,curve)]
    return {'buy_hold':[{'time':t,'equity':value} for t,value in zip(times,totals)]}
