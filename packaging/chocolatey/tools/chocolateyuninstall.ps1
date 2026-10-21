$ErrorActionPreference = 'Stop'
# Leave the user's PowerShell profile clean: drop the hook line `redo init` added.
$redo = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Definition) 'venv\Scripts\redo.exe'
if (Test-Path $redo) {
  & $redo init --remove 2>$null | Out-Null
}
# The package folder (venv included) is removed by Chocolatey itself.
