# Start both JobPilot loops DETACHED, so they survive closing this terminal.
#
# Running `powershell -File scripts\agent_loop.ps1` directly ties the loop to
# the console it was launched from: close the window, press Ctrl+C, or let the
# session end and the loop dies silently. That is how both loops ended up
# stopped after a night of work.
#
# Usage:
#   powershell -File scripts\start_loops.ps1            # start whichever is down
#   powershell -File scripts\start_loops.ps1 -Restart   # stop then start both
#
# Verify afterwards with:  .venv\Scripts\jobpilot.exe health

param([switch]$Restart)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

$appDir = & $venvPython -c "import sys; sys.path.insert(0, r'$root\src'); from jobpilot.config import APP_DIR; print(APP_DIR)" 2>$null
$appDir = $appDir.Trim()
if (-not $appDir) { $appDir = Join-Path $env:USERPROFILE ".jobpilot" }

# Inherited by every child; without it a redirected stdout defaults to the ANSI
# code page and the first non-ASCII character rich prints kills the run.
$env:PYTHONIOENCODING = "utf-8"

function Get-LoopPid($name) {
    $f = Join-Path $appDir "$name.pid"
    if (-not (Test-Path $f)) { return $null }
    $procId = (Get-Content $f -Raw).Trim()
    if (-not $procId) { return $null }
    $running = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($running) { return [int]$procId }
    return $null
}

function Stop-Loop($name) {
    $procId = Get-LoopPid $name
    if ($procId) {
        Write-Host "  stopping $name (pid $procId)"
        & taskkill /PID $procId /T /F 2>&1 | Out-Null
        Start-Sleep -Seconds 1
    }
}

function Start-Loop($name, $script) {
    $procId = Get-LoopPid $name
    if ($procId) {
        Write-Host "  $name already running (pid $procId)"
        return
    }
    # -WindowStyle Hidden keeps a console allocated (the child scripts redirect
    # stdout/stderr, which misbehaves with no console at all) while detaching
    # from this terminal.
    $p = Start-Process -FilePath "powershell.exe" `
         -ArgumentList @("-WindowStyle","Hidden","-ExecutionPolicy","Bypass","-File",(Join-Path $PSScriptRoot $script)) `
         -WorkingDirectory $root -WindowStyle Hidden -PassThru
    Write-Host "  started $name (pid $($p.Id))"
}

if ($Restart) {
    Stop-Loop "agent_loop"
    Stop-Loop "fast_lane"
}

Write-Host "JobPilot loops:"
Start-Loop "agent_loop" "agent_loop.ps1"
Start-Loop "fast_lane"  "fast_lane.ps1"

Write-Host ""
Write-Host "Both detached -- closing this window will not stop them."
Write-Host "Check with:  .venv\Scripts\jobpilot.exe health"
