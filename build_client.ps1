$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

python -m pip install -r requirements-client.txt
python -m PyInstaller --clean --noconfirm Civ6RelayClient.spec

$output = Join-Path $root "dist\Civ6RelayClient"
Copy-Item (Join-Path $root "README.md") $output -Force
Copy-Item (Join-Path $root "THIRD_PARTY_NOTICES.md") $output -Force

$archive = Join-Path $root "dist\Civ6RelayClient-windows-x64.zip"
Compress-Archive -Path $output -DestinationPath $archive -Force

Write-Host "Build complete: $output"
Write-Host "Archive: $archive"
