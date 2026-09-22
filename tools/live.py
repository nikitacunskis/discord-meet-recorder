#!/usr/bin/env python3
"""Live (during-call) transcription for the native host.

The panel streams webm/opus chunks continuously; LiveTranscriber pipes every
chunk through a persistent `ffmpeg` process into a growing 16 kHz mono s16le
PCM sidecar file, so any [start, end] slice of the call is available in O(1)
without re-decoding the webm (which is only decodable from the start).

Phrase boundaries come from the Discord speaking indicator: the panel sends
every indicator change as a `speaking` message. A phrase is a run of speech
(any speakers, overlapping or not) with no gap longer than FINAL_PAUSE_MS.
While a phrase is open, a worker thread re-transcribes the whole phrase every
DRAFT_TICK_MS with a resident `whisper-server` and sends the result as a
draft ({"type": "live", "draft": {...}}), which the panel rewrites in place.
When the phrase closes it is transcribed once more and sent as final lines
({"type": "live", "lines": [...], "done": <draft id>}); only final lines go
to <base>.live.md and SQLite. Long monologues get their stable start committed
sentence by sentence so the draft window never grows without bound.

Live output is a preview: the full-quality pipeline (tools/transcribe.py)
still runs over the whole recording on finish and is the source of truth.
"""
from __future__ import annotations
import io
import json
import logging
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import dvtdb
import transcribe as T

LOG_FILE = dvtdb.DB_PATH.parent / "live.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("live")
if not log.handlers:
    h = logging.FileHandler(LOG_FILE)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False

BYTES_PER_MS = 32          # 16000 Hz * 2 bytes * 1 ch / 1000
PAD_MS = 400               # audio kept around a phrase (indicator lags speech)
FINAL_PAUSE_MS = 600       # nobody speaking this long -> phrase is over
DRAFT_TICK_MS = 2000       # re-transcribe the open phrase this often
MIN_DRAFT_MS = 1500        # first draft needs at least this much audio
MIN_FINAL_MS = 1000        # shorter phrases are indicator noise, not speech
COMMIT_AFTER_MS = 20000    # monologue longer than this: commit its stable start
STABLE_TAIL_MS = 4000      # ...but never the last few seconds (still changing)
FORCED_COMMIT_MS = 45000   # no sentence end found: commit anyway past this
SERVER_START_TIMEOUT = 180  # model load can take a while on first run
SENTENCE_END = ".!?…"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wav_bytes(pcm: bytes) -> bytes:
    """Wrap raw 16 kHz mono s16le PCM in a WAV header."""
    hdr = io.BytesIO()
    hdr.write(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE")
    hdr.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16))
    hdr.write(b"data" + struct.pack("<I", len(pcm)))
    return hdr.getvalue() + pcm


def build_intervals(events: list[dict], now_ms: int) -> list[dict]:
    """Speaker intervals from raw indicator events (mirrors panel.js
    buildIntervals). Speakers still on at `now_ms` get end_ms == now_ms."""
    open_: dict = {}
    out = []
    for e in sorted(events, key=lambda e: e["t_ms"]):
        key = e.get("userId") or e["name"]
        if e["speaking"]:
            open_.setdefault(key, e)
        elif key in open_:
            s = open_.pop(key)
            if e["t_ms"] - s["t_ms"] >= 150:
                out.append({"name": s["name"], "userId": s.get("userId"),
                            "start_ms": s["t_ms"], "end_ms": e["t_ms"]})
    for s in open_.values():
        out.append({"name": s["name"], "userId": s.get("userId"),
                    "start_ms": s["t_ms"], "end_ms": now_ms})
    return sorted(out, key=lambda i: i["start_ms"])


class WhisperServer:
    """One resident whisper-server process (model stays loaded between jobs)."""

    def __init__(self, model: Path, vad_model: Path | None):
        self.port = _free_port()
        cmd = ["whisper-server", "-m", str(model),
               "--host", "127.0.0.1", "--port", str(self.port),
               "-mc", "0", "-t", "4"]
        if vad_model:
            cmd += ["--vad", "-vm", str(vad_model)]
        log.info("Starting whisper-server: %s", " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)

    def wait_ready(self) -> None:
        deadline = time.monotonic() + SERVER_START_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("whisper-server exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", self.port), 1):
                    return
            except OSError:
                time.sleep(0.3)
        raise RuntimeError("whisper-server did not become ready")

    def transcribe(self, pcm: bytes, language: str) -> list[dict]:
        """POST one WAV to /inference; returns [{start, end, text}] (seconds)."""
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in (("response_format", "verbose_json"),
                            ("language", language or "auto"),
                            ("temperature", "0.0")):
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                         f"name=\"{name}\"\r\n\r\n{value}\r\n".encode())
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f"name=\"file\"; filename=\"piece.wav\"\r\n"
                     f"Content-Type: audio/wav\r\n\r\n".encode())
        body = b"".join(parts) + _wav_bytes(pcm) + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/inference", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode())
        segs = []
        for s in data.get("segments") or []:
            text = (s.get("text") or "").strip()
            if text:
                segs.append({"start": float(s["start"]), "end": float(s["end"]),
                             "text": text})
        return segs

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


class LiveTranscriber:
    """Owns the ffmpeg decoder, the PCM sidecar, and the transcription worker
    for one recording. All public methods are called from the host main loop;
    heavy work happens on the worker thread."""

    def __init__(self, audio_path: Path, settings: dict, send):
        self.audio_path = audio_path
        self.settings = settings
        self.send = send
        self.language = settings.get("language", "auto")
        self.pcm_path = audio_path.with_suffix(".live.pcm")
        self.md_path = Path(str(audio_path.with_suffix("")) + ".live.md")
        self.cond = threading.Condition()
        self.events: list[dict] = []
        self.started_at: str | None = None
        self.pcm_len = 0          # bytes written so far (under self.cond)
        self.committed_ms = 0     # everything before this is final
        self.block_id: int | None = None   # start of the open phrase (draft id)
        self.draft_sent = False   # panel is showing a draft for block_id
        self.last_draft_ms = 0    # audio end of the last draft
        self.stopping = False
        self.dead = False         # pipeline failed; feed() becomes a no-op
        self.server: WhisperServer | None = None

        if shutil.which("whisper-server") is None or shutil.which("ffmpeg") is None:
            raise RuntimeError("live: whisper-server/ffmpeg not on PATH")
        self.pcm_file = open(self.pcm_path, "wb")
        # nobuffer/flush_packets: hand PCM over as soon as it is decoded —
        # ffmpeg's default output buffering alone holds ~1 s of audio.
        self.ffmpeg = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-fflags", "nobuffer", "-i", "pipe:0",
             "-f", "s16le", "-ac", "1", "-ar", "16000", "-flush_packets", "1",
             "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        self.reader = threading.Thread(target=self._read_pcm, daemon=True)
        self.reader.start()
        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()
        log.info("Live session started for %s", audio_path.name)

    # ---- host-facing API (main thread) ----

    def feed(self, data: bytes) -> None:
        """A webm chunk from the panel (already written to the .webm file)."""
        if self.dead:
            return
        try:
            # Flush every chunk: the pipe is buffered (128 KiB on Python
            # 3.14), and quiet opus runs ~130 B/s, so without a flush audio
            # sat in the buffer for minutes before ffmpeg ever saw it.
            self.ffmpeg.stdin.write(data)
            self.ffmpeg.stdin.flush()
        except (BrokenPipeError, OSError):
            log.error("ffmpeg pipe broken — live transcription disabled")
            self.dead = True

    def on_speaking(self, msg: dict) -> None:
        """One Discord indicator change: {name, userId, speaking, t_ms}."""
        if self.dead:
            return
        try:
            ev = {"name": str(msg["name"]), "userId": msg.get("userId"),
                  "speaking": bool(msg["speaking"]), "t_ms": int(msg["t_ms"])}
        except (KeyError, TypeError, ValueError):
            return
        with self.cond:
            self.events.append(ev)
            self.cond.notify()

    def on_events(self, speakers_json: str | None) -> None:
        """Cumulative speaker snapshot; only started_at is taken from it."""
        if self.dead or not speakers_json or self.started_at:
            return
        try:
            self.started_at = json.loads(speakers_json).get("started_at")
        except Exception:
            pass

    def finish(self) -> None:
        """Stop everything. The final full pipeline covers the whole file, so
        the live worker does not chase the tail — it just stops."""
        with self.cond:
            self.stopping = True
            self.cond.notify()
        try:
            if self.ffmpeg.stdin:
                self.ffmpeg.stdin.close()
        except Exception:
            pass
        self.reader.join(timeout=10)
        self.worker.join(timeout=30)
        try:
            self.ffmpeg.terminate()
        except Exception:
            pass
        if self.server:
            self.server.stop()
            self.server = None
        try:
            self.pcm_file.close()
        except Exception:
            pass
        try:
            self.pcm_path.unlink(missing_ok=True)
        except Exception:
            pass
        log.info("Live session finished for %s (final up to %d ms)",
                 self.audio_path.name, self.committed_ms)

    # ---- internals ----

    def _read_pcm(self) -> None:
        fd = self.ffmpeg.stdout.fileno()
        try:
            while True:
                # os.read returns whatever is available; a buffered read of
                # 64 KiB would block until 2 s of audio piled up.
                buf = os.read(fd, 64 * 1024)
                if not buf:
                    break
                self.pcm_file.write(buf)
                self.pcm_file.flush()
                with self.cond:
                    self.pcm_len += len(buf)
                    self.cond.notify()
        except Exception:
            log.exception("PCM reader died")
            self.dead = True

    def _read_slice(self, start_ms: int, end_ms: int) -> bytes:
        with open(self.pcm_path, "rb") as f:
            f.seek(start_ms * BYTES_PER_MS)
            return f.read((end_ms - start_ms) * BYTES_PER_MS)

    def _phrase(self) -> tuple | None:
        """Under self.cond: the first not-yet-final phrase as
        (start_ms, end_ms, closed, now_ms, cleaned_intervals), or None."""
        now = self.pcm_len // BYTES_PER_MS
        cleaned = T.clean_intervals(build_intervals(self.events, now))
        regs: list[list[int]] = []
        for iv in cleaned:
            s, e = max(iv["start_ms"], self.committed_ms), iv["end_ms"]
            if e - s <= 0:
                continue
            if regs and s - regs[-1][1] <= FINAL_PAUSE_MS:
                regs[-1][1] = max(regs[-1][1], e)
            else:
                regs.append([s, e])
        if not regs:
            return None
        s, e = regs[0]
        # Closed once the available audio shows a full pause after it; an
        # indicator still on has end_ms == now, so it can never be closed.
        closed = e + FINAL_PAUSE_MS <= now
        return s, e, closed, now, cleaned

    def _next_job(self) -> tuple | None:
        """Under self.cond: ("final"|"draft", window_start, window_end,
        phrase_start, cleaned) or None when there is nothing to do yet."""
        ph = self._phrase()
        if ph is None:
            return None
        s, e, closed, now, cleaned = ph
        ws = max(s - PAD_MS, self.committed_ms)
        if closed:
            return "final", ws, min(now, e + PAD_MS), s, cleaned
        if now - ws >= MIN_DRAFT_MS and now - self.last_draft_ms >= DRAFT_TICK_MS:
            return "draft", ws, now, s, cleaned
        return None

    def _work(self) -> None:
        try:
            while True:
                with self.cond:
                    while not self.stopping and self._next_job() is None:
                        self.cond.wait(0.25)
                    if self.stopping:
                        return
                    kind, ws, we, s, cleaned = self._next_job()
                    if self.block_id is None:
                        self.block_id = s
                self._ensure_server()
                if kind == "final":
                    self._final(ws, we, cleaned)
                else:
                    self._draft(ws, we, cleaned)
        except Exception:
            log.exception("Live worker died")
            self.dead = True

    def _final(self, ws: int, we: int, cleaned: list[dict]) -> None:
        lines = self._transcribe(ws, we, cleaned) if we - ws >= MIN_FINAL_MS else []
        log.info("Final [%d..%d]: %d line(s)", ws, we, len(lines))
        block_id, had_draft = self.block_id, self.draft_sent
        with self.cond:
            self.committed_ms = we
            self.block_id = None
            self.draft_sent = False
            self.last_draft_ms = 0
        if lines or had_draft:
            self._emit_final(lines, we, done=block_id)

    def _draft(self, ws: int, we: int, cleaned: list[dict]) -> None:
        lines = self._transcribe(ws, we, cleaned)
        commit: list[dict] = []
        if we - ws >= COMMIT_AFTER_MS:
            stable = [l for l in lines if l["end_ms"] <= we - STABLE_TAIL_MS]
            cut = -1
            for i, l in enumerate(stable):
                if l["text"][-1] in SENTENCE_END:
                    cut = i
            if cut < 0 and we - ws >= FORCED_COMMIT_MS and stable:
                cut = len(stable) - 1
            if cut >= 0:
                commit, lines = stable[:cut + 1], lines[cut + 1:]
        log.info("Draft [%d..%d]: %d line(s), %d committed",
                 ws, we, len(lines), len(commit))
        with self.cond:
            self.last_draft_ms = we
            if commit:
                self.committed_ms = commit[-1]["end_ms"]
            self.draft_sent = True
        if commit:
            self._emit_final(commit, commit[-1]["end_ms"])
        self.send({"type": "live", "draft": {"id": self.block_id, "lines": lines}})

    def _ensure_server(self) -> None:
        """Start whisper-server, or restart it if the process has died —
        otherwise every later block fails with 'Connection refused'."""
        if self.server is not None and self.server.alive():
            return
        if self.server is not None:
            log.error("whisper-server died (rc=%s) — restarting",
                      self.server.proc.returncode)
            self.server.stop()
            self.server = None
        model = T.ensure_model(self.settings.get("model", "large-v3-turbo"))
        vad = T.ensure_model(T.VAD_MODEL, T.VAD_MODEL_URL)
        server = WhisperServer(model, vad)
        try:
            server.wait_ready()
        except Exception:
            server.stop()
            raise
        self.server = server

    def _transcribe(self, bs: int, be: int, cleaned: list[dict]) -> list[dict]:
        """Transcribe one PCM window; returns attributed lines (absolute ms)."""
        pcm = self._read_slice(bs, be)
        if not pcm:
            return []
        try:
            segs = self.server.transcribe(pcm, self.language)
        except Exception:
            if self.server.alive():
                log.exception("Inference failed for [%d..%d]", bs, be)
                return []
            # The server crashed on this window: restart and retry once.
            self._ensure_server()
            try:
                segs = self.server.transcribe(pcm, self.language)
            except Exception:
                log.exception("Inference failed for [%d..%d]", bs, be)
                return []
        lines = []
        for seg in segs:
            text = seg["text"]
            if be - bs <= T.HALLUCINATION_BLOCK_MS and T.is_hallucination(text):
                log.info("Dropped hallucination %r", text)
                continue
            t0 = bs + int(seg["start"] * 1000)
            t1 = bs + int(seg["end"] * 1000)
            lines.append({"start_ms": t0, "end_ms": t1,
                          "speaker": T.attribute(t0, t1, cleaned),
                          "text": text})
        return lines

    def _emit_final(self, lines: list[dict], end_ms: int,
                    done: int | None = None) -> None:
        msg: dict = {"type": "live", "lines": lines}
        if done is not None:
            msg["done"] = done
        self.send(msg)
        if not lines:
            return
        try:
            with open(self.md_path, "a", encoding="utf-8") as f:
                for l in lines:
                    f.write(f"[{T.fmt(l['start_ms'])}] {l['speaker']}: "
                            f"{l['text']}\n")
        except OSError:
            log.exception("Cannot append %s", self.md_path)
        # Mirror into SQLite so the editor page can follow the call live.
        # These rows are a preview: the final transcription replaces them
        # (replace_lines deletes edited=0 rows).
        try:
            db = dvtdb.connect()
            rec_id = dvtdb.upsert_recording(db, self.audio_path.stem,
                                            str(self.audio_path.parent),
                                            self.started_at, end_ms)
            dvtdb.append_lines(db, rec_id, [
                {"speaker_name": l["speaker"], "speaker_line": l["text"],
                 "start_dt_ms": l["start_ms"], "end_dt_ms": l["end_ms"]}
                for l in lines])
            db.close()
        except Exception:
            log.exception("Live DB write failed")
