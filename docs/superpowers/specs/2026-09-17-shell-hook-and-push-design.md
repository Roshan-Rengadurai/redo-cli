# Stage 2: shell hook capture + fix-on-prompt

Date: 2026-09-17
Status: approved (user delegated design decisions; lane chosen: "UX speed loop")

## Goal

Make the fix loop faster and safer than thefuck's:

```
command fails  ->  redo  ->  fix is on your prompt  ->  Enter
```

Three properties thefuck lacks: failures are captured without `redo run`
wrapping (hook), the failed command is **never re-run implicitly**, and the
fix is placed on the prompt for review instead of executed blind.

Out of scope (Stage 3): live stderr capture in hook mode (pty-based,
opt-in `redo init --capture-output`). Until then, output-based rules in hook
mode are reached via a pushed `redo run <cmd>`.

## 1. Capture hook

`redo init` installs a pure-shell hook. No Python runs on the user's
commands. On a non-zero exit the hook appends one record to the spool file
`$REDO_HOME/spool` (default `~/.redo/spool`):

```
shell \x1f exit_code \x1f cwd \x1f epoch_seconds \x1f command \x1e
```

Unit separator (0x1F) between fields, record separator (0x1E) after each
record, so multi-line commands survive. Written with a single `printf`
(octal escapes for portability). Append is atomic for records under
PIPE_BUF, so concurrent shells are safe.

Per shell:

| shell | command source | exit source |
|-------|----------------|-------------|
| zsh   | `preexec` `$1` (as typed) | `precmd` `$?` (read first thing) |
| bash  | `history 1` in `PROMPT_COMMAND` | `$?` (read first thing) |
| fish  | `fish_preexec` event arg | `fish_postexec` `$status` |

bash: prepend to `PROMPT_COMMAND`, don't replace it. Requires history on
(default in interactive bash). Timestamp: `$EPOCHSECONDS` in zsh
(`zmodload zsh/datetime`) and bash>=5, else `date +%s` (only on failures).

Skipped:
- exit codes 130 (SIGINT), 141 (SIGPIPE), 148 (SIGTSTP)
- empty command
- any `redo` invocation, via a skip flag: the `redo` shell function sets
  `_REDO_SKIP=1` before running the binary; the hook clears it and skips
  the record. This works regardless of alias names.

Install (`redo_cli/shell.py`, same conventions as `alias`):
- `redo init` -> writes `$REDO_HOME/hook.<shell>` and appends
  `source <path>  # redo-cli` to the rc file (zsh `.zshrc`, bash
  `.bash_profile` on macOS / `.bashrc` elsewhere, fish
  `~/.config/fish/config.fish`). Idempotent; re-running regenerates the
  hook file (upgrade path). Bare `redo init` when installed: reports status.
- `--print` prints the hook source, `--remove` drops the rc line and the
  hook file, `--shell` overrides `$SHELL` detection.
- Sourcing a file (not `eval "$(redo init zsh)"`) means zero Python at
  shell startup.

Ingest: `Storage.ingest_spool(path)` parses records, inserts them with
`stderr=""`, truncates the file. Called at the start of `fix`, `explain`,
`learn`, `history`, `stats`. Malformed trailing partial records are
dropped. Rules already degrade gracefully with empty stderr (3 of 10 fire
on command + exit code alone; the rest are reached via the pushed
`redo run`).

## 2. `redo` shell function + push-to-prompt

`redo init` also defines a shell function `redo` that wraps the binary:

```
redo() {
  local f; f=$(mktemp)
  _REDO_SKIP=1 REDO_PUSH_FILE=$f REDO_SHELL=zsh command redo "$@"
  local rc=$?
  if [[ -s $f ]]; then
    mode=<line 1>; cmd=<rest>
    push -> print -z -- "$cmd"          # zsh
            commandline -r -- "$cmd"    # fish
            history -s -- "$cmd"        # bash (+ hint printed by Python)
    run  -> eval "$cmd" (also appended to history)
  fi
  rm -f "$f"; return $rc
}
```

Python side (`cli.py`): when `REDO_PUSH_FILE` is set and the diagnosis is
pushable, write `<mode>\n<command>` to it. Pushable means:
- rule matched and `fix_command` is a single line not starting with `#`;
  a trailing `  # comment` is stripped (zsh interactive rejects `#` by
  default);
- **or** no rule matched and `event.stderr` is empty (hook-captured):
  push `redo run <original command>` (shell-quoted).

Modes: default `push`; `-y/--yes` -> `run`; `--no-push` -> write nothing.
Outside the function (no env var) nothing changes vs today.

Rendering: the footer under the fix says `↵  fix is on your prompt`
(zsh/fish), `↑  press up to load the fix` (bash), or `running:` (`-y`).
The unmatched-in-hook-mode message says the command will be re-run with
output captured, so the user knows what Enter does.

## 3. CLI surface

- new: `redo init [--shell S] [--print] [--remove]`
- changed: `redo` / `redo fix` accept `-y/--yes`, `--no-push`
- unchanged: `run`, `explain`, `learn`, `history`, `stats`, `alias`

## 4. Speed

- Lazy-import `subprocess`, `shlex`, `time` inside `cmd_run`.
- Spool ingest is one `stat` when the spool is empty/absent.
- Targets: bare `redo` < 50 ms warm on a laptop; `tests/test_bench.py`
  asserts cold-start `redo --version` < 150 ms (loose for CI) and prints
  the median. README states the measured number.

## 5. Testing

- Unit: spool parse (incl. multi-line commands, partial trailing record),
  `ingest_spool`, hook source generation per shell, `init` rc management
  (add/exists/remove/print), push-file writing for each mode and for the
  unmatched-hook case, comment stripping.
- Integration (per shell found on PATH, skipped otherwise): start an
  interactive shell with stdin piped (`printf 'source hook\nfalse\n
  exit\n' | zsh -i`), then assert one spool record with the right fields.
  Same for the skip flag (`redo` invocation must not be recorded) and for
  Ctrl-C-style exit codes.
- Existing 45 tests stay green.

## Files

- `redo_cli/shell.py` — hook templates, `hook_path`, `init` add/remove,
  reuse of rc helpers from `alias`
- `redo_cli/storage.py` — `spool_path()`, `ingest_spool()`
- `redo_cli/cli.py` — `cmd_init`, push-file logic in `cmd_fix`, flags
- `tests/test_shell.py`, `tests/test_storage.py`, `tests/test_cli.py`,
  `tests/test_hook_integration.py`, `tests/test_bench.py`
- `README.md` — install becomes `redo init`; document `-y`, `--no-push`
