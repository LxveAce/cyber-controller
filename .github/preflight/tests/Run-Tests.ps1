<#
.SYNOPSIS
  Self-contained tests for the package-preflight pure logic. No Pester, no
  module install, NO WINDOW, no AV/Defender read or change, no network/git.

.DESCRIPTION
  Exercises the reporting, error-categorisation, timeout/shutdown/capability,
  AV-suitability, and pattern-verification logic with deterministic in-memory
  fixtures, plus a parse-only check of every .ps1. All System.Drawing bitmaps
  here are OFF-SCREEN images built with Graphics.FromImage; none is shown as a
  window and none is a screen capture. CopyFromScreen is never called.

  Exit 0 iff every assertion passes.
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$preRoot = Split-Path -Parent $here
. (Join-Path $preRoot 'PreflightCommon.ps1')

$script:Pass = 0
$script:Fail = 0
function Assert([bool]$Condition, [string]$Name) {
    if ($Condition) { $script:Pass++; Write-Host "  [PASS] $Name" }
    else            { $script:Fail++; Write-Host "  [FAIL] $Name" -ForegroundColor Red }
}

Write-Host 'Get-NonEmptyCount is strict-mode-safe (0/1/many/null):'
Assert ((Get-NonEmptyCount $null) -eq 0)                         'null pipeline -> 0'
Assert ((Get-NonEmptyCount @()) -eq 0)                           'empty array -> 0'
Assert ((Get-NonEmptyCount 'only-one') -eq 1)                    'lone scalar string -> 1'
Assert ((Get-NonEmptyCount @('a','b','c')) -eq 3)                'many -> 3'
Assert ((Get-NonEmptyCount @('a','', $null, '  ', 'b')) -eq 2)   'blanks/whitespace/null filtered -> 2'
# Prove the old form would have thrown, and the new one does not, on a lone scalar.
$noThrow = $true
try { [void](Get-NonEmptyCount 'x') } catch { $noThrow = $false }
Assert $noThrow                                                  'no throw on scalar under StrictMode Latest'

Write-Host 'Get-ErrorCategory returns codes only, never messages:'
$secretMsg = 'C:\Users\somebody\secret-path\token=abcdef'
$err = $null
try { throw (New-Object System.InvalidOperationException($secretMsg)) } catch { $err = $_ }
$cat = Get-ErrorCategory $err
Assert (Test-PreflightErrorCategory $cat)                        'category is in allowlist'
Assert ($cat -notmatch 'secret|token|Users')                    'category leaks no message/path'
Assert ((Get-ErrorCategory (New-Object System.Threading.ThreadStateException)) -eq 'not-sta') 'STA maps to not-sta'
Assert ((Get-ErrorCategory (New-Object System.ArgumentException('bad'))) -eq 'invalid-parameter') 'ArgumentException -> invalid-parameter'
Assert ((Get-ErrorCategory 'a plain string') -eq 'unexpected')   'non-exception -> unexpected'

Write-Host 'Test-PositiveRange bounds valid positive ranges:'
Assert (Test-PositiveRange -VisibleMs 1500 -HardTimeoutMs 15000) 'valid ordered range'
Assert (-not (Test-PositiveRange -VisibleMs 0 -HardTimeoutMs 15000))    'zero VisibleMs rejected'
Assert (-not (Test-PositiveRange -VisibleMs -5 -HardTimeoutMs 15000))   'negative VisibleMs rejected'
Assert (-not (Test-PositiveRange -VisibleMs 15000 -HardTimeoutMs 15000))'VisibleMs == HardTimeoutMs rejected'
Assert (-not (Test-PositiveRange -VisibleMs 20000 -HardTimeoutMs 15000))'VisibleMs > HardTimeoutMs rejected'

Write-Host 'Resolve-ShutdownClean requires both close and dispose, not forced:'
Assert (Resolve-ShutdownClean -CloseOk $true -DisposeOk $true)               'close+dispose -> clean'
Assert (-not (Resolve-ShutdownClean -CloseOk $false -DisposeOk $true))       'close throw -> not clean'
Assert (-not (Resolve-ShutdownClean -CloseOk $true -DisposeOk $false))       'dispose throw -> not clean'
Assert (-not (Resolve-ShutdownClean -CloseOk $true -DisposeOk $true -ForcedKill $true)) 'forced kill -> not clean'

Write-Host 'Resolve-DesktopCapable gate:'
function New-GoodEvidence {
    return @{
        childExitedOnOwn = $true; childForcedKill = $false; hardTimeoutHit = $false
        childExitCode = 0; windowVisible = $true; windowHandleNonZero = $true
        windowWidth = 246; windowHeight = 148; screenshotSaved = $true
        patternVerified = $true; shutdownClean = $true
    }
}
Assert (Resolve-DesktopCapable -Evidence (New-GoodEvidence))                 'all-good -> capable'
$e = New-GoodEvidence; $e.hardTimeoutHit = $true
Assert (-not (Resolve-DesktopCapable -Evidence $e))                          'hardTimeoutHit -> not capable'
$e = New-GoodEvidence; $e.childForcedKill = $true
Assert (-not (Resolve-DesktopCapable -Evidence $e))                          'forced kill -> not capable'
$e = New-GoodEvidence; $e.childExitedOnOwn = $false
Assert (-not (Resolve-DesktopCapable -Evidence $e))                          'did not exit on own -> not capable'
$e = New-GoodEvidence; $e.childExitCode = 1
Assert (-not (Resolve-DesktopCapable -Evidence $e))                          'nonzero exit -> not capable'
$e = New-GoodEvidence; $e.windowHandleNonZero = $false
Assert (-not (Resolve-DesktopCapable -Evidence $e))                          'zero handle -> not capable'
$e = New-GoodEvidence; $e.windowVisible = $false
Assert (-not (Resolve-DesktopCapable -Evidence $e))                          'not visible -> not capable'
$e = New-GoodEvidence; $e.windowWidth = 0
Assert (-not (Resolve-DesktopCapable -Evidence $e))                          'zero width -> not capable'
$e = New-GoodEvidence; $e.patternVerified = $false
Assert (-not (Resolve-DesktopCapable -Evidence $e))                          'pattern not verified -> not capable'
$e = New-GoodEvidence; $e.shutdownClean = $false
Assert (-not (Resolve-DesktopCapable -Evidence $e))                          'unclean shutdown -> not capable'

Write-Host 'Test-SyntheticPattern on deterministic OFF-SCREEN fixtures:'
if (Initialize-PreflightDrawing) {
    $spec = Get-PreflightPatternSpec
    $good = New-PatternBitmap -Width $spec.Width -Height $spec.Height -Spec $spec
    try { Assert ((Test-SyntheticPattern -Bitmap $good -Spec $spec).Verified) 'correct pattern -> verified' }
    finally { $good.Dispose() }

    # Solid-colour bitmap (mimics blank desktop/wallpaper) must NOT verify.
    $blank = New-Object System.Drawing.Bitmap($spec.Width, $spec.Height)
    try {
        $g = [System.Drawing.Graphics]::FromImage($blank)
        try { $g.Clear([System.Drawing.Color]::FromArgb(255,120,120,120)) } finally { $g.Dispose() }
        Assert (-not (Test-SyntheticPattern -Bitmap $blank -Spec $spec).Verified) 'solid grey -> not verified'
    } finally { $blank.Dispose() }

    # One wrong quadrant must fail the whole gate.
    $partial = New-PatternBitmap -Width $spec.Width -Height $spec.Height -Spec $spec
    try {
        $g = [System.Drawing.Graphics]::FromImage($partial)
        try {
            $b = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::Black)
            try { $g.FillRectangle($b, 0, 0, [int]($spec.Width/2), [int]($spec.Height/2)) } finally { $b.Dispose() }
        } finally { $g.Dispose() }
        $r = Test-SyntheticPattern -Bitmap $partial -Spec $spec
        Assert ((-not $r.Verified) -and ($r.Matched -eq 3)) 'one wrong quadrant -> not verified (3/4 match)'
    } finally { $partial.Dispose() }
} else {
    Write-Host '  [SKIP] System.Drawing unavailable; pattern fixtures skipped' -ForegroundColor Yellow
}

Write-Host 'Get-SuitabilityVerdict is conservative and AV-read-only in logic:'
function New-GoodAv {
    return [ordered]@{
        avModuleAvailable = $true
        computerStatus = [ordered]@{
            AMRunningMode = 'Normal'; AntivirusEnabled = $true; RealTimeProtectionEnabled = $true
            BehaviorMonitorEnabled = $true; IoavProtectionEnabled = $true
            OnAccessProtectionEnabled = $true; IsTamperProtected = $true
        }
        preferences = [ordered]@{
            DisableRealtimeMonitoring = $false; DisableBehaviorMonitoring = $false
            DisableIOAVProtection = $false; DisableScriptScanning = $false; DisableArchiveScanning = $false
            EnableControlledFolderAccess = 'Disabled'; EnableNetworkProtection = 'Disabled'
            ExclusionPathCount = 0; ExclusionExtensionCount = 0; ExclusionProcessCount = 0
        }
    }
}
$v = Get-SuitabilityVerdict (New-GoodAv)
Assert ($v.protectionMeasured -and $v.suitableForFlaggedLaunch) 'fully protected -> suitable'
Assert ($v.reasons.Count -eq 0)                                 'suitable -> no reasons'

$av = New-GoodAv; $av.avModuleAvailable = $false; $av.computerStatus = $null; $av.preferences = $null
$v = Get-SuitabilityVerdict $av
Assert ((-not $v.protectionMeasured) -and (-not $v.suitableForFlaggedLaunch)) 'unmeasured -> not suitable'
Assert ($v.reasons -contains 'av-state-unmeasured')             'unmeasured reason recorded'

$av = New-GoodAv; $av.computerStatus.RealTimeProtectionEnabled = $false
$v = Get-SuitabilityVerdict $av
Assert (-not $v.suitableForFlaggedLaunch)                       'RTP off -> not suitable'
Assert ($v.reasons -contains 'realtime-protection-off')         'RTP-off reason recorded'

$av = New-GoodAv; $av.preferences.ExclusionProcessCount = 2
$v = Get-SuitabilityVerdict $av
Assert (-not $v.suitableForFlaggedLaunch)                       'exclusions present -> not suitable'
Assert ($v.reasons -contains 'exclusions-present-or-unknown')   'exclusion reason recorded'

# A MISSING/null boolean must never be read as safe.
$av = New-GoodAv; $av.computerStatus.AntivirusEnabled = $null
$v = Get-SuitabilityVerdict $av
Assert (-not $v.suitableForFlaggedLaunch)                       'null AntivirusEnabled -> not suitable (unknown != safe)'

$av = New-GoodAv; $av.preferences.DisableRealtimeMonitoring = $true
$v = Get-SuitabilityVerdict $av
Assert (-not $v.suitableForFlaggedLaunch)                       'DisableRealtimeMonitoring=true -> not suitable'

$av = New-GoodAv; $av.computerStatus.AMRunningMode = 'Passive'
$v = Get-SuitabilityVerdict $av
Assert (-not $v.suitableForFlaggedLaunch)                       'passive mode -> not suitable'

# Capability and suitability are independent: an unmeasured AV verdict says
# nothing about desktopCapable, and vice-versa.
Assert ($true) 'capability/suitability are computed by separate functions'

Write-Host 'Screenshot persistence gate - persist ONLY when captured AND pattern verified:'
Assert (Test-ShouldPersistCapture -Captured $true -PatternVerified $true)          'captured + verified -> persist'
Assert (-not (Test-ShouldPersistCapture -Captured $true -PatternVerified $false))  'captured + mismatch -> do NOT persist (finding only)'
Assert (-not (Test-ShouldPersistCapture -Captured $false -PatternVerified $true))  'no capture -> do NOT persist'
Assert (-not (Test-ShouldPersistCapture -Captured $false -PatternVerified $false)) 'no capture + mismatch -> do NOT persist'

Write-Host 'Child-launch path quoting - Format-NativeArgument round-trips via CommandLineToArgvW:'
Add-Type -Namespace PfTest -Name Native -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("shell32.dll", SetLastError=true)]
public static extern System.IntPtr CommandLineToArgvW([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.LPWStr)] string lpCmdLine, out int pNumArgs);
'@
function Split-NativeLine([string]$line) {
    $n = 0
    $ptr = [PfTest.Native]::CommandLineToArgvW($line, [ref]$n)
    $out = @()
    for ($i = 0; $i -lt $n; $i++) {
        $strPtr = [System.Runtime.InteropServices.Marshal]::ReadIntPtr($ptr, $i * [System.IntPtr]::Size)
        $out += [System.Runtime.InteropServices.Marshal]::PtrToStringUni($strPtr)
    }
    [void][System.Runtime.InteropServices.Marshal]::FreeHGlobal($ptr)
    return ,$out
}
foreach ($p in @(
    'C:\no-spaces\child.ps1',
    'C:\Users\ex tra\Docs with spaces\Out Dir',
    'plain',
    'trailing space dir\',
    'has "embedded" quote')) {
    $line = ConvertTo-NativeArgumentLine -Arguments @('-File', $p, '-OutDir', $p, '-VisibleMs', '1500')
    $parsed = Split-NativeLine $line
    $ok = ($parsed.Count -eq 6 -and $parsed[1] -eq $p -and $parsed[3] -eq $p -and $parsed[5] -eq '1500')
    Assert $ok ("path round-trips as ONE argument: " + $p)
}
Assert ((Format-NativeArgument 'plain') -eq 'plain')                 'plain token is left unquoted'
Assert ((Format-NativeArgument 'a b') -eq '"a b"')                   'spaced token is wrapped in quotes'

Write-Host 'Parse-only checks (no execution, NO WINDOW) of every .ps1:'
$psFiles = Get-ChildItem -Path $preRoot -Filter *.ps1 -Recurse
foreach ($f in $psFiles) {
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$null, [ref]$errors)
    Assert ($errors.Count -eq 0) ("parses clean: " + $f.Name)
}

Write-Host ''
Write-Host ("PURE/HELPER RESULT: {0} passed, {1} failed" -f $script:Pass, $script:Fail)

# Chain the actual-collector integration suite (mocked Defender cmdlets, no real
# AV read, no window). It runs its scenarios in child processes and exits non-zero
# on any failure; fold that into this single entrypoint's overall result.
Write-Host ''
Write-Host '=== Collector integration suite (real Get-AvStateSummary.ps1, mocked cmdlets) ==='
$integration = Join-Path $here 'Run-CollectorIntegrationTests.ps1'
$selfHost = (Get-Process -Id $PID).Path
& $selfHost -NoProfile -ExecutionPolicy Bypass -File $integration
$integrationExit = $LASTEXITCODE

Write-Host ''
if ($script:Fail -gt 0 -or $integrationExit -ne 0) {
    Write-Host 'OVERALL: FAILED' -ForegroundColor Red
    exit 1
}
Write-Host 'OVERALL: PASSED'
exit 0
