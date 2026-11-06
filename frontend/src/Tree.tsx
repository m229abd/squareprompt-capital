import {useMemo,useState,useEffect,useCallback,memo} from 'react';
import {ReactFlow,Background,Controls,MiniMap,Handle,Position,BackgroundVariant, type NodeProps,type Node,type Edge,MarkerType,useReactFlow,ReactFlowProvider} from '@xyflow/react';
import {ChevronRight,ChevronDown,GitMerge,FlaskConical,Layers3,Maximize,LockKeyhole} from 'lucide-react';
import {Button,cn} from './components/ui';
import {type Campaign,type ResearchNode} from './types';
import '@xyflow/react/dist/style.css';

type Data={research:ResearchNode;collapsed:boolean;count:number;toggle:()=>void;active:boolean;best:boolean;selected:boolean};
const TreeNode=memo(({data}:{data:Data})=>{const n=data.research;const category=n.kind==='category';return <div className={cn('research-node',category&&'category-node',data.active&&'active-node',data.best&&'best-node',data.selected&&'selected-node')}>
<Handle type="target" position={Position.Left}/><div className="node-heading"><span className={cn('status-dot',n.status)}/><span className="mono">{category?'CATEGORY':n.kind==='baseline'?'BASELINE':n.kind==='merge'?'MERGE':n.id.replace('h-','').slice(0,16).toUpperCase()}</span>{data.best&&<span className="best-label">BEST</span>}{data.active&&<span className="active-label">EXPLORING</span>}{data.count>0&&<button onClick={e=>{e.stopPropagation();data.toggle();}} className="node-collapse nodrag" aria-label={data.collapsed?'Expand branch':'Collapse branch'}>{data.collapsed?<ChevronRight size={14}/>:<ChevronDown size={14}/>}</button>}</div>
<div className="node-title">{n.kind==='merge'&&<GitMerge size={14}/>} {n.summary}</div>
<div className="node-footer"><span>{category?`${data.count} hypotheses`:n.status}</span>{!category&&<span className={n.reward!==null&&n.reward<0?'text-[#db977f]':'text-[#95d9e6]'}>{n.reward===null?'Δ —':`Δ ${n.reward>=0?'+':''}${n.reward.toFixed(3)}`}</span>}</div><Handle type="source" position={Position.Right}/></div>});
const nodeTypes={research:TreeNode as unknown as React.ComponentType<NodeProps>};
function TreeInner({campaign,onSelect,selected}:{campaign:Campaign|null;onSelect:(n:ResearchNode)=>void;selected:string|null}){
 const [collapsed,setCollapsed]=useState<Set<string>>(new Set());
 const flow=useReactFlow();
 const toggle=useCallback((id:string)=>setCollapsed(current=>{const next=new Set(current);next.has(id)?next.delete(id):next.add(id);return next;}),[]);
 const {nodes,edges}=useMemo(()=>{
  if(!campaign)return {nodes:[],edges:[]};
  const children=new Map<string,ResearchNode[]>();campaign.nodes.forEach(n=>{if(n.parent)children.set(n.parent,[...(children.get(n.parent)||[]),n]);});
  const visible:ResearchNode[]=[];const positions=new Map<string,{x:number;y:number}>();let y=0;
  function visit(n:ResearchNode,depth:number){visible.push(n);const start=y;const kids=collapsed.has(n.id)?[]:children.get(n.id)||[];if(kids.length){kids.forEach(child=>visit(child,depth+1));positions.set(n.id,{x:depth*310,y:(start+y-130)/2});}else{positions.set(n.id,{x:depth*310,y});y+=130;}}
  const root=campaign.nodes.find(n=>n.id==='baseline');if(root)visit(root,0);
  const nodeIds=new Set(visible.map(n=>n.id));
  const nodes:Node[]=visible.map(n=>({id:n.id,type:'research',position:positions.get(n.id)!,data:{research:n,collapsed:collapsed.has(n.id),count:(children.get(n.id)||[]).length,toggle:()=>toggle(n.id),active:campaign.active_node===n.id&&campaign.status==='running',best:campaign.best===n.id,selected:selected===n.id},draggable:false}));
  const edges:Edge[]=visible.filter(n=>n.parent&&nodeIds.has(n.parent)).map(n=>({id:`${n.parent}-${n.id}`,source:n.parent!,target:n.id,type:'smoothstep',animated:campaign.active_node===n.id&&campaign.status==='running',style:{stroke:campaign.active_node===n.id?'#95d9e6':'#374345',strokeWidth:1.3}}));
  visible.forEach(n=>n.merge_parents?.filter(p=>p!==n.parent&&nodeIds.has(p)).forEach(p=>edges.push({id:`merge-${p}-${n.id}`,source:p,target:n.id,type:'smoothstep',style:{stroke:'#b5a4df',strokeDasharray:'5 5'},markerEnd:{type:MarkerType.ArrowClosed,color:'#b5a4df'}})));
  return {nodes,edges};
 },[campaign,collapsed,toggle,selected]);
 useEffect(()=>{const id=setTimeout(()=>flow.fitView({padding:.13,duration:450,minZoom:.25,maxZoom:.85}),100);return ()=>clearTimeout(id);},[campaign?.id,collapsed,flow]);
 return <div className="tree-wrapper"><div className="tree-toolbar"><div className="flex items-center gap-2"><Layers3 size={14}/><span>Hypothesis landscape</span><span className="mono text-[#647173]">{campaign?.nodes.length||0} NODES</span></div><Button size="sm" variant="ghost" onClick={()=>flow.fitView({padding:.1,duration:450})}><Maximize size={14}/>Fit view</Button></div>
 {campaign?<ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} onNodeClick={(_,n)=>onSelect(n.data.research as ResearchNode)} minZoom={.15} maxZoom={2} fitView proOptions={{hideAttribution:true}} colorMode="dark"><Background variant={BackgroundVariant.Dots} gap={22} size={1} color="#2c3638"/><Controls showInteractive={false}/><MiniMap pannable zoomable nodeColor={n=>(n.data.research as ResearchNode).kind==='category'?'#354649':'#78a9b3'} maskColor="rgba(9,16,12,.8)" style={{background:'#111819',height:85,width:140}}/></ReactFlow>:<div className="tree-empty"><div className="empty-tree-graphic"><div className="empty-root"><FlaskConical size={19}/></div><div className="empty-connectors"/>{[0,1,2].map(i=><div className="empty-branch" key={i}><span/><span/><span/></div>)}</div><h3>No campaign selected</h3><p>Create a campaign to view its search tree.</p></div>}
 <div className="tree-legend"><span><i className="bg-[#95d9e6]"/>Completed</span><span><i className="bg-[#e1bc7f]"/>Exploring</span><span><i className="bg-[#d58b7e]"/>Negative / failed</span><span><i className="bg-[#807099]"/>Rejected</span><span><i className="bg-[#424f51]"/>Queued</span><span className="ml-auto"><LockKeyhole size={11}/> Holdout isolated</span></div></div>
}
export default function SearchTree(props:{campaign:Campaign|null;onSelect:(n:ResearchNode)=>void;selected:string|null}){return <ReactFlowProvider><TreeInner {...props}/></ReactFlowProvider>}
