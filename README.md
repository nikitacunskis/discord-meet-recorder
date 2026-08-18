# Discord Call Recorder + Speaker Timeline

Chrome extension (MV3) that, during a Discord **web** call:

1. records the tab audio mixed with your microphone → `discord-call-<date>.webm` (opus),
2. reads from the DOM **who is speaking at every moment** (the green indicator = inline `box-shadow: var(--status-speaking)` style) → `*.speakers.json` and `*.speakers.srt`,
3. hands everything to a local native host that transcribes the call with whisper.cpp, attributes every line to a speaker, stores the result in SQLite and (optionally) generates a Claude report with a summary and action items.

Everything runs locally — no audio ever leaves the machine (the only optional network step is the `claude -p` report, which uses your own Claude subscription).

## Installation

1. Chrome → `chrome://extensions`
2. Enable **Developer mode** (top-right corner)
3. **Load unpacked** → pick the `extension/` folder
4. Register the native host (auto-save, auto-transcription, editor backend):

```bash
# one-time setup
brew install ffmpeg whisper-cpp
./native/install.sh <extension-id from chrome://extensions>
# fully restart Chrome (Cmd+Q) afterwards
```

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
