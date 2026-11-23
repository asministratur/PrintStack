param(
    [string]$Version = "1.0.0",
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Push-Location $Root

try {
    Write-Host "Building Printstack v$Version..."
    python -m PyInstaller --clean --noconfirm packaging\printstack.spec

    $BundleDir = Join-Path $Root "dist\Printstack"
    $PortableZip = Join-Path $Root "dist\Printstack-$Version-win64-portable.zip"
    if (Test-Path $PortableZip) {
        Remove-Item $PortableZip -Force
    }
    Compress-Archive -Path "$BundleDir\*" -DestinationPath $PortableZip -CompressionLevel Optimal
    Write-Host "Portable archive created: $PortableZip"

    if (-not $SkipInstaller) {
        $iscc = Get-Command iscc -ErrorAction SilentlyContinue
        if (-not $iscc) {
            $defaultIscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
            if (Test-Path $defaultIscc) {
                $iscc = Get-Item $defaultIscc
            }
        }
        if ($iscc) {
            & $iscc.Source "/DAppVersion=$Version" "packaging\installer.iss"
            Write-Host "Installer built: dist\Printstack-Setup-$Version.exe"
        } else {
            Write-Warning "Inno Setup (ISCC.exe) not found; skipped installer build."
        }
    }
} finally {
    Pop-Location
}
