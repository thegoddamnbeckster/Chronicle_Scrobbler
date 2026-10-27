# Chronicle Scrobbler - Build Script
# Reads version from addon.xml automatically. Outputs ZIP to C:\Temp\.
# Usage: powershell -ExecutionPolicy Bypass -File build.ps1
#
# Uses .NET's System.IO.Compression.ZipArchive with forward-slash entry paths on
# purpose — PowerShell's own Compress-Archive uses backslash path separators
# internally on Windows, which breaks Kodi's addon installer on non-Windows
# platforms (confirmed lesson from SIMKL_Scrobbler's own build.ps1).

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$addonXml    = Join-Path $projectRoot "addon.xml"

# Read version from addon.xml
$xml     = [xml](Get-Content $addonXml -Encoding UTF8)
$version = $xml.addon.version
if (-not $version) {
    Write-Error "Could not read version from addon.xml"
    exit 1
}

$buildTemp   = Join-Path $projectRoot "build_temp"
$addonFolder = "service.chronicle.scrobbler"
$sourcePath  = Join-Path $buildTemp $addonFolder
$outputDir   = "C:\Temp"
$outputZip   = Join-Path $outputDir "service.chronicle.scrobbler-$version.zip"

Write-Host "======================================"
Write-Host " Chronicle Scrobbler v$version - Build"
Write-Host "======================================"

# Step 1: Prepare build_temp
Write-Host ""
Write-Host "--- Preparing build_temp ---"

if (Test-Path $buildTemp) {
    Remove-Item $buildTemp -Recurse -Force
    Write-Host "  Cleaned old build_temp"
}
New-Item -ItemType Directory -Path $sourcePath -Force | Out-Null

# Copy root addon files
$rootFiles = @("addon.xml", "default.py", "service.py", "icon.png", "LICENSE")
foreach ($f in $rootFiles) {
    $src = Join-Path $projectRoot $f
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $sourcePath $f)
        Write-Host "  Copied: $f"
    } else {
        Write-Warning "  Missing expected file: $f"
    }
}

# Copy lib/ and resources/ (excluding __pycache__)
Copy-Item "$projectRoot\lib" "$sourcePath\lib" -Recurse -Force
Copy-Item "$projectRoot\resources" "$sourcePath\resources" -Recurse -Force
Get-ChildItem -Path $sourcePath -Directory -Recurse -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force
Write-Host "  Copied: lib/, resources/ (cleaned __pycache__)"

# Modules shared by every Chronicle Kodi add-on live in ONE place, Chronicle_Scraper\shared (this add-on takes only the ones it uses);
# copied into lib\ here so the zip is self-contained.
$sharedDir = Join-Path $projectRoot "..\Chronicle_Scraper\shared"
if (-not (Test-Path $sharedDir)) {
    Write-Error "Shared modules not found at $sharedDir -- check out Chronicle_Scraper next to this repository."
    exit 1
}
foreach ($name in @("logger.py", "redact.py", "http_core.py", "connection_health.py", "device_name.py", "device_auth.py", "qr_dialog.py", "connection_ui.py")) {
    $src = Join-Path $sharedDir $name
    if (-not (Test-Path $src)) { Write-Error "Missing shared module: $name"; exit 1 }
    Copy-Item $src (Join-Path $sourcePath "lib\$name") -Force
    Write-Host "  Shared module: $name"
}
Write-Host "  build_temp ready."

# Step 2: Create ZIP
Write-Host ""
Write-Host "--- Building ZIP ---"

if (-not (Test-Path $outputDir)) {
    New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
}
if (Test-Path $outputZip) {
    Remove-Item $outputZip -Force
    Write-Host "  Removed old ZIP"
}

$zip   = [System.IO.Compression.ZipFile]::Open($outputZip, 'Create')
$files = Get-ChildItem -Path $sourcePath -Recurse -File
$count = 0

foreach ($file in $files) {
    $relativePath = $file.FullName.Substring($buildTemp.Length + 1)
    $entryName    = $relativePath.Replace("\", "/")
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $file.FullName, $entryName, 'Optimal') | Out-Null
    Write-Host "  + $entryName"
    $count++
}

$zip.Dispose()

$zipInfo = Get-Item $outputZip
$zipSize = [math]::Round($zipInfo.Length / 1024, 2)

Write-Host ""
Write-Host "======================================"
Write-Host " BUILD COMPLETE"
Write-Host "======================================"
Write-Host "Version: $version"
Write-Host "Output:  $outputZip"
Write-Host "Files:   $count"
Write-Host "Size:    $zipSize KB"

# Cleanup build_temp
Remove-Item $buildTemp -Recurse -Force
Write-Host "  Cleaned build_temp"
