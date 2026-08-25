# JobPilot Dashboard - standalone window launcher
# Click the desktop shortcut > ensures the dashboard server is running, then
# opens it in its OWN Edge app window (--app + a dedicated --user-data-dir).
# The dedicated profile guarantees a true standalone window (no tabs/URL bar)
# even when the user's normal Edge is already open, and it stays isolated from
# their browsing. The Flask server runs hidden in the background; re-running
# re-focuses the same app window.
$ErrorActionPreference = 'Continue'

$port   = 8765
$proj   = 'C:\msys64\home\aksha\projects\JobPilot'
$exe    = Join-Path $proj '.venv\Scripts\jobpilot.exe'
$edge   = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
$url    = 'http://127.0.0.1:' + $port
$edgeProfile = Join-Path $env:LOCALAPPDATA 'JobPilot\edge-app'
$outLog = Join-Path $proj '_dashboard_server.log'
$errLog = Join-Path $proj '_dashboard_server.err.log'

function Test-UrlUp($target) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri $target -TimeoutSec 2
        return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500)
    } catch { return $false }
}

# 1) ensure the dashboard server is running (idempotent)
if (-not (Test-UrlUp $url)) {
    try {
        Start-Process -FilePath $exe -ArgumentList @('web','--port',"$port",'--no-browser') `
            -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    } catch {
        Start-Process -FilePath $exe -ArgumentList @('web','--port',"$port",'--no-browser') `
            -WindowStyle Hidden
    }
    $deadline = (Get-Date).AddSeconds(45)
    while ((-not (Test-UrlUp $url)) -and ((Get-Date) -lt $deadline)) {
        Start-Sleep -Milliseconds 500
    }
}

# 2) open the standalone app window in its own isolated Edge instance
if (-not (Test-Path $edge)) { Start-Process $url; exit }
$appArg     = '--app=' + $url
$profileArg = '--user-data-dir=' + $edgeProfile
Start-Process -FilePath $edge -ArgumentList $appArg, $profileArg, '--no-first-run', '--no-default-browser-check'
