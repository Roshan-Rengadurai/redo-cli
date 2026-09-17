# Shell Hook Capture + Fix-on-Prompt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Failures are captured by a pure-shell hook (no `redo run` wrapping), and `redo` places the fix on the user's prompt instead of printing it or running it blind.

**Architecture:** The hook appends one record per failure to `~/.redo/spool`; Python ingests the spool into SQLite on its next run. `redo init` installs the hook plus a `redo` shell function that wraps the binary; the binary hands a fix back through a file named by `$REDO_PUSH_FILE`, and the function pushes it to the line editor (`print -z` / `commandline -r` / `history -s`).

**Tech Stack:** Python 3.9+ stdlib only (argparse, sqlite3, re, pathlib). zsh 5.x, bash 3.2+, fish 3.x. pytest for tests.

## Global Constraints

- Zero runtime dependencies; stdlib only (`pyproject.toml` `dependencies = []`).
- `requires-python = ">=3.9"` — no `match`, no `X | Y` type syntax at runtime.
- Never re-run the failed command implicitly. A re-run only happens when the user presses Enter on a pushed `redo run …` line.
- Raw `FailureEvent`s are the single source of truth; never persist a `Diagnosis`.
- Spool record format: `shell \x1f exit_code \x1f cwd \x1f epoch \x1f command \x1e`.
- Skipped exit codes in the hook: 130, 141, 148.
- Lines redo writes to rc files end with the marker `# redo-cli` (`shell.MARKER`).
- Tests: run with `.venv/bin/python -m pytest -q`; all existing 45 tests stay green.
- Commit after every task.

## File structure

| File | Responsibility |
|------|----------------|
| `redo_cli/storage.py` | + `home_dir()`, `spool_path()`, `parse_spool()`, `Storage.ingest_spool()` |
| `redo_cli/shell.py` | + hook templates per shell, `hook_source()`, `hook_path()`, `add_init()`, `remove_init()`, `init_installed()`; generic `_add_line`/`_remove_line` reused by alias code |
| `redo_cli/cli.py` | + `cmd_init`; push-file logic in `cmd_fix`; `-y`/`--no-push`; `_store()` ingests spool; lazy imports in `cmd_run`; single-arg `redo run` verbatim |
| `tests/test_storage.py` | spool parse + ingest tests |
| `tests/test_shell.py` | hook generation + init rc tests |
| `tests/test_cli.py` | init command, push-file, flags tests |
| `tests/test_hook_integration.py` | real zsh/bash/fish sessions (skip if shell missing) |
| `tests/test_bench.py` | cold-start ceiling |
| `README.md` | install via `redo init`; `-y`, `--no-push` |

---

### Task 1: Spool parsing and ingest (`storage.py`)

**Files:**
- Modify: `redo_cli/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Produces: `home_dir() -> Path` (the `~/.redo` dir honouring `$REDO_HOME`), `spool_path() -> Path` (`home_dir()/"spool"`), `SPOOL_FS = "\x1f"`, `SPOOL_RS = "\x1e"`, `parse_spool(text: str) -> List[FailureEvent]`, `Storage.ingest_spool(path: Path) -> int` (number ingested; file removed afterwards).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_storage.py` inside `StorageTests`:

```python
    def test_spool_path_is_beside_db(self):
        from redo_cli.storage import spool_path
        self.assertEqual(spool_path(), default_db_path().parent / "spool")

    def test_parse_spool_records(self):
        from redo_cli.storage import parse_spool
        text = ("zsh\x1f127\x1f/tmp\x1f1700000000\x1fgti status\x1e"
                "bash\x1f1\x1f/home/x\x1f1700000001\x1fnpm run dev\x1e")
        events = parse_spool(text)
        self.assertEqual([e.command for e in events], ["gti status", "npm run dev"])
        self.assertEqual(events[0].exit_code, 127)
        self.assertEqual(events[0].cwd, "/tmp")
        self.assertEqual(events[0].timestamp, 1700000000.0)
        self.assertEqual(events[0].shell, "zsh")
        self.assertEqual(events[0].stderr, "")

    def test_parse_spool_keeps_multiline_commands(self):
        from redo_cli.storage import parse_spool
        text = "zsh\x1f1\x1f/tmp\x1f1\x1ffor f in *; do\n  cat $f\ndone\x1e"
        self.assertEqual(parse_spool(text)[0].command, "for f in *; do\n  cat $f\ndone")

    def test_parse_spool_drops_partial_and_malformed(self):
        from redo_cli.storage import parse_spool
        text = ("zsh\x1f1\x1f/tmp\x1f1\x1fgood\x1e"
                "zsh\x1fnotanint\x1f/tmp\x1f1\x1fbad\x1e"
                "zsh\x1f1\x1f/tmp\x1e"          # too few fields
                "zsh\x1f1\x1f/tmp\x1f1\x1fpartial-no-terminator")
        self.assertEqual([e.command for e in parse_spool(text)], ["good"])

    def test_ingest_spool_records_and_removes_file(self):
        from redo_cli.storage import spool_path
        p = spool_path()
        p.write_text("zsh\x1f127\x1f/tmp\x1f5\x1fgti status\x1e")
        n = self.store.ingest_spool(p)
        self.assertEqual(n, 1)
        self.assertFalse(p.exists())
        self.assertEqual(self.store.latest().command, "gti status")

    def test_ingest_spool_missing_file_is_noop(self):
        from redo_cli.storage import spool_path
        self.assertEqual(self.store.ingest_spool(spool_path()), 0)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_storage.py`
Expected: 6 failures, `ImportError: cannot import name 'spool_path'` / `AttributeError: ... ingest_spool`.

- [ ] **Step 3: Implement** — in `redo_cli/storage.py`, replace `default_db_path` and add the spool API:

```python
SPOOL_FS = "\x1f"   # unit separator between fields
SPOOL_RS = "\x1e"   # record separator after each record


def home_dir() -> Path:
    home = os.environ.get("REDO_HOME")
    return Path(home).expanduser() if home else Path.home() / ".redo"


def default_db_path() -> Path:
    return home_dir() / "history.db"


def spool_path() -> Path:
    """Where the shell hook appends failures (see shell.py). Pure-shell
    writers, Python reader."""
    return home_dir() / "spool"


def parse_spool(text: str) -> List[FailureEvent]:
    """Decode spool records. Anything malformed (wrong field count, bad
    numbers, a partial record without its terminator) is dropped."""
    events = []
    for rec in text.split(SPOOL_RS)[:-1]:   # last chunk is "" or a partial
        parts = rec.split(SPOOL_FS)
        if len(parts) != 5:
            continue
        shell, code, cwd, ts, command = parts
        try:
            exit_code, timestamp = int(code), float(ts)
        except ValueError:
            continue
        if not command.strip():
            continue
        events.append(FailureEvent(
            id=None, command=command, exit_code=exit_code, stderr="",
            cwd=cwd, timestamp=timestamp, shell=shell,
        ))
    return events
```

and inside `class Storage` after `record`:

```python
    def ingest_spool(self, path: Path) -> int:
        """Move hook-captured failures from the spool file into the db.

        The file is renamed first so shells that append while we read
        start a fresh spool instead of racing the truncate."""
        path = Path(path)
        if not path.exists():
            return 0
        work = path.with_suffix(".ingest")
        try:
            os.replace(path, work)
        except OSError:
            return 0
        try:
            events = parse_spool(work.read_text(errors="replace"))
        finally:
            try:
                work.unlink()
            except OSError:
                pass
        for e in events:
            self.record(e)
        return len(events)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest -q`
Expected: 51 passed.

- [ ] **Step 5: Commit**

```bash
git add redo_cli/storage.py tests/test_storage.py
git commit -m "feat(storage): spool file parsing and ingest for hook-captured failures"
```

---

### Task 2: Hook source generation (`shell.py`)

**Files:**
- Modify: `redo_cli/shell.py`
- Test: `tests/test_shell.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `hook_source(shell: str, spool: Path) -> str`, `hook_path(shell: str, home: Path) -> Path` (`home/"hook.<shell>"`).

The hook must: record `shell\x1fcode\x1fcwd\x1fepoch\x1fcommand\x1e` to `spool` on non-zero exit (not 130/141/148), honour `_REDO_SKIP`, and define the `redo` function that wraps the binary and pushes/runs the fix. The push file lives at `<spool dir>/push.<pid>` (no `mktemp` fork).

- [ ] **Step 1: Write the failing tests** — add to `tests/test_shell.py`:

```python
class HookSourceTests(unittest.TestCase):
    def setUp(self):
        self.spool = Path("/tmp/redo home/spool")

    def test_hook_path(self):
        self.assertEqual(shell.hook_path("zsh", Path("/h/.redo")), Path("/h/.redo/hook.zsh"))

    def test_zsh_hook_writes_record_to_spool(self):
        src = shell.hook_source("zsh", self.spool)
        self.assertIn("add-zsh-hook preexec _redo_preexec", src)
        self.assertIn("add-zsh-hook precmd _redo_precmd", src)
        self.assertIn(r"printf '%s\037%s\037%s\037%s\037%s\036'", src)
        self.assertIn(">> '/tmp/redo home/spool'", src)
        self.assertIn("print -z", src)

    def test_bash_hook_prepends_prompt_command(self):
        src = shell.hook_source("bash", self.spool)
        self.assertIn("PROMPT_COMMAND", src)
        self.assertIn("history 1", src)
        self.assertIn("history -s", src)

    def test_fish_hook_uses_events(self):
        src = shell.hook_source("fish", self.spool)
        self.assertIn("--on-event fish_preexec", src)
        self.assertIn("--on-event fish_postexec", src)
        self.assertIn("commandline -r", src)

    def test_every_hook_skips_signal_exits_and_defines_redo(self):
        for sh in shell.SUPPORTED:
            src = shell.hook_source(sh, self.spool)
            for code in ("130", "141", "148"):
                self.assertIn(code, src, sh)
            self.assertIn("_REDO_SKIP", src, sh)
            self.assertIn("REDO_PUSH_FILE", src, sh)
            self.assertIn("'/tmp/redo home/push.", src, sh)

    def test_unknown_shell_raises(self):
        with self.assertRaises(shell.UnsupportedShell):
            shell.hook_source("csh", self.spool)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_shell.py`
Expected: 6 failures, `AttributeError: module 'redo_cli.shell' has no attribute 'hook_path'`.

- [ ] **Step 3: Implement** — add to `redo_cli/shell.py` (after the aliases section):

```python
# --------------------------------------------------------------------- hooks
#
# The hook is pure shell: nothing Python runs on the user's commands. On a
# non-zero exit it appends one record to the spool (see storage.parse_spool).
# It also defines the `redo` function: the binary writes "<mode>\n<cmd>" to
# $REDO_PUSH_FILE and the function pushes that onto the prompt (or runs it).
# Paths are baked in at `redo init` time so no lookup happens at shell start.

_ZSH_HOOK = r"""# redo-cli shell hook (zsh) -- generated by `redo init`, safe to regenerate.
zmodload zsh/datetime 2>/dev/null
typeset -g _REDO_CMD= _REDO_SKIP=

_redo_preexec() { _REDO_CMD=$1 }

_redo_precmd() {
  local code=$?
  if [[ -n $_REDO_SKIP ]]; then
    _REDO_SKIP= _REDO_CMD=
    return
  fi
  if (( code != 0 && code != 130 && code != 141 && code != 148 )) && [[ -n $_REDO_CMD ]]; then
    printf '%s\037%s\037%s\037%s\037%s\036' zsh "$code" "$PWD" "${EPOCHSECONDS:-$(date +%s)}" "$_REDO_CMD" >> __SPOOL__ 2>/dev/null
  fi
  _REDO_CMD=
}

autoload -Uz add-zsh-hook
add-zsh-hook preexec _redo_preexec
add-zsh-hook precmd _redo_precmd

redo() {
  local f=__PUSHDIR__/push.$$ rc mode cmd
  _REDO_SKIP=1
  rm -f -- "$f"
  REDO_PUSH_FILE=$f REDO_SHELL=zsh command redo "$@"
  rc=$?
  if [[ -s $f ]]; then
    local -a lines
    lines=("${(@f)$(<"$f")}")
    mode=$lines[1]
    cmd=${(F)lines[2,-1]}
    case $mode in
      push) print -z -- "$cmd" ;;
      run)  print -s -- "$cmd"; eval "$cmd"; rc=$? ;;
    esac
  fi
  rm -f -- "$f"
  return $rc
}
"""

_BASH_HOOK = r"""# redo-cli shell hook (bash) -- generated by `redo init`, safe to regenerate.
_REDO_SKIP=
_REDO_LASTHIST=

_redo_precmd() {
  local code=$? h num cmd
  h=$(HISTTIMEFORMAT= builtin history 1)
  h=${h#"${h%%[![:space:]]*}"}
  num=${h%%[[:space:]]*}
  if [[ -n $_REDO_SKIP ]]; then
    _REDO_SKIP=
    _REDO_LASTHIST=$num
    return
  fi
  if [[ $code -ne 0 && $code -ne 130 && $code -ne 141 && $code -ne 148 && $num != "$_REDO_LASTHIST" ]]; then
    cmd=${h#"$num"}
    cmd=${cmd#"${cmd%%[![:space:]]*}"}
    if [[ -n $cmd ]]; then
      printf '%s\037%s\037%s\037%s\037%s\036' bash "$code" "$PWD" "${EPOCHSECONDS:-$(date +%s)}" "$cmd" >> __SPOOL__ 2>/dev/null
    fi
  fi
  _REDO_LASTHIST=$num
}

if [[ "$(declare -p PROMPT_COMMAND 2>/dev/null)" == "declare -a"* ]]; then
  PROMPT_COMMAND=(_redo_precmd "${PROMPT_COMMAND[@]}")
else
  PROMPT_COMMAND="_redo_precmd${PROMPT_COMMAND:+;$PROMPT_COMMAND}"
fi

redo() {
  local f=__PUSHDIR__/push.$$ rc mode cmd
  _REDO_SKIP=1
  rm -f -- "$f"
  REDO_PUSH_FILE=$f REDO_SHELL=bash command redo "$@"
  rc=$?
  if [[ -s $f ]]; then
    { IFS= read -r mode; cmd=$(cat); } < "$f"
    case $mode in
      push) builtin history -s -- "$cmd" ;;
      run)  builtin history -s -- "$cmd"; eval "$cmd"; rc=$? ;;
    esac
  fi
  rm -f -- "$f"
  return $rc
}
"""

_FISH_HOOK = r"""# redo-cli shell hook (fish) -- generated by `redo init`, safe to regenerate.
function _redo_preexec --on-event fish_preexec
    set -g _REDO_CMD $argv[1]
end

function _redo_postexec --on-event fish_postexec
    set -l code $status
    if set -q _REDO_SKIP
        set -e _REDO_SKIP
        set -e _REDO_CMD
        return
    end
    if test $code -ne 0 -a $code -ne 130 -a $code -ne 141 -a $code -ne 148
        and set -q _REDO_CMD; and test -n "$_REDO_CMD"
        printf '%s\037%s\037%s\037%s\037%s\036' fish $code $PWD (date +%s) $_REDO_CMD >> __SPOOL__ 2>/dev/null
    end
    set -e _REDO_CMD
end

function redo
    set -l f __PUSHDIR__/push.$fish_pid
    set -g _REDO_SKIP 1
    rm -f -- $f
    REDO_PUSH_FILE=$f REDO_SHELL=fish command redo $argv
    set -l rc $status
    if test -s $f
        set -l lines (cat $f)
        set -l mode $lines[1]
        set -l cmd (string join \n $lines[2..-1])
        switch $mode
            case push
                commandline -r -- $cmd
            case run
                eval $cmd
                set rc $status
        end
    end
    rm -f -- $f
    return $rc
end
"""

_HOOKS = {"zsh": _ZSH_HOOK, "bash": _BASH_HOOK, "fish": _FISH_HOOK}


def hook_path(shell: str, home: Path) -> Path:
    return Path(home) / ("hook." + shell)


def hook_source(shell: str, spool: Path) -> str:
    if shell not in _HOOKS:
        raise UnsupportedShell(shell or "(unset)")
    spool = Path(spool)
    return (_HOOKS[shell]
            .replace("__SPOOL__", _sq(str(spool)))
            .replace("__PUSHDIR__", _sq(str(spool.parent))))


def _sq(s: str) -> str:
    """Single-quote for sh/zsh/bash/fish (all treat '...' the same)."""
    return "'" + s.replace("'", "'\\''") + "'"
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest -q`
Expected: 57 passed.

- [ ] **Step 5: Commit**

```bash
git add redo_cli/shell.py tests/test_shell.py
git commit -m "feat(shell): generate zsh/bash/fish capture hooks with redo wrapper function"
```

---

### Task 3: `redo init` install / remove (`shell.py` + `cli.py`)

**Files:**
- Modify: `redo_cli/shell.py` (rc-line helpers, init functions)
- Modify: `redo_cli/cli.py` (`cmd_init`, parser)
- Test: `tests/test_shell.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `hook_source`, `hook_path` (Task 2); `storage.home_dir`, `storage.spool_path` (Task 1).
- Produces: `source_line(hook_file: Path) -> str`, `add_init(shell, rc, hook_file, spool) -> str` ('added' | 'updated'), `remove_init(rc, hook_file) -> str` ('removed' | 'missing'), `init_installed(rc, hook_file) -> bool`; CLI `redo init [--shell] [--print] [--remove]`.

- [ ] **Step 1: Write the failing tests** — add to `tests/test_shell.py`:

```python
class InitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.rc = root / ".zshrc"
        self.hook = root / ".redo" / "hook.zsh"
        self.spool = root / ".redo" / "spool"

    def tearDown(self):
        self.tmp.cleanup()

    def test_source_line_is_marked_and_quoted(self):
        self.assertEqual(shell.source_line(Path("/h/.redo/hook.zsh")),
                         "source '/h/.redo/hook.zsh'  # redo-cli")

    def test_add_init_writes_hook_and_rc_line(self):
        self.assertEqual(shell.add_init("zsh", self.rc, self.hook, self.spool), "added")
        self.assertIn("add-zsh-hook", self.hook.read_text())
        self.assertIn(shell.source_line(self.hook), self.rc.read_text().splitlines())

    def test_add_init_again_regenerates_hook_without_duplicating_line(self):
        shell.add_init("zsh", self.rc, self.hook, self.spool)
        self.hook.write_text("stale")
        self.assertEqual(shell.add_init("zsh", self.rc, self.hook, self.spool), "updated")
        self.assertIn("add-zsh-hook", self.hook.read_text())
        self.assertEqual(self.rc.read_text().count("# redo-cli"), 1)

    def test_remove_init(self):
        shell.add_init("zsh", self.rc, self.hook, self.spool)
        self.assertEqual(shell.remove_init(self.rc, self.hook), "removed")
        self.assertFalse(self.hook.exists())
        self.assertNotIn("redo-cli", self.rc.read_text())
        self.assertEqual(shell.remove_init(self.rc, self.hook), "missing")

    def test_init_installed(self):
        self.assertFalse(shell.init_installed(self.rc, self.hook))
        shell.add_init("zsh", self.rc, self.hook, self.spool)
        self.assertTrue(shell.init_installed(self.rc, self.hook))
```

and to `tests/test_cli.py` inside `CliTests`:

```python
    def test_init_installs_hook_and_rc_line(self):
        os.environ["HOME"] = self.tmp.name
        os.environ["SHELL"] = "/bin/zsh"
        rc, out = self._run(["init"])
        self.assertEqual(rc, 0)
        hook = os.path.join(self.tmp.name, "hook.zsh")   # REDO_HOME is the tmp dir
        self.assertTrue(os.path.exists(hook))
        self.assertIn("# redo-cli", open(os.path.join(self.tmp.name, ".zshrc")).read())
        self.assertIn("source", out)

    def test_init_print_writes_nothing(self):
        os.environ["HOME"] = self.tmp.name
        os.environ["SHELL"] = "/bin/zsh"
        rc, out = self._run(["init", "--print"])
        self.assertEqual(rc, 0)
        self.assertIn("add-zsh-hook", out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, ".zshrc")))

    def test_init_remove(self):
        os.environ["HOME"] = self.tmp.name
        os.environ["SHELL"] = "/bin/zsh"
        self._run(["init"])
        rc, out = self._run(["init", "--remove"])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "hook.zsh")))
        self.assertNotIn("redo-cli", open(os.path.join(self.tmp.name, ".zshrc")).read())
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_shell.py tests/test_cli.py`
Expected: 8 failures (`AttributeError ... source_line`; `SystemExit: 2` for the CLI ones).

- [ ] **Step 3: Implement** — in `redo_cli/shell.py`, add generic rc-line helpers and refactor `add_alias`/`remove_alias` to use them, then add the init functions:

```python
def _add_line(rc: Path, line: str) -> bool:
    """Append `line` to rc unless already present. True if written."""
    text = _read(rc)
    if line in text.splitlines():
        return False
    rc.parent.mkdir(parents=True, exist_ok=True)
    with rc.open("a") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        f.write(line + "\n")
    return True


def _remove_line(rc: Path, line: str) -> bool:
    """Drop exact `line` from rc. True if something was removed."""
    lines = _read(rc).splitlines(keepends=True)
    kept = [ln for ln in lines if ln.rstrip("\n") != line]
    if len(kept) == len(lines):
        return False
    rc.write_text("".join(kept))
    return True
```

`add_alias` becomes:

```python
def add_alias(name: str, rc: Path) -> str:
    """Append our alias line. Returns 'added' | 'exists' | 'conflict'."""
    _validate(name)
    text = _read(rc)
    if alias_line(name) in text.splitlines():
        return "exists"
    if _foreign_alias_re(name).search(text):
        return "conflict"
    _add_line(rc, alias_line(name))
    return "added"
```

`remove_alias` becomes:

```python
def remove_alias(name: str, rc: Path) -> str:
    """Drop our alias line (never a user-written one). Returns 'removed' | 'missing'."""
    _validate(name)
    return "removed" if _remove_line(rc, alias_line(name)) else "missing"
```

Init functions (after the hook templates):

```python
def source_line(hook_file: Path) -> str:
    return "source %s  %s" % (_sq(str(hook_file)), MARKER)


def add_init(shell: str, rc: Path, hook_file: Path, spool: Path) -> str:
    """Write the hook file and make rc source it. Returns 'added' | 'updated'."""
    hook_file = Path(hook_file)
    hook_file.parent.mkdir(parents=True, exist_ok=True)
    hook_file.write_text(hook_source(shell, spool))
    return "added" if _add_line(rc, source_line(hook_file)) else "updated"


def remove_init(rc: Path, hook_file: Path) -> str:
    """Undo add_init. Returns 'removed' | 'missing'."""
    hook_file = Path(hook_file)
    removed = _remove_line(rc, source_line(hook_file))
    if hook_file.exists():
        hook_file.unlink()
        removed = True
    return "removed" if removed else "missing"


def init_installed(rc: Path, hook_file: Path) -> bool:
    return source_line(Path(hook_file)) in _read(rc).splitlines()
```

In `redo_cli/cli.py`, change the storage import to `from .storage import Storage, default_db_path, home_dir, spool_path`, add `cmd_init` before the parser section:

```python
def cmd_init(args: argparse.Namespace) -> int:
    sh = args.shell or shell.detect_shell()
    try:
        rc = shell.rc_path(sh)
        if args.print:
            print(shell.hook_source(sh, spool_path()), end="")
            return 0
    except shell.UnsupportedShell as e:
        print("redo: unsupported shell %s -- pass --shell zsh|bash|fish" % e,
              file=sys.stderr)
        return 1
    hook = shell.hook_path(sh, home_dir())

    if args.remove:
        if shell.remove_init(rc, hook) == "removed":
            print(c.yellow("- ") + "removed redo hook" + c.dim("  (from %s)" % rc))
            print("Open a new shell for it to take effect.")
        else:
            print(c.dim("= redo hook is not installed in %s" % rc))
        return 0

    status = shell.add_init(sh, rc, hook, spool_path())
    if status == "added":
        print(c.green("+ ") + shell.source_line(hook) + c.dim("  -> %s" % rc))
    else:
        print(c.dim("= hook already in %s; regenerated %s" % (rc, hook)))
    print()
    print("Reload your shell to start capturing:  " + c.cyan("source %s" % rc))
    print("Then, after a command fails, just type  " + c.cyan("redo"))
    return rc_code_ok()
```

Replace that last line with `return 0` (no helper needed). Register the subparser in `build_parser()` before `return p`:

```python
    pi = sub.add_parser("init", help="install the shell hook so failures are captured automatically")
    pi.add_argument("--remove", action="store_true", help="uninstall the hook")
    pi.add_argument("--print", action="store_true", help="print the hook source instead of installing")
    pi.add_argument("--shell", choices=shell.SUPPORTED, help="override $SHELL detection")
    pi.set_defaults(func=cmd_init)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest -q`
Expected: 65 passed.

- [ ] **Step 5: Commit**

```bash
git add redo_cli/shell.py redo_cli/cli.py tests/test_shell.py tests/test_cli.py
git commit -m "feat: redo init installs/removes the shell capture hook"
```

---

### Task 4: Spool ingest on every read + push-to-prompt in `fix` (`cli.py`)

**Files:**
- Modify: `redo_cli/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `Storage.ingest_spool`, `spool_path` (Task 1).
- Produces: `_pushable_fix(d: Diagnosis) -> Optional[str]`, `_rerun_command(event) -> str`, `_write_push(mode: str, command: str) -> bool`; flags `-y/--yes`, `--no-push` on both bare `redo` and `redo fix`.

Push rules (from the spec): matched single-line fix not starting with `#`, trailing `  # comment` stripped → push (or run with `-y`). Unmatched **and** empty stderr → push `redo run <cmd>` (never `run` mode, even with `-y`). Footer tells the user what Enter does; bash gets the `↑` variant via `$REDO_SHELL`.

- [ ] **Step 1: Write the failing tests** — add to `tests/test_cli.py` inside `CliTests`:

```python
    def _push_file(self):
        p = os.path.join(self.tmp.name, "push")
        os.environ["REDO_PUSH_FILE"] = p
        self.addCleanup(os.environ.pop, "REDO_PUSH_FILE", None)
        return p

    def _read_push(self, p):
        return open(p).read() if os.path.exists(p) else None

    def test_fix_ingests_spool_before_reading(self):
        from redo_cli.storage import spool_path
        spool_path().write_text("zsh\x1f127\x1f/tmp\x1f5\x1fgti status\x1e")
        rc, out = self._run([])
        self.assertIn("git status", out)
        self.assertFalse(spool_path().exists())

    def test_fix_pushes_single_line_fix(self):
        p = self._push_file()
        self._seed("gti status", "zsh: command not found: gti", code=127)
        rc, out = self._run([])
        self.assertEqual(self._read_push(p), "push\ngit status\n")
        self.assertIn("on your prompt", out)

    def test_fix_strips_trailing_comment_when_pushing(self):
        p = self._push_file()
        self._seed("npm install", "npm ERR! code ETIMEDOUT")
        self._run([])
        self.assertEqual(self._read_push(p), "push\nnpm install\n")

    def test_fix_does_not_push_advice_only_fix(self):
        p = self._push_file()
        self._seed("cat /etc/shadow", "cat: /etc/shadow: Permission denied")
        rc, out = self._run([])
        self.assertIsNone(self._read_push(p))
        self.assertNotIn("on your prompt", out)

    def test_yes_flag_uses_run_mode(self):
        p = self._push_file()
        self._seed("gti status", "zsh: command not found: gti", code=127)
        rc, out = self._run(["-y"])
        self.assertEqual(self._read_push(p), "run\ngit status\n")
        self.assertIn("running", out)

    def test_yes_flag_on_fix_subcommand(self):
        p = self._push_file()
        self._seed("gti status", "zsh: command not found: gti", code=127)
        self._run(["fix", "--yes"])
        self.assertEqual(self._read_push(p), "run\ngit status\n")

    def test_no_push_flag(self):
        p = self._push_file()
        self._seed("gti status", "zsh: command not found: gti", code=127)
        rc, out = self._run(["--no-push"])
        self.assertIsNone(self._read_push(p))
        self.assertNotIn("on your prompt", out)

    def test_unmatched_hook_failure_pushes_redo_run(self):
        p = self._push_file()
        self._seed("make weird-target", "")          # hook-captured: no stderr
        rc, out = self._run(["-y"])                  # -y must NOT auto re-run
        self.assertEqual(self._read_push(p), "push\nredo run make weird-target\n")
        self.assertIn("re-run", out)

    def test_unmatched_hook_failure_quotes_shell_metachars(self):
        p = self._push_file()
        self._seed('python -c "import x" | grep y', "")
        self._run([])
        self.assertEqual(self._read_push(p),
                         "push\nredo run 'python -c \"import x\" | grep y'\n")

    def test_unmatched_with_stderr_pushes_nothing(self):
        p = self._push_file()
        self._seed("make weird-target", "make: *** No rule to make target")
        self._run([])
        self.assertIsNone(self._read_push(p))

    def test_bash_gets_up_arrow_hint(self):
        self._push_file()
        os.environ["REDO_SHELL"] = "bash"
        self.addCleanup(os.environ.pop, "REDO_SHELL", None)
        self._seed("gti status", "zsh: command not found: gti", code=127)
        rc, out = self._run([])
        self.assertIn("press up", out)

    def test_no_push_file_env_prints_only(self):
        self._seed("gti status", "zsh: command not found: gti", code=127)
        rc, out = self._run([])
        self.assertIn("git status", out)
        self.assertNotIn("on your prompt", out)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_cli.py`
Expected: 12 failures (`SystemExit: 2` for `-y`/`--no-push`; assertion failures for the push file / spool ones).

- [ ] **Step 3: Implement** — in `redo_cli/cli.py`:

Add `import re` to the imports. Replace `_latest()`:

```python
def _store() -> Storage:
    """Open the db and fold in anything the shell hook spooled since last time."""
    store = Storage(default_db_path())
    store.ingest_spool(spool_path())
    return store


def _latest():
    store = _store()
    event = store.latest()
    store.close()
    return event
```

Use `_store()` in `cmd_history` and `cmd_stats` in place of `Storage(default_db_path())`.

Add the push helpers after `_no_failures()`:

```python
# ------------------------------------------------------------ push-to-prompt
#
# Under the `redo` shell function (installed by `redo init`) the binary hands
# a command back through $REDO_PUSH_FILE as "<mode>\n<command>\n"; the
# function puts it on the prompt ("push") or evals it ("run").

_TRAILING_COMMENT = re.compile(r"\s+#.*$")


def _pushable_fix(d: Diagnosis) -> Optional[str]:
    if not d.matched:
        return None
    fix = d.fix_command.strip()
    if "\n" in fix or fix.startswith("#"):
        return None                      # multi-step or advice-only: show, don't push
    return _TRAILING_COMMENT.sub("", fix) or None


def _rerun_command(event: FailureEvent) -> str:
    """`redo run <cmd>` for a hook-captured failure we couldn't diagnose
    without output. Quoted as one arg when the command has shell syntax."""
    cmd = event.command.strip()
    try:
        plain = shlex.join(shlex.split(cmd)) == cmd
    except ValueError:
        plain = False
    return "redo run " + (cmd if plain else shlex.quote(cmd))


def _write_push(mode: str, command: str) -> bool:
    path = os.environ.get("REDO_PUSH_FILE")
    if not path:
        return False
    with open(path, "w") as f:
        f.write(mode + "\n" + command + "\n")
    return True


def _push_footer(mode: str, rerun: bool) -> str:
    bash = os.environ.get("REDO_SHELL") == "bash"
    if mode == "run":
        return c.dim("running the fix...")
    what = "re-run it with output captured" if rerun else "run the fix"
    if bash:
        return c.dim("↑  press up to load it, then enter to %s" % what)
    return c.dim("↵  it's on your prompt -- press enter to %s" % what)
```

Add `from typing import Optional` to the imports. Change `_print_fix` to accept an optional footer, and have it print the rerun explanation for the unmatched case:

```python
def _print_fix(event: FailureEvent, d: Diagnosis, footer: str = "") -> None:
    lines = [c.bold("You ran:"), "  " + c.cyan(event.command), ""]
    if d.matched:
        lines += [c.bold("Failure:"), "  " + d.summary, ""]
        color = _CONF_COLOR[d.confidence]
        lines.append(c.bold("Fix ") + color("(%s confidence):" % d.confidence.label()))
        for ln in d.fix_command.splitlines():
            if ln.strip().startswith("#"):
                lines.append("  " + c.dim(ln))
            else:
                lines.append("  " + c.green(ln))
        lines += ["", c.dim("why? -> redo explain      learn? -> redo learn")]
    else:
        lines += [
            c.bold("Failure detected, but no exact match was found."),
            "",
            "Likely causes:",
            c.dim("  - missing dependency"),
            c.dim("  - environment mismatch"),
            c.dim("  - incorrect command usage"),
            "",
            c.bold("Suggested next step:"),
        ]
        if event.stderr.strip():
            lines.append("  " + c.green("redo explain"))
        else:
            lines.append("  " + c.green(_rerun_command(event)))
            lines.append(c.dim("  (no output was captured; re-running with "
                               "`redo run` gets the full diagnosis)"))
    if footer:
        lines.append(footer)
    print("\n".join(lines))
```

Replace `cmd_fix`:

```python
def cmd_fix(args: argparse.Namespace) -> int:
    event = _latest()
    if event is None:
        print(_no_failures())
        return 0
    d = diagnose(event)

    footer = ""
    if not getattr(args, "no_push", False):
        mode = "run" if getattr(args, "yes", False) else "push"
        fix = _pushable_fix(d)
        rerun = False
        if fix is None and not d.matched and not event.stderr.strip():
            fix, mode, rerun = _rerun_command(event), "push", True
        if fix is not None and _write_push(mode, fix):
            footer = _push_footer(mode, rerun)
    _print_fix(event, d, footer)
    return 0
```

In `build_parser()`, add the flags to the top-level parser (so bare `redo -y` works) and to `fix` with `SUPPRESS` defaults so a sub-parser doesn't clobber the parent's value:

```python
    _add_fix_flags(p, top=True)
    sub = p.add_subparsers(dest="cmd")
    ...
    pf = sub.add_parser("fix", help="suggest a fix for the most recent failure")
    _add_fix_flags(pf, top=False)
    pf.set_defaults(func=cmd_fix)
```

with, above `build_parser`:

```python
def _add_fix_flags(p: argparse.ArgumentParser, top: bool) -> None:
    dflt = False if top else argparse.SUPPRESS
    p.add_argument("-y", "--yes", action="store_true", default=dflt,
                   help="run the fix immediately instead of placing it on the prompt")
    p.add_argument("--no-push", action="store_true", default=dflt,
                   help="only print the fix; don't touch the prompt")
```

Remove the old `sub.add_parser("fix", ...)` one-liner.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest -q`
Expected: 77 passed.

- [ ] **Step 5: Commit**

```bash
git add redo_cli/cli.py tests/test_cli.py
git commit -m "feat(fix): ingest spool and push the fix onto the prompt via the redo shell function"
```

---

### Task 5: `redo run` single-arg verbatim + lazy imports

**Files:**
- Modify: `redo_cli/cli.py`
- Test: `tests/test_cli.py`

Why: the pushed `redo run 'python -c "import x" | grep y'` arrives as one argv element; `shlex.join` would re-quote it into a single "command name". One arg → run verbatim through `$SHELL -c`. Also move `subprocess`/`shlex`/`time` imports into `cmd_run` — no, `shlex` is now used by `_rerun_command`; keep `shlex` top-level, lazy-load `subprocess` and `time` only.

- [ ] **Step 1: Write the failing test** — add to `tests/test_cli.py`:

```python
    def test_run_single_argument_is_executed_verbatim(self):
        import subprocess
        env = dict(os.environ, REDO_HOME=self.tmp.name, NO_COLOR="1")
        subprocess.run([sys.executable, "-m", "redo_cli", "run",
                        "python3 -c 'import sys; sys.exit(3)' | cat; exit ${PIPESTATUS[0]:-${pipestatus[1]:-1}}"],
                       env=env, capture_output=True)
        rc, out = self._run(["history"])
        self.assertIn("import sys; sys.exit(3)", out)
```

Add `import sys` to the test file imports.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest -q tests/test_cli.py -k verbatim`
Expected: FAIL — history is empty (the quoted string was treated as a command name and the shell reported 127 … actually that *is* recorded; the assertion on the command text still passes). If it passes, tighten: assert `history` shows the command **without** surrounding single quotes:

```python
        self.assertNotIn("'python3 -c", out)
```

- [ ] **Step 3: Implement** — in `cmd_run`:

```python
    import subprocess
    import time

    parts = list(args.command)
    if parts and parts[0] == "--":
        parts = parts[1:]
    if not parts:
        print("usage: redo run <command> [args...]", file=sys.stderr)
        return 2

    # One argument means the user (or a pushed `redo run '<cmd>'`) already
    # quoted a whole shell line: run it verbatim so pipes/redirects work.
    command = parts[0] if len(parts) == 1 else shlex.join(parts)
```

and delete the top-level `import subprocess` / `import time`.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest -q`
Expected: 78 passed.

- [ ] **Step 5: Commit**

```bash
git add redo_cli/cli.py tests/test_cli.py
git commit -m "feat(run): execute a single quoted argument verbatim; lazy-import subprocess"
```

---

### Task 6: Hook integration tests (real shells)

**Files:**
- Create: `tests/test_hook_integration.py`

Drive an interactive shell with stdin piped, source the generated hook, run a failing command, and inspect the spool. Skip when the shell isn't on PATH. The `redo` binary must be findable: put a stub `redo` script on PATH that writes a push file, so we can also assert the function's push path without depending on the installed entry point.

- [ ] **Step 1: Write the tests**

```python
"""Drive real shells through the generated hook. Skipped per shell when it
isn't installed. Each session: source hook, run a failing command, exit."""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from redo_cli import shell
from redo_cli.storage import parse_spool

SHELL_ARGS = {
    "zsh":  ["zsh", "-f", "-i"],                       # -f: no rc files
    "bash": ["bash", "--norc", "--noprofile", "-i"],
    "fish": ["fish", "--no-config", "-i"],
}


def _session(sh, home, script):
    env = dict(os.environ, HOME=str(home), PATH=str(home / "bin") + os.pathsep + os.environ["PATH"],
               TERM="dumb", PS1="$ ", HISTFILE=str(home / ".hist"))
    return subprocess.run(SHELL_ARGS[sh], input=script, env=env, text=True,
                          capture_output=True, timeout=20)


class HookIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.redo_home = self.home / ".redo"
        self.redo_home.mkdir()
        self.spool = self.redo_home / "spool"
        # stub `redo` binary: records argv, and pushes what $REDO_STUB_PUSH says
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "redo"
        stub.write_text("#!/bin/sh\n"
                        "printf '%s\\n' \"$*\" >> \"$HOME/redo-argv\"\n"
                        "if [ -n \"$REDO_STUB_PUSH\" ] && [ -n \"$REDO_PUSH_FILE\" ]; then\n"
                        "  printf '%s\\n' \"$REDO_STUB_PUSH\" > \"$REDO_PUSH_FILE\"\n"
                        "fi\n"
                        "exit 0\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    def tearDown(self):
        self.tmp.cleanup()

    def _hook(self, sh):
        p = self.redo_home / ("hook." + sh)
        p.write_text(shell.hook_source(sh, self.spool))
        return p

    def _records(self):
        return parse_spool(self.spool.read_text()) if self.spool.exists() else []

    def _run(self, sh, body):
        if not shutil.which(sh):
            self.skipTest(sh + " not installed")
        hook = self._hook(sh)
        return _session(sh, self.home, "source %s\n%s\nexit\n" % (hook, body))

    # -- capture ---------------------------------------------------------

    def _check_capture(self, sh):
        self._run(sh, "redo_no_such_cmd_xyz --flag\ntrue\n")
        recs = self._records()
        self.assertEqual(len(recs), 1, recs)
        self.assertEqual(recs[0].command, "redo_no_such_cmd_xyz --flag")
        self.assertEqual(recs[0].exit_code, 127)
        self.assertEqual(recs[0].shell, sh)
        self.assertEqual(recs[0].cwd, os.getcwd())
        self.assertGreater(recs[0].timestamp, 1_600_000_000)

    def test_zsh_captures_failure(self):  self._check_capture("zsh")
    def test_bash_captures_failure(self): self._check_capture("bash")
    def test_fish_captures_failure(self): self._check_capture("fish")

    def _check_success_and_empty_lines_not_recorded(self, sh):
        self._run(sh, "true\n\n\nfalse\n\n\n")
        recs = self._records()
        self.assertEqual([r.command for r in recs], ["false"])

    def test_zsh_ignores_success_and_blank_enters(self):  self._check_success_and_empty_lines_not_recorded("zsh")
    def test_bash_ignores_success_and_blank_enters(self): self._check_success_and_empty_lines_not_recorded("bash")
    def test_fish_ignores_success_and_blank_enters(self): self._check_success_and_empty_lines_not_recorded("fish")

    def _check_signal_exit_skipped(self, sh):
        self._run(sh, "sh -c 'exit 130'\nsh -c 'exit 141'\nsh -c 'exit 148'\nsh -c 'exit 2'\n")
        self.assertEqual([r.exit_code for r in self._records()], [2])

    def test_zsh_skips_signal_exits(self):  self._check_signal_exit_skipped("zsh")
    def test_bash_skips_signal_exits(self): self._check_signal_exit_skipped("bash")
    def test_fish_skips_signal_exits(self): self._check_signal_exit_skipped("fish")

    # -- the redo function -------------------------------------------------

    def _check_redo_invocation_not_recorded(self, sh):
        self._run(sh, "false\nredo\nredo fix\n")
        self.assertEqual([r.command for r in self._records()], ["false"])
        argv = (self.home / "redo-argv").read_text().splitlines()
        self.assertEqual(argv, ["", "fix"])

    def test_zsh_redo_not_recorded(self):  self._check_redo_invocation_not_recorded("zsh")
    def test_bash_redo_not_recorded(self): self._check_redo_invocation_not_recorded("bash")
    def test_fish_redo_not_recorded(self): self._check_redo_invocation_not_recorded("fish")

    def _check_run_mode_evals(self, sh):
        # stub pushes "run\necho PUSHED-OK"; the function must eval it
        script = "export REDO_STUB_PUSH='run\necho PUSHED-OK'\nredo\n"
        if sh == "fish":
            script = "set -x REDO_STUB_PUSH 'run\necho PUSHED-OK'\nredo\n"
        r = self._run(sh, script)
        self.assertIn("PUSHED-OK", r.stdout)
        self.assertFalse(list(self.redo_home.glob("push.*")), "push file not cleaned up")

    def test_zsh_run_mode_evals(self):  self._check_run_mode_evals("zsh")
    def test_bash_run_mode_evals(self): self._check_run_mode_evals("bash")
    def test_fish_run_mode_evals(self): self._check_run_mode_evals("fish")

    def test_zsh_push_mode_loads_editor_buffer(self):
        # `print -z` pushes onto the buffer stack; the next prompt pops it as
        # the line to edit, and with piped stdin zsh just reads it as input.
        r = self._run("zsh", "export REDO_STUB_PUSH='push\necho FROM-BUFFER'\nredo\n")
        self.assertIn("FROM-BUFFER", r.stdout)

    def test_bash_push_mode_adds_history_entry(self):
        r = self._run("bash", "export REDO_STUB_PUSH='push\necho FROM-HIST'\nredo\nhistory 1\n")
        self.assertIn("echo FROM-HIST", r.stdout)
```

- [ ] **Step 2: Run them**

Run: `.venv/bin/python -m pytest -q tests/test_hook_integration.py -v`
Expected: zsh + bash tests PASS, fish tests SKIPPED. If a zsh/bash test fails, fix the **hook template** in `shell.py` (that's what's under test), not the test — unless the test's assumption about the shell is wrong (e.g. `print -z` behaviour with piped stdin), in which case rewrite that single test to assert the observable behaviour.

- [ ] **Step 3: Commit**

```bash
git add tests/test_hook_integration.py redo_cli/shell.py
git commit -m "test: drive real zsh/bash sessions through the capture hook"
```

---

### Task 7: Cold-start benchmark

**Files:**
- Create: `tests/test_bench.py`

- [ ] **Step 1: Write the test**

```python
"""Startup-time ceiling. redo runs in the shell hot path; keep it cheap."""

import os
import statistics
import subprocess
import sys
import tempfile
import time
import unittest

CEILING_MS = 150   # loose: CI boxes are slow. Laptop target is <50 ms.


class Bench(unittest.TestCase):
    def test_cold_start_under_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, REDO_HOME=tmp, NO_COLOR="1")
            cmd = [sys.executable, "-m", "redo_cli"]
            subprocess.run(cmd, env=env, capture_output=True)   # warm the fs cache
            samples = []
            for _ in range(7):
                t = time.perf_counter()
                subprocess.run(cmd, env=env, capture_output=True, check=True)
                samples.append((time.perf_counter() - t) * 1000)
        median = statistics.median(samples)
        print("\nredo cold start: median %.0f ms (min %.0f)" % (median, min(samples)))
        self.assertLess(median, CEILING_MS)
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest -q tests/test_bench.py -s`
Expected: PASS, prints a median around 40-60 ms.

- [ ] **Step 3: Commit**

```bash
git add tests/test_bench.py
git commit -m "test: cold-start ceiling benchmark"
```

---

### Task 8: README + spec roadmap

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update the README** — replace the flow diagram, status table, "Try it", "Install" and "Commands" sections:

```markdown
```
command fails  ->  redo  ->  the fix is on your prompt  ->  enter
```

## Status

| Stage | Scope | State |
|-------|-------|-------|
| 1 | Detection engine, SQLite history, `run/fix/explain/learn/history/stats`, `alias` | done |
| 2 | `redo init` shell hook (auto-capture), fix pushed onto the prompt | done |
| 3 | Live stderr capture in hook mode (opt-in, pty), more rules, brew packaging | next |

## Install

```sh
pipx install -e .      # or: pip install -e . / uv tool install .
redo init              # adds one `source` line to your .zshrc / .bash_profile / config.fish
exec $SHELL
```

Now every failing command is captured automatically (zsh, bash, fish). When
one fails, type `redo`:

```
$ gti status
zsh: command not found: gti
$ redo
You ran:
  gti status

Failure:
  'gti' isn't a command — did you mean 'git'?

Fix (high confidence):
  git status

↵  it's on your prompt -- press enter to run the fix
$ git status█
```

The fix lands in your line editor; edit it or hit enter. `redo -y` runs it
straight away; `redo --no-push` just prints. In bash the fix goes into
history instead (press ↑).

**redo never re-runs your failed command behind your back.** The hook only
records the command, exit code and directory. When a diagnosis needs the
command's output, `redo` puts `redo run <your command>` on the prompt — you
decide whether it re-runs.

Shorter: `redo alias r` -> `r`.

## Commands

| command | what it does |
|---------|--------------|
| `redo` / `redo fix` | the fix for the most recent failure, placed on your prompt (`-y` runs it, `--no-push` prints only) |
| `redo explain` | root-cause reasoning + confidence (high/medium/low) |
| `redo learn` | the underlying concept, for onboarding |
| `redo history` | recent failed commands |
| `redo stats` | your most common failure categories |
| `redo run <cmd>` | run + capture a command *with its output* |
| `redo init` | install the shell hook (`--print`, `--remove`, `--shell`) |
| `redo alias <name>` | add a shell alias for redo (`--print`, `--remove`, no args = list) |
```

Update "Where things live" to include `shell.py     hooks, redo init, alias management` and add under Design principles: `never re-run a failed command implicitly`. Replace the Tests section command with `.venv/bin/python -m pytest -q` and note `tests/test_hook_integration.py` drives real shells (skipped when a shell isn't installed).

- [ ] **Step 2: Verify the README example is honest** — run in a fresh zsh with `REDO_HOME` pointed at a temp dir: `redo init --shell zsh`, source the hook, type `gti status`, then `redo`, confirm the buffer shows `git status`.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: Stage 2 install flow and push-to-prompt behaviour"
```
