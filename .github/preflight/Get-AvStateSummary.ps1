<#
.SYNOPSIS
  READ-ONLY Windows Defender state summary + conservative suitability verdict.

.DESCRIPTION
  Reports ONLY relevant booleans, the running-mode string, and the COUNTS of
  configured exclusions. It never reads, prints, or exports:
    - exclusion path/extension/process VALUES,
    - machine name, user, domain, or any identity,
    - environment variables, tokens, or secrets,
    - signature/definition file locations or any filesystem path.

  It changes NOTHING: no Set-MpPreference, no Add/Remove exclusion, no service
  control, no registry write. If the Defender module/cmdlets are unavailable,
  that is reported as a finding (avModuleAvailable=false), not worked around.

  It also emits a CONSERVATIVE protection-suitability verdict (av-suitability/v1)
  for a LATER flagged-package launch. Desktop CAPABILITY and protection
  SUITABILITY are kept separate: this script never asserts a runner is safe for
  a flagged launch, and a missing/null boolean is treated as UNKNOWN, not safe.

.OUTPUTS
  JSON to stdout and to -OutDir\av-state-summary.json.
#>
[CmdletBinding()]
param(
    [string]$OutDir = (Join-Path (Get-Location) 'preflight-out')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'PreflightCommon.ps1')

$summary = [ordered]@{
    schema             = 'av-state-summary/v1'
    timestampUtc       = (Get-Date).ToUniversalTime().ToString('o')
    avModuleAvailable  = $false
    computerStatus     = $null
    preferences        = $null
    notes              = @(
        'Booleans and counts only; no exclusion values, paths, identities, or secrets are read.',
        'Read-only: this script makes no Defender/security/registry/service changes.',
        'suitability is a conservative verdict for a later flagged launch; capability is measured elsewhere.'
    )
    errorCategories    = @()
    suitability        = $null
}

function Add-AvCategory([string]$category) {
    if (Test-PreflightErrorCategory $category) { $summary.errorCategories += $category }
    else { $summary.errorCategories += 'unexpected' }
}

try {
    $haveStatus = Get-Command -Name Get-MpComputerStatus -ErrorAction SilentlyContinue
    $havePref   = Get-Command -Name Get-MpPreference   -ErrorAction SilentlyContinue

    if ($haveStatus) {
        $summary.avModuleAvailable = $true
        $s = Get-MpComputerStatus
        $summary.computerStatus = [ordered]@{
            AMRunningMode              = [string]$s.AMRunningMode
            AntivirusEnabled           = $s.AntivirusEnabled
            RealTimeProtectionEnabled  = $s.RealTimeProtectionEnabled
            BehaviorMonitorEnabled     = $s.BehaviorMonitorEnabled
            IoavProtectionEnabled      = $s.IoavProtectionEnabled
            OnAccessProtectionEnabled  = $s.OnAccessProtectionEnabled
            IsTamperProtected          = $s.IsTamperProtected
        }
    } else {
        Add-AvCategory 'av-module-unavailable'
    }

    if ($havePref) {
        $p = Get-MpPreference
        # COUNTS ONLY, via strict-mode-safe helper. Never emit the values.
        $summary.preferences = [ordered]@{
            DisableRealtimeMonitoring     = $p.DisableRealtimeMonitoring
            DisableBehaviorMonitoring     = $p.DisableBehaviorMonitoring
            DisableIOAVProtection         = $p.DisableIOAVProtection
            DisableScriptScanning         = $p.DisableScriptScanning
            DisableArchiveScanning        = $p.DisableArchiveScanning
            EnableControlledFolderAccess  = [string]$p.EnableControlledFolderAccess
            EnableNetworkProtection       = [string]$p.EnableNetworkProtection
            ExclusionPathCount            = (Get-NonEmptyCount $p.ExclusionPath)
            ExclusionExtensionCount       = (Get-NonEmptyCount $p.ExclusionExtension)
            ExclusionProcessCount         = (Get-NonEmptyCount $p.ExclusionProcess)
        }
    } else {
        Add-AvCategory 'av-module-unavailable'
    }
}
catch {
    # Any throw from the read-only Defender queries above is an AV collection
    # failure; label it honestly rather than reusing a UI error category.
    Add-AvCategory 'av-query-failed'
}
finally {
    try { $summary.suitability = Get-SuitabilityVerdict $summary }
    catch { Add-AvCategory 'av-query-failed' }

    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
    $json = $summary | ConvertTo-Json -Depth 6
    $json | Out-File -FilePath (Join-Path $OutDir 'av-state-summary.json') -Encoding utf8
    Write-Output $json
}

