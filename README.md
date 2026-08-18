# Discord Call Recorder + Speaker Timeline

Chrome extension (MV3) that, during a Discord **web** call:

1. records the tab audio mixed with your microphone → `discord-call-<date>.webm` (opus),
2. reads from the DOM **who is speaking at every moment** (the green indicator = inline `box-shadow: var(--status-speaking)` style) → `*.speakers.json` and `*.speakers.srt`,
3. hands everything to a local native host that transcribes the call with whisper.cpp, attributes every line to a speaker, stores the result in SQLite and (optionally) generates a Claude report with a summary and action items.

Everything runs locally — no audio ever leaves the machine (the only optional network step is the `claude -p` report, which uses your own Claude subscription).

## Requirements

| Component | Why | Version |
|---|---|---|
| Google Chrome | the extension + native messaging | any recent |
| Python | native host + transcription pipeline | 3.9+ (stdlib only, no pip packages) |
| ffmpeg | audio conversion / remux | any recent |
| whisper.cpp (`whisper-cli`) | local speech-to-text | any recent |
| Claude Code CLI (`claude`) | optional `--report` step (summary + action items) | optional |

## Installation

### 1. Load the extension (all platforms)

1. Chrome → `chrome://extensions`
2. Enable **Developer mode** (top-right corner)
3. **Load unpacked** → pick the `extension/` folder
4. Copy the extension **ID** shown on the card — the next step needs it.

### 2. Install the tools + register the native host

The native host gives you auto-save into per-recording folders, automatic
transcription and the editor backend. Without it the panel still works, but files fall
back to Downloads and you transcribe manually.

**macOS**

```bash
brew install ffmpeg whisper-cpp
./native/install.sh <extension-id>
# fully restart Chrome (Cmd+Q) afterwards
```

**Linux (Debian/Ubuntu shown; Chrome or Chromium)**

```bash
sudo apt install ffmpeg cmake build-essential python3

# whisper.cpp is not packaged in most distros — build once from source:
git clone https://github.com/ggml-org/whisper.cpp
cd whisper.cpp && cmake -B build && cmake --build build -j
sudo cp build/bin/whisper-cli /usr/local/bin/
cd ..

./native/install.sh <extension-id>   # registers for Chrome and/or Chromium
# fully restart the browser afterwards
```

**Windows (PowerShell)**

```powershell
winget install Python.Python.3.12 Gyan.FFmpeg
# whisper.cpp: download the latest whisper-bin-x64.zip from
#   https://github.com/ggml-org/whisper.cpp/releases
# unzip it and add the folder containing whisper-cli.exe to PATH
# (Settings -> System -> About -> Advanced system settings -> Environment Variables)

cd native
.\install.ps1 <extension-id>   # creates dvt_host.bat + registry entry, no admin needed
# fully restart Chrome afterwards
```

Verify: `ffmpeg -version`, `whisper-cli --help` and `python3 --version` (Windows:
`python --version`) must all work in a fresh terminal. In the extension panel the
"Native host" warning line must disappear after the restart.

**Claude CLI (optional, for `--report`)** — install per
[claude.com/claude-code](https://claude.com/claude-code) (`npm install -g @anthropic-ai/claude-code`
works on all three platforms), then pick your instance in the panel settings.

## Usage

1. Open **discord.com** in a Chrome tab (the desktop app won't work — the extension cannot hear it!) and join a voice channel.
2. While on the Discord tab, click the extension icon → the side panel opens.
3. **● Start recording**. You keep hearing the call as usual. Keep the panel open while recording.
   - First time only: a microphone-permission page opens — allow it, then start again.
   - In Settings pick the same **Microphone** device Discord uses (virtual devices like
     "MS Teams Audio" record static). The level meter next to the record button shows
     live mic activity; a warning appears if the mic stays silent.
4. **■ Stop & save** → the recording lands in its own folder under `recordings/`,
   transcription starts automatically, and the recording list shows live status
   (audio → transcript → report). Click a recording to open the **editor** in a new tab:
   colored speakers, clickable rows that play the exact fragment, manual editing of
   speaker/time/text (persisted to SQLite, survives re-transcription).

If the native host is not available, the panel falls back to downloading the files into
Downloads; transcribe manually with:

```bash
python3 tools/transcribe.py ~/Downloads/discord-call-<date>.webm --report --claude claude-personal
```

Model: `large-v3-turbo` (the only option, ~1.6 GB, downloads itself on first run).
If the call is mostly in one language, add `--language ru` / `lv` / `en` for fewer hallucinations.

## Troubleshooting the native host

The panel shows a "Native host: not installed" warning when Chrome cannot start the
host. Check the registration for your platform:

- **macOS**: `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.dvt.recorder.json`
- **Linux**: `~/.config/google-chrome/NativeMessagingHosts/com.dvt.recorder.json`
  (Chromium: `~/.config/chromium/...`)
- **Windows**: registry key `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.dvt.recorder`
  pointing to `native\com.dvt.recorder.json`, which launches `native\dvt_host.bat`

The `allowed_origins` entry must contain your actual extension ID, and the browser must
be fully restarted after registration. The host runs with a minimal PATH; on
macOS/Linux the scripts add Homebrew and `~/.local/bin` themselves — on Windows make
sure `ffmpeg` and `whisper-cli` are on the *system* PATH.

## How transcription works

- The speaker timeline (from the DOM) drives everything: audio is cut into speech blocks,
  silence/music between blocks never reaches Whisper (that is where hallucination loops
  are born), each block gets its own language detection and no cross-block context.
- Speaker intervals shorter than 0.7 s are treated as noise; flickering intervals of the
  same speaker are merged first so real speech is not lost.
- Overlap heuristic: the beginning of an overlapping segment belongs to whoever started
  speaking first, the end to whoever finished last; both shown as `A → B` when significant.
- Storage: single SQLite DB `recordings/dvt.sqlite` with `recordings [id, base, dir,
  start_dt, end_dt]` and `text_lines [recording_id, speaker_name, speaker_line,
  start_dt_ms, end_dt_ms, edited]`. Hand-edited lines (`edited=1`) are never overwritten.

## Known limitations

- The audio is a single mixed track; when two people talk at once the text goes to the
  dominant speaker (with the `A → B` marker when both contributed).
- Discord class hashes change with every build — selectors match substrings and the
  semantic `--status-speaking` CSS variable, so they usually survive updates. If the
  panel stops seeing speakers, the selectors need a refresh (see RESEARCH.md, untracked).
- The recording lives in memory until Stop: ~15 MB/hour, multi-hour calls are fine.
- Everything audible in the tab is recorded — including Discord beeps.

## Layout

```
extension/           Chrome MV3 extension (panel, DOM sensor, editor UI)
native/              native messaging host + install.sh
tools/transcribe.py  whisper.cpp pipeline: blocks, attribution, SQLite, Claude report
tools/dvtdb.py       shared SQLite schema (recordings, text_lines)
recordings/          one folder per recording + dvt.sqlite (gitignored)
models/              whisper models (gitignored, self-downloading)
```
