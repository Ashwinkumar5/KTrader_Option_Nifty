[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string[]]$Profile = @(),
    [string]$TapePath = "",
    [ValidateRange(0, 100)]
    [decimal]$RoundTripCostPercent = 0.20,
    [ValidateRange(0, 1000000)]
    [int]$MaxFrames = 0
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot "myenv\Scripts\python.exe"
$replay = Join-Path $projectRoot "dummy_broker_replay\run_replay.py"
$config = Join-Path $projectRoot "config\strategy_config.json"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python environment not found: $python"
}

$configuredProfiles = @(
    (Get-Content -LiteralPath $config -Raw | ConvertFrom-Json).profiles.PSObject.Properties.Name
)
if ($Profile.Count -eq 0) {
    Write-Host ""
    for ($index = 0; $index -lt $configuredProfiles.Count; $index++) {
        Write-Host "$($index + 1)) $($configuredProfiles[$index])"
    }
    $choice = (Read-Host "Select profile number").Trim()
    $number = 0
    if (
        -not [int]::TryParse($choice, [ref]$number) -or
        $number -lt 1 -or
        $number -gt $configuredProfiles.Count
    ) {
        throw "Invalid profile selection: $choice"
    }
    $Profile = @($configuredProfiles[$number - 1])
}

foreach ($name in $Profile) {
    if ($name -notin $configuredProfiles) {
        throw "Unknown profile: $name"
    }
}

if ([string]::IsNullOrWhiteSpace($TapePath)) {
    $TapePath = (Read-Host "Tape file or folder").Trim()
}
$resolvedTapePath = Resolve-Path -LiteralPath $TapePath -ErrorAction Stop
$tapes = if (Test-Path -LiteralPath $resolvedTapePath.Path -PathType Container) {
    @(Get-ChildItem -LiteralPath $resolvedTapePath.Path -Recurse `
        -Filter "broker_replay_tape*.jsonl" -File | Sort-Object FullName)
}
else {
    @(Get-Item -LiteralPath $resolvedTapePath.Path)
}
if ($tapes.Count -eq 0) {
    throw "No broker replay tapes found: $TapePath"
}

$date = Get-Date -Format "yyyy-MM-dd"
$outputRoot = Join-Path $projectRoot "dummy_broker_replay\runs\profile_replay\$date"
$runStamp = Get-Date -Format "HHmmssfff"
$index = 0
foreach ($name in $Profile | Select-Object -Unique) {
    foreach ($tape in $tapes) {
        $index += 1
        $arguments = @(
            $replay,
            $tape.FullName,
            "--mode", "event-time",
            "--strategy-profile", $name,
            "--round-trip-cost-percent", [string]$RoundTripCostPercent,
            "--output-root", (Join-Path $outputRoot $name),
            "--run-id", ("{0}_{1}_{2:D3}" -f $runStamp, $PID, $index)
        )
        if ($MaxFrames -gt 0) {
            $arguments += @("--max-frames", [string]$MaxFrames)
        }
        if ($PSCmdlet.ShouldProcess("$name / $($tape.Name)", "Run causal replay")) {
            & $python @arguments
            if ($LASTEXITCODE -ne 0) {
                throw "Replay failed with exit code $LASTEXITCODE."
            }
        }
    }
}

Write-Host "Replay output: $outputRoot" -ForegroundColor Green
