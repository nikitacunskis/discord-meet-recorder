# Installation — Windows (step-by-step)

All commands below are for **PowerShell** or **cmd** unless noted otherwise.

> **Golden rule:** keep every path ASCII-only. Folders with non-Latin characters
> (Cyrillic, Chinese, umlauts — e.g. `C:\Users\Мария\Downloads`) crash
> whisper.cpp when it loads models. Put everything under something like
> `C:\Tools\`. See [Known issues](#known-issues) for the symptoms.

## Step 1 — Get the project

```powershell
mkdir C:\Tools
git clone https://github.com/nikitacunskis/discord-meet-recorder.git C:\Tools\discord-meet-recorder
cd C:\Tools\discord-meet-recorder
```

No git? On the GitHub page use **Code → Download ZIP**, unzip, and move the
inner `discord-meet-recorder-main` folder to `C:\Tools\discord-meet-recorder`.
(To update later: git users run `git pull`; ZIP users download the ZIP again
and unzip over the folder — recordings and models are not in the ZIP and
survive the overwrite.)

Keep the folder where it is — the native host is registered with an absolute
path, so moving or renaming it later means re-running the installer (step 4).

## Step 2 — Install the dependencies

```powershell
winget install Python.Python.3.12 Gyan.FFmpeg
```

whisper.cpp is not on winget:

1. Download the latest `whisper-bin-x64.zip` from the
   [whisper.cpp releases](https://github.com/ggml-org/whisper.cpp/releases)
   (**Assets** section of the newest release).
2. Unzip it to `C:\Tools\whisper`. Open the folder and find `whisper-cli.exe` —
   it may sit in a `Release\` subfolder. The folder that directly contains
   `whisper-cli.exe` (with the `.dll` files next to it) is the one you need.
3. Add that exact folder to PATH: press Win+R, run

   ```
   rundll32 sysdm.cpl,EditEnvironmentVariables
   ```

   In the **upper** (user) list select `Path` → **Edit** → **New** → paste the
   folder path → **OK** → **OK** (confirm every dialog — Cancel discards the
   change silently).

Now verify. Close the terminal **completely** (in Windows Terminal: all tabs
and windows — a new tab keeps the old environment) and open a new one:

```powershell
python --version
ffmpeg -version
whisper-cli --help
```

All three must print something.

- If `python` opens the Microsoft Store, disable the stub in
  Settings → Apps → Advanced app settings → **App execution aliases**.
- If `whisper-cli` is "not recognized", check what is actually saved:
  `reg query "HKCU\Environment" /v Path` — the whisper folder must appear
  there, spelled exactly like the folder that holds `whisper-cli.exe`.

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
cd C:\Tools\discord-meet-recorder\native
powershell -ExecutionPolicy Bypass -File .\install.ps1 <extension-id>
```

Do not double-click the file or run it as a bare `install.ps1` command — Windows
opens `.ps1` files in Notepad instead of running them, and the default execution
policy blocks scripts (that is what `-ExecutionPolicy Bypass` is for).

The script also checks that `python`, `ffmpeg` and `whisper-cli` are reachable and
prints a hint for anything missing. No admin rights are needed — it creates
`dvt_host.bat` and a per-user registry entry.

Then **fully restart Chrome**: right-click the Chrome icon in the system tray →
**Exit** (closing the windows is not enough — Chrome keeps running in the
background with the old environment). When in doubt, reboot.

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

## Known issues

Symptoms seen on real Windows setups, with causes and fixes. When something
fails, `recordings\transcribe.log` and `recordings\dvt_host.log` contain the
full tracebacks — start there.

### whisper-cli exits with code 3221226505 (0xC0000409) right after start

The model file cannot be loaded. Two known causes:

- **Non-ASCII characters in the path.** If the project (or the whisper folder)
  lives under a path with Cyrillic or other non-Latin characters, whisper.cpp
  aborts on model load. Move everything to an ASCII-only path (`C:\Tools\...`),
  fix the PATH entry, re-run `install.ps1` (the registration stores the old
  absolute path) and fully restart Chrome.
- **A corrupt model file** left over from an interrupted or concurrent
  download (versions before 2026-08-24 could corrupt it when the live and
  post-call pipelines downloaded simultaneously). Delete and re-download:

  ```cmd
  del models\*.bin models\*.part
  ```

  The next transcription downloads fresh copies. The correct size of
  `ggml-large-v3-turbo.bin` is 1 624 555 275 bytes (`dir models` to compare).

### "whisper-cli is not recognized" in a terminal

`whisper-cli.exe` is not on PATH:

- The PATH entry must be the folder that **directly contains**
  `whisper-cli.exe` — after unzipping, the binaries often sit in a `Release\`
  subfolder, not in the folder you unzipped to.
- The environment-variables dialog saves only when every window is closed with
  **OK**. Verify what was actually stored: `reg query "HKCU\Environment" /v Path`.
- Already-open terminals never see a new PATH. In Windows Terminal a new *tab*
  is not enough — close **all** terminal windows and open a fresh one.
- If you later move or rename the whisper folder (or any parent folder), the
  PATH entry silently breaks — update it to the new location.

### Terminal works, but transcription from the panel still fails

Chrome (and the native host it spawns) keeps the environment it was started
with. After any PATH change, exit Chrome via the tray icon (**Exit**, not just
closing windows) or reboot.

### "Native host: not installed" in the panel

- `install.ps1` was not run, was run with a wrong extension ID, or the project
  folder was moved/renamed after registration — re-run step 4.
- Registration lives in the registry key
  `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.dvt.recorder`, pointing
  to `native\com.dvt.recorder.json`, which launches `native\dvt_host.bat`.
- Chrome must be fully restarted after registration (tray → Exit).

### Running install.ps1 opens Notepad instead of running

Windows opens `.ps1` files in an editor by design. Run it through PowerShell
with the exact command from step 4 (`powershell -ExecutionPolicy Bypass -File ...`).

### `python` opens the Microsoft Store

Windows ships a fake `python` alias. Disable it in Settings → Apps →
Advanced app settings → **App execution aliases**, or install Python via
winget (step 2) and reopen the terminal.

### Garbled characters (Ð..., â€¦) in logs or transcripts

Fixed in versions after 2026-08-24 (all file and process I/O is UTF-8 now).
If you still see it, update the project (step 1, "To update later").
