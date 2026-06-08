$ErrorActionPreference = "Stop"

$repo = if ($env:AUTO_AI_CR_REPO) { $env:AUTO_AI_CR_REPO } else { "lzwcyd/auto-ai-cr" }
$version = if ($env:AUTO_AI_CR_VERSION) { $env:AUTO_AI_CR_VERSION } else { "latest" }
$installDir = if ($env:AUTO_AI_CR_INSTALL_DIR) { $env:AUTO_AI_CR_INSTALL_DIR } else { Join-Path $HOME ".auto-ai-cr\bin" }
$binDir = if ($env:AUTO_AI_CR_BIN_DIR) { $env:AUTO_AI_CR_BIN_DIR } else { Join-Path $HOME ".local\bin" }

$arch = $env:PROCESSOR_ARCHITECTURE
if ($arch -notin @("AMD64", "x86_64")) {
  throw "auto-ai-cr: unsupported Windows architecture: $arch"
}

$asset = if ($env:AUTO_AI_CR_ASSET) { $env:AUTO_AI_CR_ASSET } else { "auto-ai-cr-windows-x64.zip" }
if ($env:AUTO_AI_CR_ARCHIVE_URL) {
  $url = $env:AUTO_AI_CR_ARCHIVE_URL
} elseif ($version -eq "latest") {
  $url = "https://github.com/$repo/releases/latest/download/$asset"
} else {
  $url = "https://github.com/$repo/releases/download/$version/$asset"
}

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("auto-ai-cr-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force $tmp | Out-Null
try {
  $archive = Join-Path $tmp $asset
  Write-Host "Downloading auto-ai-cr $version ($asset)"
  $downloaded = $false
  for ($attempt = 1; $attempt -le 6; $attempt++) {
    try {
      Invoke-WebRequest -Uri $url -OutFile $archive
      $downloaded = $true
      break
    } catch {
      if ($attempt -eq 6) {
        throw
      }
      Write-Host "Download failed, retrying in 2 seconds ($attempt/6)..."
      Start-Sleep -Seconds 2
    }
  }
  if (-not $downloaded) {
    throw "auto-ai-cr: failed to download $url"
  }

  if (Test-Path $installDir) {
    Remove-Item -Recurse -Force $installDir
  }
  New-Item -ItemType Directory -Force $installDir | Out-Null
  New-Item -ItemType Directory -Force $binDir | Out-Null

  Expand-Archive -Path $archive -DestinationPath $installDir -Force
  $target = Join-Path $binDir "auto-ai-cr.exe"
  $targetTmp = Join-Path $binDir ("auto-ai-cr-" + [System.Guid]::NewGuid().ToString("N") + ".tmp.exe")
  Copy-Item (Join-Path $installDir "auto-ai-cr.exe") $targetTmp -Force
  if (Test-Path $target) {
    Remove-Item -Force $target
  }
  Move-Item -Force $targetTmp $target
  & $target --version | Out-Null

  Write-Host "auto-ai-cr installed: $target"
  if (($env:Path -split ";") -notcontains $binDir) {
    Write-Host ""
    Write-Host "Add this directory to PATH if auto-ai-cr.exe is not found:"
    Write-Host "  $binDir"
  }
  Write-Host ""
  Write-Host "Start the UI:"
  Write-Host "  auto-ai-cr.exe ui --open"
} finally {
  if (Test-Path $tmp) {
    Remove-Item -Recurse -Force $tmp
  }
}
