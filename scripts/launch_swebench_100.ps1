param(
    [string]$RunRoot = "",
    [string]$Manifest = "",
    [string]$RowsDir = "",
    [string]$HarnessPython = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$parentRoot = (Resolve-Path (Join-Path $repoRoot "..")).Path
if (-not $RunRoot) { $RunRoot = Join-Path $repoRoot "runs\swebench-verified-100" }
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

$jobs = @(
    @{ Name = "plain_deepseek_v4_flash"; Treatment = "plain"; Model = "deepseek-v4-flash" },
    @{ Name = "plain_hy3"; Treatment = "plain"; Model = "hy3" },
    @{ Name = "harnessmetric_deepseek_v4_flash"; Treatment = "harnessmetric"; Model = "deepseek-v4-flash" },
    @{ Name = "harnessmetric_hy3"; Treatment = "harnessmetric"; Model = "hy3" }
)

foreach ($job in $jobs) {
    $pidPath = Join-Path $RunRoot ($job.Name + ".pid")
    if (Test-Path -LiteralPath $pidPath) {
        $existingPid = [int](Get-Content -LiteralPath $pidPath)
        if (Get-Process -Id $existingPid -ErrorAction SilentlyContinue) {
            Write-Host "$($job.Name) already running as PID $existingPid"
            continue
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
        "--generator-timeout", "1800",
        "--verifier-timeout", "1800",
        "--grade-timeout", "1800",
        "--max-refinements", "12",
        "--max-loop-hours", "12",
        "--infrastructure-retries", "3"
    )
    $process = Start-Process -FilePath $python -ArgumentList $jobArgs `
        -WorkingDirectory $repoRoot -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $RunRoot ($job.Name + ".stdout.log")) `
        -RedirectStandardError (Join-Path $RunRoot ($job.Name + ".stderr.log")) `
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
