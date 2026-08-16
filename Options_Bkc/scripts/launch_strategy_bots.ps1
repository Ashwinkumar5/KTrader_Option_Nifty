[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$watchdogLauncher = Join-Path $projectRoot "process_watch_dog\Run Watchdog.cmd"

if (-not (Test-Path -LiteralPath $watchdogLauncher -PathType Leaf)) {
    throw "Process Watchdog launcher not found: $watchdogLauncher"
}

if ($PSCmdlet.ShouldProcess("Process Watchdog", "Start production bot topology")) {
    & $watchdogLauncher
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
