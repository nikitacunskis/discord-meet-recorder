#!/usr/bin/env python3
"""Local Discord call transcription with speaker attribution.

Usage:
    python3 tools/transcribe.py <recording>.webm [--model NAME] [--language XX]
                                [--report --ai-provider claude --ai-instance <x>]

Requires ffmpeg and whisper-cli on PATH. A matching <base>.speakers.json must
sit next to the audio file. Outputs: SQLite rows (tools/dvtdb.py),
<base>.transcript.md, and optionally report sections via the configured AI
provider (tools/ai_providers.py; only Claude for now).
Prepends Homebrew and ~/.local/bin to PATH: native hosts inherit a minimal
environment from Chrome.
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

os.environ["PATH"] = ":".join([
    "/opt/homebrew/bin",
    "/usr/local/bin",
    os.path.expanduser("~/.local/bin"),
    os.environ.get("PATH", ""),
])

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_providers
import dvtdb

LOG_FILE = dvtdb.DB_PATH.parent / "transcribe.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("transcribe")

import builtins

# Windows console/pipe encoding is a legacy codepage (e.g. cp1251) that cannot
# represent the Latvian status messages — switch stdout/stderr to UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

def print(*args, **kwargs):  # noqa: A001 — deliberate module-wide shadow
    """stdout is a pipe to the native host, and the host can die mid-run
    (panel or Chrome closed). Console output must never abort the pipeline:
    on the first broken pipe, route stdout to /dev/null and keep going —
    the log file still records everything."""
    try:
        builtins.print(*args, **kwargs)
    except (BrokenPipeError, OSError):
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass

def fail(msg: str) -> None:
    """Log a fatal error and exit with it (replaces bare sys.exit(msg))."""
    log.error(msg)
    sys.exit(msg)

REPORT_LANGS = {"en": "English", "lv": "Latvian", "de": "German",
                "zh": "Chinese", "ru": "Russian"}

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{m}.bin"
VAD_MODEL = "silero-v5.1.2"
VAD_MODEL_URL = "https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-{m}.bin"

def ensure_model(name: str, url_tmpl: str = MODEL_URL) -> Path:
    path = MODELS_DIR / f"ggml-{name}.bin"
    if path.exists():
        return path
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    url = url_tmpl.format(m=name)
    print(f"Lejupielādēju modeli {name} … ({url})")
    tmp = path.with_suffix(".part")
    try:
        urllib.request.urlretrieve(url, tmp)
        tmp.rename(path)
    except Exception:
        log.exception("Stage 'model download' failed (%s)", url)
        sys.exit(f"Modeļa lejupielāde neizdevās: {url} (skat. {LOG_FILE})")
    return path

def run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log.error("Command failed (rc=%d): %s\nstderr: %s",
                  r.returncode, " ".join(cmd), r.stderr[-2000:])
        sys.exit(f"Komanda neizdevās: {' '.join(cmd)}\n{r.stderr[-2000:]}")

def fmt(ms: float) -> str:
    s = int(ms // 1000)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"

NOISE_MS = 700
SAME_SPEAKER_GAP = 500

# Non-speech audio makes whisper emit one word looped ("boom, boom, boom",
# "поп-поп-поп"). Language-agnostic; only trusted for SHORT blocks, where a
# real chant/echo is unlikely. Everything else is left to Silero VAD — token
# probabilities can't help here (whisper hallucinates confidently, measured
# avg_p 0.90 on noise vs 0.82 on real speech).
HALLUCINATION_BLOCK_MS = 3500

def is_hallucination(text: str) -> bool:
    words = re.sub(r"[^\w\s]", " ", text.lower()).split()
    return not words or (len(words) >= 4 and len(set(words)) == 1)

def clean_intervals(intervals: list[dict]) -> list[dict]:
    """Merge one speaker's flickering intervals (gap <= SAME_SPEAKER_GAP),
    then drop intervals shorter than NOISE_MS. Merge must run first: the
    Discord indicator flickers during real speech.
    """
    by_key: dict = {}
    for iv in sorted(intervals, key=lambda i: i["start_ms"]):
        key = iv.get("userId") or iv["name"]
        merged = by_key.setdefault(key, [])
        if merged and iv["start_ms"] - merged[-1]["end_ms"] <= SAME_SPEAKER_GAP:
            merged[-1]["end_ms"] = max(merged[-1]["end_ms"], iv["end_ms"])
        else:
            merged.append(dict(iv))
    out = [iv for ivs in by_key.values() for iv in ivs
           if iv["end_ms"] - iv["start_ms"] >= NOISE_MS]
    return sorted(out, key=lambda i: i["start_ms"])

def attribute(seg_start: float, seg_end: float, intervals: list[dict]) -> str:
    """Attribute a transcript segment to a speaker by interval overlap.

    Overlap heuristic: the segment start belongs to whoever started speaking
    first, the end to whoever finished last; returns "A → B" when both hold a
    significant share (>=25% each, dominant <75%), otherwise the dominant name.
    Falls back to the nearest preceding speaker within 3 s, else "(?)".
    """
    per_name: dict = {}
    for iv in intervals:
        ov = min(seg_end, iv["end_ms"]) - max(seg_start, iv["start_ms"])
        if ov <= 0:
            continue
        e = per_name.setdefault(iv["name"], {"ov": 0, "first_start": iv["start_ms"],
                                             "last_end": iv["end_ms"]})
        e["ov"] += ov
        e["first_start"] = min(e["first_start"], iv["start_ms"])
        e["last_end"] = max(e["last_end"], iv["end_ms"])

    if not per_name:
        prev, prev_gap = None, float("inf")
        for iv in intervals:
            if iv["end_ms"] <= seg_start and seg_start - iv["end_ms"] < prev_gap:
                prev, prev_gap = iv["name"], seg_start - iv["end_ms"]
        return f"{prev} (?)" if prev and prev_gap < 3000 else "(?)"

    total = max(1.0, seg_end - seg_start)
    ranked = sorted(per_name.items(), key=lambda kv: -kv[1]["ov"])
    top_name, top = ranked[0]
    if len(ranked) == 1 or top["ov"] >= 0.75 * total:
        return top_name

    starter = min(per_name.items(), key=lambda kv: kv[1]["first_start"])
    finisher = max(per_name.items(), key=lambda kv: kv[1]["last_end"])
    if starter[0] != finisher[0] and \
            starter[1]["ov"] >= 0.25 * total and finisher[1]["ov"] >= 0.25 * total:
        return f"{starter[0]} → {finisher[0]}"
    return top_name

def build_blocks(intervals: list[dict], duration_ms: int,
                 pad: int = 400, gap: int = 2000, max_len: int = 60000) -> list[tuple]:
    """Build speech blocks from the speaker timeline: intervals padded by
    `pad` ms, gaps <= `gap` ms merged, blocks capped at `max_len` ms.
    Silence/music between blocks is never transcribed (Whisper hallucination
    source); each block gets independent language detection and no context.
    """
    if not intervals:
        return [(0, duration_ms)] if duration_ms else []
    regs: list[list[int]] = []
    for iv in sorted(intervals, key=lambda i: i["start_ms"]):
        s = max(0, iv["start_ms"] - pad)
        e = min(duration_ms or iv["end_ms"] + pad, iv["end_ms"] + pad)
        if regs and s - regs[-1][1] <= gap:
            regs[-1][1] = max(regs[-1][1], e)
        else:
            regs.append([s, e])
    blocks: list[tuple] = []
    for s, e in regs:
        if e - s < 250:
            continue
        while e - s > max_len:
            blocks.append((s, s + max_len))
            s += max_len
        blocks.append((s, e))
    return blocks

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", type=Path, nargs="+")
    ap.add_argument("--model", default="large-v3-turbo",
                    help="whisper.cpp modelis (noklusēti large-v3-turbo)")
    ap.add_argument("--language", default="auto")
    ap.add_argument("--no-vad", action="store_true",
                    help="izslēgt Silero VAD priekšfiltru (transkribēt visus blokus)")
    ap.add_argument("--ai-provider", default="claude",
                    help="AI provider report soļiem (pašlaik tikai: claude)")
    ap.add_argument("--ai-instance", default="",
                    help="provider instance, piem. claude | claude-personal | "
                         "pilns ceļš (noklusēti: provider noklusētā)")
    ap.add_argument("--claude", default=None,
                    help="DEPRECATED: īsceļš uz --ai-provider claude --ai-instance <x>")
    ap.add_argument("--report-lang", default="en",
                    help="report language code (matches the panel UI language)")
    ap.add_argument("--report-only", action="store_true",
                    help="skip transcription; only (re)generate the report from the existing transcript")
    ap.add_argument("--fix-convo", action="store_true",
                    help="Claude pre-pass before the report sections: merge sentence "
                         "fragments split by Discord voice detection and fix clearly "
                         "mis-attributed speakers")
    ap.add_argument("--report", action="store_true",
                    help="generate the Summary section via claude -p")
    ap.add_argument("--decisions", action="store_true",
                    help="generate the Decisions section via claude -p")
    ap.add_argument("--actions", action="store_true",
                    help="generate the Action items section via claude -p")
    ap.add_argument("--threads", action="store_true",
                    help="split the call into discussion threads (topics): each gets "
                         "an AI-generated name plus its own summary/decisions/action "
                         "items, and transcript lines are linked to their thread")
    ap.add_argument("--set-title", action="store_true",
                    help="Claude post-pass after the report sections: set a short "
                         "thematic title (never overwrites an existing title)")
    args = ap.parse_args()
    if args.claude:  # legacy flag from older setups/scripts
        args.ai_provider, args.ai_instance = "claude", args.claude

    provider = ai_providers.get(args.ai_provider)
    if (args.fix_convo or args.report or args.decisions or args.actions
            or args.threads or args.set_title) \
            and (provider is None or not provider.available(args.ai_instance)):
        # No AI CLI on this machine is a normal setup, not an error:
        # quietly drop every AI stage and keep the plain transcript.
        log.info("AI provider %r unavailable — AI stages skipped", args.ai_provider)
        args.fix_convo = args.report = args.decisions = False
        args.actions = args.threads = args.set_title = False

    log.info("Run: audio=%s report_only=%s fix_convo=%s report=%s decisions=%s "
             "actions=%s threads=%s set_title=%s model=%s language=%s report_lang=%s ai=%s",
             [str(a) for a in args.audio], args.report_only, args.fix_convo,
             args.report, args.decisions, args.actions, args.threads, args.set_title,
             args.model, args.language, args.report_lang, ai_name(args))
    for one in args.audio:
        try:
            process(one, args)
        except SystemExit as e:
            if e.code not in (None, 0):
                log.error("process(%s) aborted: %s", one, e.code)
                mark_error(one)
            raise
        except Exception:
            log.exception("Unhandled error while processing %s", one)
            mark_error(one)
            sys.exit(f"Neparedzēta kļūda apstrādājot {one} (skat. {LOG_FILE})")

def set_status(base: Path, status: str) -> None:
    """Best-effort recordings.status update; must never break the pipeline."""
    try:
        db = dvtdb.connect()
        dvtdb.set_status(db, base.name, status)
        db.close()
    except Exception:
        log.exception("Cannot set status %r for %s", status, base.name)

def mark_error(audio_arg: Path) -> None:
    set_status(audio_arg.expanduser().resolve().with_suffix(""), "error")

def process(audio_arg: Path, args) -> None:
    audio = audio_arg.expanduser().resolve()
    base = audio.with_suffix("")
    speakers_file = Path(str(base) + ".speakers.json")
    if not audio.exists():
        fail(f"Nav faila: {audio}")
    if not speakers_file.exists():
        log.warning("Skipping %s: no %s", audio.name, speakers_file.name)
        print(f"Izlaižu {audio.name}: nav {speakers_file.name}")
        return
    print(f"\n=== {audio.name} ===")
    log.info("Processing %s", audio)

    if args.report_only:
        out_file = Path(str(base) + ".transcript.md")
        if not out_file.exists():
            log.warning("Skipping %s: no %s", audio.name, out_file.name)
            print(f"Izlaižu {audio.name}: nav {out_file.name}")
            return
        run_postprocess(base, out_file, args)
        return

    # The host sets 'transcribing' when the user presses stop; repeat it here
    # for plain-CLI runs (no-op while the row does not exist yet).
    set_status(base, "transcribing")

    try:
        data = json.loads(speakers_file.read_text())
        raw = data["intervals"]
        intervals = clean_intervals(raw)
    except Exception:
        log.exception("Stage 'speakers parse' failed (%s)", speakers_file)
        sys.exit(f"Nederīgs {speakers_file.name} (skat. {LOG_FILE})")
    duration_ms = data.get("duration_ms", 0)
    print(f"Runātāju intervāli: {len(raw)} -> {len(intervals)} pēc tīrīšanas "
          f"(troksnis <{NOISE_MS}ms izmests)")
    model = ensure_model(args.model)
    vad_model = None if args.no_vad else ensure_model(VAD_MODEL, VAD_MODEL_URL)

    blocks = build_blocks(intervals, duration_ms)

    segments = []
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "audio.wav"
        print("Konvertēju audio …")
        log.info("Stage 'audio convert': %s -> wav", audio.name)
        run(["ffmpeg", "-y", "-v", "error", "-i", str(audio),
             "-ar", "16000", "-ac", "1", str(wav)])
        print(f"Transkribēju ar whisper.cpp ({args.model}), {len(blocks)} bloki …")
        log.info("Stage 'transcription': %d blocks, model=%s", len(blocks), args.model)
        for i, (bs, be) in enumerate(blocks):
            piece = Path(td) / f"piece{i}.wav"
            out = Path(td) / f"piece{i}"
            run(["ffmpeg", "-y", "-v", "error", "-ss", str(bs / 1000),
                 "-to", str(be / 1000), "-i", str(wav), str(piece)])
            cmd = ["whisper-cli", "-m", str(model), "-f", str(piece),
                   "-l", args.language, "-oj", "-of", str(out), "-np", "-mc", "0"]
            if vad_model:
                cmd += ["--vad", "-vm", str(vad_model)]
            run(cmd)
            json_file = out.with_suffix(".json")
            if not json_file.exists():
                log.error("Block %d/%d failed: missing %s", i + 1, len(blocks), json_file.name)
                print(f"  bloks {i + 1}/{len(blocks)} NEIZDEVĀS (nav {json_file.name}), izlaižu")
                continue
            try:
                trans = json.loads(json_file.read_text())["transcription"]
            except Exception:
                log.exception("Block %d/%d failed: bad whisper JSON %s",
                              i + 1, len(blocks), json_file.name)
                print(f"  bloks {i + 1}/{len(blocks)} NEIZDEVĀS (nederīgs JSON), izlaižu")
                continue
            for seg in trans:
                text = seg["text"].strip()
                if not text:
                    continue
                if be - bs <= HALLUCINATION_BLOCK_MS and is_hallucination(text):
                    log.info("Block %d/%d: dropped hallucination %r",
                             i + 1, len(blocks), text)
                    print(f"  bloks {i + 1}/{len(blocks)} izmests kā halucinācija: {text!r}")
                    continue
                segments.append((bs + seg["offsets"]["from"],
                                 bs + seg["offsets"]["to"], text))
            print(f"  bloks {i + 1}/{len(blocks)} [{fmt(bs)}–{fmt(be)}] gatavs")
    log.info("Stage 'transcription' done: %d segments", len(segments))

    try:
        rows = []
        prev_text = None
        repeats = 0
        for t0, t1, text in segments:
            if text == prev_text:
                repeats += 1
                if repeats >= 2:
                    continue
            else:
                repeats = 0
            prev_text = text
            rows.append({"speaker_name": attribute(t0, t1, intervals),
                         "start_dt_ms": int(t0), "end_dt_ms": int(t1),
                         "speaker_line": text})
    except Exception:
        log.exception("Stage 'speaker merge' failed (%s)", base.name)
        sys.exit(f"Runātāju piesaiste neizdevās (skat. {LOG_FILE})")

    try:
        db = dvtdb.connect()
        rec_id = dvtdb.upsert_recording(db, base.name, str(audio.parent),
                                        data.get("started_at"), duration_ms)
        dvtdb.replace_lines(db, rec_id, rows)
        db.close()
        print(f"SQLite: {dvtdb.DB_PATH} ({len(rows)} rindas, ieraksts '{base.name}')")
    except Exception:
        # Non-fatal: the transcript file below is written from `rows`, not the DB.
        log.exception("Stage 'DB write' failed (%s)", base.name)
        print(f"SQLite ieraksts neizdevās — turpinu ar failu (skat. {LOG_FILE})")

    lines = [f"[{fmt(r['start_dt_ms'])}] {r['speaker_name']}: {r['speaker_line']}"
             for r in rows]
    out_file = Path(str(base) + ".transcript.md")
    header = f"# Discord zvans {base.name}\n\nRunātāji: " + ", ".join(
        sorted({iv['name'] for iv in intervals})) + "\n\n"
    try:
        out_file.write_text(header + "\n".join(lines) + "\n")
    except OSError:
        log.exception("Stage 'transcript write' failed (%s)", out_file)
        log.error("Transcript that could not be written:\n%s", "\n".join(lines))
        sys.exit(f"Nevar ierakstīt {out_file} (transkripts saglabāts {LOG_FILE})")
    print(f"Gatavs: {out_file}\n")
    print("\n".join(lines))
    log.info("Stage 'transcript write' done: %s (%d lines)", out_file, len(lines))

    run_postprocess(base, out_file, args)

def run_postprocess(base: Path, out_file: Path, args) -> None:
    """AI post-processor state machine, after transcription: autofix →
    thread split → report chain for EACH thread, then for the whole record
    (the pre-threads pipeline, unchanged) → auto title. Disabled stages are
    skipped; the machine always ends at status 'done'."""
    if args.fix_convo:
        set_status(base, "ai_postprocess_autofix")
        fix_conversation(base, out_file, args)
    if args.threads:
        set_status(base, "ai_postprocess_threads")
        split_threads(base, args)
    if args.report or args.decisions or args.actions:
        set_status(base, "ai_postprocess_report")
        if args.threads:
            generate_thread_reports(base, args)
        generate_report(base, out_file, args)
    if args.set_title:
        set_status(base, "ai_postprocess_title")
        generate_title(base, out_file, args)
    set_status(base, "done")

def ai_name(args) -> str:
    """Human-readable provider[:instance] for logs and progress lines."""
    if args.ai_instance and args.ai_instance != args.ai_provider:
        return f"{args.ai_provider}:{args.ai_instance}"
    return args.ai_provider

def call_ai(prompt: str, args, stage: str):
    """Run the configured AI provider on `prompt` and parse the JSON answer.
    Returns (parsed, raw); parsed is None on any failure (missing CLI,
    timeout, non-zero exit, non-JSON) — details go to the log and stdout.
    A machine without the provider's CLI is a normal setup, not an error."""
    name = ai_name(args)
    provider = ai_providers.get(args.ai_provider)
    if provider is None:
        log.info("Stage '%s': unknown AI provider %r — skipped", stage, args.ai_provider)
        return None, ""
    try:
        r = provider.run(prompt, args.ai_instance)
    except FileNotFoundError:
        # CLI vanished between the main() check and this call (or a full
        # path was given that does not exist). Not an error — skip.
        log.info("Stage '%s': %s CLI not found — skipped", stage, name)
        return None, ""
    except subprocess.TimeoutExpired:
        log.error("Stage '%s': %s timed out after %ds", stage, name, ai_providers.TIMEOUT_S)
        print(f"{name} timed out after {ai_providers.TIMEOUT_S}s — {stage} skipped, "
              "re-run with --report-only")
        return None, ""
    if r.returncode != 0:
        log.error("Stage '%s': %s failed (rc=%d)\nstderr: %s\nstdout: %s",
                  stage, name, r.returncode, r.stderr[-800:], r.stdout[-800:])
        print(f"{name} failed (rc={r.returncode}) — {stage} skipped, "
              f"re-run with --report-only\n"
              f"stderr: {r.stderr[-800:]}\nstdout: {r.stdout[-800:]}")
        return None, r.stdout
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", r.stdout.strip())
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError:
        log.error("Stage '%s': non-JSON output from %s:\n%s",
                  stage, name, r.stdout[-4000:])
        print(f"{name} returned non-JSON output — {stage} skipped, "
              "re-run with --report-only")
        return None, raw

def rewrite_transcript(base: Path, out_file: Path, lines: list[dict]) -> None:
    header = f"# Discord zvans {base.name}\n\nRunātāji: " + ", ".join(
        sorted({l["speaker_name"] for l in lines})) + "\n\n"
    body = "\n".join(f"[{fmt(l['start_dt_ms'])}] {l['speaker_name']}: {l['speaker_line']}"
                     for l in lines)
    out_file.write_text(header + body + "\n")

def fix_conversation(base: Path, out_file: Path, args) -> None:
    """Claude pre-pass over the raw transcript, before any report section.

    Discord's per-user voice detection splits one sentence into consecutive
    fragment lines, and the overlap heuristic sometimes attributes a line to
    the wrong speaker. Claude proposes merges of consecutive same-speaker
    fragments and speaker reattributions it is certain about; anything
    uncertain is left alone. Hand-edited lines (edited=1) are never touched,
    and auto-fixes do not set edited=1, so re-transcription can redo them.
    Applies the fixes to SQLite and rewrites out_file for the later stages.
    """
    print(f"\nConvo fix via {ai_name(args)} …")
    log.info("Stage 'convo fix': base=%s ai=%s", base.name, ai_name(args))
    try:
        db = dvtdb.connect()
    except Exception:
        log.exception("Stage 'convo fix' skipped: cannot open DB")
        print(f"Convo fix skipped — DB unavailable (skat. {LOG_FILE})")
        return
    try:
        rec = dvtdb.get_recording(db, base.name)
        if rec is None:
            log.warning("Stage 'convo fix' skipped: '%s' not in DB", base.name)
            print("Convo fix skipped — recording not in DB")
            return
        lines = dvtdb.get_lines(db, rec["id"])
        if len(lines) < 2:
            return
        speakers = sorted({l["speaker_name"] for l in lines})
        listing = "\n".join(
            f"{l['id']} | [{fmt(l['start_dt_ms'])}-{fmt(l['end_dt_ms'])}] | "
            f"{l['speaker_name']} | {l['speaker_line']}" for l in lines)
        prompt = (
            "You are given an automatic Whisper transcript of a Discord call, "
            "one line per row in the form: id | [start-end] | speaker | text. "
            "Speech may freely mix several languages.\n\n"
            "Discord's voice detection often splits ONE sentence by ONE speaker "
            "into several consecutive fragment lines, and occasionally a line "
            "is attributed to the wrong speaker.\n"
            "Known speakers: " + ", ".join(speakers) + "\n\n"
            "Output STRICT JSON only — no markdown, no code fences:\n"
            '{"merges": [[id, id, ...], ...], '
            '"speaker_fixes": [{"id": id, "speaker": "name"}, ...]}\n\n'
            "Rules:\n"
            "- merges: groups of CONSECUTIVE line ids from the SAME speaker whose "
            "texts clearly form one sentence or thought that was split apart. "
            "Never merge different speakers, never reorder, never rewrite text.\n"
            "- speaker_fixes: reattribute a line to another known speaker ONLY "
            "when the surrounding context makes it certain beyond doubt (e.g. an "
            "obvious mid-sentence continuation of the previous speaker). If you "
            "are not completely sure, leave the line alone — doing nothing is "
            "always acceptable.\n"
            "- Empty arrays are fine. Never invent ids or speaker names.\n\n"
            "Transcript:\n\n" + listing
        )
        rep, _raw = call_ai(prompt, args, "convo fix")
        if not isinstance(rep, dict):
            return
        by_id = {l["id"]: dict(l) for l in lines}
        pos = {l["id"]: i for i, l in enumerate(lines)}
        n_sp = n_mg = 0
        for f in rep.get("speaker_fixes") or []:
            if not isinstance(f, dict):
                continue
            l = by_id.get(f.get("id"))
            sp = f.get("speaker")
            if not l or l["edited"] or sp not in speakers or sp == l["speaker_name"]:
                continue
            dvtdb.set_line_speaker(db, l["id"], sp)
            l["speaker_name"] = sp
            n_sp += 1
        merged_ids: set = set()
        for group in rep.get("merges") or []:
            if not isinstance(group, list) or len(group) < 2:
                continue
            rows = [by_id.get(i) for i in group]
            if any(r is None or r["edited"] or r["id"] in merged_ids for r in rows):
                continue
            rows = sorted(rows, key=lambda r: pos[r["id"]])
            if len({r["speaker_name"] for r in rows}) != 1:
                continue
            idxs = [pos[r["id"]] for r in rows]
            if idxs != list(range(idxs[0], idxs[0] + len(idxs))):
                continue  # fragments must be adjacent lines
            text = " ".join(r["speaker_line"].strip() for r in rows)
            dvtdb.merge_lines(db, rows[0]["id"], [r["id"] for r in rows[1:]],
                              text, max(r["end_dt_ms"] for r in rows))
            merged_ids.update(r["id"] for r in rows)
            n_mg += 1
        if n_sp or n_mg:
            try:
                rewrite_transcript(base, out_file, dvtdb.get_lines(db, rec["id"]))
            except OSError:
                log.exception("Stage 'convo fix': cannot rewrite %s", out_file)
                print(f"Convo fix: nevar pārrakstīt {out_file} (skat. {LOG_FILE})")
        print(f"Convo fix: {n_mg} merges, {n_sp} speaker fixes")
        log.info("Stage 'convo fix' done: %d merges, %d speaker fixes", n_mg, n_sp)
    except Exception:
        # Best-effort pre-pass: the raw transcript is still usable as is.
        log.exception("Stage 'convo fix' failed (%s)", base.name)
        print(f"Convo fix neizdevās — turpinu ar esošo transkriptu (skat. {LOG_FILE})")
    finally:
        db.close()

def generate_title(base: Path, out_file: Path, args) -> None:
    """Claude post-pass, after all report sections: give the recording a short
    thematic title. A title the user already set is never overwritten."""
    try:
        db = dvtdb.connect()
    except Exception:
        log.exception("Stage 'title' skipped: cannot open DB")
        print(f"Title skipped — DB unavailable (skat. {LOG_FILE})")
        return
    try:
        rec = dvtdb.get_recording(db, base.name)
        if rec is None:
            log.warning("Stage 'title' skipped: '%s' not in DB", base.name)
            print("Title skipped — recording not in DB")
            return
        if rec["title"]:
            log.info("Stage 'title' skipped: '%s' already titled %r",
                     base.name, rec["title"])
            return
        try:
            transcript = out_file.read_text()
        except OSError:
            log.exception("Stage 'title' failed: cannot read %s", out_file)
            print(f"Nevar nolasīt {out_file} — title skipped (skat. {LOG_FILE})")
            return
        lang_name = REPORT_LANGS.get(args.report_lang, "English")
        print(f"\nGenerating title via {ai_name(args)} ({lang_name}) …")
        log.info("Stage 'title': base=%s lang=%s ai=%s",
                 base.name, lang_name, ai_name(args))
        prompt = (
            "Below is an automatic transcript of a Discord voice call. "
            f"Give the call a short title in {lang_name} that captures its main "
            "theme: 2-5 words, plain text, no quotes, no trailing period.\n"
            'Output STRICT JSON only — no markdown, no code fences: {"title": "..."}\n\n'
            + transcript
        )
        rep, _raw = call_ai(prompt, args, "title")
        title = str(rep.get("title", "")).strip()[:80] if isinstance(rep, dict) else ""
        if not title:
            return
        dvtdb.set_title(db, base.name, title, rec["dir"])
        print(f"Title: {title}")
        log.info("Stage 'title' done: %r", title)
    except Exception:
        log.exception("Stage 'title' failed (%s)", base.name)
        print(f"Title neizdevās (skat. {LOG_FILE})")
    finally:
        db.close()

def report_sections(args) -> list[tuple]:
    """(key, JSON-shape fragment) for every enabled report chain section,
    in chain order: Summary -> Decisions -> Action items."""
    wanted = []
    if args.report:
        wanted.append(("summary", '"summary": "..."'))
    if args.decisions:
        wanted.append(("decisions", '"decisions": ["..."]'))
    if args.actions:
        wanted.append(("action_items", '"action_items": ["..."]'))
    return wanted

def generate_report(base: Path, out_file: Path, args) -> None:
    """Ask claude -p for the requested report sections as strict JSON and
    store them in record_reports. Claude only returns text; all DB writes
    happen here. Sections that come back empty are not written; sections
    that were not requested keep their existing DB values.
    """
    wanted = report_sections(args)
    if not wanted:
        return
    lang_name = REPORT_LANGS.get(args.report_lang, "English")
    keys = ", ".join(k for k, _ in wanted)
    shape = "{" + ", ".join(s for _, s in wanted) + "}"
    print(f"\nGenerating report sections [{keys}] via {ai_name(args)} ({lang_name}) …")
    log.info("Stage 'report generation': sections=[%s] lang=%s ai=%s base=%s",
             keys, lang_name, ai_name(args), base.name)
    try:
        transcript = out_file.read_text()
    except OSError:
        log.exception("Stage 'report generation' failed: cannot read %s", out_file)
        print(f"Nevar nolasīt {out_file} — report skipped (skat. {LOG_FILE})")
        return
    prompt = (
        "You are given an automatic Whisper transcript of a Discord call "
        "with speaker attribution from timestamps. Speech may freely mix "
        "several languages within one sentence.\n\n"
        f"Produce meeting minutes written in {lang_name}. Output STRICT "
        "JSON only — no markdown, no code fences — with exactly this shape:\n"
        f"{shape}\n"
        "Empty strings/arrays are allowed when the call has no such content. "
        "Do not invent content that is not in the transcript.\n\n"
        "Transcript:\n\n" + transcript
    )
    rep, raw = call_ai(prompt, args, "report generation")
    if not isinstance(rep, dict):
        return
    requested = {k for k, _ in wanted}
    try:
        db = dvtdb.connect()
        rec = dvtdb.get_recording(db, base.name)
        if rec is None:
            raise LookupError(
                f"recording '{base.name}' not found in {dvtdb.DB_PATH} "
                "(DB deleted/regenerated?)")
        existing = dvtdb.get_report(db, rec["id"]) or \
            {"summary": "", "decisions": [], "action_items": []}
        summary = str(rep.get("summary", "")) if "summary" in requested else existing["summary"]
        decisions = [str(x) for x in rep.get("decisions", [])] \
            if "decisions" in requested else existing["decisions"]
        action_items = [str(x) for x in rep.get("action_items", [])] \
            if "action_items" in requested else existing["action_items"]
        dvtdb.set_report(db, rec["id"], summary, decisions, action_items)
        db.close()
    except Exception:
        # The claude -p call is expensive: never lose its output on a DB failure.
        log.exception("Stage 'report DB write' failed (%s)", base.name)
        log.error("Raw report JSON for '%s':\n%s", base.name, raw)
        report_file = Path(str(base) + ".report.md")
        try:
            report_file.write_text(raw + "\n")
            print(f"DB write failed — raw report saved to {report_file} "
                  f"(skat. {LOG_FILE})")
        except OSError:
            log.exception("Could not write fallback report file %s", report_file)
            print(f"DB write failed — raw report preserved only in {LOG_FILE}")
        return
    log.info("Stage 'report DB write' done: summary=%s, %d decisions, %d action items",
             "yes" if summary else "no", len(decisions), len(action_items))
    print(f"Stored in record_reports ({lang_name}): "
          f"summary={'yes' if summary else 'no'}, "
          f"{len(decisions)} decisions, {len(action_items)} action items")

def split_threads(base: Path, args) -> None:
    """Status 'ai_postprocess_threads': split the call into discussion
    threads (topics). Stores only the thread names (record_threads) and the
    line→thread links (text_lines.thread_id); each thread's own report
    sections are generated afterwards by generate_thread_reports. Topics
    interleave freely (A, B, A again): all segments of one topic form ONE
    thread, which is why the model returns line ids rather than time ranges.
    A single-topic call stores no threads — the whole-record report covers it.
    """
    lang_name = REPORT_LANGS.get(args.report_lang, "English")
    print(f"\nSplitting into discussion threads via {ai_name(args)} ({lang_name}) …")
    log.info("Stage 'thread split': base=%s lang=%s ai=%s", base.name, lang_name,
             ai_name(args))
    try:
        db = dvtdb.connect()
    except Exception:
        log.exception("Stage 'thread split' skipped: cannot open DB")
        print(f"Thread split skipped — DB unavailable (skat. {LOG_FILE})")
        return
    try:
        rec = dvtdb.get_recording(db, base.name)
        if rec is None:
            log.warning("Stage 'thread split' skipped: '%s' not in DB", base.name)
            print("Thread split skipped — recording not in DB")
            return
        lines = dvtdb.get_lines(db, rec["id"])
        if not lines:
            return
        listing = "\n".join(
            f"{l['id']} | [{fmt(l['start_dt_ms'])}-{fmt(l['end_dt_ms'])}] | "
            f"{l['speaker_name']} | {l['speaker_line']}" for l in lines)
        prompt = (
            "You are given an automatic Whisper transcript of a Discord call, "
            "one line per row in the form: id | [start-end] | speaker | text. "
            "Speech may freely mix several languages.\n\n"
            "Split the call into its distinct discussion topics (threads); a "
            "call usually has 1-5. Topics are often interleaved: the call may "
            "move to a second topic and later RETURN to the first one — all "
            "parts of one topic still form ONE thread.\n\n"
            f"For every thread produce:\n"
            f"- name: a short topic name in {lang_name} (2-6 words)\n"
            "- line_ids: ids of ALL transcript lines belonging to this topic, "
            "including later revisits\n\n"
            "Output STRICT JSON only — no markdown, no code fences:\n"
            '{"threads": [{"name": "...", "line_ids": [1, 2, 3]}]}\n\n'
            "Rules:\n"
            "- If the whole call is essentially ONE topic, return "
            '{"threads": []} — the whole-call report already covers it.\n'
            "- Each line belongs to at most one thread; unrelated small talk "
            "may be left unassigned.\n"
            "- Never invent line ids.\n\n"
            "Transcript:\n\n" + listing
        )
        rep, raw = call_ai(prompt, args, "thread split")
        if not isinstance(rep, dict):
            return
        known = {l["id"] for l in lines}
        claimed: set = set()
        threads = []
        for th in rep.get("threads") or []:
            if not isinstance(th, dict):
                continue
            name = str(th.get("name", "")).strip()
            if not name:
                continue
            ids = [i for i in th.get("line_ids") or []
                   if isinstance(i, int) and i in known and i not in claimed]
            claimed.update(ids)
            threads.append({"name": name[:120], "line_ids": ids})
        if len(threads) < 2:
            # A single-topic call needs no split — the whole-call report
            # already covers it. Storing [] also clears stale threads from a
            # previous run over an older transcript.
            threads = []
        try:
            dvtdb.set_threads(db, rec["id"], threads)
        except Exception:
            # The claude -p call is expensive: never lose its output on a DB failure.
            log.exception("Stage 'thread split DB write' failed (%s)", base.name)
            log.error("Raw thread split JSON for '%s':\n%s", base.name, raw)
            print(f"Thread split DB write failed — raw JSON preserved in {LOG_FILE}")
            return
        if not threads:
            log.info("Stage 'thread split' done: single-topic call, no threads stored")
            print("Thread split: single-topic call — only the recording report is kept")
            return
        n_lines = sum(len(t["line_ids"]) for t in threads)
        log.info("Stage 'thread split' done: %d threads, %d/%d lines assigned",
                 len(threads), n_lines, len(lines))
        print(f"Stored in record_threads: {len(threads)} threads, "
              f"{n_lines}/{len(lines)} lines assigned")
    except Exception:
        # Best-effort stage: transcript and whole-call report stay usable.
        log.exception("Stage 'thread split' failed (%s)", base.name)
        print(f"Thread split neizdevās (skat. {LOG_FILE})")
    finally:
        db.close()

def generate_thread_reports(base: Path, args) -> None:
    """Status 'ai_postprocess_report', part 1: the report chain — Summary →
    Decisions → Action items, as enabled — for EACH thread, one AI call per
    thread over only that thread's lines. Results land in record_reports
    under the thread's id. The whole-record chain (generate_report) runs
    after this, exactly as it did before threads existed."""
    wanted = report_sections(args)
    if not wanted:
        return
    lang_name = REPORT_LANGS.get(args.report_lang, "English")
    keys = ", ".join(k for k, _ in wanted)
    shape = "{" + ", ".join(s for _, s in wanted) + "}"
    requested = {k for k, _ in wanted}
    try:
        db = dvtdb.connect()
    except Exception:
        log.exception("Stage 'thread reports' skipped: cannot open DB")
        print(f"Thread reports skipped — DB unavailable (skat. {LOG_FILE})")
        return
    try:
        rec = dvtdb.get_recording(db, base.name)
        if rec is None:
            return
        threads = dvtdb.get_threads(db, rec["id"])
        if not threads:
            return
        lines = dvtdb.get_lines(db, rec["id"])
        print(f"\nGenerating thread reports [{keys}] via {ai_name(args)} ({lang_name}) …")
        log.info("Stage 'thread reports': %d threads, sections=[%s] lang=%s ai=%s base=%s",
                 len(threads), keys, lang_name, ai_name(args), base.name)
        for th in threads:
            th_lines = [l for l in lines if l["thread_id"] == th["id"]]
            if not th_lines:
                continue
            listing = "\n".join(
                f"[{fmt(l['start_dt_ms'])}] {l['speaker_name']}: {l['speaker_line']}"
                for l in th_lines)
            prompt = (
                f"You are given the lines of ONE discussion topic, \"{th['name']}\", "
                "extracted from an automatic Whisper transcript of a longer Discord "
                "call (parts may be non-consecutive in the original call). Speech "
                "may freely mix several languages.\n\n"
                f"Produce meeting minutes for THIS topic only, written in {lang_name}. "
                "Output STRICT JSON only — no markdown, no code fences — with "
                f"exactly this shape:\n{shape}\n"
                "Empty strings/arrays are allowed when the topic has no such "
                "content. Do not invent content that is not in the transcript.\n\n"
                "Topic lines:\n\n" + listing
            )
            rep, _raw = call_ai(prompt, args, f"thread report ({th['name']})")
            if not isinstance(rep, dict):
                continue
            summary = str(rep.get("summary", "")) if "summary" in requested else ""
            decisions = [str(x) for x in rep.get("decisions", [])] \
                if "decisions" in requested else []
            action_items = [str(x) for x in rep.get("action_items", [])] \
                if "action_items" in requested else []
            dvtdb.set_thread_report(db, rec["id"], th["id"], summary,
                                    decisions, action_items)
            print(f"  thread '{th['name']}': summary={'yes' if summary else 'no'}, "
                  f"{len(decisions)} decisions, {len(action_items)} action items")
        log.info("Stage 'thread reports' done")
    except Exception:
        log.exception("Stage 'thread reports' failed (%s)", base.name)
        print(f"Thread reports neizdevās (skat. {LOG_FILE})")
    finally:
        db.close()

if __name__ == "__main__":
    main()
