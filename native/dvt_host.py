#!/usr/bin/env python3
"""Native messaging host paplašinājumam Discord Call Recorder.

Saņem no paneļa ierakstu (audio chunkos + runātāju failus), saglabā tos
ierakstu mapē, automātiski palaiž tools/transcribe.py un straumē tā izvadi
atpakaļ uz paneli. Protokols: Chrome native messaging (4 baitu garums + JSON).
"""
from __future__ import annotations
import base64
import json
import os
import struct
import subprocess
import sys
import threading
from pathlib import Path

# Chrome palaiž hostu ar minimālu PATH — pievienojam brew un ~/.local/bin,
# lai transcribe.py atrod ffmpeg, whisper-cli un claude.
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
import dvtdb  # noqa: E402

_send_lock = threading.Lock()


def send(obj) -> None:
    data = json.dumps(obj).encode()
    with _send_lock:
        sys.stdout.buffer.write(struct.pack("<I", len(data)))
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


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
    if settings.get("report"):
        cmd += ["--report", "--claude", settings.get("claude", "claude")]
    send({"type": "log", "line": "$ " + " ".join(cmd)})
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            send({"type": "log", "line": line.rstrip()})
        p.wait()
        send({"type": "done", "code": p.returncode, "base": audio.stem})
    except Exception as e:  # noqa: BLE001
        send({"type": "log", "line": f"Kļūda: {e}"})
        send({"type": "done", "code": 1, "base": audio.stem})


def _search_bases(db, q: str) -> set:
    """Meklēšana pēc ID, nosaukuma vai transkripta satura (SQLite)."""
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
        # jaunais izkārtojums: <dir>/<base>/<base>.webm; vecais: <dir>/<base>.webm
        found = list(dirpath.glob("*/*.webm")) + list(dirpath.glob("*.webm"))
        db = dvtdb.connect()
        title_map = dvtdb.titles(db)
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
                "report": Path(b + ".report.md").exists(),
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
    """Vecajiem ierakstiem bez DB rindām: importē no .transcript.md."""
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
    for i, r in enumerate(rows):  # beigas = nākamā sākums (MD failā beigu laika nav)
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
    send({"type": "recording", "base": base,
          "title": rec["title"] if rec else None,
          "start_dt": rec["start_dt"] if rec else None,
          "end_dt": rec["end_dt"] if rec else None,
          "lines": lines})
    db.close()

    audio = find_audio(base, base_dir)
    if not audio:
        send({"type": "audio-missing"})
        return
    send({"type": "audio-begin", "size": audio.stat().st_size})
    with open(audio, "rb") as f:
        while chunk := f.read(256 * 1024):
            send({"type": "audio-chunk", "data": base64.b64encode(chunk).decode()})
    send({"type": "audio-end"})


def main() -> None:
    out = None
    audio_path = None
    settings: dict = {}
    base_dir = DEFAULT_DIR
    outdir = DEFAULT_DIR
    while True:
        msg = read_msg()
        if msg is None:
            break
        t = msg.get("type")
        if t == "ping":
            send({"type": "pong", "dir": str(base_dir)})
        elif t == "load":
            try:
                handle_load(msg, Path(msg.get("dir") or base_dir).expanduser())
            except Exception as e:  # noqa: BLE001
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
        elif t == "pick-dir":
            # macOS mapes izvēles dialogs — panelim nav pieejas failu sistēmai
            r = subprocess.run(
                ["osascript", "-e",
                 'POSIX path of (choose folder with prompt "Izvades mape ierakstiem")'],
                capture_output=True, text=True)
            picked = r.stdout.strip().rstrip("/") if r.returncode == 0 else None
            send({"type": "dir-picked", "dir": picked})
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
            outdir = base_dir / msg["base"]  # katram ierakstam sava mape

            outdir.mkdir(parents=True, exist_ok=True)
            audio_path = outdir / (msg["base"] + ".webm")
            out = open(audio_path, "wb")
        elif t == "chunk" and out:
            out.write(base64.b64decode(msg["data"]))
        elif t == "finish" and out:
            out.close()
            out = None
            # MediaRecorder webm nav ilguma metadatu (pārlūkā duration=Infinity);
            # ātrs remux ar ffmpeg to ieraksta
            try:
                tmp = audio_path.with_suffix(".fixed.webm")
                r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i",
                                    str(audio_path), "-c", "copy", str(tmp)],
                                   capture_output=True)
                if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                    tmp.replace(audio_path)
                elif tmp.exists():
                    tmp.unlink()
            except Exception:  # noqa: BLE001
                pass
            base = str(outdir / msg["base"])
            Path(base + ".speakers.json").write_text(msg["speakers"])
            Path(base + ".speakers.srt").write_text(msg["srt"])
            if msg.get("prompt"):
                Path(base + ".prompt.txt").write_text(msg["prompt"])
            send({"type": "saved", "path": str(audio_path)})
            if settings.get("autoTranscribe", True):
                threading.Thread(target=run_transcribe,
                                 args=(audio_path, settings),
                                 daemon=True).start()


if __name__ == "__main__":
    main()
