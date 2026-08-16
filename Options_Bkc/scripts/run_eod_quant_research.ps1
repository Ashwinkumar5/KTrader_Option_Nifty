[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [Alias("BrokerTape")]
    [string]$BrokerTapeFolder,

    [string[]]$AnalyticsTrace = @(),

    [string]$ReportsRoot = "",

    [ValidateRange(1, 365)]
    [int]$RollingDays = 14,

    [ValidateRange(1, 1000000)]
    [int]$MinimumRankingTrades = 3,

    [ValidateRange(1, 1000000)]
    [int]$MinimumCumulativeTrades = 30,

    [Alias("MinimumHistorySessions")]
    [ValidateRange(1, 365)]
    [int]$MinimumTradingDays = 8,

    [ValidateRange(0, 100)]
    [decimal]$RoundTripCostPercent = 0.20,

    [ValidateRange(0, 100000000)]
    [int]$MaxFrames = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot "myenv\Scripts\python.exe"
if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    $python = $venvPython
}
else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw "Python was not found. Create myenv or install Python."
    }
    $python = $pythonCommand.Source
}

$tapeFolder = Resolve-Path -LiteralPath $BrokerTapeFolder -ErrorAction Stop
if (-not (Test-Path -LiteralPath $tapeFolder.Path -PathType Container)) {
    throw "BrokerTapeFolder must be a directory: $($tapeFolder.Path)"
}
$tapes = @(
    Get-ChildItem -LiteralPath $tapeFolder.Path -Recurse `
        -Filter "broker_replay_tape*.jsonl" -File |
        Sort-Object FullName
)
if ($tapes.Count -eq 0) {
    throw (
        "No broker_replay_tape*.jsonl files were found under " +
        $tapeFolder.Path
    )
}
$tracePaths = @()
foreach ($trace in $AnalyticsTrace) {
    $tracePaths += (
        Resolve-Path -LiteralPath $trace -ErrorAction Stop
    ).Path
}

if ([string]::IsNullOrWhiteSpace($ReportsRoot)) {
    $reportsRootPath = Join-Path (
        Join-Path $projectRoot "dummy_broker_replay\runs"
    ) "eod_quant_research"
}
elseif ([IO.Path]::IsPathRooted($ReportsRoot)) {
    $reportsRootPath = $ReportsRoot
}
else {
    $reportsRootPath = Join-Path $projectRoot $ReportsRoot
}
$reportsRootPath = [IO.Path]::GetFullPath($reportsRootPath)

$dateName = Get-Date -Format "yyyy-MM-dd"
$timeName = Get-Date -Format "HHmmssfff"
$runId = "{0}_{1}" -f $timeName, $PID
$dateFolder = Join-Path $reportsRootPath $dateName
$runFolder = Join-Path $dateFolder ("run_{0}" -f $runId)
$phase1Root = Join-Path $runFolder "phase1"
$phase2Root = Join-Path $runFolder "phase2"
$dashboardFolder = Join-Path $runFolder "dashboard"
New-Item -ItemType Directory -Path $runFolder | Out-Null

function Invoke-ResearchCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Stage,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    Write-Host ""
    Write-Host "=== $Stage ===" -ForegroundColor Cyan
    & $python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Stage failed with exit code $LASTEXITCODE."
    }
}

$commonTraceArguments = @()
foreach ($tracePath in $tracePaths) {
    $commonTraceArguments += "--analytics-trace"
    $commonTraceArguments += $tracePath
}

$phase1Summaries = @()
$phase2Summaries = @()
$tapeNumber = 0
foreach ($tape in $tapes) {
    $tapeNumber += 1
    $tapeLabel = "[{0}/{1}] {2}" -f (
        $tapeNumber,
        $tapes.Count,
        $tape.Name
    )
    $sourceStem = [IO.Path]::GetFileNameWithoutExtension($tape.FullName)
    $tapeRunId = "{0}_t{1:D3}" -f $runId, $tapeNumber

    $phase1Arguments = @(
        "-m",
        "dummy_broker_replay.run_phase1_feature_research",
        $tape.FullName,
        "--output-root",
        $phase1Root,
        "--phase1-id",
        $tapeRunId,
        "--round-trip-cost-percent",
        [string]$RoundTripCostPercent
    )
    $phase1Arguments += $commonTraceArguments
    if ($MaxFrames -gt 0) {
        $phase1Arguments += "--max-frames"
        $phase1Arguments += [string]$MaxFrames
    }
    Invoke-ResearchCommand `
        -Stage "$tapeLabel - Phase 1: feature ablations" `
        -Arguments $phase1Arguments

    $phase1Run = Join-Path $phase1Root (
        "{0}_phase1_{1}" -f $sourceStem, $tapeRunId
    )
    $phase1Summary = Join-Path $phase1Run "phase1_summary.json"
    if (-not (Test-Path -LiteralPath $phase1Summary -PathType Leaf)) {
        throw "Phase-1 summary was not generated: $phase1Summary"
    }
    $phase1Summaries += $phase1Summary

    $phase2Arguments = @(
        "-m",
        "dummy_broker_replay.run_phase2_combination_research",
        $tape.FullName,
        "--output-root",
        $phase2Root,
        "--phase2-id",
        $tapeRunId,
        "--minimum-ranking-trades",
        [string]$MinimumRankingTrades,
        "--round-trip-cost-percent",
        [string]$RoundTripCostPercent,
        "--phase1-summary",
        $phase1Summary
    )
    $phase2Arguments += $commonTraceArguments
    if ($MaxFrames -gt 0) {
        $phase2Arguments += "--max-frames"
        $phase2Arguments += [string]$MaxFrames
    }
    Invoke-ResearchCommand `
        -Stage "$tapeLabel - Phase 2: quantitative combinations" `
        -Arguments $phase2Arguments

    $phase2Run = Join-Path $phase2Root (
        "{0}_phase2_{1}" -f $sourceStem, $tapeRunId
    )
    $phase2Summary = Join-Path $phase2Run "phase2_summary.json"
    if (-not (Test-Path -LiteralPath $phase2Summary -PathType Leaf)) {
        throw "Phase-2 summary was not generated: $phase2Summary"
    }
    $phase2Summaries += $phase2Summary
}

$dashboardArguments = @(
    "-m",
    "dummy_broker_replay.generate_research_dashboard",
    "--reports-root",
    $reportsRootPath,
    "--output-directory",
    $dashboardFolder,
    "--rolling-days",
    [string]$RollingDays,
    "--minimum-cumulative-trades",
    [string]$MinimumCumulativeTrades,
    "--minimum-trading-days",
    [string]$MinimumTradingDays
)
foreach ($phase1Summary in $phase1Summaries) {
    $dashboardArguments += "--phase1-summary"
    $dashboardArguments += $phase1Summary
}
foreach ($phase2Summary in $phase2Summaries) {
    $dashboardArguments += "--phase2-summary"
    $dashboardArguments += $phase2Summary
}
Invoke-ResearchCommand -Stage "Consolidated dashboard" `
    -Arguments $dashboardArguments

Write-Host ""
Write-Host "EOD quantitative research completed." -ForegroundColor Green
Write-Host "Tapes       : $($tapes.Count)"
Write-Host "Date folder : $dateFolder"
Write-Host "Run folder  : $runFolder"
Write-Host "Dashboard   : $(Join-Path $dashboardFolder 'dashboard.html')"
Write-Host "Rolling CSV : $(Join-Path $dashboardFolder ('rolling_{0}d_combinations.csv' -f $RollingDays))"
