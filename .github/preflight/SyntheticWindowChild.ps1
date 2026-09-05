<#
.SYNOPSIS
  OWNED CHILD: renders one synthetic WinForms window, verifies it rendered a
  distinctive pattern, and records a clean-or-failed shutdown. Bounded by the
  PARENT process, never by itself.

.DESCRIPTION
  This is the ONLY component that touches the interactive desktop. It is
  intended to run as a short-lived child that the parent
  (Invoke-DesktopCapabilityPreflight.ps1) launches with -WindowStyle Hidden and
  can time out and terminate. Running it directly WILL attempt to show a window,
  so it must only be invoked on a disposable CI runner, never interactively on a
  workstation. The parent enforces that separation; this script does the UI work.

  It:
    - asserts STA (fails closed otherwise),
    - creates a tiny Form whose client area is painted with a KNOWN four-quadrant
      colour pattern (see Get-PreflightPatternSpec),
    - captures ONLY the client region (the verified probe area), not the desktop,
    - verifies the captured pixels match the expected pattern within tolerance,
    - closes and disposes the form, recording whether EACH step succeeded,
    - writes child-result.json (always) and a PNG of the probe area ONLY when the
      synthetic pattern verified; an unverified capture is a finding, never saved.

  It downloads nothing, executes no release artifact, changes no AV/security
  setting, and touches no git/tokens/secrets/network.

.OUTPUTS
  <OutDir>\child-result.json  (always)
  <OutDir>\probe-window.png   (only when the synthetic pattern verified)
  Exit code 0 ONLY on: STA, visible non-zero window, saved screenshot, verified
  pattern, AND clean shutdown. Any other outcome exits non-zero.
#>
[CmdletBinding()]
param(
    [string]$OutDir = (Join-Path (Get-Location) 'preflight-out-child'),
    [int]$VisibleMs = 1500
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'PreflightCommon.ps1')

$result = [ordered]@{
    schema              = 'synthetic-window-child/v1'
    timestampUtc        = (Get-Date).ToUniversalTime().ToString('o')
    apartmentState      = [System.Threading.Thread]::CurrentThread.GetApartmentState().ToString()
    userInteractive     = [System.Environment]::UserInteractive
    windowCreated       = $false
    windowVisible       = $false
    windowHandleNonZero = $false
    windowWidth         = $null
    windowHeight        = $null
    clientWidth         = $null
    clientHeight        = $null
    screenshotSaved     = $false
    screenshotFile      = $null
    patternVerified     = $false
    patternMatched      = 0
    patternTotal        = 0
    closeOk             = $false
    disposeOk           = $false
    shutdownClean       = $false
    errorCategories     = @()
}

function Add-Cat([string]$category) {
    if (Test-PreflightErrorCategory $category) { $result.errorCategories += $category }
    else { $result.errorCategories += 'unexpected' }
}

$form = $null
try {
    if ($result.apartmentState -ne 'STA') { Add-Cat 'not-sta'; throw (New-Object System.Threading.ThreadStateException) }

    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

    try { Add-Type -AssemblyName System.Windows.Forms } catch { Add-Cat 'winforms-load-failed'; throw }
    if (-not (Initialize-PreflightDrawing)) { Add-Cat 'drawing-load-failed'; throw (New-Object System.TypeInitializationException('System.Drawing', $null)) }

    $spec = Get-PreflightPatternSpec

    try {
        $form = New-Object System.Windows.Forms.Form
        $form.Text = 'preflight-probe'
        $form.FormBorderStyle = 'FixedSingle'
        $form.StartPosition = 'Manual'
        $form.Location = New-Object System.Drawing.Point(0, 0)
        $form.ClientSize = New-Object System.Drawing.Size($spec.Width, $spec.Height)
        $form.TopMost = $true
        # Paint the KNOWN pattern into the client area as the background image.
        $bg = New-PatternBitmap -Width $spec.Width -Height $spec.Height -Spec $spec
        $form.BackgroundImage = $bg
        $form.BackgroundImageLayout = [System.Windows.Forms.ImageLayout]::None
    } catch { Add-Cat 'window-create-failed'; throw }

    $form.Show()
    [System.Windows.Forms.Application]::DoEvents()
    $result.windowCreated = $true
    $result.windowHandleNonZero = ($form.Handle -ne [IntPtr]::Zero)
    if (-not $result.windowHandleNonZero) { Add-Cat 'window-handle-zero' }

    # Pump messages for a bounded interval so the client area actually paints.
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.ElapsedMilliseconds -lt $VisibleMs) {
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 50
    }
    $sw.Stop()

    $result.windowVisible = [bool]$form.Visible
    $result.windowWidth   = $form.Bounds.Width
    $result.windowHeight  = $form.Bounds.Height
    $result.clientWidth   = $form.ClientSize.Width
    $result.clientHeight  = $form.ClientSize.Height
    if (-not $result.windowVisible) { Add-Cat 'window-not-visible' }

    # Capture ONLY the client region (the verified probe area) — never the desktop.
    if ($result.windowVisible -and $form.ClientSize.Width -gt 0 -and $form.ClientSize.Height -gt 0) {
        $shotPath = Join-Path $OutDir 'probe-window.png'
        $cw = $form.ClientSize.Width; $ch = $form.ClientSize.Height
        $origin = $form.PointToScreen([System.Drawing.Point]::new(0, 0))
        $bmp = New-Object System.Drawing.Bitmap($cw, $ch)
        $captured = $false
        try {
            try {
                $g = [System.Drawing.Graphics]::FromImage($bmp)
                try {
                    $g.CopyFromScreen($origin, [System.Drawing.Point]::Empty, (New-Object System.Drawing.Size($cw, $ch)))
                } finally { $g.Dispose() }
                $captured = $true
            } catch { Add-Cat 'screenshot-failed' }

            if ($captured) {
                # Verify the KNOWN synthetic pattern on the IN-MEMORY capture BEFORE
                # persisting anything. Only a verified synthetic probe area is written
                # to disk (and hence available for upload); a mismatch is recorded as a
                # JSON finding and the pixels are discarded, never saved or uploaded.
                $check = Test-SyntheticPattern -Bitmap $bmp -Spec $spec
                $result.patternMatched  = $check.Matched
                $result.patternTotal    = $check.Total
                $result.patternVerified = [bool]$check.Verified
                if (Test-ShouldPersistCapture -Captured $captured -PatternVerified $result.patternVerified) {
                    try {
                        $bmp.Save($shotPath, [System.Drawing.Imaging.ImageFormat]::Png)
                        $result.screenshotSaved = $true
                        $result.screenshotFile  = 'probe-window.png'
                    } catch { Add-Cat 'screenshot-failed' }
                } else {
                    Add-Cat 'pattern-mismatch'
                }
            }
        } finally { $bmp.Dispose() }
    } else {
        Add-Cat 'window-not-visible'
    }
}
catch {
    $cat = Get-ErrorCategory $_
    if ($result.errorCategories -notcontains $cat) { Add-Cat $cat }
}
finally {
    if ($null -ne $form) {
        try { $form.Close();   $result.closeOk   = $true } catch { Add-Cat 'shutdown-close-failed' }
        try { $form.Dispose(); $result.disposeOk = $true } catch { Add-Cat 'shutdown-dispose-failed' }
    }
    # Clean shutdown ONLY if both close and dispose succeeded.
    $result.shutdownClean = Resolve-ShutdownClean -CloseOk $result.closeOk -DisposeOk $result.disposeOk

    try {
        New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
        ($result | ConvertTo-Json -Depth 6) | Out-File -FilePath (Join-Path $OutDir 'child-result.json') -Encoding utf8
    } catch { }
}

$ok = $result.windowVisible -and $result.windowHandleNonZero -and $result.screenshotSaved -and $result.patternVerified -and $result.shutdownClean
if ($ok) { exit 0 } else { exit 1 }
