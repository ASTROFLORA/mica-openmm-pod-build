# Diagnostic script for ASTROFLORA/mica-openmm-pod-build workflow runs.
# Usage:
#   ./diag-runs.ps1                # diagnose most recent run
#   ./diag-runs.ps1 -RunId 29080103905
#
# This script:
# 1. Lists the latest 5 workflow runs with status and conclusion.
# 2. If a run has failed, downloads the failed-step logs to ./logs/<run_id>_<job>.log
#    for offline analysis.

param(
    [Parameter(Mandatory=$false)]
    [string]$RunId
)

$ErrorActionPreference = 'Stop'
$repo = 'ASTROFLORA/mica-openmm-pod-build'
$logsDir = Join-Path $PSScriptRoot 'logs'

if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir | Out-Null }

Write-Host "=== Last 5 runs on $repo ===" -ForegroundColor Cyan
$allRuns = gh run list --repo $repo --limit 5 --json status,conclusion,name,headBranch,createdAt,databaseId,displayTitle | ConvertFrom-Json
foreach ($r in $allRuns) {
    Write-Host ("{0,-22} {1,-10} {2,-10} {3}" -f $r.createdAt, $r.status, ($r.conclusion ?? 'NA'), $r.displayTitle) -ForegroundColor Gray
}

if ([string]::IsNullOrEmpty($RunId)) {
    $RunId = $allRuns[0].databaseId
}

Write-Host ""
Write-Host "=== Selected run $RunId ===" -ForegroundColor Cyan
$jobs = gh run view $RunId --repo $repo --json jobs --jq '.jobs[]' 2>&1
Write-Host $jobs

foreach ($line in ($jobs -split "`n")) {
    if ($line -match '"name":\s*"([^"]+)"') {
        $jobName = $matches[1] -replace '[^\w\-]', '_'
        $logFile = Join-Path $logsDir "$RunId`_$jobName.log"
        Write-Host "Fetching failed log for job '$jobName'..."
        gh run view $RunId --repo $repo --log --job $jobName 2>&1 | Out-File -FilePath $logFile -Encoding utf8
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  FAILED to fetch log (run may still be in_progress)" -ForegroundColor Yellow
        } else {
            Write-Host "  Saved to $logFile" -ForegroundColor Green
            # Show last 60 lines
            Get-Content $logFile -Tail 60 | ForEach-Object { Write-Host "    $_" }
        }
    }
}
