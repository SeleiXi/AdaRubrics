param(
    [string]$RunRoot = "",
    [string]$Manifest = "",
    [string]$RowsDir = "",
    [string]$HarnessPython = "",
    # Optional: only restart these arm names. Empty = all four arms.
    [string[]]$Only = @()
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$parentRoot = (Resolve-Path (Join-Path $repoRoot "..")).Path
if (-not $RunRoot) { $RunRoot = Join-Path $parentRoot "runs\hm100" }
if (-not $Manifest) { $Manifest = Join-Path $parentRoot "runs\swebench-100\manifest.json" }
if (-not $RowsDir) { $RowsDir = Join-Path $parentRoot "tmp\swebench" }
if (-not $HarnessPython) {
    $HarnessPython = Join-Path $parentRoot "tmp\runtime\swebench-venv\Scripts\python.exe"
}

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$runner = Join-Path $repoRoot "scripts\run_swebench_harnessmetric.py"
$monitor = Join-Path $repoRoot "scripts\monitor_harnessmetric.py"
$ledger = Join-Path $RunRoot "experiment.json"
foreach ($required in @($python, $runner, $monitor, $Manifest, $HarnessPython)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "Required path not found: $required" }
}
New-Item -ItemType Directory -Force -Path $RunRoot | Out-Null

# IMPORTANT: This launcher never kills processes. To resume a dead arm, just rerun it.
# Do NOT taskkill /T the runner PID — that aborts mid-loop checkpoints and leaves the
# ledger stuck at status=running. Agent timeouts already isolate-kill only CodeBuddy.

# Every task starts with `docker pull`. Launching into a stopped daemon (for example
# after a reboot where Docker Desktop did not auto-start) turns the whole 100-task
# queue into infrastructure failures within minutes, so wait for it here.
function Wait-DockerDaemon {
    param([int]$TimeoutSeconds = 300)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $started = $false
    while ((Get-Date) -lt $deadline) {
        docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return $true }
        if (-not $started) {
            $desktop = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
            if (Test-Path -LiteralPath $desktop) {
                Write-Host "Docker daemon unreachable; starting Docker Desktop..."
                Start-Process -FilePath $desktop -WindowStyle Minimized
            }
            $started = $true
        }
        Start-Sleep -Seconds 5
    }
    return $false
}

if (-not (Wait-DockerDaemon)) {
    throw "Docker daemon is not reachable. Start Docker Desktop, then rerun this launcher."
}
Write-Host "Docker daemon ready."

function Test-ArmProcess {
    param(
        [int]$ProcessId,
        [string]$Treatment,
        [string]$Model
    )
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $proc -or -not $proc.CommandLine) { return $false }
    $cmd = $proc.CommandLine
    return (
        ($cmd -match 'run_swebench_harnessmetric\.py') -and
        ($cmd -match [regex]::Escape("--treatment $Treatment")) -and
        ($cmd -match [regex]::Escape("--model $Model"))
    )
}

$jobs = @(
    @{ Name = "plain_deepseek_v4_flash"; Treatment = "plain"; Model = "deepseek-v4-flash" },
    @{ Name = "plain_hy3"; Treatment = "plain"; Model = "hy3" },
    @{ Name = "harnessmetric_deepseek_v4_flash"; Treatment = "harnessmetric"; Model = "deepseek-v4-flash" },
    @{ Name = "harnessmetric_hy3"; Treatment = "harnessmetric"; Model = "hy3" }
)
if ($Only.Count -gt 0) {
    $onlyNames = @(
        $Only |
            ForEach-Object { $_ -split ',' } |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ }
    )
    $jobs = @($jobs | Where-Object { $onlyNames -contains $_.Name })
    if ($jobs.Count -eq 0) { throw "No matching arms for -Only $($onlyNames -join ', ')" }
}

foreach ($job in $jobs) {
    $pidPath = Join-Path $RunRoot ($job.Name + ".pid")
    if (Test-Path -LiteralPath $pidPath) {
        $existingPid = 0
        [void][int]::TryParse(((Get-Content -LiteralPath $pidPath -Raw).Trim()), [ref]$existingPid)
        if ($existingPid -gt 0 -and (Test-ArmProcess -ProcessId $existingPid -Treatment $job.Treatment -Model $job.Model)) {
            Write-Host "$($job.Name) already running as PID $existingPid (left untouched)"
            continue
        }
        if ($existingPid -gt 0) {
            Write-Host "$($job.Name) pid file $existingPid is stale/dead; resuming without killing anyone"
            Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        }
    }
    $stdoutLog = Join-Path $RunRoot ($job.Name + ".stdout.log")
    $stderrLog = Join-Path $RunRoot ($job.Name + ".stderr.log")
    # Append-friendly rotate: keep prior logs when resuming a dead arm.
    foreach ($logPath in @($stdoutLog, $stderrLog)) {
        if ((Test-Path -LiteralPath $logPath) -and ((Get-Item -LiteralPath $logPath).Length -gt 0)) {
            $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
            Rename-Item -LiteralPath $logPath -NewName ("{0}.{1}.bak" -f (Split-Path $logPath -Leaf), $stamp)
        }
    }
    $jobArgs = @(
        $runner,
        "--manifest", $Manifest,
        "--rows-dir", $RowsDir,
        "--run-root", $RunRoot,
        "--ledger", $ledger,
        "--harness-python", $HarnessPython,
        "--treatment", $job.Treatment,
        "--model", $job.Model,
        "--effort", "medium",
        "--initial-metric-policy", "off",
        "--agent-timeout", "7200",
        "--generator-timeout", "7200",
        "--verifier-timeout", "7200",
        "--grade-timeout", "7200",
        "--max-refinements", "12",
        "--max-loop-hours", "48",
        "--infrastructure-retries", "3",
        "--quota-retry-seconds", "300",
        "--quota-max-wait-hours", "168"
    )
    $process = Start-Process -FilePath $python -ArgumentList $jobArgs `
        -WorkingDirectory $repoRoot -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru
    Set-Content -LiteralPath $pidPath -Value $process.Id
    Write-Host "Started $($job.Name) as PID $($process.Id)"
}

$monitorPidPath = Join-Path $RunRoot "monitor.pid"
$monitorRunning = $false
if (Test-Path -LiteralPath $monitorPidPath) {
    $monitorPid = [int](Get-Content -LiteralPath $monitorPidPath)
    $monitorRunning = [bool](Get-Process -Id $monitorPid -ErrorAction SilentlyContinue)
}
if (-not $monitorRunning) {
    $monitorCommand = "& '$python' '$monitor' --ledger '$ledger' --interval 300"
    $monitorProcess = Start-Process -FilePath "powershell.exe" `
        -ArgumentList @("-NoLogo", "-NoExit", "-Command", $monitorCommand) `
        -WorkingDirectory $repoRoot -PassThru
    Set-Content -LiteralPath $monitorPidPath -Value $monitorProcess.Id
    Write-Host "Opened five-minute monitor as PID $($monitorProcess.Id)"
}

Write-Host "Ledger: $ledger"
