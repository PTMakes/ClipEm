<#
Fetches OBS, a portable Python, and the shared ClipEm resources into
$InstallDir\App - the same layout Setup_Windows.bat produces from a local
checkout. Run by ClipEmSetup.nsi's Install section; does nothing else (no
OBS configuration, wizard or shortcuts - App\Shared\Installer\setup.py,
launched from the installer's Finish page, handles all of that).

Trust chain (nothing unverified is ever extracted or run):
  1. The embeddable Python's URL and sha256 are baked into the installer at
     build time (bootstrap-pins.json, next to this script in $BundleDir),
     as are Tcl/Tk (tcltk.zip, from a pinned tcltk.msi) and the VC++ runtime
     OBS needs (vcruntime.zip, from a pinned VC_redist.x64.exe).
  2. That Python runs ed25519_verify.py (also bundled) to check
     install-manifest.json's release signature against the bundled public
     key (signing_public_key.txt). Windows PowerShell 5.1 has no Ed25519.
  3. Only then are the manifest's sha256 values trusted for OBS and the
     shared zip.

Everything is downloaded, verified and extracted into a temp staging folder
first and copied into $InstallDir only once all of it succeeded, so a
network failure or checksum mismatch never leaves a half-populated App
folder behind. Exits non-zero on any failure so the .nsi can abort cleanly.

Progress: the .nsi runs this with no console and polls -StatusFile (an
append-only UTF-16LE text file), one line per update:
  @<0-1000>   overall progress, shown on the installer's progress bar
  ~<text>     transient status (e.g. bytes downloaded), shown above the bar
  <text>      a log line, added to the installer's details list
#>

param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [Parameter(Mandatory = $true)][string]$BundleDir,
    [string]$StatusFile = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$ManifestUrl = "https://github.com/PTMakes/ClipEm/releases/latest/download/install-manifest.json"
$DownloadAttempts = 3
$StatusEncoding = New-Object System.Text.UnicodeEncoding($false, $false)  # UTF-16LE, no BOM - read with FileReadUTF16LE

# The current step's slice of the overall progress (out of 1000). The
# Start-Step calls below size each slice roughly by how long it takes -
# OBS and ffmpeg dominate.
$script:StepStart = 0
$script:StepEnd = 0
$script:LastProgress = -1
$script:LastTransientAt = [DateTime]::MinValue

Add-Type -AssemblyName System.IO.Compression.FileSystem

function Write-Status($Line) {
    Write-Host $Line
    # The .nsi reads lines into a 1024-character buffer.
    if ($Line.Length -gt 900) { $Line = $Line.Substring(0, 900) }
    if (-not $StatusFile) { return }
    # Progress reporting must never be what fails an install.
    try {
        $stream = New-Object System.IO.FileStream($StatusFile, [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)
        try {
            $bytes = $StatusEncoding.GetBytes("$Line`r`n")
            $stream.Write($bytes, 0, $bytes.Length)
        }
        finally { $stream.Dispose() }
    }
    catch { }
}

function Start-Step($Start, $End, $Message) {
    $script:StepStart = $Start
    $script:StepEnd = $End
    Write-Status $Message
    Set-StepProgress 0
}

# $Fraction (0-1) of the current step is done.
function Set-StepProgress($Fraction) {
    if ($Fraction -gt 1) { $Fraction = 1 }
    $value = [int]($script:StepStart + ($script:StepEnd - $script:StepStart) * $Fraction)
    if ($value -gt $script:LastProgress) {
        $script:LastProgress = $value
        Write-Status "@$value"
    }
}

# Throttled so a fast download doesn't flood the status file.
function Write-Transient($Text) {
    $now = [DateTime]::UtcNow
    if (($now - $script:LastTransientAt).TotalMilliseconds -lt 250) { return }
    $script:LastTransientAt = $now
    Write-Status "~$Text"
}

function Format-MB($Bytes) {
    return ("{0:N1} MB" -f ($Bytes / 1MB))
}

function Invoke-Download($Url, $DestPath, $Label) {
    $request = [System.Net.HttpWebRequest]::Create($Url)
    $request.Timeout = 60000
    $request.ReadWriteTimeout = 300000
    $request.UserAgent = "ClipEmSetup"
    $response = $request.GetResponse()
    try {
        $total = $response.ContentLength  # -1 if the server didn't send one
        $source = $response.GetResponseStream()
        $dest = [System.IO.File]::Create($DestPath)
        try {
            $buffer = New-Object byte[] 262144
            $done = 0
            while (($read = $source.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $dest.Write($buffer, 0, $read)
                $done += $read
                if ($total -gt 0) {
                    Set-StepProgress ($done / $total)
                    Write-Transient "$($Label): $(Format-MB $done) of $(Format-MB $total)"
                }
                else {
                    Write-Transient "$($Label): $(Format-MB $done)"
                }
            }
        }
        finally {
            $dest.Dispose()
            $source.Dispose()
        }
    }
    finally { $response.Close() }
}

# Per-entry rather than Expand-Archive, which has no progress (and is much
# slower on Windows PowerShell 5.1). Refuses entries that would land
# outside $DestDir, as Expand-Archive does. Entries ending in any of
# $SkipSuffixes aren't extracted.
function Expand-ZipWithProgress($ZipPath, $DestDir, $Label, [string[]]$SkipSuffixes = @()) {
    New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
    $root = [System.IO.Path]::GetFullPath($DestDir).TrimEnd('\') + '\'
    $zip = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $total = 0
        foreach ($entry in $zip.Entries) { $total += $entry.Length }
        $done = 0
        foreach ($entry in $zip.Entries) {
            $target = [System.IO.Path]::GetFullPath((Join-Path $root $entry.FullName))
            if (-not $target.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Refusing to extract $($entry.FullName) outside $DestDir"
            }
            if ($entry.FullName.EndsWith('/') -or $entry.FullName.EndsWith('\')) {
                [System.IO.Directory]::CreateDirectory($target) | Out-Null
                continue
            }
            $done += $entry.Length
            $skip = $false
            foreach ($suffix in $SkipSuffixes) {
                if ($entry.FullName.EndsWith($suffix, [StringComparison]::OrdinalIgnoreCase)) { $skip = $true }
            }
            if ($skip) { continue }
            [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($target)) | Out-Null
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
            if ($total -gt 0) { Set-StepProgress ($done / $total) }
            Write-Transient "Extracting $($Label): $($entry.FullName)"
        }
    }
    finally { $zip.Dispose() }
    Set-StepProgress 1
}

$AppDir = Join-Path $InstallDir "App"
$TempDir = Join-Path $env:TEMP ("ClipEmSetup_" + [guid]::NewGuid().ToString("N"))
$StageAppDir = Join-Path $TempDir "App"
$StageWindowsDir = Join-Path $StageAppDir "Windows"
$StagePythonDir = Join-Path $StageWindowsDir "Python"

# Some Windows/.NET Framework defaults still refuse TLS 1.2 unless asked for
# explicitly, which would otherwise fail every request below.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Get-Download($Url, $DestPath, $Label) {
    if (-not $Url.StartsWith("https://")) {
        throw "Refusing non-HTTPS download URL: $Url"
    }
    for ($attempt = 1; $attempt -le $DownloadAttempts; $attempt++) {
        try {
            Write-Status "Downloading $Url"
            Invoke-Download $Url $DestPath $Label
            return
        }
        catch {
            if ($attempt -eq $DownloadAttempts) { throw }
            Write-Status "  download failed ($($_.Exception.Message)) - retrying..."
            Start-Sleep -Seconds (5 * $attempt)
        }
    }
}

function Get-VerifiedDownload($Entry, $DestPath, $Label) {
    $expected = ([string]$Entry.sha256).ToLowerInvariant()
    Get-Download $Entry.url $DestPath $Label
    Write-Status "~Verifying $Label"
    $actual = (Get-FileHash -Algorithm SHA256 -Path $DestPath).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        throw (
            "Checksum mismatch for $($Entry.url)`n" +
            "  expected: $expected`n" +
            "  actual:   $actual`n" +
            "The download may be corrupted or tampered with - aborting."
        )
    }
    Write-Status "  sha256 OK"
}

# Exit code for "files in the install folder are in use": the .nsi asks the
# user to close what's running and runs this again.
$ExitInUse = 3
$script:InUse = $false

# Processes running an executable from inside $AppDir - OBS (obs64.exe), the
# ClipEm tray widget / settings dialog (pythonw.exe) or a clip re-export
# (ffmpeg.exe). Windows won't let files they have loaded be overwritten.
function Get-InstallProcesses {
    $root = [System.IO.Path]::GetFullPath($AppDir).TrimEnd('\') + '\'
    @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $path = $null
        try { $path = $_.Path } catch { }
        $path -and $path.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)
    })
}

function Assert-InstallNotInUse {
    $running = Get-InstallProcesses
    if ($running.Count -gt 0) {
        $names = ($running | ForEach-Object { "$($_.ProcessName).exe" } | Sort-Object -Unique) -join ", "
        $script:InUse = $true
        throw "OBS or ClipEm is still running from $AppDir ($names)."
    }
}

# Overwriting a file can briefly fail while antivirus scans it; retry a few
# times before giving up.
function Copy-FileWithRetry($Source, $Target) {
    for ($attempt = 1; ; $attempt++) {
        try {
            [System.IO.File]::Copy($Source, $Target, $true)
            return
        }
        catch {
            if ($attempt -ge 5) {
                if ((Get-InstallProcesses).Count -gt 0) { $script:InUse = $true }
                throw "Couldn't replace $Target - it's in use by another program. ($($_.Exception.Message))"
            }
            Start-Sleep -Milliseconds (400 * $attempt)
        }
    }
}

$UserFiles = @(
    "Shared\OBS-Config\",
    "Shared\run_config.json",
    "Shared\update_config.json",
    "Shared\pending_obs_settings.json",
    "Shared\Scripts\replay_buffer_save_sound.wav"
)

function Test-UserFile($Relative) {
    foreach ($entry in $UserFiles) {
        if ($entry.EndsWith('\')) {
            if ($Relative.StartsWith($entry, [StringComparison]::OrdinalIgnoreCase)) { return $true }
        } elseif ($Relative.Equals($entry, [StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

$failed = $false
try {
    # Before downloading anything: nothing gets replaced while it's running.
    Assert-InstallNotInUse
    New-Item -ItemType Directory -Force -Path $StagePythonDir | Out-Null
    $pins = Get-Content (Join-Path $BundleDir "bootstrap-pins.json") -Raw | ConvertFrom-Json

    Start-Step 0 40 "Fetching portable Python"
    $pythonZip = Join-Path $TempDir "python.zip"
    Get-VerifiedDownload $pins.python $pythonZip "Python"
    # The embeddable zip's contents land directly in App\Windows\Python.
    Start-Step 40 52 "Extracting Python"
    Expand-ZipWithProgress $pythonZip $StagePythonDir "Python"
    $pythonExe = Join-Path $StagePythonDir "python.exe"

    # Tcl/Tk + tkinter for the GUI setup wizard (the embeddable Python has
    # none) - bundled in the installer, pre-extracted from python.org's
    # pinned tcltk.msi by build.sh. setup.py adds Lib to the ._pth.
    Start-Step 52 58 "Adding Tcl/Tk for the setup wizard"
    Expand-ZipWithProgress (Join-Path $BundleDir "tcltk.zip") $StagePythonDir "Tcl/Tk"

    Start-Step 58 65 "Fetching and verifying the release manifest"
    $manifestPath = Join-Path $TempDir "install-manifest.json"
    $signaturePath = Join-Path $TempDir "install-manifest.json.sig"
    Get-Download $ManifestUrl $manifestPath "Release manifest"
    Get-Download "$ManifestUrl.sig" $signaturePath "Release signature"
    & $pythonExe (Join-Path $BundleDir "ed25519_verify.py") (Join-Path $BundleDir "signing_public_key.txt") $manifestPath $signaturePath
    if ($LASTEXITCODE -ne 0) {
        throw "The install manifest's release signature is INVALID - refusing to download anything it lists."
    }
    $windowsManifest = (Get-Content $manifestPath -Raw | ConvertFrom-Json).windows
    Set-StepProgress 1

    # OBS - the official Windows release zip's bin/data/obs-plugins land
    # directly under App\Windows, matching setup.py's setup_windows().
    Start-Step 65 480 "Fetching portable OBS"
    $obsZip = Join-Path $TempDir "obs.zip"
    Get-VerifiedDownload $windowsManifest.obs $obsZip "OBS"
    Start-Step 480 620 "Extracting OBS"
    # Skips OBS's ~76 MB of debug symbols (obs_install.DEBUG_FILE_SUFFIXES).
    Expand-ZipWithProgress $obsZip $StageWindowsDir "OBS" @(".pdb")
    # OBS's portable zip doesn't include the Visual C++ runtime (its own
    # installer runs vc_redist, which needs admin) - without it obs64.exe
    # fails with "MSVCP140.dll was not found". Bundled in the installer
    # (build.sh) and placed app-local, next to obs64.exe.
    Expand-ZipWithProgress (Join-Path $BundleDir "vcruntime.zip") (Join-Path $StageWindowsDir "bin\64bit") "VC++ runtime"

    # Shared resources - the zip contains a top-level Shared\ folder.
    Start-Step 620 680 "Fetching ClipEm files"
    $sharedZip = Join-Path $TempDir "shared.zip"
    Get-VerifiedDownload $windowsManifest.shared $sharedZip "ClipEm files"
    Start-Step 680 700 "Extracting ClipEm files"
    Expand-ZipWithProgress $sharedZip $StageAppDir "ClipEm files"

    # ClipEm's own static ffmpeg build (clip re-export) - its zip contains a
    # top-level ffmpeg\ folder, so this extracts to App\Windows\ffmpeg.
    Start-Step 700 870 "Fetching ffmpeg"
    $ffmpegZip = Join-Path $TempDir "ffmpeg.zip"
    Get-VerifiedDownload $windowsManifest.ffmpeg $ffmpegZip "ffmpeg"
    Start-Step 870 930 "Extracting ffmpeg"
    Expand-ZipWithProgress $ffmpegZip $StageWindowsDir "ffmpeg"

    Start-Step 930 1000 "All artifacts downloaded and verified - installing to $AppDir"
    # Again, in case OBS or ClipEm was started during the download.
    Assert-InstallNotInUse
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    # Program folders are replaced whole, not copied over: files an older
    # version had but this one doesn't would otherwise stay behind - e.g. a
    # Python 3.10 install's python310._pth next to 3.12's (Setup then edited
    # the wrong one and pip failed), or plugins an older OBS shipped. The
    # tray widget's packages live in Python\ and are reinstalled by Setup.
    foreach ($dir in @("Windows\Python", "Windows\bin", "Windows\data", "Windows\obs-plugins", "Windows\ffmpeg")) {
        $old = Join-Path $AppDir $dir
        if ((Test-Path -LiteralPath (Join-Path $StageAppDir $dir)) -and (Test-Path -LiteralPath $old)) {
            Write-Transient "Removing the previous $dir"
            Remove-Item -LiteralPath $old -Recurse -Force
        }
    }
    $stageRoot = [System.IO.Path]::GetFullPath($StageAppDir).TrimEnd('\') + '\'
    # The user's own settings, which the shared zip only ships defaults for:
    # never overwritten when installing over an existing install (the same
    # files ClipEm's updater preserves - updater.PRESERVED_RELATIVE_PATHS).
    $files = @(Get-ChildItem -Path $StageAppDir -Recurse -File -Force | Where-Object {
        $relative = $_.FullName.Substring($stageRoot.Length)
        -not ((Test-UserFile $relative) -and (Test-Path -LiteralPath (Join-Path $AppDir $relative)))
    })
    $totalBytes = ($files | Measure-Object -Property Length -Sum).Sum
    $copiedBytes = 0
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($stageRoot.Length)
        $target = Join-Path $AppDir $relative
        [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($target)) | Out-Null
        Copy-FileWithRetry $file.FullName $target
        $copiedBytes += $file.Length
        if ($totalBytes -gt 0) { Set-StepProgress ($copiedBytes / $totalBytes) }
        Write-Transient "Installing $relative"
    }
    Set-StepProgress 1
    # Which OBS this is (from its file name, e.g. OBS-Studio-32.2.2-Windows-x64.zip):
    # ClipEm's updater falls back on this if obs64.exe's own version can't
    # be read - see obs_install.INSTALLED_VERSIONS_PATH.
    if ($windowsManifest.obs.url -match '(\d+\.\d+\.\d+)') {
        $versionsPath = Join-Path $AppDir "installed_versions.json"
        $versions = @{}
        if (Test-Path -LiteralPath $versionsPath) {
            try { (Get-Content -LiteralPath $versionsPath -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $versions[$_.Name] = $_.Value } } catch { }
        }
        $versions["obs"] = $Matches[1]
        $versions | ConvertTo-Json | Set-Content -LiteralPath $versionsPath -Encoding ASCII
    }
    Write-Status "Done."
}
catch {
    # No console to print to under the installer - put the reason in its log.
    Write-Status "ERROR: $($_.Exception.Message)"
    $failed = $true
}
finally {
    Remove-Item -Recurse -Force $TempDir -ErrorAction SilentlyContinue
}
if ($failed -and $script:InUse) { exit $ExitInUse }
if ($failed) { exit 1 }
exit 0
