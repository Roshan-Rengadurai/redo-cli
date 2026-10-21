# QA harness

Black-box checks that drive the real `redo` binary (`.venv/bin/redo`, or
`$REDO_BIN`). Built from six rounds of blind testing; every case here once
exposed a real bug. Nothing touches your real `~/.redo` or shell history.

| command | what it checks | time |
|---|---|---|
| `qa/gate.sh` | unit tests + typo fuzzer + "never destroys files" replay. **Run before every commit.** | ~2 min |
| `bash qa/parity/run_all.sh` then `python3 qa/parity/summarize.py qa/parity/parity.out qa/parity/argtypos.out` | 77 thefuck-parity + argument-typo scenarios, run mode and hook mode | ~3 min |
| `python3 qa/fuzz_dev/fuzz.py all` then `python3 qa/fuzz_dev/fuzz.py report` | independent dev-workflow fuzzer (seed 2026), run + hook mode, ~830 measurements | ~10 min |
| `python3 qa/fuzz_dev/session.py` | scripted interactive zsh/bash sessions through a pty | ~1 min |
| `python3 qa/picker_digit.py 3` | the inline picker in a real pty | seconds |

Current baseline (gate): 309/345 typos fixed, **1** wrong (`di`: id vs du,
medium), 0 high-confidence wrong, safety replay ok, p50 ~40 ms.
A change that makes any of these worse doesn't ship.

Notes: `fuzz_dev/session.py` verdicts are timing-sensitive under a pty;
re-read the log before trusting a FAIL. Fish isn't covered (not installed
where this was built).
