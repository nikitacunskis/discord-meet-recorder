# Registers the native messaging host for Chrome (Windows) and checks dependencies.
# Usage (PowerShell):
#   .\install.ps1 <extension-id from chrome://extensions>
#   .\install.ps1 <extension-id> -DownloadModels   # also fetch whisper models (~1.6 GB)
param(
  [Parameter(Mandatory = $true)]
  [string]$ExtensionId,
  [switch]$DownloadModels
)

$ErrorActionPreference = 'Stop'
$Dir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Dir

if ($ExtensionId -notmatch '^[a-p]{32}$') {
  Write-Warning "'$ExtensionId' does not look like a Chrome extension ID (32 letters a-p). Continuing anyway."
}

function Test-Tool([string]$Name, [string]$Hint) {
  if (Get-Command $Name -ErrorAction SilentlyContinue) {
    Write-Host "found: $Name"
  } else {
    Write-Warning "$Name not found in PATH. $Hint"
  }
}
Test-Tool 'python'      'Install: winget install Python.Python.3.12 (or python.org). If "python" opens the Microsoft Store, disable the stub in Settings > Apps > Advanced app settings > App execution aliases.'
Test-Tool 'ffmpeg'      'Install: winget install Gyan.FFmpeg, then open a NEW terminal.'
Test-Tool 'whisper-cli' 'Download whisper-bin-x64.zip from https://github.com/ggml-org/whisper.cpp/releases, unzip it and add the folder to PATH.'

# Chrome launches the host via a .bat shim (it cannot start .py directly).
$Bat = Join-Path $Dir 'dvt_host.bat'
@"
@echo off
python "%~dp0dvt_host.py" %*
"@ | Set-Content -Path $Bat -Encoding ASCII

# Manifest is written without a UTF-8 BOM: Chrome rejects a manifest that has one.
$Manifest = Join-Path $Dir 'com.dvt.recorder.json'
$Json = @{
  name            = 'com.dvt.recorder'
  description     = 'Discord Voice Transcriber native host'
  path            = $Bat
  type            = 'stdio'
  allowed_origins = @("chrome-extension://$ExtensionId/")
} | ConvertTo-Json
[System.IO.File]::WriteAllText($Manifest, $Json, (New-Object System.Text.UTF8Encoding($false)))

$Key = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.dvt.recorder'
New-Item -Path $Key -Force | Out-Null
Set-ItemProperty -Path $Key -Name '(Default)' -Value $Manifest
Write-Host "Registered: $Key -> $Manifest"

if ($DownloadModels) {
  $Models = Join-Path $Root 'models'
  New-Item -ItemType Directory -Force -Path $Models | Out-Null
  $Files = @(
    @{ Name = 'ggml-large-v3-turbo.bin'
       Url  = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin' },
    @{ Name = 'ggml-silero-v5.1.2.bin'
       Url  = 'https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin' }
  )
  foreach ($F in $Files) {
    $Dest = Join-Path $Models $F.Name
    if (Test-Path $Dest) { Write-Host "already present: $($F.Name)"; continue }
    Write-Host "Downloading $($F.Name) ..."
    curl.exe -L --progress-bar -o $Dest $F.Url
    if ($LASTEXITCODE -ne 0) { Remove-Item -Path $Dest -ErrorAction SilentlyContinue; throw "Download failed: $($F.Url)" }
  }
} else {
  Write-Host 'Models: skipped (they auto-download on first transcription; re-run with -DownloadModels to fetch now).'
}

Write-Host 'Fully restart Chrome so it picks up the host.'
