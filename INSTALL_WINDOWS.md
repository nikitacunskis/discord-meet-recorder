# Installation — Windows (step-by-step)

All commands below are for **PowerShell**.

## Step 1 — Clone the repository

```powershell
git clone https://github.com/nikitacunskis/discord-meet-recorder.git
cd discord-meet-recorder
```

No git? On the GitHub page use **Code → Download ZIP** and unzip it.

Keep the folder where you want it to live — the native host is registered with an
absolute path to it, so moving it later means re-running the installer (step 4).

## Step 2 — Install the dependencies

```powershell
winget install Python.Python.3.12 Gyan.FFmpeg
```

whisper.cpp is not on winget: download the latest `whisper-bin-x64.zip` from the
[whisper.cpp releases](https://github.com/ggml-org/whisper.cpp/releases), unzip it
and add the folder containing `whisper-cli.exe` to PATH
(Settings → System → About → Advanced system settings → Environment Variables).

Verify in a **new** terminal — all three must print something:

```powershell
python --version
ffmpeg -version
whisper-cli --help
```

If `python` opens the Microsoft Store instead, disable the stub in
Settings → Apps → Advanced app settings → **App execution aliases**.

## Step 3 — Load the Chrome extension

1. Chrome → `chrome://extensions`
2. Enable **Developer mode** (top-right corner)
3. **Load unpacked** → pick the `extension/` folder of this repo
4. A one-time notice page opens — please read it (it is the short version of the
   [Disclaimer](README.md#disclaimer)) and click **I understand**.
5. Copy the extension **ID** shown on the card — the next step needs it.

## Step 4 — Register the native host

The native host gives you auto-save into per-recording folders, automatic
transcription and the editor backend. Without it the panel still works, but files fall
back to Downloads and you transcribe manually.

```powershell
cd native
powershell -ExecutionPolicy Bypass -File .\install.ps1 <extension-id>
```

Do not double-click the file or run it as a bare `install.ps1` command — Windows
opens `.ps1` files in Notepad instead of running them, and the default execution
policy blocks scripts (that is what `-ExecutionPolicy Bypass` is for).

The script also checks that `python`, `ffmpeg` and `whisper-cli` are reachable and
prints a hint for anything missing. No admin rights are needed — it creates
`dvt_host.bat` and a per-user registry entry.

Then **fully restart Chrome** (quit completely, not just close the window). After the
restart the "Native host" warning line in the extension panel must disappear.

## Step 5 — Whisper models

Nothing to do by default: on the first transcription the models are downloaded
automatically into `models/`. To fetch them right away instead, run the installer
with the extra switch:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 <extension-id> -DownloadModels
```

Or place the two files into `models/` yourself:

| File | Source project | Size |
|---|---|---|
| `ggml-large-v3-turbo.bin` | [whisper.cpp models](https://huggingface.co/ggerganov/whisper.cpp) | ~1.6 GB |
| `ggml-silero-v5.1.2.bin` | [Silero VAD (whisper.cpp build)](https://huggingface.co/ggml-org/whisper-vad) | ~2 MB |

```powershell
mkdir models -Force
curl.exe -L -o models\ggml-large-v3-turbo.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
curl.exe -L -o models\ggml-silero-v5.1.2.bin https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin
```

If another project on your machine already downloaded the same `ggml-*.bin` files,
copying them into `models/` works too — the files are identical.

## Step 6 — Claude CLI (optional, for the AI report)

Install per [claude.com/claude-code](https://claude.com/claude-code)
(`npm install -g @anthropic-ai/claude-code`), then pick your instance in the
panel settings. Without it everything works except the generated summary /
action-items report.

## Usage

Same as on any platform — see [Usage in the README](README.md#usage). The manual
fallback transcription command on Windows is:

```powershell
python tools\transcribe.py "$env:USERPROFILE\Downloads\discord-call-<date>.webm" --report
```

## Troubleshooting the native host

The panel shows a "Native host: not installed" warning when Chrome cannot start the
host. Check the registration:

- Registry key `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.dvt.recorder`
  must point to `native\com.dvt.recorder.json`, which launches `native\dvt_host.bat`.
- The `allowed_origins` entry inside that JSON must contain your actual extension ID.
- Chrome must be fully restarted after registration.
- The host inherits a minimal environment — `python`, `ffmpeg` and `whisper-cli`
  must be on the PATH that Chrome sees (log out/in or reboot after changing PATH
  if in doubt).
