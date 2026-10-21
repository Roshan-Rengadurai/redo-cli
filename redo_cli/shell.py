"""Shell integration: rc-file detection and `redo alias` management.

Everything that touches the user's shell config lives here so it has one
owner. Lines we write carry a trailing ``# redo-cli`` marker; that marker is
how we recognise our own entries on later runs (idempotent add, safe remove,
list) and how we avoid clobbering an alias the user defined by hand.

Stage 2's ``redo init`` hook will be managed the same way.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path
from typing import List, Mapping, Optional

MARKER = "# redo-cli"
SUPPORTED = ("zsh", "bash", "fish", "pwsh", "powershell")
POWERSHELLS = ("pwsh", "powershell")

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class UnsupportedShell(Exception):
    pass


# ---------------------------------------------------------------- detection

def detect_shell(env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> str:
    """Basename of $SHELL; on Windows (no $SHELL) PowerShell 7, else 5.1.
    On macOS/Linux, pwsh inherits $SHELL from the login shell, so use
    `redo init --shell pwsh` there."""
    env = os.environ if env is None else env
    if env.get("SHELL"):
        return os.path.basename(env["SHELL"]).replace(".exe", "")
    if (platform or sys.platform) == "win32":
        return "pwsh" if shutil.which("pwsh") else "powershell"
    return ""


def rc_path(shell: str, home: Optional[Path] = None, platform: Optional[str] = None) -> Path:
    home = Path(home) if home is not None else Path.home()
    platform = platform or sys.platform
    if shell == "zsh":
        return home / ".zshrc"
    if shell == "bash":
        # macOS terminals open login shells, which read .bash_profile and
        # usually never .bashrc.
        return home / (".bash_profile" if platform == "darwin" else ".bashrc")
    if shell == "fish":
        return home / ".config" / "fish" / "config.fish"
    if shell in POWERSHELLS:
        return _ps_profile(shell, home, platform)
    raise UnsupportedShell(shell or "(unset)")


def _ps_profile(shell: str, home: Path, platform: str) -> Path:
    """Ask PowerShell for $PROFILE (Documents may be redirected to OneDrive);
    fall back to the documented default location."""
    import subprocess
    exe = shutil.which(shell)
    if exe and home == Path.home():
        try:
            out = subprocess.run([exe, "-NoProfile", "-NoLogo", "-Command",
                                  "$PROFILE.CurrentUserCurrentHost"],
                                 capture_output=True, text=True, timeout=15).stdout.strip()
            if out:
                return Path(out)
        except (OSError, subprocess.SubprocessError):
            pass
    name = "Microsoft.PowerShell_profile.ps1"
    if platform != "win32":
        return home / ".config" / "powershell" / name
    return home / "Documents" / ("PowerShell" if shell == "pwsh" else "WindowsPowerShell") / name


# ------------------------------------------------------------------ aliases

def alias_line(name: str, shell: str = "") -> str:
    if shell in POWERSHELLS:
        return "Set-Alias -Name %s -Value redo  %s" % (name, MARKER)
    # `alias NAME=DEF` is accepted by zsh, bash and fish alike.
    return "alias %s='redo'  %s" % (name, MARKER)


def _validate(name: str) -> None:
    if not _NAME_RE.match(name):
        raise ValueError("invalid alias name: %r" % name)


def _read(rc: Path) -> str:
    return rc.read_text() if rc.exists() else ""


def _foreign_alias_re(name: str) -> "re.Pattern[str]":
    n = re.escape(name)
    return re.compile(r"^\s*(?:alias\s+%s(=|\s)|(?:set|new)-alias\s+(?:-name\s+)?%s\b)" % (n, n), re.M | re.I)


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


def add_alias(name: str, rc: Path, shell: str = "") -> str:
    """Append our alias line. Returns 'added' | 'exists' | 'conflict'."""
    _validate(name)
    text = _read(rc)
    if alias_line(name, shell) in text.splitlines():
        return "exists"
    if _foreign_alias_re(name).search(text):
        return "conflict"
    _add_line(rc, alias_line(name, shell))
    return "added"


def remove_alias(name: str, rc: Path, shell: str = "") -> str:
    """Drop our alias line (never a user-written one). Returns 'removed' | 'missing'."""
    _validate(name)
    return "removed" if _remove_line(rc, alias_line(name, shell)) else "missing"


def list_aliases(rc: Path) -> List[str]:
    pat = re.compile(r"^(?:alias\s+([A-Za-z_][A-Za-z0-9_-]*)='redo'|Set-Alias -Name ([A-Za-z_][A-Za-z0-9_-]*) "
                     r"-Value redo)\s+%s$" % re.escape(MARKER))
    out = []
    for ln in _read(rc).splitlines():
        m = pat.match(ln)
        if m:
            out.append(m.group(1) or m.group(2))
    return out


# --------------------------------------------------------------------- hooks
#
# The hook is pure shell: nothing Python runs on the user's commands. On a
# non-zero exit it appends one record to the spool (see storage.parse_spool).
# It also defines the `redo` function: the binary writes "<mode>\n<cmd>" to
# $REDO_PUSH_FILE and the function pushes that onto the prompt (or runs it).
# Paths are baked in at `redo init` time so no lookup happens at shell start.

_ZSH_HOOK = r"""# redo-cli shell hook (zsh). Generated by `redo init`, safe to regenerate.
zmodload zsh/datetime 2>/dev/null
typeset -g _REDO_CMD= _REDO_SKIP=

_redo_preexec() { _REDO_CMD=$1 }

_redo_precmd() {
  local code=$? ps=" ${pipestatus[*]} "
  [[ $ps == *" 127 "* ]] && code=127   # `gti log | head` exits 0 but still failed
  if [[ -n $_REDO_SKIP ]]; then
    _REDO_SKIP= _REDO_CMD=
    return
  fi
  if (( code == 0 )) && [[ -n $_REDO_CMD ]]; then
    : 2>/dev/null >| __PUSHDIR__/ok      # lets `redo` tell a stale failure from a fresh one
  elif (( code != 130 && code != 141 && code != 148 )) && [[ -n $_REDO_CMD ]]; then
    [[ -e __PUSHDIR__/ok ]] && command rm -f __PUSHDIR__/ok   # ok exists <=> last command succeeded
    printf '%s\037%s\037%s\037%s\037%s\036' zsh "$code" "$PWD" "${EPOCHSECONDS:-$(date +%s)}" "$_REDO_CMD" 2>/dev/null >> __SPOOL__
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
  fc -ln -200 2>/dev/null >| $f.hist
  REDO_PUSH_FILE=$f REDO_SESSION_HISTORY=$f.hist REDO_SHELL=zsh __REDO__ "$@"
  rc=$?
  if [[ -s $f ]]; then
    local -a lines
    lines=("${(@f)$(<"$f")}")
    mode=$lines[1]
    cmd=${(F)lines[2,-1]}
    case $mode in
      push) print -rz -- "$cmd" ;;
      hist) print -rs -- "$cmd" ;;      # risky: up-arrow only, never pre-filled
      run)  print -rs -- "$cmd"; eval "$cmd"; rc=$? ;;
    esac
  fi
  rm -f -- "$f" "$f.hist"
  return $rc
}
"""

_BASH_HOOK = r"""# redo-cli shell hook (bash). Generated by `redo init`, safe to regenerate.
[[ -n $_REDO_HOOKED ]] && return 0      # sourced from both .bash_profile and .bashrc
_REDO_HOOKED=1
_REDO_SKIP=
_REDO_LASTHIST=

_redo_precmd() {
  local code=$? ps=" ${PIPESTATUS[*]} " h num cmd
  [[ $ps == *" 127 "* ]] && code=127   # `gti log | head` exits 0 but still failed
  h=$(HISTTIMEFORMAT= builtin history 1)
  h=${h#"${h%%[![:space:]]*}"}
  num=${h%%[[:space:]]*}
  if [[ -n $_REDO_SKIP ]]; then
    _REDO_SKIP=
    _REDO_LASTHIST=$num
    return
  fi
  if [[ $code -eq 0 && $num != "$_REDO_LASTHIST" ]]; then
    : 2>/dev/null > __PUSHDIR__/ok
  elif [[ $code -ne 130 && $code -ne 141 && $code -ne 148 && $num != "$_REDO_LASTHIST" ]]; then
    [[ -e __PUSHDIR__/ok ]] && command rm -f __PUSHDIR__/ok
    cmd=${h#"$num"}
    cmd=${cmd#"${cmd%%[![:space:]]*}"}
    if [[ -n $cmd ]]; then
      printf '%s\037%s\037%s\037%s\037%s\036' bash "$code" "$PWD" "${EPOCHSECONDS:-$(date +%s)}" "$cmd" 2>/dev/null >> __SPOOL__
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
  builtin fc -ln -200 2>/dev/null > $f.hist
  REDO_PUSH_FILE=$f REDO_SESSION_HISTORY=$f.hist REDO_SHELL=bash __REDO__ "$@"
  rc=$?
  if [[ -s $f ]]; then
    { IFS= read -r mode; cmd=$(cat); } < "$f"
    case $mode in
      push|hist) builtin history -s -- "$cmd" ;;
      run)  builtin history -s -- "$cmd"; eval "$cmd"; rc=$? ;;
    esac
  fi
  rm -f -- "$f" "$f.hist"
  return $rc
}
"""

_FISH_HOOK = r"""# redo-cli shell hook (fish). Generated by `redo init`, safe to regenerate.
function _redo_preexec --on-event fish_preexec
    set -g _REDO_CMD $argv[1]
end

function _redo_postexec --on-event fish_postexec
    set -l ps $pipestatus
    set -l code $ps[-1]
    contains 127 $ps; and set code 127
    if set -q _REDO_SKIP
        set -e _REDO_SKIP
        set -e _REDO_CMD
        return
    end
    # `exit` fires postexec too, still carrying the previous command's status
    if string match -qr '^\s*exit(\s|$)' -- "$_REDO_CMD"
        set -e _REDO_CMD
        return
    end
    if test $code -eq 0; and set -q _REDO_CMD
        true 2>/dev/null > __PUSHDIR__/ok
    else if test $code -ne 130 -a $code -ne 141 -a $code -ne 148
        and set -q _REDO_CMD; and test -n "$_REDO_CMD"
        rm -f __PUSHDIR__/ok
        printf '%s\037%s\037%s\037%s\037%s\036' fish $code $PWD (date +%s) $_REDO_CMD 2>/dev/null >> __SPOOL__
    end
    set -e _REDO_CMD
end

function redo
    set -l f __PUSHDIR__/push.$fish_pid
    set -g _REDO_SKIP 1
    rm -f -- $f
    history -n 200 2>/dev/null > $f.hist
    REDO_PUSH_FILE=$f REDO_SESSION_HISTORY=$f.hist REDO_SHELL=fish __REDO__ $argv
    set -l rc $status
    if test -s $f
        set -l lines (cat $f)
        set -l mode $lines[1]
        set -l cmd (string join \n $lines[2..-1])
        switch $mode
            case push
                commandline -r -- $cmd
            case hist
                echo "  $cmd"
            case run
                eval $cmd
                set rc $status
        end
    end
    rm -f -- $f $f.hist
    return $rc
end
"""

_PS_HOOK = r"""# redo-cli shell hook (PowerShell). Generated by `redo init`, safe to regenerate.
# Works in PowerShell 7 (pwsh) and Windows PowerShell 5.1.
if ($global:_RedoHooked) { return }
$global:_RedoHooked = $true
$global:_RedoSkip = $false
$global:_RedoLastId = -1
$global:_RedoLastErr = $global:Error[0]
$global:_RedoPrompt = $function:prompt

function global:prompt {
    $ok = $?                                   # read first: anything else resets it
    $native = $global:LASTEXITCODE
    $h = Get-History -Count 1
    if ($h -and $h.Id -ne $global:_RedoLastId) {
        $global:_RedoLastId = $h.Id
        $err = $global:Error[0]
        $newErr = $err -and -not [object]::ReferenceEquals($err, $global:_RedoLastErr)
        $global:_RedoLastErr = $err
        # $? alone isn't enough: on Linux, pwsh's command-not-found feedback
        # runs after the failure and leaves $? true. A new $Error entry or a
        # Failed history status still says what happened.
        $failed = (-not $ok) -or $newErr -or ($h.ExecutionStatus -eq 'Failed')
        if ($global:_RedoSkip) {
            $global:_RedoSkip = $false
        } elseif (-not $failed) {
            try { [IO.File]::WriteAllText(__OK__, '') } catch {}
        } elseif ($h.ExecutionStatus -ne 'Stopped' -and                # Ctrl-C isn't a failure
                  $native -notin 130, 141, 148, -1073741510) {        # (0xC000013A = Ctrl-C on Windows)
            # $LASTEXITCODE goes stale after a cmdlet error: only trust it
            # when no new PowerShell error explains the failure.
            if ($newErr -and $err.FullyQualifiedErrorId -like 'CommandNotFoundException*') { $code = 127 }
            elseif ($newErr -or -not $native) { $code = 1 }
            else { $code = $native }
            $us = [string][char]0x1f
            $rec = 'pwsh' + $us + $code + $us + $PWD.ProviderPath + $us + `
                   [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() + $us + $h.CommandLine + [char]0x1e
            try {
                [IO.File]::Delete(__OK__)
                [IO.File]::AppendAllText(__SPOOL__, $rec)
            } catch {}
        }
    }
    if ($global:_RedoPrompt) { & $global:_RedoPrompt } else { "PS $($PWD.ProviderPath)> " }
}

function global:redo {
    $f = Join-Path __PUSHDIR__ ("push." + $PID)
    $global:_RedoSkip = $true
    Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $f, "$f.hist"
    try { Get-History -Count 200 | ForEach-Object { $_.CommandLine } | Set-Content -LiteralPath "$f.hist" } catch {}
    $env:REDO_PUSH_FILE = $f; $env:REDO_SESSION_HISTORY = "$f.hist"; $env:REDO_SHELL = 'pwsh'
    try { & __REDO__ @args; $rc = $LASTEXITCODE }
    finally { Remove-Item -ErrorAction SilentlyContinue Env:REDO_PUSH_FILE, Env:REDO_SESSION_HISTORY, Env:REDO_SHELL }
    if (Test-Path -LiteralPath $f) {
        $lines = @(Get-Content -LiteralPath $f)
        $cmd = ($lines | Select-Object -Skip 1) -join "`n"
        # PSReadLine can't pre-fill the next prompt from a function, so the
        # fix goes into history: press Up to load it (same as bash).
        if (Get-Module PSReadLine) { [Microsoft.PowerShell.PSConsoleReadLine]::AddToHistory($cmd) }
        if ($lines[0] -eq 'run') { Invoke-Expression $cmd; $rc = $LASTEXITCODE }
    }
    Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $f, "$f.hist"
    $global:LASTEXITCODE = $rc
}
"""

_HOOKS = {"zsh": _ZSH_HOOK, "bash": _BASH_HOOK, "fish": _FISH_HOOK,
          "pwsh": _PS_HOOK, "powershell": _PS_HOOK}


def hook_path(shell: str, home: Path) -> Path:
    # PowerShell only dot-sources .ps1 files
    return Path(home) / ("hook.ps1" if shell in POWERSHELLS else "hook." + shell)


def hook_source(shell: str, spool: Path, binary: str = "command redo") -> str:
    if shell not in _HOOKS:
        raise UnsupportedShell(shell or "(unset)")
    spool = Path(spool)
    if shell in POWERSHELLS:
        exe = ("(Get-Command -CommandType Application redo | Select-Object -First 1).Source"
               if binary == "command redo" else _psq(binary.strip("'")))
        return (_HOOKS[shell].replace("__SPOOL__", _psq(str(spool)))
                .replace("__OK__", _psq(str(spool.parent / "ok")))
                .replace("__PUSHDIR__", _psq(str(spool.parent)))
                .replace("__REDO__", exe))
    return (_HOOKS[shell]
            .replace("__SPOOL__", _sq(str(spool)))
            .replace("__PUSHDIR__", _sq(str(spool.parent)))
            .replace("__REDO__", binary))


def redo_binary() -> str:
    """The redo that ran `redo init` -- not whichever is first on $PATH
    later (a stale pipx copy, say). Falls back to plain `command redo`."""
    me = os.path.splitext(os.path.basename(sys.argv[0]))[0]
    exe = sys.argv[0] if me == "redo" else shutil.which("redo")
    return _sq(os.path.abspath(exe)) if exe else "command redo"


def _psq(s: str) -> str:
    """Single-quote for PowerShell: ' is escaped by doubling."""
    return "'" + s.replace("'", "''") + "'"


def _sq(s: str) -> str:
    """Single-quote for sh/zsh/bash/fish (all treat '...' the same)."""
    return "'" + s.replace("'", "'\\''") + "'"


def source_line(hook_file: Path, shell: str = "") -> str:
    if shell in POWERSHELLS or str(hook_file).endswith(".ps1"):
        return ". %s  %s" % (_psq(str(hook_file)), MARKER)
    return "source %s  %s" % (_sq(str(hook_file)), MARKER)


def add_init(shell: str, rc: Path, hook_file: Path, spool: Path) -> str:
    """Write the hook file and make rc source it. Returns 'added' | 'updated'."""
    hook_file = Path(hook_file)
    hook_file.parent.mkdir(parents=True, exist_ok=True)
    hook_file.write_text(hook_source(shell, spool, redo_binary()))
    return "added" if _add_line(rc, source_line(hook_file, shell)) else "updated"


def remove_init(rc: Path, hook_file: Path, shell: str = "") -> str:
    """Undo add_init. Returns 'removed' | 'missing'."""
    hook_file = Path(hook_file)
    removed = _remove_line(rc, source_line(hook_file, shell))
    if hook_file.exists():
        hook_file.unlink()
        removed = True
    return "removed" if removed else "missing"


def init_installed(rc: Path, hook_file: Path) -> bool:
    return source_line(Path(hook_file)) in _read(rc).splitlines()
