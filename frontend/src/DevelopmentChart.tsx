import {useEffect,useState} from 'react';
import {TrendingUp} from 'lucide-react';
import {Area,AreaChart,CartesianGrid,ReferenceLine,ResponsiveContainer,Tooltip,XAxis,YAxis} from 'recharts';
import {Button} from './components/ui';
import {api,pct,type Campaign,type Curve} from './types';

type Point=Pick<Curve,'time'|'equity'>;
type Benchmarks={buy_hold:Point[]};
const colors={best:'#95d9e6',baseline:'#9baaad',buy:'#e3bd82'};
const series=[
 {key:'baseline',label:'Baseline',fill:'none',width:1.5,dash:'4 4'},
 {key:'best',label:'Best pipeline',fill:'url(#equity)',width:2},
 {key:'buy',label:'Buy & hold',fill:'none',width:2},
] as const;
const legendOrder=[series[1],series[0],series[2]];

export default function DevelopmentChart({campaign}:{campaign:Campaign|null}){
 const id=campaign?.id;
 const baseline=campaign?.nodes.find(n=>n.id==='baseline');
 const best=campaign?.nodes.find(n=>n.id===campaign.best);
 const ready=!!campaign?.calibration?.evaluation_bars||!!baseline?.curve?.length;
 const [visible,setVisible]=useState({best:true,baseline:true,buy:false});
 const [retry,setRetry]=useState(0);
 const [response,setResponse]=useState<{id:string;data?:Benchmarks;error?:string}|null>(null);
 const result=response?.id===id?response:null;
 const benchmarks=result?.data;
 useEffect(()=>{
  if(!id||!ready||!visible.buy||benchmarks)return;
  const request=new AbortController();
  api<Benchmarks>(`/campaigns/${id}/benchmarks`,{signal:request.signal})
   .then(data=>{if(!request.signal.aborted)setResponse({id,data});})
   .catch(e=>{if(!request.signal.aborted)setResponse({id,error:(e as Error).message});});
  return ()=>request.abort();
 },[id,ready,retry,visible.buy,benchmarks]);
 const initial=campaign?.config.initial_capital||10000;
 const lookup=(curve:Point[]|undefined)=>new Map(curve?.map(r=>[r.time,(r.equity/initial-1)*100]));
 const base=lookup(baseline?.curve),buy=lookup(benchmarks?.buy_hold);
 const pipeline=lookup(best?.curve);
 const shown=series.filter(({key})=>visible[key]);
 const curves={best:best?.curve,baseline:baseline?.curve,buy:benchmarks?.buy_hold};
 const axis=shown.map(({key})=>curves[key]).find(curve=>curve?.length)??[];
 const values=axis.map(({time})=>({time,best:pipeline.get(time),baseline:base.get(time),buy:buy.get(time)}));
 return <><div className="performance-header"><span className="performance-number">{best?.metrics?pct(best.metrics.return):'—'}<small>net return</small></span>
 <div className="flex flex-wrap gap-2" role="group" aria-label="Development performance series">
  {legendOrder.map(({key,label})=><Button key={key} size="sm" variant={visible[key]?'outline':'ghost'}
   aria-pressed={visible[key]} className={visible[key]?'':'muted'}
   onClick={()=>setVisible(current=>({...current,[key]:!current[key]}))}>
   <i aria-hidden="true" className="inline-block h-0.5 w-2.5" style={{background:colors[key],opacity:visible[key]?1:.45}}/>{label}
  </Button>)}
 </div></div>
 <div className="chart-area">{values.length?<ResponsiveContainer width="100%" height="100%"><AreaChart data={values} margin={{top:12,right:15,left:-20,bottom:0}} aria-label="Development returns with passive benchmarks"><defs><linearGradient id="equity" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor={colors.best} stopOpacity={.22}/><stop offset="100%" stopColor={colors.best} stopOpacity={0}/></linearGradient></defs><CartesianGrid stroke="#262e30" vertical={false} strokeDasharray="3 5"/><XAxis dataKey="time" tickFormatter={t=>new Date(t).toLocaleDateString(undefined,{month:'short',day:'numeric'})} minTickGap={50} tick={{fontSize:10,fill:'#717e81'}} axisLine={false} tickLine={false}/><YAxis tickFormatter={v=>`${v.toFixed(1)}%`} tick={{fontSize:10,fill:'#717e81'}} axisLine={false} tickLine={false}/><Tooltip contentStyle={{background:'#161e1f',border:'1px solid #3b4648',fontSize:12,borderRadius:8}} labelFormatter={t=>new Date(t).toLocaleString()} formatter={(v:number)=>[`${v.toFixed(2)}%`]}/><ReferenceLine y={0} stroke="#3d494b"/>{shown.filter(({key})=>curves[key]?.length).map(series=><Area key={series.key} type="linear" dataKey={series.key}
  stroke={colors[series.key]} strokeDasharray={'dash' in series?series.dash:undefined} fill={series.fill}
  strokeWidth={series.width} name={series.label} isAnimationActive={false}/>)}</AreaChart></ResponsiveContainer>:<div className="chart-placeholder"><TrendingUp size={28}/><span>{!shown.length?'Select a series to show':visible.buy&&ready&&!benchmarks?'Waiting for Buy & hold':'Waiting for the development window'}</span></div>}</div>
 {visible.buy&&benchmarks&&<p className="text-xs muted mt-2">Buy &amp; hold keeps a fixed quantity from the first scored open to the final close, net of execution costs and funding.</p>}
 {visible.buy&&ready&&!benchmarks&&<p className="text-xs muted mt-2" role="status">{result?.error?<>{result.error} <button className="underline" onClick={()=>{setResponse(null);setRetry(x=>x+1);}}>Retry Buy &amp; hold</button></>:'Loading Buy & hold…'}</p>}</>;
}
