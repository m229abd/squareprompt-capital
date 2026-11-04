import ast
import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path
import nbformat
from .constants import CATEGORIES
from .store import ROOT

BASELINE_DIR=Path(os.getenv('BASELINE_DIR','/opt/baselines'))

def source(cell):
    s=cell.get('source','')
    return ''.join(s) if isinstance(s,list) else s

def validate(nb, parent=None, category=None, merge=False):
    nbformat.validate(nbformat.from_dict(nb))
    cells={c.get('id'):c for c in nb['cells']}
    expected={'contract',*[k for k,_,_ in CATEGORIES]}
    if set(cells)!=expected or len(cells)!=len(nb['cells']):
        raise ValueError('Notebook must retain the contract and all twelve stable cell IDs.')
    if parent:
        old={c['id']:c for c in parent['cells']}
        for key,cell in cells.items():
            if key=='contract' or (not merge and key!=category):
                if source(cell)!=source(old[key]) or cell['cell_type']!=old[key]['cell_type']:
                    raise ValueError(f'Edit outside allowed cell: {key}')
    for cell in nb['cells']:
        if cell['cell_type']!='code':
            continue
        tree=ast.parse(source(cell))
        # Defense in depth. The runner is also network-isolated and has no credentials or dataset mounts.
        for node in ast.walk(tree):
            if isinstance(node,(ast.Import,ast.ImportFrom)):
                names=[x.name.split('.')[0] for x in node.names] if isinstance(node,ast.Import) else [(node.module or '').split('.')[0]]
                if any(n not in {'math','numpy','pandas','statistics','collections'} for n in names):
                    raise ValueError('Only numerical imports are allowed inside strategy cells; request sources through the managed gateway.')
            if isinstance(node,ast.Name) and (node.id.startswith('__') or node.id in {'open','eval','exec','compile','globals','locals','getattr','setattr','vars','input','breakpoint','exit','quit'}):
                raise ValueError(f'Forbidden runtime access: {node.id}')
            if isinstance(node,ast.Attribute) and (node.attr.startswith('_') or node.attr.startswith(('read_','to_')) or node.attr in {'load','save','loadtxt','savetxt','fromfile','memmap','system','popen'}):
                raise ValueError('File, process and introspection operations are forbidden in strategy cells.')
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr in {'shift','diff','pct_change'}:
                if node.args and isinstance(node.args[0],ast.UnaryOp) and isinstance(node.args[0].op,ast.USub):
                    raise ValueError('Future-looking shifts are forbidden.')
            if isinstance(node,ast.keyword) and node.arg=='center' and isinstance(node.value,ast.Constant) and node.value.value is True:
                raise ValueError('Centered rolling windows leak future information.')
    return nb

def repo(cid):
    from dulwich.repo import Repo
    path=ROOT/'notebooks'
    path.mkdir(parents=True,exist_ok=True)
    if not (path/'.git').exists():
        Repo.init(str(path))
    return path

def commit(cid,nid,attempt,nb,status,parents=None):
    import time
    import fcntl
    from dulwich.repo import Repo
    from dulwich import porcelain
    from dulwich.objects import Commit
    path=repo(cid)
    name=f'{cid}/{nid}-attempt-{attempt}-{status}.ipynb'
    (path/cid).mkdir(exist_ok=True)
    target=path/name
    encoded=json.dumps(nb,indent=1,ensure_ascii=False)
    # One lock covers the index and refs for all campaigns in the shared inner Git.
    with (path/'.git'/'capital.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        repository=Repo(str(path))
        if target.exists() and target.read_text()==encoded:
            try:
                return {'path':name,'sha':repository.head().decode()}
            except KeyError:
                pass
        target.write_text(encoded)
        porcelain.add(repository,paths=[str(target)])
        sha=porcelain.commit(repository,message=f'{nid}: {status} (attempt {attempt})'.encode(),author=b'Squareprompt Research <research@squareprompt.local>')
        if parents and len(set(parents))>1:
            merge=Commit()
            merge.tree=repository[sha].tree
            merge.parents=[p.encode() for p in dict.fromkeys(parents)]
            merge.author=merge.committer=b'Squareprompt Research <research@squareprompt.local>'
            merge.author_time=merge.commit_time=int(time.time())
            merge.author_timezone=merge.commit_timezone=0
            merge.message=f'{nid}: semantic merge'.encode()
            repository.object_store.add_object(merge)
            sha=merge.id
        repository.refs[f'refs/heads/campaigns/{cid}/{nid}'.encode()]=sha
        return {'path':name,'sha':sha.decode()}

def read(cid,ref):
    return json.loads((repo(cid)/ref['path']).read_text())

def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()

def apply_patch(parent, response, category, merge=False):
    result=copy.deepcopy(parent)
    patches=response.get('cells',{})
    if not patches or not isinstance(patches,dict):
        raise ValueError('Developer must return a cells object with stable cell IDs and replacement source strings.')
    for cell in result['cells']:
        if cell['id'] in patches:
            if not isinstance(patches[cell['id']],str):
                raise ValueError('Cell source must be a string')
            cell['source']=patches[cell['id']]
        if cell['cell_type']=='code':
            cell['outputs']=[]
            cell['execution_count']=None
    # Save first; validate later so invalid attempts remain in Git.
    return result
