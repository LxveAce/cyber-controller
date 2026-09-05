<#
.SYNOPSIS
  Runs the REAL Get-AvStateSummary.ps1 with MOCKED Defender cmdlets so the
  actual data-collection path (not just the pure helper) is exercised without
  ever reading real AV/Defender state.

.DESCRIPTION
  Each scenario defines function stand-ins for Get-MpComputerStatus and/or
  Get-MpPreference in this scope. PowerShell command resolution prefers a
  function over a cmdlet of the same name, so the collector's
  Get-Command / invocation binds to these mocks and the real Defender cmdlets
  are never called. Intended to be launched in a CHILD pwsh/powershell process,
  one scenario per process, for full isolation. It changes nothing on the host.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Scenario,
    [Parameter(Mandatory)][string]$OutDir
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$collector = Join-Path (Split-Path -Parent $PSScriptRoot) 'Get-AvStateSummary.ps1'

# Healthy computer-status object reused by several scenarios.
function New-HealthyStatus {
    [pscustomobject]@{
        AMRunningMode = 'Normal'; AntivirusEnabled = $true; RealTimeProtectionEnabled = $true
        BehaviorMonitorEnabled = $true; IoavProtectionEnabled = $true
        OnAccessProtectionEnabled = $true; IsTamperProtected = $true
    }
}

switch ($Scenario) {

    # Healthy status, but every preference flag is $null and exclusions are null.
    # This is the regression the [bool]-coercion defect hid: null Disable* must
    # NOT be reported as "not disabled" (safe).
    'healthy-null-prefs' {
        function Get-MpComputerStatus { New-HealthyStatus }
        function Get-MpPreference {
            [pscustomobject]@{
                DisableRealtimeMonitoring = $null; DisableBehaviorMonitoring = $null
                DisableIOAVProtection = $null; DisableScriptScanning = $null; DisableArchiveScanning = $null
                EnableControlledFolderAccess = $null; EnableNetworkProtection = $null
                ExclusionPath = $null; ExclusionExtension = $null; ExclusionProcess = $null
            }
        }
    }

    # Fully protected: all Disable* explicitly $false, zero exclusions.
    'fully-protected' {
        function Get-MpComputerStatus { New-HealthyStatus }
        function Get-MpPreference {
            [pscustomobject]@{
                DisableRealtimeMonitoring = $false; DisableBehaviorMonitoring = $false
                DisableIOAVProtection = $false; DisableScriptScanning = $false; DisableArchiveScanning = $false
                EnableControlledFolderAccess = 'Disabled'; EnableNetworkProtection = 'Disabled'
                ExclusionPath = @(); ExclusionExtension = @(); ExclusionProcess = @()
            }
        }
    }

    # Protected flags, but ONE exclusion path present.
    'one-exclusion' {
        function Get-MpComputerStatus { New-HealthyStatus }
        function Get-MpPreference {
            [pscustomobject]@{
                DisableRealtimeMonitoring = $false; DisableBehaviorMonitoring = $false
                DisableIOAVProtection = $false; DisableScriptScanning = $false; DisableArchiveScanning = $false
                EnableControlledFolderAccess = 'Disabled'; EnableNetworkProtection = 'Disabled'
                ExclusionPath = 'C:\one'; ExclusionExtension = @(); ExclusionProcess = @()
            }
        }
    }

    # Protected flags, but MANY exclusions across all three kinds.
    'many-exclusions' {
        function Get-MpComputerStatus { New-HealthyStatus }
        function Get-MpPreference {
            [pscustomobject]@{
                DisableRealtimeMonitoring = $false; DisableBehaviorMonitoring = $false
                DisableIOAVProtection = $false; DisableScriptScanning = $false; DisableArchiveScanning = $false
                EnableControlledFolderAccess = 'Disabled'; EnableNetworkProtection = 'Disabled'
                ExclusionPath = @('C:\a','C:\b','C:\c'); ExclusionExtension = @('.exe','.dll')
                ExclusionProcess = @('p1.exe')
            }
        }
    }

    # Status cmdlet present, preference cmdlet ABSENT (partial module).
    'pref-cmdlet-absent' {
        function Get-MpComputerStatus { New-HealthyStatus }
        # Deliberately do NOT define Get-MpPreference.
        # On a dev host the real cmdlet may exist; neutralise it so the mock
        # scenario is honest about "module unavailable".
        if (Get-Command Get-MpPreference -ErrorAction SilentlyContinue) {
            function Get-MpPreference { throw [System.Management.Automation.CommandNotFoundException]::new('mock: absent') }
        }
    }

    # Status query throws (transient/query failure) — must be conservative.
    'status-query-throws' {
        function Get-MpComputerStatus { throw [System.InvalidOperationException]::new('mock query failure') }
        function Get-MpPreference {
            [pscustomobject]@{
                DisableRealtimeMonitoring = $false; DisableBehaviorMonitoring = $false
                DisableIOAVProtection = $false; DisableScriptScanning = $false; DisableArchiveScanning = $false
                EnableControlledFolderAccess = 'Disabled'; EnableNetworkProtection = 'Disabled'
                ExclusionPath = @(); ExclusionExtension = @(); ExclusionProcess = @()
            }
        }
    }

    # Status object is MISSING fields the collector reads (abnormal shape).
    'status-missing-fields' {
        function Get-MpComputerStatus {
            [pscustomobject]@{ AMRunningMode = 'Normal'; AntivirusEnabled = $true }  # rest missing
        }
        function Get-MpPreference {
            [pscustomobject]@{
                DisableRealtimeMonitoring = $false; DisableBehaviorMonitoring = $false
                DisableIOAVProtection = $false; DisableScriptScanning = $false; DisableArchiveScanning = $false
                EnableControlledFolderAccess = 'Disabled'; EnableNetworkProtection = 'Disabled'
                ExclusionPath = @(); ExclusionExtension = @(); ExclusionProcess = @()
            }
        }
    }

    # Both cmdlets absent (Defender module unavailable entirely).
    'both-cmdlets-absent' {
        if (Get-Command Get-MpComputerStatus -ErrorAction SilentlyContinue) {
            function Get-MpComputerStatus { throw [System.Management.Automation.CommandNotFoundException]::new('mock: absent') }
        }
        if (Get-Command Get-MpPreference -ErrorAction SilentlyContinue) {
            function Get-MpPreference { throw [System.Management.Automation.CommandNotFoundException]::new('mock: absent') }
        }
    }

    default { throw "Unknown scenario: $Scenario" }
}

# For the "absent" scenarios we must also fool Get-Command inside the collector.
# The collector runs in a nested scope and uses Get-Command -Name <cmd>; when we
# intend "absent" but the real cmdlet exists on this host, shadow Get-Command
# with a GLOBAL override (visible to every nested scope) and read the hidden
# list from a GLOBAL variable so it resolves regardless of the caller's scope.
if ($Scenario -eq 'pref-cmdlet-absent' -or $Scenario -eq 'both-cmdlets-absent') {
    $Global:PfHiddenCommands = @()
    if ($Scenario -eq 'pref-cmdlet-absent') { $Global:PfHiddenCommands = @('Get-MpPreference') }
    if ($Scenario -eq 'both-cmdlets-absent') { $Global:PfHiddenCommands = @('Get-MpComputerStatus','Get-MpPreference') }
    function Global:Get-Command {
        param([string]$Name, $ErrorAction)
        if ($Global:PfHiddenCommands -contains $Name) { return $null }
        Microsoft.PowerShell.Core\Get-Command @PSBoundParameters
    }
}

& $collector -OutDir $OutDir | Out-Null
Get-Content -LiteralPath (Join-Path $OutDir 'av-state-summary.json') -Raw
