# docker_watchdog.ps1
# Monitors the Docker daemon and restarts Docker Desktop if it goes down.
# Logs every state change to docker_watchdog.log. Run in background:
#   powershell -WindowStyle Hidden -File docker_watchdog.ps1
#
# Why: the hm100 SWE-bench run repeatedly lost all workers to Docker Desktop
# outages (daemon pipe disappears). This watchdog keeps the daemon alive and
# logs downtime so the ledger can be re-checked after recovery.

$log = Join-Path $PSScriptRoot "docker_watchdog.log"
$desktop = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
$checkSeconds = 30
$graceSeconds = 90   # wait after launching before declaring failure

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content -Path $log -Value $line
    Write-Host $line
}

function Test-Daemon {
    $out = docker info --format '{{.ServerVersion}}' 2>$null | Out-String
    return ($LASTEXITCODE -eq 0 -and $out.Trim() -ne "")
}

Write-Log "docker_watchdog started. Checking every ${checkSeconds}s."

$downSince = $null
while ($true) {
    Start-Sleep -Seconds $checkSeconds
    if (Test-Daemon) {
        if ($downSince -ne $null) {
            $dur = [math]::Round(((Get-Date) - $downSince).TotalMinutes, 1)
            Write-Log "Docker daemon RECOVERED after ${dur} min downtime."
            $downSince = $null
        }
        continue
    }
    # daemon is down
    if ($downSince -eq $null) {
        $downSince = Get-Date
        Write-Log "Docker daemon UNREACHABLE. Restarting Docker Desktop..."
    }
    $alreadyStarted = Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue
    if (-not $alreadyStarted) {
        if (Test-Path $desktop) {
            Start-Process -FilePath $desktop
            Write-Log "  Launched Docker Desktop."
        } else {
            Write-Log "  ERROR: Docker Desktop.exe not found at $desktop"
        }
    } else {
        Write-Log "  Docker Desktop process present but daemon still down (since $($downSince.ToString('HH:mm:ss')))."
        # Give it grace time, then force-restart the process to clear a wedged state.
        if (((Get-Date) - $downSince).TotalSeconds -gt $graceSeconds) {
            Write-Log "  Grace period exceeded; force-restarting Docker Desktop."
            Stop-Process -Name "Docker Desktop" -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 3
            if (Test-Path $desktop) { Start-Process -FilePath $desktop }
            $downSince = Get-Date
        }
    }
}
