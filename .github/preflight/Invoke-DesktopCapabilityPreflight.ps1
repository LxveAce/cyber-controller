<#
.SYNOPSIS
  PARENT orchestrator: launches the synthetic-window child as an OWNED, externally
  bounded process and reports desktop capability. Renders no window itself.

.DESCRIPTION
  The interactive UI portion runs in SyntheticWindowChild.ps1. This parent:
    - validates VisibleMs / HardTimeoutMs are positive and ordered,
    - launches the child via Start-Process -WindowStyle Hidden -PassThru,
    - waits at most HardTimeoutMs for the OWNED child to exit,
    - if it does not exit in time, terminates ONLY that owned child (by its
      process handle) and records a FORCED, unclean shutdown,
    - reads the child's result JSON and computes desktopCapable from the full
      evidence (handle, dimensions, screenshot, verified pattern, clean shutdown,
      timeout, child exit code) — never from the timeout alone.

  Externally bounding the whole child is what lets the parent cap Show(),
  DoEvents(), CopyFromScreen(), Close(), and Dispose() together — none of which
  an in-process sleep-loop timeout could interrupt. A hit hard timeout is a LAST
  RESORT and is recorded as a failure, never as evidence of a clean child exit.

  It downloads nothing, executes no release artifact, changes no AV/security
  setting, and touches no git/tokens/secrets/network.

.OUTPUTS
  <OutDir>\desktop-capability-evidence.json and (via the child) probe-window.png
  and child/child-result.json. Prints the evidence JSON. Exit 0 only when
  desktopCapable is true; otherwise non-zero.
#>
[CmdletBinding()]
param(
    [string]$OutDir = (Join-Path (Get-Location) 'preflight-out'),
    [int]$VisibleMs = 1500,
    [int]$HardTimeoutMs = 15000,
    # Host to run the STA child. Windows PowerShell is STA-by-default; pwsh needs -STA.
    [string]$ChildHost = 'powershell.exe'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'PreflightCommon.ps1')

$evidence = @{
    schema              = 'desktop-capability-preflight/v2'
    timestampUtc        = (Get-Date).ToUniversalTime().ToString('o')
    imageVersion        = $env:ImageVersion
    imageOs             = $env:ImageOS
    osVersion           = [System.Environment]::OSVersion.Version.ToString()
    visibleMs           = $VisibleMs
    hardTimeoutMs       = $HardTimeoutMs
    parametersValid     = $false
    childLaunched       = $false
    childExitedOnOwn    = $false
    childForcedKill     = $false
    hardTimeoutHit      = $false
    childExitCode       = $null
    childReported       = $false
    windowVisible       = $false
    windowHandleNonZero = $false
    windowWidth         = $null
    windowHeight        = $null
    screenshotSaved     = $false
    patternVerified     = $false
    shutdownClean       = $false
    desktopCapable      = $false
    limits              = @(
        'Synthetic window only; exercises no packaged application.',
        'Screenshot is the child window client area only; no full-desktop capture.',
        'A hit hard timeout is a forced-kill failure, never proof of a clean exit.',
        'Absence of a visible/verified window is a negative finding, not a fix.'
    )
    errorCategories     = @()
}

function Add-Cat([string]$category) {
    if (Test-PreflightErrorCategory $category) { $evidence.errorCategories += $category }
    else { $evidence.errorCategories += 'unexpected' }
}

$proc = $null
try {
    if (-not (Test-PositiveRange -VisibleMs $VisibleMs -HardTimeoutMs $HardTimeoutMs)) {
        Add-Cat 'invalid-parameter'
        throw (New-Object System.ArgumentException('VisibleMs/HardTimeoutMs out of range'))
    }
    $evidence.parametersValid = $true

    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
    $childDir    = Join-Path $OutDir 'child'
    $childScript = Join-Path $PSScriptRoot 'SyntheticWindowChild.ps1'

    # Quote every token: childScript/childDir may sit under a path with spaces
    # (runner work dir, "C:\Program Files", ...). An unquoted path would split
    # into multiple ArgumentList tokens and the child would get a truncated
    # -File/-OutDir value. ConvertTo-NativeArgumentLine builds a correct line.
    $childArgs = @(
        '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass',
        '-File', $childScript,
        '-OutDir', $childDir,
        '-VisibleMs', "$VisibleMs"
    )
    $childArgLine = ConvertTo-NativeArgumentLine -Arguments $childArgs

    try {
        $proc = Start-Process -FilePath $ChildHost -ArgumentList $childArgLine -WindowStyle Hidden -PassThru
        $evidence.childLaunched = $true
    } catch { Add-Cat 'child-launch-failed'; throw }

    # Externally bound the ENTIRE child (Show/DoEvents/CopyFromScreen/Close/Dispose).
    $exited = $proc.WaitForExit($HardTimeoutMs)
    if ($exited) {
        $evidence.childExitedOnOwn = $true
        $evidence.childExitCode = $proc.ExitCode
    } else {
        # Last resort: terminate ONLY this owned child (its PID/handle).
        $evidence.hardTimeoutHit = $true
        $evidence.childForcedKill = $true
        Add-Cat 'child-timeout-forced-kill'
        try { $proc.Kill() } catch { }
        try { [void]$proc.WaitForExit(5000) } catch { }
    }

    # Merge child-reported evidence when present.
    $childResultPath = Join-Path $childDir 'child-result.json'
    if (Test-Path -LiteralPath $childResultPath) {
        try {
            $cr = Get-Content -LiteralPath $childResultPath -Raw | ConvertFrom-Json
            $evidence.childReported       = $true
            $evidence.windowVisible       = [bool]$cr.windowVisible
            $evidence.windowHandleNonZero = [bool]$cr.windowHandleNonZero
            $evidence.windowWidth         = $cr.windowWidth
            $evidence.windowHeight        = $cr.windowHeight
            $evidence.screenshotSaved     = [bool]$cr.screenshotSaved
            $evidence.patternVerified     = [bool]$cr.patternVerified
            $evidence.shutdownClean       = [bool]$cr.shutdownClean
            foreach ($c in @($cr.errorCategories)) {
                if ($c -and ($evidence.errorCategories -notcontains $c)) { Add-Cat "$c" }
            }
        } catch { Add-Cat 'child-missing-result' }
    } else {
        Add-Cat 'child-missing-result'
    }

    $evidence.desktopCapable = Resolve-DesktopCapable -Evidence $evidence
}
catch {
    $cat = Get-ErrorCategory $_
    if ($evidence.errorCategories -notcontains $cat) { Add-Cat $cat }
    $evidence.desktopCapable = $false
}
finally {
    try {
        New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
        $json = $evidence | ConvertTo-Json -Depth 6
        $json | Out-File -FilePath (Join-Path $OutDir 'desktop-capability-evidence.json') -Encoding utf8
        Write-Output $json
    } catch {
        Write-Output '{"schema":"desktop-capability-preflight/v2","desktopCapable":false,"errorCategories":["evidence-write-failed"]}'
    }
}

if ($evidence.desktopCapable) { exit 0 } else { exit 1 }
