# redo

Terminal-native debugging that explains *why* your last command failed and gives you the fix.

```
run command  ->  it fails  ->  redo  ->  recovery step
```

`redo` is deterministic: a rule-based matcher inspects the failed command, its
exit code, and its stderr, then returns a concrete next step. No LLM, no
network, zero runtime dependencies — it's meant to run in your shell's hot path.

## Status

MVP, built in stages. **Stage 1 (the engine) is done and tested.**

| Stage | Scope | State |
|-------|-------|-------|
| 1 | Detection engine, SQLite history, `run/fix/explain/learn/history/stats`, `alias` | done |
| 2 | `redo init` shell hook (auto-capture command/exit/cwd/time) | next |
| 3 | stderr capture in hook mode, more rules, brew packaging | planned |

## Try it (no install)

```sh
python3 -m redo_cli run python3 -c "import cv2"
python3 -m redo_cli fix
python3 -m redo_cli explain
```

`redo run <cmd>` runs the command, streams its output, and records it if it
exits non-zero. Until Stage 2 lands, `run` is how failures get captured.

## Install (editable)

```sh
pipx install -e .      # or: pip install -e .
redo run npm run dev
redo                   # bare `redo` == `redo fix`
```

Want something shorter? `redo alias r` adds `alias r='redo'` to your shell rc
(zsh / bash / fish detected from `$SHELL`), so the loop becomes
`r run npm run dev` -> `r`. It's idempotent, refuses to clobber an alias you
defined yourself, and `redo alias --remove r` undoes it. Use
`redo alias --print r` if you'd rather paste the line into your dotfiles.

## Commands

| command | what it does |
|---------|--------------|
| `redo` / `redo fix` | the primary command — the fix for the most recent failure |
| `redo explain` | root-cause reasoning + confidence (high/medium/low) |
| `redo learn` | the underlying concept, for onboarding |
| `redo history` | recent failed commands |
| `redo stats` | your most common failure categories |
| `redo run <cmd>` | run + capture a command (Stage 1 capture path) |
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
  cli.py       argparse front-end + rendering
  shell.py     rc-file detection, alias management (init hook lands here)
  colors.py    NO_COLOR / TTY-aware ANSI
```

## Design principles

deterministic first · zero runtime deps · fast startup · raw events are the
source of truth (stats/explain/learn re-derive from them) · always return
something actionable.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```
