import asyncio
import hashlib
import ipaddress
import json
import math
import random
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import httpx
from . import store
from .notebooks import fingerprint

INFO='https://api.hyperliquid.xyz/info'
INTERVAL_MS = {
    '1m': 60_000, '3m': 180_000, '5m': 300_000, '15m': 900_000, '30m': 1_800_000,
    '1h': 3_600_000, '2h': 7_200_000, '4h': 14_400_000, '8h': 28_800_000, '12h': 43_200_000,
    '1d': 86_400_000, '3d': 259_200_000, '1w': 604_800_000,
}

async def info(payload):
    async with httpx.AsyncClient(timeout=40) as client:
        response=await client.post(INFO,json=payload)
        response.raise_for_status()
        return response.json()

async def discover(force=False):
    key='market-universe'
    with store.connect() as db:
        row=db.execute('SELECT body FROM cache WHERE id=?',(key,)).fetchone()
    if row:
        saved=json.loads(row['body'])
        if not force and time.time()-saved['time']<1800:
            return saved
    dexes=await info({'type':'perpDexs'})
    result=[]
    for dex in ['',*[d['name'] for d in dexes if d]]:
        meta=await info({'type':'meta','dex':dex})
        for m in meta['universe']:
            if m.get('isDelisted'):
                continue
            coin=m['name']
            if dex and ':' not in coin:
                coin=f'{dex}:{coin}'
            result.append({'id':coin,'symbol':coin.split(':')[-1],'type':'perp','dex':dex or 'Hyperliquid','max_leverage':m['maxLeverage'],'size_decimals':m['szDecimals'],'collateral_token':meta.get('collateralToken',0),'fee_metadata':{k:m[k] for k in ['growthMode','deployerFeeScale','lastFeeScaleChangeTime'] if k in m}})
        await asyncio.sleep(.15)
    spot=await info({'type':'spotMeta'})
    tokens={t['index']:t['name'] for t in spot['tokens']}
    for m in spot['universe']:
        base,quote=[tokens[t] for t in m['tokens']]
        result.append({'id':m['name'] if m['name']=='PURR/USDC' else f"@{m['index']}",'symbol':f'{base}/{quote}','type':'spot','dex':'Hyperliquid','max_leverage':1,'base_token':m['tokens'][0],'quote_token':m['tokens'][1]})
    saved={'time':time.time(),'markets':result}
    with store.connect() as db:
        db.execute('INSERT OR REPLACE INTO cache VALUES(?,?)',(key,json.dumps(saved)))
    return saved

async def fee_for(market,wallet=''):
    # Query effective account rates, including HIP-3 per-dex adjustments. No fee constants are guessed.
    data=await info({'type':'userFees','user':wallet or '0x0000000000000000000000000000000000000000'})
    field='userSpotCrossRate' if market['type']=='spot' else 'userCrossRate'
    if field not in data:
        raise ValueError(f'Exchange did not return {field}; evaluation blocked rather than assuming fees.')
    rate=float(data[field])
    adjustments={}
    if market['type']=='perp' and market['dex']!='Hyperliquid':
        meta=await info({'type':'meta','dex':market['dex']})
        item=next((m for m in meta['universe'] if m['name']==market['id'] or m['name']==market['symbol']),{})
        if 'deployerFeeScale' not in item:
            raise ValueError(f"Missing deployer fee scale for {market['id']}; scoring blocked.")
        deployer=float(item['deployerFeeScale'])
        if not 0<=deployer<=3:
            raise ValueError('Unknown deployer fee scale semantics')
        # Protocol fee rises to match a deployer fee above the base fee.
        multiplier=max(1.,deployer)+deployer
        growth=item.get('growthMode','disabled')
        if growth=='enabled':multiplier*=.1
        elif growth not in {'disabled',None}:raise ValueError('Unknown growth mode; cannot resolve fees')
        collateral=meta.get('collateralToken',0)
        if collateral!=0:
            raise ValueError(f"Collateral token {collateral}: aligned-quote version cannot yet be verified automatically. USDC-collateral markets are supported.")
        rate*=multiplier
        adjustments={'deployer_scale':deployer,'growth_mode':growth,'multiplier':multiplier,'collateral_token':collateral,'metadata':item}
    if market['type']=='spot' and market.get('quote_token',0)!=0:
        raise ValueError('Non-USDC quote-asset fee discounts and quote conversion are not yet supported. Select an explicit USDC spot pair.')
    if not math.isfinite(rate) or rate<0:
        raise ValueError('Invalid exchange fee metadata')
    return {'taker_rate':rate,'fetched_at':time.time(),'source':'userFees','account_specific':bool(wallet),'adjustments':adjustments,'assumption':'Current fee schedule held constant through replay.'}

async def candles(market,cfg,end_ms):
    interval=INTERVAL_MS[cfg['interval']]
    closed_before = (end_ms // interval) * interval
    end_ms = closed_before - 1
    start = closed_before - (cfg['bars'] + 3) * interval
    body={'type':'candleSnapshot','req':{'coin':market['id'],'interval':cfg['interval'],'startTime':start,'endTime':closed_before}}
    key='candles-'+fingerprint(body)
    with store.connect() as db:
        row=db.execute('SELECT body FROM cache WHERE id=?',(key,)).fetchone()
    if row:
        return json.loads(row['body'])
    data=await info(body)
    records=sorted([{'time':int(r['t']),'available_at':int(r['T'])+1,'open':float(r['o']),'close':float(r['c']),'high':float(r['h']),'low':float(r['l']),'volume':float(r['v']),
                     'trade_count':int(r['n']) if r.get('n') is not None else None}
                    for r in data if int(r['T']) < closed_before],key=lambda x:x['time'])
    if len(records)<cfg['bars']:
        raise ValueError(f"{market['id']}: only {len(records)} closed bars available, need {cfg['bars']}; choose a shorter history.")
    records=records[-cfg['bars']:]
    if any(b['time']-a['time']!=interval for a,b in zip(records,records[1:])):
        raise ValueError(f"{market['id']}: historical candles have gaps; refusing to align missing bars silently.")
    funding=[]
    if market['type']=='perp':
        cursor=records[0]['time']
        while cursor<end_ms:
            page=await info({'type':'fundingHistory','coin':market['id'],'startTime':cursor,'endTime':end_ms})
            if not page:
                break
            funding.extend(page)
            nxt=max(int(x['time']) for x in page)+1
            if nxt<=cursor:
                break
            cursor=nxt
            if len(page)<500:
                break
            await asyncio.sleep(.15)
        if not funding:
            raise ValueError(f"No historical funding for {market['id']}; perpetual scoring requires it.")
    for record in records:
        record['funding'] = 0.0
    first_open = records[0]['time']
    for event in funding:
        timestamp = int(event['time'])
        index = (timestamp - first_open) // interval
        if 0 <= index < len(records) and timestamp < records[index]['available_at']:
            records[index]['funding'] += float(event['fundingRate'])
    saved={'id':key,'market':market,'records':records,'downloaded_at':time.time(),'bytes':len(json.dumps(records)),'start':records[0]['time'],'end':records[-1]['time'],'provenance':{'request':body,'sha256':fingerprint(records),'funding_events':len(funding)}}
    with store.connect() as db:
        db.execute('INSERT OR REPLACE INTO cache VALUES(?,?)',(key,json.dumps(saved)))
    return saved

def sandbox_instrument(symbol,market_type):
    # Catalog spot IDs are @indices or slash-delimited pairs.
    return 'spot' if market_type=='spot' or (market_type=='both' and (symbol.startswith('@') or '/' in symbol)) else 'perp'


def synthetic(symbol,cfg,end_ms):
    rng=random.Random(int(hashlib.sha256(symbol.encode()).hexdigest()[:12],16))
    price={'BTC':68000,'SOL':160,'HYPE':25}.get(symbol,100)
    interval=INTERVAL_MS[cfg['interval']]
    start=(end_ms//interval-cfg['bars']-1)*interval
    rows=[]
    for i in range(cfg['bars']):
        prev=price
        price*=math.exp(.00015+.001*math.sin(i/21)+rng.gauss(0,.008))
        rows.append({'time':start+i*interval,'available_at':start+(i+1)*interval,'open':prev,'close':price,'high':max(prev,price)*1.002,'low':min(prev,price)*.998,'volume':rng.uniform(100,2000),'funding':.00001 if i%8==0 else 0})
    return {'id':'demo-'+fingerprint([symbol,cfg['bars'],start]),'market':{'id':symbol,'symbol':symbol,'type':sandbox_instrument(symbol,cfg['market_type'])},'records':rows,'downloaded_at':time.time(),'bytes':len(json.dumps(rows)),'start':rows[0]['time'],'end':rows[-1]['time'],'provenance':{'synthetic':True,'seed':symbol,'sha256':fingerprint(rows)}}

async def download_source(request):
    """Managed JSON point-in-time source; no credentials, redirects, private IPs or executable downloads."""
    url=request['url']
    parsed=urlparse(url)
    if parsed.scheme!='https' or parsed.username or parsed.password or parsed.port not in (None,443):
        raise ValueError('Sources require public HTTPS URLs without credentials.')
    addresses=await asyncio.get_running_loop().run_in_executor(None,socket.getaddrinfo,parsed.hostname,443)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Source hostname resolves to a non-public IP.')
    async with httpx.AsyncClient(timeout=40,follow_redirects=False) as client:
        async with client.stream('GET',url,headers={'accept':'application/json'}) as response:
            response.raise_for_status()
            chunks=bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks)>5_000_000:
                    raise ValueError('Source exceeds 5 MB limit')
    data=json.loads(chunks)
    if not isinstance(data,list) or len(data)>50000:
        raise ValueError('Source must be a JSON array of at most 50,000 observations')
    for r in data:
        if set(r)!={'time','available_at','value'} or not all(isinstance(r[k],(int,float)) and math.isfinite(r[k]) for k in r) or r['available_at']<r['time']:
            raise ValueError('Each source row needs numeric time, available_at (Unix milliseconds, >= time), and value.')
    return {'name':request['name'],'url':url,'sha256':fingerprint(data),'records':sorted(data,key=lambda x:x['available_at']),'downloaded_at':time.time(),'availability':'provider-declared; not independently verified'}

async def resolve_markets(ids, instrument='both'):
    catalog=(await discover())['markets']
    if any(not any(m['id']==coin for m in catalog) for coin in ids):
        # A manually entered newly listed ID must be checked against a fresh universe.
        with store.connect() as db: db.execute('DELETE FROM cache WHERE id=?',('market-universe',))
        catalog=(await discover())['markets']
    resolved=[]
    for coin in ids:
        choices=[m for m in catalog if m['id']==coin and (instrument=='both' or m['type']==instrument)]
        if len(choices)!=1: raise ValueError(f'{coin}: no matching listed Hyperliquid {instrument} market. Use an exact native, HIP-3, or spot ID.')
        resolved.append(choices[0])
    return resolved

async def execution_snapshot(config):
    import uuid
    rows=[]
    if config['mode']=='demo':
        for coin in config['markets']:
            instrument=sandbox_instrument(coin,config['market_type'])
            rows.append({'market':coin,'instrument':instrument,'taker_rate':.00045,
                         'funding_rate_hourly':0. if instrument=='spot' else .00001,
                         'spread_bps':2.,'half_spread_rate':.0001,'bid':None,'ask':None,
                         'source':'Synthetic sandbox assumptions','fetched_at':time.time()})
    else:
        markets=await resolve_markets(config['markets'],config['market_type'])
        contexts={}
        for m in markets:
            try:
                fee=await fee_for(m,config.get('wallet_address',''))
                book=await info({'type':'l2Book','coin':m['id']})
                bid=float(book['levels'][0][0]['px']);ask=float(book['levels'][1][0]['px'])
                if not 0<bid<=ask: raise ValueError('Invalid bid/ask')
                funding=0.
                if m['type']=='perp':
                    dex=m['dex'] if m['dex']!='Hyperliquid' else ''
                    if dex not in contexts:
                        meta,ctx=await info({'type':'metaAndAssetCtxs','dex':dex})
                        contexts[dex]={a['name']:b for a,b in zip(meta['universe'],ctx)}
                    funding=float(contexts[dex][m['id']]['funding'])
                if not math.isfinite(funding): raise ValueError('Invalid funding rate')
                rows.append({'market':m['id'],'instrument':m['type'],'taker_rate':fee['taker_rate'],'fee_details':fee,'funding_rate_hourly':funding,'spread_bps':2*(ask-bid)/(ask+bid)*10000,'half_spread_rate':(ask-bid)/(ask+bid),'bid':bid,'ask':ask,'source':'Hyperliquid userFees / asset context / l2Book','fetched_at':time.time()})
            except (ValueError,KeyError,IndexError,httpx.HTTPError) as exc:
                rows.append({'market':m['id'],'error':str(exc),'fetched_at':time.time()})
    snapshot={'id':uuid.uuid4().hex,'created_at':time.time(),'mode':config['mode'],'market_type':config['market_type'],'wallet_address':config.get('wallet_address',''),'markets':config['markets'],'quotes':rows,'funding_policy':'Replay charges the frozen hourly funding rate. Historical funding is available to the strategy.'}
    with store.connect() as db:db.execute('INSERT INTO cache VALUES(?,?)',('execution:'+snapshot['id'],json.dumps(snapshot)))
    return snapshot

def load_execution_snapshot(sid,config):
    with store.connect() as db:row=db.execute('SELECT body FROM cache WHERE id=?',('execution:'+sid,)).fetchone()
    if not row: raise ValueError('Execution snapshot not found. Refresh execution costs.')
    snapshot=json.loads(row['body'])
    if any(snapshot.get(k)!=config.get(k,'') for k in ['mode','market_type','wallet_address']) or snapshot['markets']!=config['markets']:
        raise ValueError('Markets or fee account changed. Refresh execution costs before launching.')
    if any(q.get('error') for q in snapshot['quotes']): raise ValueError('Resolve unavailable execution costs before launching.')
    return snapshot
