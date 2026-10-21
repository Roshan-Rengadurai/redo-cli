"""redo black-box fuzzer v2 (qa5). Usage: python3 fuzz.py [build|run|report]"""
import os, sys, random, subprocess, shutil, json, time, re, shlex, tempfile
from multiprocessing import Pool
HERE=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,HERE)
from ptyd import Sh, clean, R
FX=os.path.join(HERE,'fx'); WORK=os.path.join(HERE,'work'); OUT=os.path.join(HERE,'results.json')
ENV=dict(PATH=os.path.dirname(R)+':'+os.environ['PATH'], NO_COLOR='1', SHELL='/bin/zsh', LANG='en_US.UTF-8', TERM='dumb',
         HOMEBREW_NO_AUTO_UPDATE='1', HOMEBREW_NO_ANALYTICS='1', npm_config_offline='true',
         PIP_NO_INDEX='1', PIP_DISABLE_PIP_VERSION_CHECK='1', GIT_PAGER='cat', PAGER='cat')
CORPUS = r'''git status -s
git status
git log --oneline -5
git diff --stat
git branch -a
git checkout develop
git checkout feature/auth
git switch develop
git show HEAD --stat
git stash list
git remote -v
git log --graph --oneline
git blame README.md
git diff HEAD~1 --stat
git branch --list
git tag
git fetch origin
git push origin develop
git merge develop
git rev-parse HEAD
git log -1 --stat
git shortlog -sn HEAD
git ls-files
git grep add
git add README.md
git log --oneline develop
git diff main develop
npm run test
npm run lint
npm run build
npm run dev
npm test
npm ls
npm --version
npm pkg get name
python3 src/app.py
python3 -m json.tool package.json
python3 -c "print(1)"
python3 --version
pip3 list
pip3 show pip
pip3 --version
brew list --versions
brew --prefix
make build
make test
make clean
make -n build
cat README.md
head -5 src/utils.py
tail -n 3 README.md
grep -rn def src
grep -c def src/utils.py
ls -la src
wc -l README.md
find . -name "*.py"
du -sh src
cd src && ls
tar -czf /tmp/qa5x.tgz src
sort README.md
cp README.md README.bak
mkdir -p build/out
cat .env
node --version
stat README.md
sed -n 1,3p README.md'''.splitlines()
SUBCMDS={'git','npm','pip3','brew','make'}
QW='qwertyuiop asdfghjkl zxcvbnm'.split()
def neigh(c):
    lc=c.lower()
    for r,row in enumerate(QW):
        if lc in row:
            i=row.index(lc); n=[row[j] for j in (i-1,i+1) if 0<=j<len(row)]
            return n
    return []
RISKY_SUB={'install','i','uninstall','publish','s','se','search','update','up','upgrade','ci','exec','x','init','link','ln','unlink','unpublish','dedupe','prune','rebuild','start','stop','restart','audit','doctor','login','logout','adduser','cache','reinstall','cleanup','tap','untap','services','rm','remove','download','wheel','r','un','add','set','edit','config','c','fund','pack','version','completion','bugs','docs','home','repo','owner','team','token','access','hook','org','profile','star','stars','unstar','view','v','info','show','outdated','explore','help-search','pkg','query','sbom','diff','dist-tag','deprecate','shrinkwrap'}
def roles(cmd):
    toks=cmd.split(' '); out=[]
    for i,t in enumerate(toks):
        if any(ch in t for ch in '"\'&|~') or t in('&&',): r=None
        elif i==0: r='cmd'
        elif i==1 and toks[0] in SUBCMDS and not t.startswith('-'): r='sub'
        elif t.startswith('-') and len(t)>1: r='flag'
        elif t.startswith('/'): r=None
        else: r='arg'
        out.append(r)
    return toks,out
def mutate(rng,tok,typ,role):
    pre=''; core=tok
    if role=='flag': m=re.match(r'^(-+)(.*)$',tok); pre,core=m.group(1),m.group(2)
    if typ=='dash':
        if role!='flag': return None
        opts=[pre+'-'+core, pre[:-1]+core]
        return rng.choice(opts)
    if typ=='case':
        letters=[i for i,c in enumerate(core) if c.isalpha()]
        if not letters: return None
        if rng.random()<.5: core2=core.upper() if core!=core.upper() else core.lower()
        else: i=rng.choice(letters); core2=core[:i]+core[i].swapcase()+core[i+1:]
        return pre+core2
    if len(core)<2 and typ in('transpose','delete'): return None
    if typ=='transpose':
        idx=[i for i in range(len(core)-1) if core[i]!=core[i+1]]
        if not idx: return None
        i=rng.choice(idx); return pre+core[:i]+core[i+1]+core[i]+core[i+2:]
    if typ=='delete':
        i=rng.randrange(len(core)); return pre+core[:i]+core[i+1:]
    if typ=='duplicate':
        i=rng.randrange(len(core)); return pre+core[:i+1]+core[i]+core[i+1:]
    if typ=='qwerty':
        idx=[i for i,c in enumerate(core) if neigh(c)]
        if not idx: return None
        i=rng.choice(idx); return pre+core[:i]+rng.choice(neigh(core[i]))+core[i+1:]
TYPES=['transpose','delete','duplicate','qwerty','nospace','dash','case']
def variants(rng):
    vs=[]
    for cmd in CORPUS:
        toks,rs=roles(cmd)
        for pos in ('cmd','sub','flag','arg'):
            idxs=[i for i,r in enumerate(rs) if r==pos]
            if not idxs: continue
            for typ in TYPES:
                i=rng.choice(idxs); t2=toks[:]
                if typ=='nospace':
                    if pos=='cmd':
                        if len(toks)<2: continue
                        t2=[toks[0]+toks[1]]+toks[2:]
                    else: t2=toks[:i-1]+[toks[i-1]+toks[i]]+toks[i+1:]
                else:
                    m=mutate(rng,toks[i],typ,pos)
                    if m is None or m==toks[i]: continue
                    t2[i]=m
                v=' '.join(t2)
                if v!=cmd: vs.append(dict(orig=cmd,var=v,pos=pos,typ=typ))
    seen=set(); out=[]
    for v in vs:
        if v['var'] not in seen: seen.add(v['var']); out.append(v)
    return out
def fresh():
    d=tempfile.mkdtemp(dir=WORK); subprocess.run(['cp','-Rp',FX+'/.',d]); return d
def sh(line,cwd,env=ENV,timeout=20):
    try: p=subprocess.run(['/bin/zsh','-fc',line],cwd=cwd,env=env,capture_output=True,text=True,timeout=timeout)
    except subprocess.TimeoutExpired: return 124,'',''
    return p.returncode,p.stdout,p.stderr
def unsafe(v):
    w=v['var'].split(' ')
    if v['pos']=='cmd' and v['typ']!='nospace' or (v['pos']=='cmd'):
        if w[0]!=v['orig'].split(' ')[0] and shutil.which(w[0],path=ENV['PATH']): return 'variant cmd exists: '+w[0]
    if w[0] in('npm','brew','pip3') and len(w)>1 and w[1] in RISKY_SUB: return 'risky sub '+w[1]
    if w[0] in('npm','brew','pip3') and len(w)>1 and w[1].lower() in RISKY_SUB: return 'risky sub '+w[1]
    return None
def viable(v):
    why=unsafe(v)
    if why: return dict(v,skip=why)
    d=fresh(); rc,o,e=sh(v['var'],d+'/repo'); shutil.rmtree(d,ignore_errors=True)
    if rc==0: return dict(v,skip='succeeds')
    if rc==124: return dict(v,skip='timeout')
    return dict(v,skip=None,vrc=rc)
FIX_RE=re.compile(r'Fix \((\w+) confidence\):\n  (.*)')
LIST_RE=re.compile(r'Did you mean:\n((?:  \d+\) .*\n?)+)')
def parse(out,var):
    out=clean(out); r={}
    m=re.search(r'You ran:.*\n  (.*)',out); r['ran']=m.group(1).strip() if m else None
    r['crash']='Traceback' in out
    m=FIX_RE.search(out)
    if m: r['fix'],r['conf']=m.group(2).strip(),m.group(1)
    m=re.search(r'Did you mean:.*\(.*cancel\)\n((?:[> ] .*\n?)+)',out)
    if m: r['list']=[l[2:].strip() for l in m.group(1).strip('\n').split('\n') if l.strip()]; r['picker_blocked']=True
    m=LIST_RE.search(out)
    if m: r['list']=[re.sub(r'^\s*\d+\)\s*','',l).strip() for l in m.group(1).strip('\n').split('\n')]
    r['raw']=out[-1500:]
    return r
DANGER=re.compile(r'\b(rm|install|uninstall|reinstall|upgrade|--force|-f\b|reset|clean -|push)\b')
def same(fix,orig):
    try: return shlex.split(fix)==shlex.split(orig)
    except ValueError: return fix.split()==orig.split()
def equiv(fix,orig):
    if DANGER.search(fix) and not DANGER.search(orig): return False
    d1,d2=fresh(),fresh()
    a=sh(fix,d1+'/repo'); b=sh(orig,d2+'/repo')
    shutil.rmtree(d1,ignore_errors=True); shutil.rmtree(d2,ignore_errors=True)
    return a[0]==0 and a[1]==b[1]
def classify(v,r):
    if r.get('crash'): return 'CRASH'
    if 'No failures captured' in r.get('raw',''): return 'NOT-RECORDED'
    if r.get('ran')!=v['var']: return 'MISREPORT'
    if 'fix' in r:
        f=r['fix']
        if f.startswith('#') or f.startswith('redo run'): return 'NONE'
        if same(f,v['orig']): return 'EXACT'
        if equiv(f,v['orig']): return 'EQUIVALENT'
        return 'WRONG-'+r['conf'].upper()
    if 'list' in r:
        if any(same(x,v['orig']) or equiv(x,v['orig']) for x in r['list']): return 'LIST-HAS-IT'
        return 'LIST-MISS'
    return 'NONE'
def measure_run(v):
    d=fresh(); env=dict(ENV,REDO_HOME=d+'/rh',HOME=d+'/home',HISTFILE=d+'/hist'); os.makedirs(env['HOME'])
    subprocess.run([R,'run',v['var']],cwd=d+'/repo',env=env,capture_output=True,timeout=30)
    t=time.perf_counter(); p=subprocess.run([R,'--no-push'],cwd=d+'/repo',env=env,capture_output=True,text=True,timeout=30); dt=time.perf_counter()-t
    r=parse(p.stdout+p.stderr,v); r['lat']=dt; r['rc']=p.returncode
    shutil.rmtree(d,ignore_errors=True)
    return dict(v,mode='run',**r,cls=classify(v,r))
def measure_hook(v):
    d=fresh(); s=None
    try:
        s=Sh('zsh',d+'/home',d+'/rh',d+'/repo')
        s.line(v['var'],timeout=20)
        t=time.perf_counter(); out=s.line('redo --no-push',timeout=20); dt=time.perf_counter()-t
        out=out.split('redo --no-push',1)[-1]
    finally:
        if s: s.close()
    r=parse(out,v); r['lat']=dt
    shutil.rmtree(d,ignore_errors=True)
    return dict(v,mode='hook',**r,cls=classify(v,r))
def main():
    cmd=sys.argv[1] if len(sys.argv)>1 else 'all'
    os.makedirs(WORK,exist_ok=True)
    if cmd in('build','all'):
        shutil.rmtree(FX,ignore_errors=True); subprocess.run([HERE+'/fixture.sh',FX],check=True,capture_output=True)
        bad=[c for c in CORPUS if sh(c,fresh()+'/repo')[0]!=0]
        print('corpus',len(CORPUS),'failing originals:',bad)
    if cmd in('run','all'):
        random.seed(2026); rng=random.Random(2026)
        vs=variants(rng); print('candidates',len(vs))
        with Pool(8) as p: vv=p.map(viable,vs)
        keep=[v for v in vv if not v['skip']]
        keep=sorted(rng.sample(keep,min(420,len(keep))),key=lambda v:(v['orig'],v['pos'],v['typ']))
        print('viable',len(keep),'skipped',len(vv)-len(keep))
        with Pool(8) as p: rr=p.map(measure_run,keep)
        with Pool(6) as p: hh=p.map(measure_hook,keep)
        json.dump(dict(skipped=[v for v in vv if v['skip']],results=rr+hh),open(OUT,'w'),indent=1)
    if cmd in('report','all','run'): report()
def report():
    D=json.load(open(OUT)); res=D['results']
    for r in res:
        if 'No failures captured' in r.get('raw',''): r['cls']='NOT-RECORDED'
        if 'cancel)' in r.get('raw',''):
            pr=parse(r['raw'],r); r.update(list=pr.get('list'),picker_blocked=True); r['cls']=classify(r,dict(pr,ran=r['var']))
    from collections import Counter,defaultdict
    CL=['EXACT','EQUIVALENT','LIST-HAS-IT','NONE','LIST-MISS','WRONG-LOW','WRONG-MEDIUM','WRONG-HIGH','MISREPORT','CRASH','NOT-RECORDED']
    print(f"\nmeasurements: {len(res)}  (variants: {len(res)//2}, skipped candidates: {len(D['skipped'])})")
    tab=defaultdict(Counter)
    for r in res: tab[(r['mode'],r['pos'],r['typ'])][r['cls']]+=1; tab[(r['mode'],'ALL','ALL')][r['cls']]+=1; tab[(r['mode'],r['pos'],'ALL')][r['cls']]+=1; tab[(r['mode'],'ALL',r['typ'])][r['cls']]+=1
    print(f"{'mode':5}{'pos':5}{'type':10}{'n':>4} {'ok%':>5} "+' '.join(f'{c[:7]:>7}' for c in CL))
    for k in sorted(tab):
        c=tab[k]; n=sum(c.values())-c['NOT-RECORDED']; ok=c['EXACT']+c['EQUIVALENT']
        print(f"{k[0]:5}{k[1]:5}{k[2]:10}{n:>4} {100*ok/n:5.0f} "+' '.join(f'{c[x]:>7}' for x in CL))
    for m in('run','hook'):
        L=sorted(r['lat'] for r in res if r['mode']==m and not r.get('picker_blocked'))
        print(f"latency {m} redo --no-push: p50={1000*L[len(L)//2]:.0f}ms p95={1000*L[int(len(L)*.95)]:.0f}ms max={1000*L[-1]:.0f}ms")
    print('--no-push opened an interactive picker (blocked until timeout):',sum(1 for r in res if r.get('picker_blocked')),'cases')
    print('\nWRONG / MISREPORT / CRASH cases:')
    for r in sorted(res,key=lambda r:(r['cls'],r['var'])):
        if r['cls'].startswith('WRONG') or r['cls'] in('MISREPORT','CRASH','LIST-MISS'):
            print(f"  {r['cls']:12} {r['mode']:4} {r['pos']}/{r['typ']:9} {r['var']!r:40} -> {r.get('fix') or r.get('list')!r}  (want {r['orig']!r})" + (f" ran={r['ran']!r}" if r['cls']=='MISREPORT' else ''))
if __name__=='__main__': main()
