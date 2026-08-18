# Registers the native messaging host for Chrome (Windows).
# Usage (PowerShell):  .\install.ps1 <extension-id from chrome://extensions>
param(
  [Parameter(Mandatory = $true)]
  [string]$ExtensionId
)

$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Chrome on Windows can only launch .exe/.bat hosts, so wrap the Python script.
$Bat = Join-Path $Dir "dvt_host.bat"
@"
@echo off
python "%~dp0dvt_host.py" %*
"@ | Set-Content -Path $Bat -Encoding ASCII

# Host manifest
$Manifest = Join-Path $Dir "com.dvt.recorder.json"
@{
  name            = "com.dvt.recorder"
  description     = "Discord Voice Transcriber native host"
  path            = $Bat
  type            = "stdio"
  allowed_origins = @("chrome-extension://$ExtensionId/")
} | ConvertTo-Json | Set-Content -Path $Manifest -Encoding UTF8

# Registry pointer (per-user, no admin needed)
$Key = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.dvt.recorder"
New-Item -Path $Key -Force | Out-Null
Set-ItemProperty -Path $Key -Name "(Default)" -Value $Manifest

Write-Host "Registered: $Key -> $Manifest"
Write-Host "Fully restart Chrome so it picks up the host."
