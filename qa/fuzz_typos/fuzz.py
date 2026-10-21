#!/usr/bin/env python3
"""Typo fuzzer for redo (black-box). Usage: python3 fuzz.py [outjson]"""
import json, os, random, re, subprocess, sys, tempfile, time, statistics, collections

REDO = os.environ.get("REDO_BIN") or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".venv", "bin", "redo")
random.seed(7)

CMDS = """git status|git log --oneline -3|git branch|git diff --stat|git remote -v|git tag|git stash list
ls -la|python3 --version|node --version|npm --version|brew --version|grep -c root /etc/passwd
cat /etc/hosts|head -3 /etc/hosts|which python3|echo hi|date|uname -a|whoami|pwd|df -h|du -sh .
printenv HOME|wc -l /etc/hosts|sort /etc/hosts|uniq /etc/hosts|tail -2 /etc/hosts|file /bin/ls
stat /etc/hosts|sw_vers|sysctl -n hw.ncpu|curl --version|ssh -V|tar --version|zip -v|less --version
man -w ls|id|hostname|uptime|ps -p 1|top -l 1 -n 0|cargo --version|basename /etc/hosts|dirname /etc/hosts
env""".replace("\n", "|").split("|")
CMDS = [c.strip() for c in CMDS if c.strip()]

NB = {}
rows = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
for r, row in enumerate(rows):
    for i, ch in enumerate(row):
        n = set()
        if i: n.add(row[i-1])
        if i < len(row)-1: n.add(row[i+1])
        NB[ch] = sorted(n)

def variants(word):
    out = {}
    L = len(word)
    if L >= 2:
        i = random.randrange(L-1)
        if word[i] != word[i+1]:
            out["transpose"] = word[:i] + word[i+1] + word[i] + word[i+2:]
    if L >= 3:
        i = random.randrange(1, L)  # keep first char mostly
        out["delete"] = word[:i] + word[i+1:]
    i = random.randrange(L); out["duplicate"] = word[:i+1] + word[i] + word[i+1:]
    cand = [j for j, c in enumerate(word) if c in NB]
    if cand:
        j = random.choice(cand); out["neighbour"] = word[:j] + random.choice(NB[word[j]]) + word[j+1:]
    if word.lower() != word.upper():
        out["case"] = word.capitalize() if word[0].islower() else word.lower()
        if out["case"] == word: out["case"] = word.upper()
    return {k: v for k, v in out.items() if v != word}

def gen():
    cases = []
    for cmd in CMDS:
        toks = cmd.split()
        for t, v in variants(toks[0]).items():
            cases.append((cmd, "name", t, " ".join([v] + toks[1:])))
        if len(toks) > 1:
            for t, v in variants(toks[1]).items():
                cases.append((cmd, "arg", t, " ".join([toks[0], v] + toks[2:])))
            cases.append((cmd, "join", "missing_space", toks[0] + toks[1] + (" " + " ".join(toks[2:]) if toks[2:] else "")))
    return cases

def norm(s): return " ".join(s.split())

EQUIV = {("git stash list", "git stash"), ("ls -la", "ls -al"), ("ls -la", "ls -l -a")}

def classify(orig, fix):
    if fix is None: return "NONE"
    if norm(fix) == norm(orig): return "EXACT"
    if (norm(orig), norm(fix)) in EQUIV: return "ACCEPTABLE"
    return "WRONG"

def extract_fix(out):
    lines = out.splitlines()
    for i, l in enumerate(lines):
        if re.match(r"\s*Fix\b.*:\s*$", l):
            for nl in lines[i+1:]:
                if nl.strip(): return nl.strip()
    return None

def main():
    outpath = sys.argv[1] if len(sys.argv) > 1 else "results.json"
    base = tempfile.mkdtemp(prefix="redoqa4.")
    repo = os.path.join(base, "repo"); os.makedirs(repo)
    for c in (["git", "init", "-q", "-b", "main"], ["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(c, cwd=repo, check=True)
    hist = os.path.join(base, "hist"); open(hist, "w").close()
    env = dict(os.environ, REDO_HOME=os.path.join(base, "rh"), NO_COLOR="1", SHELL="/bin/zsh", HISTFILE=hist, HOMEBREW_NO_AUTO_UPDATE="1", HOMEBREW_NO_ANALYTICS="1", NPM_CONFIG_UPDATE_NOTIFIER="false")
    os.makedirs(env["REDO_HOME"])
    env["PATH"] = env["PATH"]  # real PATH: tools must be found
    results, lat = [], []
    for orig, pos, typ, var in gen():
        try:
            r = subprocess.run([REDO, "run", var], cwd=repo, env=env, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            results.append(dict(orig=orig, pos=pos, typ=typ, var=var, verdict="SKIP_TIMEOUT")); continue
        if r.returncode == 0:
            results.append(dict(orig=orig, pos=pos, typ=typ, var=var, verdict="SKIP")); continue
        t = time.perf_counter()
        f = subprocess.run([REDO, "--no-push"], cwd=repo, env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        lat.append(time.perf_counter() - t)
        if f.returncode not in (0, 1) or "Traceback" in f.stderr + f.stdout:
            v, fix = "CRASH", None
        else:
            fix = extract_fix(f.stdout); v = classify(orig, fix)
        if var not in f.stdout:
            v = "STALE?" if v != "CRASH" else v
        results.append(dict(orig=orig, pos=pos, typ=typ, var=var, verdict=v, fix=fix, rc=r.returncode, out=f.stdout, err=f.stderr[-500:]))
    json.dump(dict(results=results, lat=lat), open(outpath, "w"), indent=1)
    lat.sort()
    print("ran", len(results), "fix calls", len(lat), "p50 %.0fms p95 %.0fms" % (statistics.median(lat)*1000, lat[int(.95*len(lat))]*1000))
    tab = collections.defaultdict(collections.Counter)
    for x in results: tab[(x["pos"], x["typ"])][x["verdict"]] += 1
    for k in sorted(tab): print(k, dict(tab[k]))
    print("TOTAL", collections.Counter(x["verdict"] for x in results))

if __name__ == "__main__":
    main()
