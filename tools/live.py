#!/usr/bin/env python3
"""Live (during-call) transcription for the native host.

The panel streams webm/opus chunks continuously; LiveTranscriber pipes every
chunk through a persistent `ffmpeg` process into a growing 16 kHz mono s16le
PCM sidecar file, so any [start, end] slice of the call is available in O(1)
without re-decoding the webm (which is only decodable from the start).

Speaker-timeline snapshots arrive as `events` messages; ones flagged
`cut: true` mark silence boundaries — safe places to split. A worker thread
transcribes each new region [done, boundary] with a resident `whisper-server`
(model loaded once per recording), attributes speakers by interval overlap
(reusing transcribe.py helpers), sends the lines to the panel as
{"type": "live", "lines": [...]}, and appends them to <base>.live.md.

Live output is a preview: the full-quality pipeline (tools/transcribe.py)
still runs over the whole recording on finish and is the source of truth.
"""
from __future__ import annotations
import io
import json
import logging
import shutil
import socket
import struct
import subprocess
import threading
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
MIN_PIECE_MS = 3000        # don't bother transcribing less than this
FORCED_CUT_MS = 45000      # no silence boundary for this long -> cut mid-speech
FORCED_TAIL_GUARD_MS = 1500
SERVER_START_TIMEOUT = 180  # model load can take a while on first run


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
        import time
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
        self.intervals: list[dict] = []
        self.started_at: str | None = None
        self.duration_ms = 0
        self.boundary_ms = 0
        self.done_ms = 0
        self.pcm_len = 0          # bytes written so far (under self.cond)
        self.stopping = False
        self.dead = False         # pipeline failed; feed() becomes a no-op
        self.server: WhisperServer | None = None

        if shutil.which("whisper-server") is None or shutil.which("ffmpeg") is None:
            raise RuntimeError("live: whisper-server/ffmpeg not on PATH")
        self.pcm_file = open(self.pcm_path, "wb")
        self.ffmpeg = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-i", "pipe:0",
             "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
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
            self.ffmpeg.stdin.write(data)
        except (BrokenPipeError, OSError):
            log.error("ffmpeg pipe broken — live transcription disabled")
            self.dead = True

    def on_events(self, speakers_json: str | None, cut: bool) -> None:
        """Cumulative speaker snapshot; cut=True marks a silence boundary."""
        if self.dead or not speakers_json:
            return
        try:
            data = json.loads(speakers_json)
        except Exception:
            return
        with self.cond:
            self.intervals = data.get("intervals") or []
            self.started_at = data.get("started_at") or self.started_at
            self.duration_ms = int(data.get("duration_ms") or 0)
            if cut:
                self.boundary_ms = self.duration_ms
            self.cond.notify()

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
        log.info("Live session finished for %s (transcribed to %d ms)",
                 self.audio_path.name, self.done_ms)

    # ---- internals ----

    def _read_pcm(self) -> None:
        try:
            while True:
                buf = self.ffmpeg.stdout.read(64 * 1024)
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

    def _pick_target(self) -> int:
        """Under self.cond: how far transcription may advance right now."""
        pcm_ms = self.pcm_len // BYTES_PER_MS
        target = self.boundary_ms if self.boundary_ms > self.done_ms else 0
        if pcm_ms - self.done_ms > FORCED_CUT_MS:
            target = max(target, pcm_ms - FORCED_TAIL_GUARD_MS)
        target = min(target, pcm_ms)
        return target if target - self.done_ms >= MIN_PIECE_MS else 0

    def _read_slice(self, start_ms: int, end_ms: int) -> bytes:
        with open(self.pcm_path, "rb") as f:
            f.seek(start_ms * BYTES_PER_MS)
            return f.read((end_ms - start_ms) * BYTES_PER_MS)

    def _work(self) -> None:
        try:
            while True:
                with self.cond:
                    while not self.stopping and self._pick_target() == 0:
                        self.cond.wait()
                    if self.stopping:
                        return
                    target = self._pick_target()
                    intervals = [dict(iv) for iv in self.intervals]
                    done = self.done_ms
                if self.server is None:
                    model = T.ensure_model(self.settings.get("model",
                                                            "large-v3-turbo"))
                    vad = T.ensure_model(T.VAD_MODEL, T.VAD_MODEL_URL)
                    self.server = WhisperServer(model, vad)
                    self.server.wait_ready()
                self._transcribe_region(done, target, intervals)
                with self.cond:
                    self.done_ms = target
        except Exception:
            log.exception("Live worker died")
            self.dead = True

    def _transcribe_region(self, start_ms: int, end_ms: int,
                           intervals: list[dict]) -> None:
        cleaned = T.clean_intervals(intervals)
        clipped = []
        for iv in cleaned:
            s, e = max(iv["start_ms"], start_ms), min(iv["end_ms"], end_ms)
            if e - s > 0:
                clipped.append({**iv, "start_ms": s, "end_ms": e})
        blocks = T.build_blocks(clipped, end_ms)
        blocks = [(max(bs, start_ms), be) for bs, be in blocks if be > start_ms]
        if not blocks:
            return
        log.info("Region [%d..%d]: %d block(s)", start_ms, end_ms, len(blocks))
        lines = []
        for bs, be in blocks:
            pcm = self._read_slice(bs, be)
            if not pcm:
                continue
            try:
                segs = self.server.transcribe(pcm, self.language)
            except Exception:
                log.exception("Inference failed for block [%d..%d]", bs, be)
                continue
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
        if not lines:
            return
        self.send({"type": "live", "lines": lines})
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
