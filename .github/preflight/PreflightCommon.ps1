<#
.SYNOPSIS
  Shared, side-effect-free helpers for the package-preflight probe.

.DESCRIPTION
  This file contains ONLY pure functions and constants. Dot-sourcing it:
    - shows no window,
    - reads no AV / Defender state,
    - touches no network, git, tokens, secrets, registry, or services,
    - starts no process.

  It exists so the reporting, error-categorisation, timeout-decision,
  pattern-verification, and AV-suitability logic can be unit-tested with
  deterministic in-memory fixtures WITHOUT rendering a window or changing
  anything on the host. System.Drawing bitmaps built here are off-screen
  images, never a visible window and never a screen capture.
#>

Set-StrictMode -Version Latest

# System.Drawing is needed for the off-screen pattern fixtures/verification.
# It is loaded lazily and guarded; absence is a finding, not a crash.
$script:DrawingLoaded = $false
function Initialize-PreflightDrawing {
    if ($script:DrawingLoaded) { return $true }
    try {
        Add-Type -AssemblyName System.Drawing -ErrorAction Stop
        $script:DrawingLoaded = $true
    } catch {
        $script:DrawingLoaded = $false
    }
    return $script:DrawingLoaded
}

# --- Error categories -------------------------------------------------------
# We record CATEGORY CODES only, never raw exception messages, paths, machine
# names, users, or any value that could leak an identity or filesystem layout.
$script:PreflightErrorCategories = @(
    'not-sta'
    'winforms-load-failed'
    'drawing-load-failed'
    'screen-query-failed'
    'window-create-failed'
    'window-not-visible'
    'window-handle-zero'
    'screenshot-failed'
    'pattern-mismatch'
    'shutdown-close-failed'
    'shutdown-dispose-failed'
    'evidence-write-failed'
    'child-launch-failed'
    'child-timeout-forced-kill'
    'child-missing-result'
    'invalid-parameter'
    'av-module-unavailable'
    'av-query-failed'
    'unexpected'
)

function Test-PreflightErrorCategory {
    param([Parameter(Mandatory)][string]$Category)
    return ($script:PreflightErrorCategories -contains $Category)
}

# Map an ErrorRecord/Exception to a safe category WITHOUT retaining its message.
function Get-ErrorCategory {
    param([Parameter(Mandatory)]$ErrorObject)

    $ex = $null
    if ($ErrorObject -is [System.Management.Automation.ErrorRecord]) {
        $ex = $ErrorObject.Exception
    } elseif ($ErrorObject -is [System.Exception]) {
        $ex = $ErrorObject
    }

    if ($null -eq $ex) { return 'unexpected' }

    $typeName = $ex.GetType().Name
    switch -Regex ($typeName) {
        'ThreadStateException'            { return 'not-sta' }
        'FileNotFoundException'           { return 'winforms-load-failed' }
        'TypeInitializationException'     { return 'drawing-load-failed' }
        'InvalidOperationException'       { return 'window-create-failed' }
        'ExternalException'               { return 'screenshot-failed' }
        'ArgumentException'               { return 'invalid-parameter' }
        default                           { return 'unexpected' }
    }
}

# --- Native argument quoting (child-launch path safety) --------------------
# Start-Process -ArgumentList @(...) joins the elements with single spaces and
# does NOT quote them. An unquoted path containing a space (e.g. a runner work
# dir or a temp path under "C:\Program Files") would therefore split into two
# arguments and the child would receive a truncated -File / -OutDir value.
# Format-NativeArgument quotes ONE token per the Windows CommandLineToArgvW
# convention (double backslashes before a quote, escape embedded quotes), so
# the joined line round-trips exactly.
function Format-NativeArgument {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Argument)

    if ($Argument.Length -gt 0 -and $Argument -notmatch '[\s"]') {
        return $Argument
    }

    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    $backslashes = 0
    foreach ($ch in $Argument.ToCharArray()) {
        if ($ch -eq '\') {
            $backslashes++
            continue
        }
        if ($ch -eq '"') {
            # Escape every backslash run that precedes a quote, then the quote.
            [void]$sb.Append('\' * ($backslashes * 2 + 1))
            [void]$sb.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$sb.Append('\' * $backslashes)
            $backslashes = 0
        }
        [void]$sb.Append($ch)
    }
    # Backslashes immediately before the closing quote must be doubled.
    [void]$sb.Append('\' * ($backslashes * 2))
    [void]$sb.Append('"')
    return $sb.ToString()
}

# Join an argument vector into a single, correctly quoted native command line.
function ConvertTo-NativeArgumentLine {
    param([Parameter(Mandatory)][string[]]$Arguments)
    return (($Arguments | ForEach-Object { Format-NativeArgument $_ }) -join ' ')
}

# --- Exclusion counting -----------------------------------------------------
# Strict-mode-safe count of non-empty pipeline values. Wrapping in @() before
# .Count is what makes 0 / 1-scalar / many / $null all behave, instead of
# ($x | Where-Object {..}).Count throwing on null or a lone scalar.
function Get-NonEmptyCount {
    param($Values)
    $filtered = @($Values | Where-Object { $_ -ne $null -and "$_".Trim().Length -gt 0 })
    return $filtered.Count
}

# --- AV suitability verdict -------------------------------------------------
# Desktop CAPABILITY and protection SUITABILITY are deliberately separate.
# This returns a CONSERVATIVE verdict about whether the measured Defender
# state would be suitable for a LATER flagged-package launch. It never changes
# AV, and a missing/null boolean is treated as UNKNOWN (not safe).
# Input: the ordered hashtable produced by Get-AvStateSummary ($summary).
function Get-SuitabilityVerdict {
    param([Parameter(Mandatory)]$AvSummary)

    $reasons = New-Object System.Collections.Generic.List[string]

    function Get-Field($obj, [string]$name) {
        if ($null -eq $obj) { return $null }
        if ($obj -is [System.Collections.IDictionary]) {
            if ($obj.Contains($name)) { return $obj[$name] }
            return $null
        }
        $prop = $obj.PSObject.Properties[$name]
        if ($null -ne $prop) { return $prop.Value }
        return $null
    }

    $moduleAvailable = [bool](Get-Field $AvSummary 'avModuleAvailable')
    if (-not $moduleAvailable) {
        $reasons.Add('av-state-unmeasured')
        return [ordered]@{
            schema                   = 'av-suitability/v1'
            protectionMeasured       = $false
            suitableForFlaggedLaunch = $false
            reasons                  = @($reasons.ToArray())
        }
    }

    $status = Get-Field $AvSummary 'computerStatus'
    $pref   = Get-Field $AvSummary 'preferences'

    # A null (unmeasured) boolean must NOT be read as "protected".
    function Test-EnabledTrue($v, [string]$reasonIfNot, $reasonList) {
        if ($v -is [bool] -and $v) { return $true }
        $reasonList.Add($reasonIfNot)
        return $false
    }
    function Test-DisabledFalse($v, [string]$reasonIfDisabled, $reasonList) {
        # Disable* flags: safe only when explicitly $false. null => unknown => unsafe.
        if ($v -is [bool] -and (-not $v)) { return $true }
        $reasonList.Add($reasonIfDisabled)
        return $false
    }

    [void](Test-EnabledTrue (Get-Field $status 'AntivirusEnabled')          'antivirus-not-enabled'      $reasons)
    [void](Test-EnabledTrue (Get-Field $status 'RealTimeProtectionEnabled') 'realtime-protection-off'    $reasons)
    [void](Test-EnabledTrue (Get-Field $status 'BehaviorMonitorEnabled')    'behavior-monitor-off'       $reasons)
    [void](Test-EnabledTrue (Get-Field $status 'OnAccessProtectionEnabled') 'on-access-protection-off'   $reasons)

    $mode = Get-Field $status 'AMRunningMode'
    if ($mode -isnot [string] -or $mode -ne 'Normal') {
        $reasons.Add('running-mode-not-normal')
    }

    [void](Test-DisabledFalse (Get-Field $pref 'DisableRealtimeMonitoring') 'realtime-monitoring-disabled' $reasons)
    [void](Test-DisabledFalse (Get-Field $pref 'DisableBehaviorMonitoring') 'behavior-monitoring-disabled' $reasons)
    [void](Test-DisabledFalse (Get-Field $pref 'DisableIOAVProtection')     'ioav-protection-disabled'     $reasons)
    [void](Test-DisabledFalse (Get-Field $pref 'DisableScriptScanning')     'script-scanning-disabled'     $reasons)

    foreach ($countField in 'ExclusionPathCount','ExclusionExtensionCount','ExclusionProcessCount') {
        $c = Get-Field $pref $countField
        if ($c -isnot [int] -or $c -gt 0) {
            # >0 exclusions => excluded; non-int/null => unknown. Both unsafe.
            $reasons.Add('exclusions-present-or-unknown')
            break
        }
    }

    $suitable = ($reasons.Count -eq 0)
    return [ordered]@{
        schema                   = 'av-suitability/v1'
        protectionMeasured       = $true
        suitableForFlaggedLaunch = $suitable
        reasons                  = @($reasons.ToArray())
    }
}

# --- Synthetic pattern ------------------------------------------------------
# A distinctive four-quadrant colour pattern. An unrelated desktop, wallpaper,
# or window will not reproduce these four interior colours at these positions,
# so verifying them is real evidence the SYNTHETIC window actually rendered.
function Get-PreflightPatternSpec {
    return [pscustomobject]@{
        Width     = 240
        Height    = 120
        Tolerance = 40
        Regions   = @(
            [pscustomobject]@{ Name = 'q-tl'; Fx = 0.25; Fy = 0.25; R = 222; G = 28;  B = 33  }
            [pscustomobject]@{ Name = 'q-tr'; Fx = 0.75; Fy = 0.25; R = 30;  G = 196; B = 60  }
            [pscustomobject]@{ Name = 'q-bl'; Fx = 0.25; Fy = 0.75; R = 40;  G = 66;  B = 214 }
            [pscustomobject]@{ Name = 'q-br'; Fx = 0.75; Fy = 0.75; R = 230; G = 208; B = 42  }
        )
    }
}

# Build the off-screen pattern bitmap (no window). Caller owns Dispose().
function New-PatternBitmap {
    param(
        [int]$Width  = 240,
        [int]$Height = 120,
        $Spec = (Get-PreflightPatternSpec)
    )
    if (-not (Initialize-PreflightDrawing)) { throw (New-Object System.TypeInitializationException('System.Drawing', $null)) }

    $bmp = New-Object System.Drawing.Bitmap($Width, $Height)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    try {
        $hw = [int]($Width / 2)
        $hh = [int]($Height / 2)
        $quad = @{
            'q-tl' = @(0,     0,      $hw,           $hh)
            'q-tr' = @($hw,   0,      ($Width-$hw),  $hh)
            'q-bl' = @(0,     $hh,    $hw,           ($Height-$hh))
            'q-br' = @($hw,   $hh,    ($Width-$hw),  ($Height-$hh))
        }
        foreach ($r in $Spec.Regions) {
            $rect = $quad[$r.Name]
            $color = [System.Drawing.Color]::FromArgb(255, $r.R, $r.G, $r.B)
            $brush = New-Object System.Drawing.SolidBrush($color)
            try {
                $g.FillRectangle($brush, $rect[0], $rect[1], $rect[2], $rect[3])
            } finally { $brush.Dispose() }
        }
    } finally { $g.Dispose() }
    return $bmp
}

# Verify a captured bitmap against the known spec. Returns a detail object;
# .Verified is true only when EVERY region matches within tolerance.
function Test-SyntheticPattern {
    param(
        [Parameter(Mandatory)]$Bitmap,
        $Spec = (Get-PreflightPatternSpec)
    )
    $matched = 0
    $total = $Spec.Regions.Count
    $w = $Bitmap.Width
    $h = $Bitmap.Height

    foreach ($r in $Spec.Regions) {
        $cx = [Math]::Min([Math]::Max([int]($r.Fx * $w), 0), $w - 1)
        $cy = [Math]::Min([Math]::Max([int]($r.Fy * $h), 0), $h - 1)
        # Average a small neighbourhood to tolerate anti-aliasing.
        $sumR = 0; $sumG = 0; $sumB = 0; $n = 0
        for ($dx = -1; $dx -le 1; $dx++) {
            for ($dy = -1; $dy -le 1; $dy++) {
                $px = [Math]::Min([Math]::Max($cx + $dx, 0), $w - 1)
                $py = [Math]::Min([Math]::Max($cy + $dy, 0), $h - 1)
                $c = $Bitmap.GetPixel($px, $py)
                $sumR += $c.R; $sumG += $c.G; $sumB += $c.B; $n++
            }
        }
        $avgR = [int]($sumR / $n); $avgG = [int]($sumG / $n); $avgB = [int]($sumB / $n)
        if (([Math]::Abs($avgR - $r.R) -le $Spec.Tolerance) -and
            ([Math]::Abs($avgG - $r.G) -le $Spec.Tolerance) -and
            ([Math]::Abs($avgB - $r.B) -le $Spec.Tolerance)) {
            $matched++
        }
    }

    return [pscustomobject]@{
        Verified = ($matched -eq $total -and $total -gt 0)
        Matched  = $matched
        Total    = $total
    }
}

# --- Screenshot persistence gate -------------------------------------------
# A captured probe image is written to disk (and therefore made available for
# upload) ONLY when the KNOWN synthetic pattern verified. A failed match is a
# finding, never a reason to persist/upload unrelated screen pixels. Keeping the
# decision here makes it unit-testable without rendering a window.
function Test-ShouldPersistCapture {
    param([bool]$Captured, [bool]$PatternVerified)
    return ($Captured -and $PatternVerified)
}

# --- Timeout / shutdown / capability decisions ------------------------------
function Test-PositiveRange {
    # VisibleMs and HardTimeoutMs must be positive and VisibleMs strictly less
    # than HardTimeoutMs, so the bounded window has room before the hard cap.
    param([int]$VisibleMs, [int]$HardTimeoutMs)
    if ($VisibleMs -le 0)            { return $false }
    if ($HardTimeoutMs -le 0)        { return $false }
    if ($VisibleMs -ge $HardTimeoutMs) { return $false }
    return $true
}

# Clean shutdown requires BOTH close and dispose to have succeeded. A thrown
# Close/Dispose, or a forced kill, is NOT a clean shutdown.
function Resolve-ShutdownClean {
    param([bool]$CloseOk, [bool]$DisposeOk, [bool]$ForcedKill = $false)
    if ($ForcedKill) { return $false }
    return ($CloseOk -and $DisposeOk)
}

# desktopCapable gate. Considers hardTimeoutHit, windowHandleNonZero, window
# dimensions, screenshot, verified pattern, clean shutdown, child exit, and
# whether the owned child had to be force-killed.
function Resolve-DesktopCapable {
    param([Parameter(Mandatory)][hashtable]$Evidence)

    function G([string]$k) {
        if ($Evidence.Contains($k)) { return $Evidence[$k] }
        return $null
    }

    $checks = @(
        (G 'childExitedOnOwn') -eq $true
        (G 'childForcedKill')  -ne $true
        (G 'hardTimeoutHit')   -ne $true
        (G 'childExitCode')    -eq 0
        (G 'windowVisible')    -eq $true
        (G 'windowHandleNonZero') -eq $true
        (([int](G 'windowWidth'))  -gt 0)
        (([int](G 'windowHeight')) -gt 0)
        (G 'screenshotSaved')  -eq $true
        (G 'patternVerified')  -eq $true
        (G 'shutdownClean')    -eq $true
    )
    return (-not ($checks -contains $false))
}
