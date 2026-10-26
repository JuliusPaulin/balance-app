# Build the portable Windows x64 app. Run from any folder with Python 3.11.
param([switch]$SkipInstall, [switch]$TestAI)

$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
if (-not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 is required.' }

function Invoke-Python {
    & python @args
    if ($LASTEXITCODE -ne 0) { throw "Python failed: $args" }
}

if (-not $SkipInstall) {
    Invoke-Python -m pip install -r requirements-windows.txt -r requirements-dev.txt
}
Invoke-Python -c "import struct; assert struct.calcsize('P') == 8, 'Use 64-bit Python'"
Invoke-Python scripts/fetch_runtime_windows.py
# Check the native runtime can load its DLLs before packaging it.
& ./vendor/llama/llama-server.exe --version
if ($LASTEXITCODE -ne 0) { throw 'The Windows AI runtime could not start.' }

Invoke-Python -c "from PIL import Image; Image.open('static/icon.png').save('static/icon.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"
Invoke-Python -m PyInstaller Balance.spec --noconfirm --clean
if ($TestAI) {
    Invoke-Python scripts/smoke_windows.py dist/Balance/Balance.exe --with-ai
} else {
    Invoke-Python scripts/smoke_windows.py dist/Balance/Balance.exe
}
Copy-Item build/windows-smoke/report.json build/windows-smoke/portable-report.json

$archive = Join-Path (Get-Location) 'dist/Balance-Windows-x64.zip'
Compress-Archive -Path dist/Balance -DestinationPath $archive -Force
$hash = (Get-FileHash $archive -Algorithm SHA256).Hash.ToLower()
Set-Content -Path "$archive.sha256" -Value "$hash  Balance-Windows-x64.zip" -Encoding ascii

# Bundle Microsoft's signed prerequisite installers. Setup requests elevation
# only if the system Visual C++ runtime needs installing or updating.
$prerequisites = Join-Path (Get-Location) 'build/windows-prerequisites'
New-Item -ItemType Directory -Force $prerequisites | Out-Null
function Get-MicrosoftInstaller($url, $filename) {
    $target = Join-Path $prerequisites $filename
    Invoke-WebRequest -Uri $url -OutFile $target
    $signature = Get-AuthenticodeSignature $target
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
        throw "Invalid Microsoft signature: $filename"
    }
}
Get-MicrosoftInstaller 'https://aka.ms/vs/17/release/vc_redist.x64.exe' 'VC_redist.x64.exe'
Get-MicrosoftInstaller 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' 'MicrosoftEdgeWebview2Setup.exe'
$vcVersion = [System.Diagnostics.FileVersionInfo]::GetVersionInfo((Join-Path $prerequisites 'VC_redist.x64.exe'))
$compiler = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6/ISCC.exe'
if (-not (Test-Path $compiler)) { throw 'Install Inno Setup 6 to build the Windows installer.' }
$version = (Get-Content VERSION -Raw).Trim()
& $compiler "/DAppVersion=$version" "/DVCMajor=$($vcVersion.FileMajorPart)" "/DVCMinor=$($vcVersion.FileMinorPart)" "/DVCBuild=$($vcVersion.FileBuildPart)" "/DVCRevision=$($vcVersion.FilePrivatePart)" scripts/windows/Balance.iss
if ($LASTEXITCODE -ne 0) { throw 'Windows installer compilation failed.' }
$setup = Join-Path (Get-Location) 'dist/Balance-Windows-x64-Setup.exe'
$installTest = Join-Path ([System.IO.Path]::GetTempPath()) ("Balance installer smoke " + [guid]::NewGuid())
try {
    $installed = Start-Process -FilePath $setup -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=`"$installTest`"") -Wait -PassThru
    if ($installed.ExitCode -ne 0) { throw "Installer smoke failed: $($installed.ExitCode)" }
    Invoke-Python scripts/smoke_windows.py (Join-Path $installTest 'Balance.exe')
    Copy-Item build/windows-smoke/report.json build/windows-smoke/installer-report.json
} finally {
    $uninstaller = Join-Path $installTest 'unins000.exe'
    if (Test-Path $uninstaller) {
        $removed = Start-Process -FilePath $uninstaller -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -Wait -PassThru
        if ($removed.ExitCode -ne 0) { throw "Uninstaller smoke failed: $($removed.ExitCode)" }
    }
}
$setupHash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
Set-Content -Path "$setup.sha256" -Value "$setupHash  Balance-Windows-x64-Setup.exe" -Encoding ascii
Write-Host "Built and checked $archive and $setup"
