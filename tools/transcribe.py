#!/usr/bin/env python3
"""Local Discord call transcription with speaker attribution.

Usage:
    python3 tools/transcribe.py <recording>.webm [--model NAME] [--language XX]
                                [--report --claude <instance>]

Requires ffmpeg and whisper-cli on PATH. A matching <base>.speakers.json must
sit next to the audio file. Outputs: SQLite rows (tools/dvtdb.py),
<base>.transcript.md, and optionally <base>.report.md via `claude -p`.
Prepends Homebrew and ~/.local/bin to PATH: native hosts inherit a minimal
environment from Chrome.
"""
from __future__ import annotations
import argparse
import json
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
import dvtdb

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{m}.bin"

def ensure_model(name: str) -> Path:
    path = MODELS_DIR / f"ggml-{name}.bin"
    if path.exists():
        return path
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    url = MODEL_URL.format(m=name)
    print(f"Lejupielādēju modeli {name} … ({url})")
    tmp = path.with_suffix(".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.rename(path)
    return path

def run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"Komanda neizdevās: {' '.join(cmd)}\n{r.stderr[-2000:]}")

def fmt(ms: float) -> str:
    s = int(ms // 1000)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"

NOISE_MS = 700
SAME_SPEAKER_GAP = 500

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
    ap.add_argument("--claude", default="claude",
                    help="claude CLI binārijs --report solim: claude | claude-personal | claude-rgp | pilns ceļš")
    ap.add_argument("--report", action="store_true",
                    help="pēc transkripcijas izlaist caur Claude (claude -p): "
                         "kļūdu labošana + kopsavilkums + action items")
    args = ap.parse_args()

    for one in args.audio:
        process(one, args)

def process(audio_arg: Path, args) -> None:
    audio = audio_arg.expanduser().resolve()
    base = audio.with_suffix("")
    speakers_file = Path(str(base) + ".speakers.json")
    if not audio.exists():
        sys.exit(f"Nav faila: {audio}")
    if not speakers_file.exists():
        print(f"Izlaižu {audio.name}: nav {speakers_file.name}")
        return
    print(f"\n=== {audio.name} ===")

    data = json.loads(speakers_file.read_text())
    raw = data["intervals"]
    intervals = clean_intervals(raw)
    duration_ms = data.get("duration_ms", 0)
    print(f"Runātāju intervāli: {len(raw)} -> {len(intervals)} pēc tīrīšanas "
          f"(troksnis <{NOISE_MS}ms izmests)")
    model = ensure_model(args.model)

    blocks = build_blocks(intervals, duration_ms)

    segments = []
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "audio.wav"
        print("Konvertēju audio …")
        run(["ffmpeg", "-y", "-v", "error", "-i", str(audio),
             "-ar", "16000", "-ac", "1", str(wav)])
        print(f"Transkribēju ar whisper.cpp ({args.model}), {len(blocks)} bloki …")
        for i, (bs, be) in enumerate(blocks):
            piece = Path(td) / f"piece{i}.wav"
            out = Path(td) / f"piece{i}"
            run(["ffmpeg", "-y", "-v", "error", "-ss", str(bs / 1000),
                 "-to", str(be / 1000), "-i", str(wav), str(piece)])
            run(["whisper-cli", "-m", str(model), "-f", str(piece),
                 "-l", args.language, "-oj", "-of", str(out), "-np", "-mc", "0"])
            json_file = out.with_suffix(".json")
            if not json_file.exists():
                print(f"  bloks {i + 1}/{len(blocks)} NEIZDEVĀS (nav {json_file.name}), izlaižu")
                continue
            trans = json.loads(json_file.read_text())["transcription"]
            for seg in trans:
                text = seg["text"].strip()
                if text:
                    segments.append((bs + seg["offsets"]["from"],
                                     bs + seg["offsets"]["to"], text))
            print(f"  bloks {i + 1}/{len(blocks)} [{fmt(bs)}–{fmt(be)}] gatavs")

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

    db = dvtdb.connect()
    rec_id = dvtdb.upsert_recording(db, base.name, str(audio.parent),
                                    data.get("started_at"), duration_ms)
    dvtdb.replace_lines(db, rec_id, rows)
    db.close()
    print(f"SQLite: {dvtdb.DB_PATH} ({len(rows)} rindas, ieraksts '{base.name}')")

    lines = [f"[{fmt(r['start_dt_ms'])}] {r['speaker_name']}: {r['speaker_line']}"
             for r in rows]
    out_file = Path(str(base) + ".transcript.md")
    header = f"# Discord zvans {base.name}\n\nRunātāji: " + ", ".join(
        sorted({iv['name'] for iv in intervals})) + "\n\n"
    out_file.write_text(header + "\n".join(lines) + "\n")
    print(f"Gatavs: {out_file}\n")
    print("\n".join(lines))

    if args.report:
        print(f"\nSūtu Claude uz salabošanu un kopsavilkumu ({args.claude} -p) …")
        prompt = (
            "Šis ir automātisks Whisper transkripts no Discord zvana ar runātāju "
            "atribūciju pēc laika zīmogiem. Runa brīvi jaucas starp latviešu, krievu "
            "un angļu valodu — tas ir normāli.\n\n"
            "Uzdevums:\n"
            "1. Salabo acīmredzamas atpazīšanas kļūdas pēc konteksta, bet NEizdomā "
            "saturu, kura tur nav, un saglabā valodu maisījumu, kā runāts.\n"
            "2. Saglabā formātu [HH:MM:SS] vārds: teksts.\n"
            "3. Beigās pievieno sadaļas: Kopsavilkums, Lēmumi, Action items "
            "(ja tādu nav — raksti 'nav').\n"
            "4. Atbildē izvadi TIKAI gatavo dokumentu — bez ievada, bez "
            "komentāriem par uzdevumu, bez noslēguma frāzēm.\n\n"
            "Transkripts:\n\n" + out_file.read_text()
        )
        env = os.environ.copy()
        cmd = [args.claude, "-p"]
        m = re.fullmatch(r"claude-([\w.-]+)", args.claude)
        if m:
            env["CLAUDE_CONFIG_DIR"] = str(Path.home() / f".claude-{m.group(1)}")
            cmd = ["claude", "-p"]
        resolved = shutil.which(cmd[0])
        if resolved:
            cmd[0] = resolved
        r = subprocess.run(cmd, input=prompt,
                           capture_output=True, text=True, env=env)
        if r.returncode != 0:
            sys.exit(f"claude -p neizdevās (rc={r.returncode}):\n"
                     f"stderr: {r.stderr[-1000:]}\nstdout: {r.stdout[-1000:]}")
        report_file = Path(str(base) + ".report.md")
        report_file.write_text(r.stdout)
        print(f"Gatavs: {report_file}")

if __name__ == "__main__":
    main()
