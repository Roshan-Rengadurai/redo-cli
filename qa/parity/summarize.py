import re, sys
for f in sys.argv[1:]:
    case = mode = None; got = {}
    def flush():
        if case: print("%-28s run: %-45s hook: %s" % (case[:28], got.get("run","-")[:45], got.get("hook","-")[:60]))
    lines = open(f).read().splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("################ "):
            flush(); case = ln[17:]; got = {}; continue
        m = re.match(r"--- (run|hook): redo", ln)
        if m: mode = m.group(1); got.setdefault(mode, "NONE"); continue
        s = ln.strip()
        if mode and got.get(mode) == "NONE":
            if s.startswith(("Fix (", "Did you mean")):
                nxt = lines[i+1].strip() if i+1 < len(lines) else ""
                got[mode] = ("[%s] " % s.split("(")[1].split()[0] if s.startswith("Fix") else "[list] ") + re.sub(r"^\d\) ", "", nxt)
            elif s.startswith(("No rule matched", "It failed", "Failure detected")):
                got[mode] = "NONE(" + s[:30] + ")"
    flush()
