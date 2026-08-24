#!/usr/bin/env python3
"""Native messaging host for the Discord Call Recorder extension.

Receives a recording from the panel (base64 audio chunks + speaker files),
stores it in a per-recording folder, launches tools/transcribe.py and streams
its output back to the panel; also serves the editor page (load/update/delete
against the SQLite store). Protocol: Chrome native messaging (4-byte little-
endian length + JSON). Windows requires binary stdio; Chrome spawns the host
with a minimal PATH, hence the Homebrew/~/.local/bin prepend.
"""
from __future__ import annotations
import base64
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

os.environ["PATH"] = ":".join([
    "/opt/homebrew/bin",
    "/usr/local/bin",
    os.path.expanduser("~/.local/bin"),
    os.environ.get("PATH", ""),
])

PROJECT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = PROJECT / "recordings"
TRANSCRIBE = PROJECT / "tools" / "transcribe.py"

sys.path.insert(0, str(PROJECT / "tools"))
import ai_providers
import dvtdb

try:
    import live
except Exception:
    live = None  # live transcription is optional; recording must never break

_send_lock = threading.Lock()

# Chrome kills the native-messaging port when a single host→extension message
# exceeds 1 MB; keep every serialized message well below that.
MSG_MAX_BYTES = 512 * 1024

def pick_folder() -> str | None:
    """Native folder-picker dialog; returns the chosen path or None."""
    try:
        if sys.platform == "darwin":
            r = subprocess.run(
                ["osascript", "-e",
                 'POSIX path of (choose folder with prompt "Izvades mape ierakstiem")'],
                capture_output=True, text=True)
            return r.stdout.strip().rstrip("/") if r.returncode == 0 else None
        if sys.platform == "win32":
            ps = ("Add-Type -AssemblyName System.Windows.Forms; "
                  "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
                  "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK)"
                  " { Write-Output $d.SelectedPath }")
            r = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", ps],
                capture_output=True, text=True)
            picked = r.stdout.strip()
            return picked or None
        r = subprocess.run(["zenity", "--file-selection", "--directory"],
                           capture_output=True, text=True)
        return (r.stdout.strip() or None) if r.returncode == 0 else None
    except OSError:
        return None


def iter_batches(items: list, budget: int = MSG_MAX_BYTES):
    """Yield slices of items whose JSON-encoded size stays under budget."""
    batch, size = [], 0
    for it in items:
        n = len(json.dumps(it).encode()) + 2  # +2 for the ", " separator
        if batch and size + n > budget:
            yield batch
            batch, size = [], 0
        batch.append(it)
        size += n
    if batch:
        yield batch

def send(obj) -> None:
    data = json.dumps(obj).encode()
    try:
        with _send_lock:
            sys.stdout.buffer.write(struct.pack("<I", len(data)))
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
    except Exception:
        pass  # panel gone (pipe closed) — keep finalization/transcription alive

def log_error(base_dir: Path) -> None:
    """Append the current exception traceback to <base_dir>/dvt_host.log."""
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
        with open(base_dir / "dvt_host.log", "a") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S]\n") + traceback.format_exc() + "\n")
    except Exception:
        pass

def read_msg():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    (n,) = struct.unpack("<I", raw)
    return json.loads(sys.stdin.buffer.read(n))

def run_transcribe(audio: Path, settings: dict) -> None:
    cmd = [sys.executable, str(TRANSCRIBE), str(audio),
           "--model", settings.get("model", "large-v3-turbo")]
    lang = settings.get("language", "auto")
    if lang != "auto":
        cmd += ["--language", lang]
    sections = [f for f in ("report", "decisions", "actions", "threads")
                if settings.get(f)]
    cmd += ["--" + s for s in sections]
    if settings.get("fixConvo"):
        cmd.append("--fix-convo")
    if settings.get("autoTitle"):
        cmd.append("--set-title")
    if sections or settings.get("fixConvo") or settings.get("autoTitle"):
        cmd += ["--ai-provider", settings.get("aiProvider", "claude"),
                "--ai-instance", settings.get("aiInstance", ""),
                "--report-lang", settings.get("uiLang", "en")]
    send({"type": "log", "line": "$ " + " ".join(cmd)})
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            send({"type": "log", "line": line.rstrip()})
        p.wait()
        send({"type": "done", "code": p.returncode, "base": audio.stem})
    except Exception as e:
        send({"type": "log", "line": f"Kļūda: {e}"})
        send({"type": "done", "code": 1, "base": audio.stem})

def _search_bases(db, q: str) -> set:
    """Search recordings by base ID, title, speaker name or transcript content."""
    like = f"%{q}%"
    rows = db.execute(
        """SELECT DISTINCT r.base FROM recordings r
           LEFT JOIN text_lines l ON l.recording_id = r.id
           WHERE r.base LIKE ? OR r.title LIKE ?
              OR l.speaker_line LIKE ? OR l.speaker_name LIKE ?""",
        (like, like, like, like))
    return {r["base"] for r in rows}

def list_recordings(dirpath: Path, page: int = 0, page_size: int = 5,
                    q: str = "") -> dict:
    items = []
    if dirpath.exists():
        found = list(dirpath.glob("*/*.webm")) + list(dirpath.glob("*.webm"))
        db = dvtdb.connect()
        title_map = dvtdb.titles(db)
        reports = dvtdb.report_bases(db)
        status_map = {r["base"]: r["status"] for r in db.execute(
            "SELECT base, status FROM recordings WHERE status IS NOT NULL")}
        matched = _search_bases(db, q) if q else None
        db.close()
        for f in sorted(found, key=lambda p: p.stem, reverse=True):
            if matched is not None and f.stem not in matched \
                    and q.lower() not in f.stem.lower():
                continue
            b = str(f.with_suffix(""))
            items.append({
                "base": f.stem,
                "title": title_map.get(f.stem),
                "transcript": Path(b + ".transcript.md").exists(),
                "report": f.stem in reports,
                "status": status_map.get(f.stem),
            })
    total = len(items)
    pages = max(1, -(-total // page_size))
    page = max(0, min(page, pages - 1))
    return {"items": items[page * page_size:(page + 1) * page_size],
            "page": page, "pages": pages, "total": total}

def find_audio(base: str, base_dir: Path) -> Path | None:
    for cand in (base_dir / base / f"{base}.webm", base_dir / f"{base}.webm"):
        if cand.exists():
            return cand
    return None

MD_LINE = __import__("re").compile(r"^\[(\d+):(\d+):(\d+)\]\s+(.+?):\s+(.*)$")

def import_from_md(db, base: str, base_dir: Path) -> int | None:
    """Import legacy recordings without DB rows from their .transcript.md."""
    audio = find_audio(base, base_dir)
    md = (audio.with_suffix("") if audio else base_dir / base / base)
    md = Path(str(md) + ".transcript.md")
    if not md.exists():
        return None
    rows = []
    for line in md.read_text().splitlines():
        m = MD_LINE.match(line.strip())
        if m:
            h, mnt, s, speaker, text = m.groups()
            t0 = (int(h) * 3600 + int(mnt) * 60 + int(s)) * 1000
            rows.append({"speaker_name": speaker, "start_dt_ms": t0,
                         "end_dt_ms": t0, "speaker_line": text})
    for i, r in enumerate(rows):
        r["end_dt_ms"] = rows[i + 1]["start_dt_ms"] if i + 1 < len(rows) \
            else r["start_dt_ms"] + 5000
    rec_id = dvtdb.upsert_recording(db, base, str(audio.parent if audio else base_dir),
                                    None, rows[-1]["end_dt_ms"] if rows else 0)
    dvtdb.replace_lines(db, rec_id, rows)
    return rec_id

def handle_load(msg, base_dir: Path) -> None:
    base = msg["base"]
    db = dvtdb.connect()
    rec = dvtdb.get_recording(db, base)
    rec_id = rec["id"] if rec else None
    if rec_id is None or not dvtdb.get_lines(db, rec_id):
        imported = import_from_md(db, base, base_dir)
        rec_id = imported if imported is not None else rec_id
    lines = dvtdb.get_lines(db, rec_id) if rec_id is not None else []
    rec = dvtdb.get_recording(db, base)
    report = dvtdb.get_report(db, rec_id) if rec_id is not None else None
    threads = dvtdb.get_threads(db, rec_id) if rec_id is not None else []
    audio = find_audio(base, base_dir)
    # An existing .live.pcm sidecar means a live session is writing preview
    # lines right now — the editor keeps polling (linesOnly) while it lasts.
    live_now = bool(audio and audio.with_suffix(".live.pcm").exists())
    # Chunked reply: metadata first, then the lines in size-bounded batches,
    # then an end marker — a 3–4 h call easily exceeds Chrome's 1 MB cap.
    send({"type": "recording", "base": base,
          "title": rec["title"] if rec else None,
          "start_dt": rec["start_dt"] if rec else None,
          "end_dt": rec["end_dt"] if rec else None,
          "status": rec["status"] if rec else None,
          "report": report,
          "threads": threads,
          "live": live_now,
          "line_count": len(lines)})
    for batch in iter_batches(lines):
        send({"type": "recording-lines", "base": base, "lines": batch})
    send({"type": "recording-end", "base": base})
    db.close()

    if msg.get("linesOnly"):
        return
    if not audio:
        send({"type": "audio-missing"})
        return
    send({"type": "audio-begin", "size": audio.stat().st_size})
    with open(audio, "rb") as f:
        while chunk := f.read(256 * 1024):
            send({"type": "audio-chunk", "data": base64.b64encode(chunk).decode()})
    send({"type": "audio-end"})

def finalize_recording(out, audio_path: Path, outdir: Path, base: str,
                       speakers: str | None, srt: str | None,
                       prompt: str | None, settings: dict) -> None:
    """Close the audio, remux, write sidecars, launch transcription.

    Shared by the 'finish' handler and the EOF path (panel closed
    mid-recording): finalizes whatever audio and speaker data exists.
    """
    out.close()
    try:
        tmp = audio_path.with_suffix(".fixed.webm")
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i",
                            str(audio_path), "-c", "copy", str(tmp)],
                           capture_output=True)
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(audio_path)
        elif tmp.exists():
            tmp.unlink()
    except Exception:
        pass
    base_path = str(outdir / base)
    if speakers is not None:
        Path(base_path + ".speakers.json").write_text(speakers)
    if srt is not None:
        Path(base_path + ".speakers.srt").write_text(srt)
    if prompt:
        Path(base_path + ".prompt.txt").write_text(prompt)
    send({"type": "saved", "path": str(audio_path)})
    auto = settings.get("autoTranscribe", True)
    try:
        # User pressed stop: stamp end_dt and advance the status machine —
        # 'transcribing' while the pipeline runs, 'done' when it never will.
        db = dvtdb.connect()
        dvtdb.finish_recording(db, base, datetime.now(timezone.utc).isoformat(),
                               "transcribing" if auto else "done")
        db.close()
    except Exception:
        log_error(outdir)
    if auto:
        # Non-daemon: the pipeline must survive main() returning on EOF.
        threading.Thread(target=run_transcribe,
                         args=(audio_path, settings)).start()

def main() -> None:
    out = None
    audio_path = None
    settings: dict = {}
    base_dir = DEFAULT_DIR
    outdir = DEFAULT_DIR
    rec_base = None
    speakers = None
    srt = None
    live_session = None

    def stop_live() -> None:
        nonlocal live_session
        if live_session is not None:
            ls, live_session = live_session, None
            try:
                ls.finish()
            except Exception:
                log_error(base_dir)

    def handle(msg) -> None:
        nonlocal out, audio_path, settings, base_dir, outdir
        nonlocal rec_base, speakers, srt, live_session
        t = msg.get("type")
        if t == "ping":
            send({"type": "pong", "dir": str(base_dir)})
        elif t == "ai-providers":
            # Providers whose CLI is not installed are simply omitted — a
            # normal setup, not an error. An empty list makes the panel hide
            # the whole AI section and never request AI stages.
            send({"type": "ai-providers", "providers": ai_providers.detect()})
        elif t == "load":
            try:
                handle_load(msg, Path(msg.get("dir") or base_dir).expanduser())
            except Exception as e:
                send({"type": "error", "message": str(e)})
        elif t == "update":
            db = dvtdb.connect()
            dvtdb.update_line(db, msg["id"], msg["speaker_name"],
                              int(msg["start_dt_ms"]), int(msg["end_dt_ms"]),
                              msg["speaker_line"])
            db.close()
            send({"type": "updated", "id": msg["id"]})
        elif t == "delete":
            db = dvtdb.connect()
            dvtdb.delete_line(db, msg["id"])
            db.close()
            send({"type": "deleted", "id": msg["id"]})
        elif t == "list":
            d = Path(msg.get("dir") or base_dir).expanduser()
            res = list_recordings(d, int(msg.get("page") or 0),
                                  int(msg.get("pageSize") or 5),
                                  (msg.get("q") or "").strip())
            res.update({"type": "list", "dir": str(d)})
            send(res)
        elif t == "delete-recording":
            b = msg["base"]
            db = dvtdb.connect()
            dvtdb.delete_recording(db, b)
            db.close()
            folder = base_dir / b
            if folder.is_dir() and folder.parent == base_dir:
                shutil.rmtree(folder)
            for legacy in base_dir.glob(b + ".*"):
                legacy.unlink()
            send({"type": "recording-deleted", "base": b})
        elif t == "pick-dir":
            send({"type": "dir-picked", "dir": pick_folder()})
        elif t == "set-title":
            db = dvtdb.connect()
            audio = find_audio(msg["base"], base_dir)
            dvtdb.set_title(db, msg["base"], (msg.get("title") or "").strip(),
                            str(audio.parent) if audio else None)
            db.close()
            send({"type": "title-set", "base": msg["base"],
                  "title": (msg.get("title") or "").strip() or None})
        elif t == "begin":
            settings = msg.get("settings") or {}
            base_dir = Path(settings.get("outDir") or DEFAULT_DIR).expanduser()
            outdir = base_dir / msg["base"]

            outdir.mkdir(parents=True, exist_ok=True)
            audio_path = outdir / (msg["base"] + ".webm")
            out = open(audio_path, "wb")
            try:
                # Status machine entry: the row exists from the very first
                # second of recording (status='recording').
                db = dvtdb.connect()
                dvtdb.start_recording(db, msg["base"], str(outdir),
                                      datetime.now(timezone.utc).isoformat())
                db.close()
            except Exception:
                log_error(base_dir)  # recording must never break on DB issues
            rec_base = msg["base"]
            speakers = None
            srt = None
            stop_live()  # stray session from an aborted recording
            if live is not None and settings.get("live"):
                try:
                    live_session = live.LiveTranscriber(audio_path, settings, send)
                except Exception:
                    live_session = None
                    log_error(base_dir)
        elif t == "chunk" and out:
            data = base64.b64decode(msg["data"])
            out.write(data)
            if live_session is not None:
                live_session.feed(data)
        elif t == "events" and out:
            speakers = msg.get("speakers")
            srt = msg.get("srt")
            if live_session is not None:
                live_session.on_events(speakers, bool(msg.get("cut")))
        elif t == "finish" and out:
            fh, out = out, None
            stop_live()
            finalize_recording(fh, audio_path, outdir, msg["base"],
                               msg["speakers"], msg["srt"], msg.get("prompt"),
                               settings)

    while True:
        try:
            msg = read_msg()
        except Exception:
            log_error(base_dir)
            msg = None
        if msg is None:
            break
        try:
            handle(msg)
        except Exception as e:
            log_error(base_dir)
            send({"type": "error", "message": str(e)})
    if out is not None:  # EOF mid-recording (panel closed) — save what we have
        fh, out = out, None
        stop_live()
        try:
            finalize_recording(fh, audio_path, outdir, rec_base,
                               speakers, srt, None, settings)
        except Exception:
            log_error(base_dir)
    else:
        stop_live()

if __name__ == "__main__":
    main()
