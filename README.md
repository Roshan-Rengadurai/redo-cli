# redo

Terminal-native debugging that explains *why* your last command failed and gives you the fix.

```
command fails  ->  redo  ->  the fix is on your prompt  ->  enter
```

`redo` is deterministic: a rule-based matcher inspects the failed command, its
exit code, and its stderr, then returns a concrete next step. No LLM, no
network, zero runtime dependencies — it's meant to run in your shell's hot path.

## Status

MVP, built in stages.

| Stage | Scope | State |
|-------|-------|-------|
| 1 | Detection engine, SQLite history, `run/fix/explain/learn/history/stats`, `alias` | done |
| 2 | `redo init` shell hook (auto-capture), fix pushed onto the prompt | done |
| 3 | PowerShell hook, Homebrew tap + Chocolatey packages, CI on Linux/macOS/Windows | done |
| 4 | Live stderr capture in hook mode (opt-in, pty) | next |

## Install

```sh
brew install roshan-rengadurai/tap/redo-cli     # macOS / Linux
pipx install git+https://github.com/Roshan-Rengadurai/redo-cli   # anywhere with Python 3.9+
```

Then, once:

```sh
redo init        # zsh / bash / fish: adds one `source` line to your rc file
exec $SHELL
```

```powershell
redo init        # PowerShell 7 or Windows PowerShell: adds one line to $PROFILE
. $PROFILE       # (on macOS/Linux pwsh: redo init --shell pwsh)
```

The package is called `redo-cli`; the command is `redo`. (Homebrew's own
`redo` formula is an unrelated build tool -- the two can't be installed side
by side.)

Now every failing command is captured automatically (zsh, bash, fish,
PowerShell). When
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
straight away; `redo --no-push` just prints. In bash and PowerShell the fix
goes into your history instead (press ↑). A fix that deletes or overwrites
anything is never pre-filled -- it's always ↑ then enter, so a command you
start typing can't get glued onto it.

**redo never re-runs your failed command behind your back.** The hook only
records the command, exit code and directory — it costs one `printf`, on
failures only. When a diagnosis needs the command's output, `redo` puts
`redo run <your command>` on the prompt and *you* decide whether it re-runs.

Shorter: `redo alias r` adds `alias r='redo'` to your rc (idempotent, won't
clobber an alias you wrote; `--remove` undoes it; `--print` if you'd rather
paste it into your dotfiles).

### Try it without installing the hook

```sh
python3 -m redo_cli run python3 -c "import cv2"
python3 -m redo_cli
```

`redo run <cmd>` runs the command, streams its output, and records it — with
stderr — if it exits non-zero.

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

## What it fixes

No hand-written typo tables. redo checks a failure against what's actually
true on your machine, in this order of evidence:

- **what the tool said:** its own "did you mean" (git, npm, pip, cargo,
  gh/kubectl, brew, Python), a command it printed (`git push --set-upstream
  origin x`), the option it rejected, checked against its man page / `--help`
- **what's installed:** every executable on `$PATH`, ranked by how people
  actually slip (swapped letters before far-away keys) and how common the tool
  is; real ties are shown as a list, not guessed
- **what's on disk / in the repo:** close file and folder names (any path
  component, case-only differences), Makefile targets, package.json scripts,
  git branches and upstreams, ports already in use
- **what you've run before:** your shell history, including this session
- **failure shapes thefuck knows:** pasted `$ ` prompts, `git git`,
  `git-log`, missing `./`, missing spaces (`grep-c`, `gitstatus`), a folder
  where a file was expected (`cat dir` -> `ls`, `grep`/`cp` -> `-r`), missing
  parents (`mkdir -p`), `ln -s` order, unbalanced quotes, `python3 -m` typos,
  `.class`/`.java`/`.go` names, nothing staged, branch exists / doesn't exist,
  unrelated histories, flags after file names

Most of this works in hook mode too, where no output is captured: the evidence
comes from the disk, the repo and your history instead.

**Safety.** A fix that deletes, moves, overwrites or kills is never high
confidence, is marked *careful*, and is never run by `-y`. redo never turns a
typo into `rm`, never retargets `rm`/`mv`/`git branch -D`, never "corrects" a
file into one already in the command (`sort a.tx > a.txt` would empty it), and
never re-runs your failed command behind your back.

## Where things live

```
redo_cli/
  rules.py     deterministic matchers (one function per failure class)
  engine.py    first-match-wins dispatch + fallback
  storage.py   ~/.redo/history.db  (sqlite, single source of truth)
  models.py    FailureEvent, Diagnosis, Confidence
  cli.py       argparse front-end + rendering + push-to-prompt handoff
  shell.py     zsh/bash/fish hook templates, `redo init`, alias management
  colors.py    NO_COLOR / TTY-aware ANSI
```

How the hook hands off: the shell hook appends `shell␟exit␟cwd␟time␟command␞`
to `~/.redo/spool` on failure; the next `redo` ingests that into SQLite. The
`redo` shell function runs the binary with `$REDO_PUSH_FILE` set; the binary
writes `push\n<fix>` (or `run\n<fix>` for `-y`) and the function does
`print -z` (zsh) / `commandline -r` (fish) / `history -s` (bash).

## Design principles

deterministic first · zero runtime deps · fast startup (~35 ms cold) · never
re-run a failed command implicitly · never auto-run a destructive fix · raw events are the source of truth
(stats/explain/learn re-derive from them) · always return something actionable.

## Tests

```sh
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

`tests/test_hook_integration.py` drives real zsh/bash/fish sessions and
`tests/test_pwsh_integration.py` a real PowerShell through a pty (each skipped
when that shell isn't installed); `tests/test_bench.py` enforces a cold-start
ceiling. `qa/` holds the black-box regression harness (see `qa/README.md`);
releasing is in `packaging/RELEASING.md`.
