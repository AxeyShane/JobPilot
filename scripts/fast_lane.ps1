# JobPilot fast lane.
#
# Polls the job boards on a short interval, alerts you the moment a fresh
# posting clears your fit threshold, prepares a tailored resume + cover letter,
# and then STOPS. It never submits -- run `jobpilot apply` (or use the
# dashboard) when you have looked at what it found.
#
# Run this ALONGSIDE agent_loop.ps1, not instead of it. agent_loop works the
# backlog on a 4-hour cycle; this one exists so nothing posted this morning has
# to wait behind that.
#
# Usage:
#   powershell -File scripts\fast_lane.ps1
#
# Env overrides:
#   JOBPILOT_FL_INTERVAL    seconds between polls (default 300)
#   JOBPILOT_FL_MIN_SCORE   minimum fit score worth alerting on (default 7)
#   JOBPILOT_FL_HOURS_OLD   discovery window in hours (default 2)
#   JOBPILOT_FL_WORKERS     parallel threads for enrich/score/prep (default 4)
#   JOBPILOT_FL_NOPREP      set to "1" to alert only, skipping prep

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$venvPy = Join-Path $root ".venv\Scripts\jobpilot.exe"
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

$appDir = & $venvPython -c "import sys; sys.path.insert(0, r'$root\src'); from jobpilot.config import APP_DIR; print(APP_DIR)" 2>$null
$appDir = $appDir.Trim()
if (-not $appDir) { $appDir = Join-Path $env:USERPROFILE ".jobpilot" }

$logDir = Join-Path $appDir "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$loopLog = Join-Path $logDir "fast_lane.log"
$pidFile = Join-Path $appDir "fast_lane.pid"

$interval = if ($env:JOBPILOT_FL_INTERVAL)  { [int]$env:JOBPILOT_FL_INTERVAL }  else { 300 }
# Threshold resolution: env var, then a flag file, then the default. The env
# var only reaches this script if it was set in the *same* session that
# launched it -- start_loops.ps1 spawns a detached process, so a value exported
# in another terminal is silently lost. The flag file survives restarts and
# reboots, matching live.flag / engine.flag.
$minScoreFile = Join-Path $appDir "fastlane_min_score"
if ($env:JOBPILOT_FL_MIN_SCORE) {
    $minScore = [int]$env:JOBPILOT_FL_MIN_SCORE
} elseif (Test-Path $minScoreFile) {
    $minScore = [int](Get-Content $minScoreFile -Raw).Trim()
} else {
    $minScore = 7
}
$hoursOld = if ($env:JOBPILOT_FL_HOURS_OLD) { [int]$env:JOBPILOT_FL_HOURS_OLD } else { 2 }
$workers  = if ($env:JOBPILOT_FL_WORKERS)   { [int]$env:JOBPILOT_FL_WORKERS }   else { 4 }
$noPrep   = ($env:JOBPILOT_FL_NOPREP -eq "1")

# Child processes inherit this; without it a redirected stdout defaults to the
# ANSI code page and the first non-ASCII character rich prints kills the run.
$env:PYTHONIOENCODING = "utf-8"

$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($pidFile, "$PID", $utf8NoBom)

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Output $line
    [System.IO.File]::AppendAllText($loopLog, "$line`r`n", $utf8NoBom)
}

$applyArgs = @("watch", "--once",
               "--min-score", $minScore,
               "--hours-old", $hoursOld,
               "--workers", $workers,
               "--validation", "lenient")
if ($noPrep) { $applyArgs += "--no-prep" }

Log "=== fast_lane starting (pid=$PID, interval=${interval}s, minScore=$minScore, hoursOld=${hoursOld}h, workers=$workers, prep=$(-not $noPrep), submit=never) ==="

# --once per iteration rather than letting `watch` loop internally: a poll that
# wedges on a hung board request dies with its process instead of stalling the
# watcher forever, and the next tick starts clean.
while ($true) {
    $tmpOut = [System.IO.Path]::GetTempFileName()
    $tmpErr = [System.IO.Path]::GetTempFileName()
    try {
        $p = Start-Process -FilePath $venvPy -ArgumentList $applyArgs -NoNewWindow -PassThru `
             -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr
        # A single poll should take well under a minute; 15 is a generous cap.
        if (-not $p.WaitForExit(15 * 60 * 1000)) {
            Log "!!! poll exceeded 15min -- killing"
            & taskkill /PID $p.Id /T /F 2>&1 | Out-Null
            Start-Sleep -Seconds 2
        }
        $out = Get-Content $tmpOut -Raw -ErrorAction SilentlyContinue
        $err = Get-Content $tmpErr -Raw -ErrorAction SilentlyContinue
        if ($out) { [System.IO.File]::AppendAllText($loopLog, $out, $utf8NoBom) }
        if ($err) { [System.IO.File]::AppendAllText($loopLog, $err, $utf8NoBom) }
    } catch {
        Log "poll error: $_"
    } finally {
        Remove-Item $tmpOut,$tmpErr -ErrorAction SilentlyContinue
    }

    Start-Sleep -Seconds $interval
}
