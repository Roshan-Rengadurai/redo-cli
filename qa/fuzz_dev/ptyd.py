"""Minimal pty shell driver (rebuilt): interactive zsh with the redo hook."""
import os, pty, re, select, subprocess, time
R = os.environ.get("REDO_BIN") or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".venv", "bin", "redo")
MARK = "@@P@@"


def clean(s):
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\r", "", s)


class Sh:
    def __init__(self, shell, home, redo_home, cwd):
        os.makedirs(home, exist_ok=True)
        env = dict(os.environ, HOME=home, REDO_HOME=redo_home, SHELL="/bin/" + shell, NO_COLOR="1",
                   TERM="dumb", HISTFILE=home + "/hist", PATH=os.path.dirname(R) + ":" + os.environ["PATH"],
                   GIT_PAGER="cat", PAGER="cat", PIP_NO_INDEX="1")
        subprocess.run([R, "init", "--shell", shell], env=env, capture_output=True)
        self.m, s = pty.openpty()
        args = [shell, "-f", "-i"] if shell == "zsh" else [shell, "--norc", "--noprofile", "-i"]
        env["TERM"] = "xterm"
        self.p = subprocess.Popen(args, stdin=s, stdout=s, stderr=s, cwd=cwd, env=env, close_fds=True)
        os.close(s)
        rc = "~/.zshrc" if shell == "zsh" else "~/.bashrc"
        self.line("source %s; PS1='%s'; PROMPT_EOL_MARK=''; true" % (rc, MARK), 10)

    def send(self, keys):
        os.write(self.m, keys.encode())

    def read(self, t=2.5):
        return self._read(t)

    def _read(self, timeout):
        out, end = b"", time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([self.m], [], [], 0.05)
            if r:
                try:
                    out += os.read(self.m, 65536)
                except OSError:
                    break
                if out.decode(errors="replace").rstrip().endswith(MARK):
                    break
        return out.decode(errors="replace")

    def line(self, cmd, timeout=20):
        os.write(self.m, (cmd + "\n").encode())
        return clean(self._read(timeout))

    def close(self):
        try:
            os.write(self.m, b"exit\n")
            self.p.wait(timeout=3)
        except Exception:
            self.p.kill()
        os.close(self.m)
