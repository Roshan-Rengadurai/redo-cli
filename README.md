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
straight away; `redo --no-push` just prints. In bash the fix replaces `redo`
in your history instead (press ↑).

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

## What it detects today

git push rejected · merge conflicts · command not found (incl. common typos) ·
permission denied · port already in use (EADDRINUSE) · missing Python module ·
missing Node module · npm install failures · missing virtualenv · brew formula
missing — with a safe, actionable fallback for everything else.

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
re-run a failed command implicitly · raw events are the source of truth
(stats/explain/learn re-derive from them) · always return something actionable.

## Tests

```sh
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

`tests/test_hook_integration.py` drives real zsh/bash/fish sessions through
the hook (skipped per shell when it isn't installed); `tests/test_bench.py`
enforces a cold-start ceiling.
