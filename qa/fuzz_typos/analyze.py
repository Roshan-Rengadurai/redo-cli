#!/usr/bin/env python3
"""Re-classify fuzz results.json: single fix vs 'Did you mean' list vs '#' hint."""
import json, re, sys, collections, statistics
d = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "results.json"))
R, lat = d["results"], sorted(d["lat"])
norm = lambda s: " ".join(s.split())
EQUIV = {("ls -la", "ls -al"), ("git stash list", "git stash")}
def parse(out):
    L = out.splitlines()
    for i, l in enumerate(L):
        m = re.match(r"\s*Fix \((\w+) confidence\):(.*)", l)
        if m: return "fix", m.group(1) + ("+warn" if m.group(2).strip() else ""), [L[i+1].strip()]
        if l.strip() == "Did you mean:":
            items = []
            for nl in L[i+1:]:
                mm = re.match(r"\s*\d+\)\s*(.*)", nl)
                if not mm: break
                items.append(mm.group(1).strip())
            return "list", "", items
    return "none", "", []
def cls(x):
    if x["verdict"] in ("SKIP", "SKIP_TIMEOUT", "CRASH"): return x["verdict"], "", []
    kind, conf, items = parse(x["out"])
    o = norm(x["orig"])
    if kind == "none" or (kind == "fix" and items[0].startswith("#")): return "NONE", conf, items
    if kind == "fix":
        f = norm(items[0])
        return ("EXACT" if f == o else "ACCEPTABLE" if (o, f) in EQUIV else "WRONG"), conf, items
    if norm(items[0]) == o: return "EXACT(list#1)", "", items
    if o in map(norm, items): return "ACCEPTABLE(in list)", "", items
    return "WRONG(list)", "", items
tab = collections.defaultdict(collections.Counter); wrong = []
for x in R:
    v, conf, items = cls(x); x["v2"] = v
    if x["pos"] == "join": key = ("join", "missing_space")
    else: key = (x["pos"], x["typ"])
    tab[key][v] += 1
    if v.startswith("WRONG"): wrong.append((x["pos"], x["typ"], x["var"], x["orig"], conf, items))
cols = ["EXACT", "EXACT(list#1)", "ACCEPTABLE", "ACCEPTABLE(in list)", "WRONG", "WRONG(list)", "NONE", "CRASH", "SKIP"]
print("key".ljust(26), *[c[:12].rjust(13) for c in cols])
tot = collections.Counter()
for k in sorted(tab):
    tot += tab[k]; print(("%s/%s" % k).ljust(26), *[str(tab[k][c]).rjust(13) for c in cols])
print("TOTAL".ljust(26), *[str(tot[c]).rjust(13) for c in cols])
print("tested(non-skip)", sum(v for k, v in tot.items() if not k.startswith("SKIP")))
print("fix latency n=%d p50=%.0fms p95=%.0fms max=%.0fms" % (len(lat), statistics.median(lat)*1e3, lat[int(.95*len(lat))]*1e3, lat[-1]*1e3))
print("\nWRONG:")
for w in wrong: print(" %s/%s  %-24s (want %s) -> [%s] %s" % (w[0], w[1], w[2], w[3], w[4], " || ".join(w[5])))
