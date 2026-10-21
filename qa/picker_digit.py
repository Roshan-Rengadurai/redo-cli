import os, pty, time, select, subprocess, tempfile, sys
R = os.environ.get("REDO_BIN") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".venv", "bin", "redo")
home = tempfile.mkdtemp(); P = home + "/push"
env = dict(os.environ, REDO_HOME=home, NO_COLOR="1", HISTFILE=os.devnull)
cwd = tempfile.mkdtemp()
subprocess.run([R, "run", "git sta"], env=env, cwd=cwd, capture_output=True)
pid, fd = pty.fork()
if pid == 0:
    os.chdir(cwd); env["REDO_PUSH_FILE"] = P
    os.execve(R, ["redo"], env)
out = b""
def pump(t):
    global out
    end = time.time() + t
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try: out += os.read(fd, 4096)
            except OSError: return
pump(1.5); os.write(fd, sys.argv[1].encode()); pump(1.5)
print(out.decode(errors="replace")[-400:])
print("PUSHED:", repr(open(P).read()) if os.path.exists(P) else None)
