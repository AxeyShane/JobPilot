# JobPilot autonomous agent loop.
# Cycles: discover -> enrich -> score -> tailor -> cover -> pdf -> apply, forever.
#
# Usage:
#   powershell -File scripts\agent_loop.ps1              # starts dry-run (safe default)
#
# Live/dry-run is controlled by ~/.jobpilot/live.flag ("1" or "0"), re-read every
# cycle -- toggle it from the web UI (jobpilot web) without restarting the loop.
#
# Env overrides:
#   JOBPILOT_CYCLE_HOURS   hours between pipeline cycles (default 4, set to 0 for continuous)
#   JOBPILOT_APPLY_LIMIT   max applications submitted per cycle (default 15, 0=unlimited)
#   JOBPILOT_MIN_SCORE     minimum fit score to apply (default 6)
#   JOBPILOT_WORKERS       parallel threads for discover/enrich (default 4)
#   JOBPILOT_CONTINUOUS    set to "1" to run apply in continuous mode (no sleep between applies)
#   JOBPILOT_PIPELINE_MAX_MIN  watchdog cap for one pipeline cycle (default 180)
#
# NOTE: this loop works the BACKLOG. For fresh postings run scripts\fast_lane.ps1
# alongside it -- a full cycle here regularly hits the watchdog, so nothing
# time-sensitive should depend on it finishing.

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$venvPy = Join-Path $root ".venv\Scripts\jobpilot.exe"
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

# Resolve the active profile's directory the same way config.py does
$appDir = & $venvPython -c "import sys; sys.path.insert(0, r'$root\src'); from jobpilot.config import APP_DIR; print(APP_DIR)" 2>$null
$appDir = $appDir.Trim()
if (-not $appDir) {
    $appDir = Join-Path $env:USERPROFILE ".jobpilot"
}
$logDir = Join-Path $appDir "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$loopLog = Join-Path $logDir "agent_loop.log"
$liveFlag = Join-Path $appDir "live.flag"
$engineFlag = Join-Path $appDir "engine.flag"
$pidFile = Join-Path $appDir "agent_loop.pid"

$cycleHours = if ($env:JOBPILOT_CYCLE_HOURS) { [int]$env:JOBPILOT_CYCLE_HOURS } else { 4 }
$applyLimit = if ($env:JOBPILOT_APPLY_LIMIT) { [int]$env:JOBPILOT_APPLY_LIMIT } else { 15 }
$minScore   = if ($env:JOBPILOT_MIN_SCORE)   { [int]$env:JOBPILOT_MIN_SCORE }   else { 6 }
$workers    = if ($env:JOBPILOT_WORKERS)     { [int]$env:JOBPILOT_WORKERS }     else { 4 }
$continuous = if ($env:JOBPILOT_CONTINUOUS)  { $env:JOBPILOT_CONTINUOUS -eq "1" } else { $false }
$pipelineMax = if ($env:JOBPILOT_PIPELINE_MAX_MIN) { [int]$env:JOBPILOT_PIPELINE_MAX_MIN } else { 180 }

# Inherited by every child. Without it a redirected stdout defaults to the ANSI
# code page (cp1252) and the first non-ASCII character rich emits raises
# UnicodeEncodeError -- which is what was silently killing every apply cycle.
$env:PYTHONIOENCODING = "utf-8"

$utf8NoBom = New-Object System.Text.UTF8Encoding $false

[System.IO.File]::WriteAllText($pidFile, "$PID", $utf8NoBom)
if (-not (Test-Path $liveFlag)) { [System.IO.File]::WriteAllText($liveFlag, "0", $utf8NoBom) }

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Output $line
    [System.IO.File]::AppendAllText($loopLog, "$line`r`n", $utf8NoBom)
}

function IsLive() {
    if (-not (Test-Path $liveFlag)) { return $false }
    return (Get-Content $liveFlag -Raw).Trim() -eq "1"
}

function Engine() {
    if (-not (Test-Path $engineFlag)) { return "claude" }
    $v = (Get-Content $engineFlag -Raw).Trim()
    if ($v -eq "local") { return "local" }
    return "claude"
}

# Run a command with stdout+stderr appended to the log, bounded by a hard
# wall-clock watchdog.
function RunLogged($exe, [string[]]$argArray, [int]$MaxRuntimeMinutes = 180) {
    $tmpOut = [System.IO.Path]::GetTempFileName()
    $tmpErr = [System.IO.Path]::GetTempFileName()
    try {
        $p = Start-Process -FilePath $exe -ArgumentList $argArray -NoNewWindow -PassThru `
             -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr
        $finished = $p.WaitForExit($MaxRuntimeMinutes * 60 * 1000)
        if (-not $finished) {
            Log "!!! WATCHDOG: '$exe $($argArray -join ' ')' exceeded ${MaxRuntimeMinutes}min -- killing process tree"
            & taskkill /PID $p.Id /T /F 2>&1 | Out-Null
            Start-Sleep -Seconds 2
        }
        $out = Get-Content $tmpOut -Raw -ErrorAction SilentlyContinue
        $err = Get-Content $tmpErr -Raw -ErrorAction SilentlyContinue
        if ($out) { [System.IO.File]::AppendAllText($loopLog, $out, $utf8NoBom) }
        if ($err) { [System.IO.File]::AppendAllText($loopLog, $err, $utf8NoBom) }
        if (-not $finished) { return -1 }
        return $p.ExitCode
    } finally {
        Remove-Item $tmpOut,$tmpErr -ErrorAction SilentlyContinue
    }
}

Log "=== agent_loop starting (pid=$PID, cycleHours=$cycleHours, applyLimit=$applyLimit, minScore=$minScore, workers=$workers, continuous=$continuous) ==="

while ($true) {
    # --- PIPELINE: discover -> enrich -> score -> tailor -> cover -> pdf ---
    Log "--- pipeline cycle: discover/enrich/score/tailor/cover/pdf (workers=$workers, stream, min-score=$minScore) ---"
    $rc = RunLogged $venvPy @("run", "--min-score", $minScore, "--workers", $workers, "--validation", "lenient", "--stream") $pipelineMax
    Log "--- pipeline cycle exit code: $rc ---"

    # Surface how many jobs are actually submittable. An apply cycle that ends
    # in seconds is ambiguous -- empty queue or crashed? -- and that ambiguity
    # hid a hard crash for days. The query lives in its own file because it
    # uses a less-than comparison, and PowerShell treats that character as a
    # reserved redirection operator even inside a quoted -c argument.
    $ready = & $venvPython (Join-Path $PSScriptRoot "ready_count.py") $minScore 2>$null
    if (-not $ready) { $ready = "?" }
    Log "--- ready to apply: $($ready.ToString().Trim()) ---"

    $eng = Engine

    # --- APPLY: submit applications ---
    if (IsLive) {
        if ($continuous) {
            Log "--- apply cycle: CONTINUOUS mode, headless, engine=$eng, min-score=$minScore (LIVE) ---"
            # In continuous mode, run apply with --fast --continuous --poll-interval 5
            $rc = RunLogged $venvPy @("apply", "--min-score", $minScore, "--headless", "--engine", $eng, "--fast", "--continuous", "--poll-interval", "5") 240
        } else {
            Log "--- apply cycle: submitting up to $applyLimit application(s), headless, engine=$eng (LIVE) ---"
            $rc = RunLogged $venvPy @("apply", "--limit", $applyLimit, "--min-score", $minScore, "--headless", "--engine", $eng, "--fast") 120
        }
    } else {
        if ($continuous) {
            Log "--- apply cycle (DRY RUN): continuous mode, filling forms, not submitting, engine=$eng ---"
            $rc = RunLogged $venvPy @("apply", "--min-score", $minScore, "--headless", "--dry-run", "--engine", $eng, "--fast", "--continuous", "--poll-interval", "5") 240
        } else {
            Log "--- apply cycle (DRY RUN): filling forms, not submitting, engine=$eng ---"
            $rc = RunLogged $venvPy @("apply", "--limit", $applyLimit, "--min-score", $minScore, "--headless", "--dry-run", "--engine", $eng, "--fast") 120
        }
    }
    Log "--- apply cycle exit code: $rc ---"

    # --- SLEEP ---
    if ($continuous) {
        Log "--- continuous mode: pipeline cycle done, restarting immediately ---"
        Start-Sleep -Seconds 5  # Brief pause to avoid hammering
    } elseif ($cycleHours -gt 0) {
        Log "--- cycle done, sleeping ${cycleHours}h ---"
        Start-Sleep -Seconds ($cycleHours * 3600)
    } else {
        Log "--- cycle done, restarting immediately (cycleHours=0) ---"
        Start-Sleep -Seconds 5
    }
}
