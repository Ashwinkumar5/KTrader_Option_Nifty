[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$version = "2.14.3"
$archiveName = "nats-server-v$version-windows-amd64.zip"
$expectedSha256 = "94e338d742761272e31eab1efb1f767eac3a2e56e4c05a7933c65a73fe95a27b"
$releaseBase = "https://github.com/nats-io/nats-server/releases/download/v$version"
$projectRoot = Split-Path -Parent $PSScriptRoot
$installRoot = Join-Path $projectRoot ".runtime\nats-server-v$version"
$archivePath = Join-Path $installRoot $archiveName
$checksumsPath = Join-Path $installRoot "SHA256SUMS"
$executable = Join-Path $installRoot (
    "nats-server-v$version-windows-amd64\nats-server.exe"
)

New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
Invoke-WebRequest -Uri "$releaseBase/$archiveName" -OutFile $archivePath
Invoke-WebRequest -Uri "$releaseBase/SHA256SUMS" -OutFile $checksumsPath

$checksumEntry = Select-String -LiteralPath $checksumsPath `
    -Pattern ([regex]::Escape($archiveName) + '$') |
    Select-Object -First 1
if ($null -eq $checksumEntry) {
    throw "Official checksum entry not found for $archiveName"
}

$published = ($checksumEntry.Line -split '\s+')[0].ToLowerInvariant()
if ($published -ne $expectedSha256) {
    throw "Published NATS checksum differs from the pinned release checksum"
}

$actual = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $expectedSha256) {
    throw "NATS checksum mismatch: expected=$expectedSha256 actual=$actual"
}

Expand-Archive -LiteralPath $archivePath -DestinationPath $installRoot -Force
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "NATS executable was not extracted: $executable"
}

Write-Host "Installed verified NATS Server v$version at $executable"
& $executable --version
