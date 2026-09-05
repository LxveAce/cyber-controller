<#
.SYNOPSIS
  INTEGRATION tests for the actual Get-AvStateSummary.ps1 data-collection path,
  with MOCKED Defender cmdlets. No real AV/Defender read, no window, no network.

.DESCRIPTION
  Helper-only tests exercise Get-SuitabilityVerdict on hand-built summaries and
  therefore CANNOT catch a data-conversion bug inside the collector itself (for
  example the earlier `[bool]$p.Disable*` coercion that turned an unmeasured
  null into a false "not disabled"). These tests run the REAL collector via
  CollectorMockHarness.ps1 in a CHILD process per scenario and assert on the
  emitted av-state-summary.json, so the actual property reads and JSON shape are
  covered. Every scenario's Defender cmdlets are function mocks; the genuine
  Get-MpComputerStatus / Get-MpPreference are never invoked.

  The healthy-null-prefs scenario is the concrete regression guard: a collector
  that coerces a null preference flag to $false would report the runner as
  suitable and FAIL this scenario.
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$harness = Join-Path $here 'CollectorMockHarness.ps1'
$selfHost = (Get-Process -Id $PID).Path   # run child scenarios on the same host

$script:Pass = 0
$script:Fail = 0
function Assert([bool]$Condition, [string]$Name) {
    if ($Condition) { $script:Pass++; Write-Host "  [PASS] $Name" }
    else            { $script:Fail++; Write-Host "  [FAIL] $Name" -ForegroundColor Red }
}

function Invoke-Scenario([string]$Scenario) {
    $outDir = Join-Path ([System.IO.Path]::GetTempPath()) ("pf-collector-" + [guid]::NewGuid().ToString('N').Substring(0,8))
    $args = @('-NoProfile','-ExecutionPolicy','Bypass','-File',$harness,'-Scenario',$Scenario,'-OutDir',$outDir)
    $json = & $selfHost @args
    $obj = ($json | Out-String | ConvertFrom-Json)
    Remove-Item -Recurse -Force $outDir -ErrorAction SilentlyContinue
    return $obj
}

# Distinguish a JSON null from boolean $false on a preference property.
function Test-JsonNull($obj, [string]$prop) {
    $p = $obj.PSObject.Properties[$prop]
    return ($null -ne $p -and $null -eq $p.Value)
}

Write-Host 'Integration - healthy status but ALL preference flags null (the regression counterexample):'
$r = Invoke-Scenario 'healthy-null-prefs'
Assert ($r.avModuleAvailable -eq $true)                                  'avModuleAvailable true'
Assert (Test-JsonNull $r.preferences 'DisableRealtimeMonitoring')        'null Disable* preserved as JSON null (not coerced to false)'
Assert (Test-JsonNull $r.preferences 'DisableScriptScanning')            'null DisableScriptScanning preserved as JSON null'
Assert ($r.suitability.protectionMeasured -eq $true)                     'protection measured'
Assert ($r.suitability.suitableForFlaggedLaunch -eq $false)              'null flags => NOT suitable (would be TRUE on pre-fix collector)'
Assert ($r.suitability.reasons -contains 'realtime-monitoring-disabled') 'reason: realtime-monitoring-disabled (null=unknown=unsafe)'
Assert ($r.suitability.reasons -contains 'script-scanning-disabled')     'reason: script-scanning-disabled'
# A NULL exclusion list is legitimately "no exclusions configured" => count 0,
# so it must NOT raise the exclusions reason; the null Disable* flags alone
# already force the not-suitable verdict above.
Assert ($r.preferences.ExclusionPathCount -eq 0)                         'null exclusion list => count 0 (not "unknown")'
Assert ($r.suitability.reasons -notcontains 'exclusions-present-or-unknown') 'null exclusion list raises no exclusion reason'

Write-Host 'Integration - fully protected, zero exclusions:'
$r = Invoke-Scenario 'fully-protected'
Assert ($r.preferences.ExclusionPathCount -eq 0)                         '0 exclusion paths'
Assert ($r.preferences.ExclusionExtensionCount -eq 0)                    '0 exclusion extensions'
Assert ($r.preferences.ExclusionProcessCount -eq 0)                      '0 exclusion processes'
Assert ($r.preferences.DisableRealtimeMonitoring -eq $false)             'explicit false Disable* preserved as false'
Assert ($r.suitability.suitableForFlaggedLaunch -eq $true)               'fully protected => suitable'
Assert (@($r.suitability.reasons).Count -eq 0)                           'suitable => no reasons'

Write-Host 'Integration - ONE exclusion path:'
$r = Invoke-Scenario 'one-exclusion'
Assert ($r.preferences.ExclusionPathCount -eq 1)                         'ExclusionPathCount = 1'
Assert ($r.suitability.suitableForFlaggedLaunch -eq $false)             '1 exclusion => not suitable'
Assert ($r.suitability.reasons -contains 'exclusions-present-or-unknown')'reason: exclusions present'
# No exclusion VALUE must ever appear in the output.
Assert (($r | ConvertTo-Json -Depth 6) -notmatch 'C:\\one')             'exclusion path VALUE never emitted'

Write-Host 'Integration - MANY exclusions across all three kinds:'
$r = Invoke-Scenario 'many-exclusions'
Assert ($r.preferences.ExclusionPathCount -eq 3)                         'ExclusionPathCount = 3'
Assert ($r.preferences.ExclusionExtensionCount -eq 2)                    'ExclusionExtensionCount = 2'
Assert ($r.preferences.ExclusionProcessCount -eq 1)                      'ExclusionProcessCount = 1'
Assert ($r.suitability.suitableForFlaggedLaunch -eq $false)             'exclusions => not suitable'

Write-Host 'Integration - preference cmdlet ABSENT (partial Defender module):'
$r = Invoke-Scenario 'pref-cmdlet-absent'
Assert ($r.avModuleAvailable -eq $true)                                  'status present => module flagged available'
Assert ($null -eq $r.preferences)                                        'preferences null when Get-MpPreference absent'
Assert ($r.errorCategories -contains 'av-module-unavailable')            'records av-module-unavailable'
Assert ($r.suitability.suitableForFlaggedLaunch -eq $false)             'missing preferences => not suitable (conservative)'

Write-Host 'Integration - status query THROWS (transient failure, conservative):'
$r = Invoke-Scenario 'status-query-throws'
Assert ($null -eq $r.computerStatus)                                     'computerStatus null after throw'
Assert ($r.errorCategories -contains 'av-query-failed')                  'records av-query-failed (honest AV category)'
Assert ($r.suitability.suitableForFlaggedLaunch -eq $false)             'query failure => not suitable'

Write-Host 'Integration - status object MISSING fields (abnormal shape, conservative):'
$r = Invoke-Scenario 'status-missing-fields'
Assert ($r.suitability.suitableForFlaggedLaunch -eq $false)             'missing fields => not suitable'
Assert (@($r.errorCategories).Count -ge 1)                              'missing fields => a category is recorded, no crash'
Assert ($r.schema -eq 'av-state-summary/v1')                            'still writes a well-formed summary'

Write-Host 'Integration - BOTH cmdlets absent (Defender module unavailable):'
$r = Invoke-Scenario 'both-cmdlets-absent'
Assert ($r.avModuleAvailable -eq $false)                                 'avModuleAvailable false'
Assert ($r.suitability.protectionMeasured -eq $false)                    'nothing measured'
Assert ($r.suitability.suitableForFlaggedLaunch -eq $false)             'unmeasured => not suitable'
Assert ($r.suitability.reasons -contains 'av-state-unmeasured')          'reason: av-state-unmeasured'

Write-Host ''
Write-Host ("COLLECTOR INTEGRATION RESULT: {0} passed, {1} failed" -f $script:Pass, $script:Fail)
if ($script:Fail -gt 0) { exit 1 } else { exit 0 }
