"""Single SQLite store for Discord call transcripts.

Schema:
  recordings(id, base, dir, title, start_dt, end_dt, status)
    status is the pipeline state machine: 'recording' (row is created the
    moment recording starts) -> 'transcribing' (user pressed stop, end_dt is
    set, BE finishes the transcript) -> 'ai_postprocess_autofix' ->
    'ai_postprocess_threads' -> 'ai_postprocess_report' (per-thread report
    chains, then the whole-record one) -> 'ai_postprocess_title' -> 'done';
    'error' on a fatal pipeline failure. Disabled stages are skipped.
  record_threads(id, record_id, name)
    one row per discussion topic of a call; `name` is AI-generated
  text_lines(id, recording_id, speaker_name, speaker_line,
             start_dt_ms, end_dt_ms, edited, thread_id)
    thread_id links a line to the topic it belongs to (NULL = unassigned)
  record_reports(id, record_id, thread_id, summary, decision, action_item)
    long format: one row carries the summary, one row per decision,
    one row per action item; thread_id NULL = whole-call report,
    otherwise the section belongs to that thread

`base`/`dir` locate the files on disk; `edited=1` marks hand-edited lines that
re-transcription must not overwrite. connect() creates the schema and migrates
the legacy (recordings/utterances) layout in place.
"""
from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "recordings" / "dvt.sqlite"

def _migrate_legacy(db: sqlite3.Connection) -> None:
    names = {r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "utterances" not in names:
        return
    old_recs = [dict(r) for r in db.execute("SELECT * FROM recordings")]
    old_utts = [dict(r) for r in db.execute("SELECT * FROM utterances")]
    # One transaction for the whole migration: the DROPs only become permanent
    # once everything below succeeded (BEGIN is explicit because sqlite3 runs
    # DDL in autocommit mode by default).
    db.execute("BEGIN")
    try:
        db.execute("DROP TABLE utterances")
        db.execute("DROP TABLE recordings")
        _create(db)
        for r in old_recs:
            start = r.get("started_at")
            end = None
            if start and r.get("duration_ms"):
                try:
                    dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    end = (dt + timedelta(milliseconds=r["duration_ms"])).isoformat()
                except ValueError:
                    pass
            db.execute("INSERT INTO recordings(id, base, dir, start_dt, end_dt) VALUES(?,?,?,?,?)",
                       (r["id"], r["base"], r["dir"], start, end))
        for u in old_utts:
            db.execute("""INSERT INTO text_lines(id, recording_id, speaker_name, speaker_line,
                          start_dt_ms, end_dt_ms, edited) VALUES(?,?,?,?,?,?,?)""",
                       (u["id"], u["recording_id"], u["speaker"], u["text"],
                        u["start_ms"], u["end_ms"], u.get("edited", 0)))
        db.commit()
    except Exception:
        db.rollback()
        raise

def _create(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS recordings(
        id INTEGER PRIMARY KEY,
        base TEXT UNIQUE NOT NULL,
        dir TEXT NOT NULL,
        start_dt TEXT,
        end_dt TEXT,
        status TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS record_threads(
        id INTEGER PRIMARY KEY,
        record_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
        name TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS text_lines(
        id INTEGER PRIMARY KEY,
        recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
        speaker_name TEXT NOT NULL,
        speaker_line TEXT NOT NULL,
        start_dt_ms INTEGER NOT NULL,
        end_dt_ms INTEGER NOT NULL,
        edited INTEGER NOT NULL DEFAULT 0,
        thread_id INTEGER REFERENCES record_threads(id) ON DELETE SET NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_lines ON text_lines(recording_id, start_dt_ms)")
    db.execute("""CREATE TABLE IF NOT EXISTS record_reports(
        id INTEGER PRIMARY KEY,
        record_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
        summary TEXT,
        decision TEXT,
        action_item TEXT,
        thread_id INTEGER REFERENCES record_threads(id) ON DELETE CASCADE)""")

def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    _create(db)
    _migrate_legacy(db)
    cols = {r["name"] for r in db.execute("PRAGMA table_info(recordings)")}
    if "title" not in cols:
        db.execute("ALTER TABLE recordings ADD COLUMN title TEXT")
        db.commit()
    if "status" not in cols:
        db.execute("ALTER TABLE recordings ADD COLUMN status TEXT")
        db.commit()
    line_cols = {r["name"] for r in db.execute("PRAGMA table_info(text_lines)")}
    if "thread_id" not in line_cols:
        db.execute("""ALTER TABLE text_lines ADD COLUMN thread_id INTEGER
                      REFERENCES record_threads(id) ON DELETE SET NULL""")
        db.commit()
    rep_cols = {r["name"] for r in db.execute("PRAGMA table_info(record_reports)")}
    if "thread_id" not in rep_cols:
        db.execute("""ALTER TABLE record_reports ADD COLUMN thread_id INTEGER
                      REFERENCES record_threads(id) ON DELETE CASCADE""")
        db.commit()
    return db

def set_report(db, record_id: int, summary: str,
               decisions: list, action_items: list) -> None:
    """Replace the whole-call report rows (long format: one row per fact).
    Per-thread rows (thread_id NOT NULL) are managed by set_threads."""
    db.execute("DELETE FROM record_reports WHERE record_id=? AND thread_id IS NULL",
               (record_id,))
    rows = [(record_id, summary, None, None)] if summary else []
    rows += [(record_id, None, x, None) for x in decisions if x]
    rows += [(record_id, None, None, x) for x in action_items if x]
    db.executemany(
        "INSERT INTO record_reports(record_id, summary, decision, action_item) VALUES(?,?,?,?)",
        rows)
    db.commit()

def get_report(db, record_id: int) -> dict | None:
    rows = db.execute(
        """SELECT summary, decision, action_item FROM record_reports
           WHERE record_id=? AND thread_id IS NULL ORDER BY id""",
        (record_id,)).fetchall()
    if not rows:
        return None
    return {
        "summary": next((r["summary"] for r in rows if r["summary"]), ""),
        "decisions": [r["decision"] for r in rows if r["decision"]],
        "action_items": [r["action_item"] for r in rows if r["action_item"]],
    }

def set_threads(db, record_id: int, threads: list[dict]) -> None:
    """Replace a recording's discussion threads (topics), their per-thread
    report rows and the line→thread links.

    threads: [{"name", "summary", "decisions", "action_items", "line_ids"}].
    A topic revisited later in the call is still ONE thread: callers pass the
    union of its line ids and already-merged report sections.
    """
    db.execute("UPDATE text_lines SET thread_id=NULL WHERE recording_id=?",
               (record_id,))
    db.execute("DELETE FROM record_reports WHERE record_id=? AND thread_id IS NOT NULL",
               (record_id,))
    db.execute("DELETE FROM record_threads WHERE record_id=?", (record_id,))
    for th in threads:
        tid = db.execute("INSERT INTO record_threads(record_id, name) VALUES(?,?)",
                         (record_id, th["name"])).lastrowid
        rows = [(record_id, tid, th["summary"], None, None)] if th.get("summary") else []
        rows += [(record_id, tid, None, x, None) for x in th.get("decisions", []) if x]
        rows += [(record_id, tid, None, None, x) for x in th.get("action_items", []) if x]
        db.executemany("""INSERT INTO record_reports(record_id, thread_id, summary,
                          decision, action_item) VALUES(?,?,?,?,?)""", rows)
        db.executemany(
            "UPDATE text_lines SET thread_id=? WHERE id=? AND recording_id=?",
            [(tid, i, record_id) for i in th.get("line_ids", [])])
    db.commit()

def set_thread_report(db, record_id: int, thread_id: int, summary: str,
                      decisions: list, action_items: list) -> None:
    """Replace one thread's report rows (long format, thread_id set).
    Written per thread by the post-processor report chain."""
    db.execute("DELETE FROM record_reports WHERE thread_id=?", (thread_id,))
    rows = [(record_id, thread_id, summary, None, None)] if summary else []
    rows += [(record_id, thread_id, None, x, None) for x in decisions if x]
    rows += [(record_id, thread_id, None, None, x) for x in action_items if x]
    db.executemany("""INSERT INTO record_reports(record_id, thread_id, summary,
                      decision, action_item) VALUES(?,?,?,?,?)""", rows)
    db.commit()

def get_threads(db, record_id: int) -> list[dict]:
    """Threads of a recording with their report sections, in creation order."""
    out = []
    for t in db.execute(
            "SELECT id, name FROM record_threads WHERE record_id=? ORDER BY id",
            (record_id,)):
        rows = db.execute(
            """SELECT summary, decision, action_item FROM record_reports
               WHERE thread_id=? ORDER BY id""", (t["id"],)).fetchall()
        out.append({
            "id": t["id"], "name": t["name"],
            "summary": next((r["summary"] for r in rows if r["summary"]), ""),
            "decisions": [r["decision"] for r in rows if r["decision"]],
            "action_items": [r["action_item"] for r in rows if r["action_item"]],
        })
    return out

def report_bases(db) -> set:
    return {r["base"] for r in db.execute(
        """SELECT DISTINCT r.base FROM recordings r
           JOIN record_reports p ON p.record_id = r.id""")}

def set_title(db, base: str, title: str, rec_dir: str | None = None) -> None:
    """Set the user-given title; `base` (the ID) never changes."""
    if db.execute("SELECT 1 FROM recordings WHERE base=?", (base,)).fetchone() is None:
        db.execute("INSERT INTO recordings(base, dir, title) VALUES(?,?,?)",
                   (base, rec_dir or "", title or None))
    else:
        db.execute("UPDATE recordings SET title=? WHERE base=?", (title or None, base))
    db.commit()

def titles(db) -> dict:
    return {r["base"]: r["title"] for r in db.execute(
        "SELECT base, title FROM recordings WHERE title IS NOT NULL")}

def start_recording(db, base: str, rec_dir: str, start_dt: str) -> None:
    """Create the recording row the moment recording starts (status machine
    entry point: status='recording', end_dt not known yet)."""
    db.execute("""INSERT INTO recordings(base, dir, start_dt, status)
                  VALUES(?,?,?,'recording')
                  ON CONFLICT(base) DO UPDATE SET
                    dir=excluded.dir, start_dt=excluded.start_dt,
                    end_dt=NULL, status='recording'""",
               (base, rec_dir, start_dt))
    db.commit()

def finish_recording(db, base: str, end_dt: str, status: str) -> None:
    """User pressed stop: stamp end_dt and advance the status machine
    ('transcribing' when the pipeline launches, 'done' otherwise)."""
    db.execute("UPDATE recordings SET end_dt=?, status=? WHERE base=?",
               (end_dt, status, base))
    db.commit()

def set_status(db, base: str, status: str) -> None:
    """Advance the recording status machine. No-op when the row is missing
    (plain-CLI runs create it later via upsert_recording)."""
    db.execute("UPDATE recordings SET status=? WHERE base=?", (status, base))
    db.commit()

def upsert_recording(db, base: str, rec_dir: str, start_dt: str | None,
                     duration_ms: int | None) -> int:
    end_dt = None
    if start_dt and duration_ms:
        try:
            dt = datetime.fromisoformat(start_dt.replace("Z", "+00:00"))
            end_dt = (dt + timedelta(milliseconds=duration_ms)).isoformat()
        except ValueError:
            pass
    db.execute("""INSERT INTO recordings(base, dir, start_dt, end_dt)
                  VALUES(?,?,?,?)
                  ON CONFLICT(base) DO UPDATE SET
                    dir=excluded.dir, start_dt=excluded.start_dt, end_dt=excluded.end_dt""",
               (base, rec_dir, start_dt, end_dt))
    db.commit()
    return db.execute("SELECT id FROM recordings WHERE base=?", (base,)).fetchone()["id"]

def replace_lines(db, recording_id: int, rows: list[dict]) -> None:
    """Replace a recording's transcript, preserving rows with edited=1.

    New rows that cover the same utterance as a kept edited line are skipped:
    any time overlap with the same speaker, or an overlap spanning at least
    half of the new line, in case the speaker label shifted between runs.
    """
    db.execute("DELETE FROM text_lines WHERE recording_id=? AND edited=0", (recording_id,))
    kept = db.execute(
        """SELECT speaker_name, start_dt_ms, end_dt_ms FROM text_lines
           WHERE recording_id=? AND edited=1""", (recording_id,)).fetchall()

    def covered(r: dict) -> bool:
        for k in kept:
            overlap = (min(r["end_dt_ms"], k["end_dt_ms"])
                       - max(r["start_dt_ms"], k["start_dt_ms"]))
            if overlap <= 0:
                continue
            if (k["speaker_name"] == r["speaker_name"]
                    or overlap * 2 >= r["end_dt_ms"] - r["start_dt_ms"]):
                return True
        return False

    db.executemany(
        """INSERT INTO text_lines(recording_id, speaker_name, speaker_line,
           start_dt_ms, end_dt_ms) VALUES(?,?,?,?,?)""",
        [(recording_id, r["speaker_name"], r["speaker_line"],
          r["start_dt_ms"], r["end_dt_ms"]) for r in rows if not covered(r)])
    db.commit()

def append_lines(db, recording_id: int, rows: list[dict]) -> None:
    """Append transcript lines without touching existing ones (live preview;
    the final transcription replaces them via replace_lines)."""
    db.executemany(
        """INSERT INTO text_lines(recording_id, speaker_name, speaker_line,
           start_dt_ms, end_dt_ms) VALUES(?,?,?,?,?)""",
        [(recording_id, r["speaker_name"], r["speaker_line"],
          r["start_dt_ms"], r["end_dt_ms"]) for r in rows])
    db.commit()

def get_recording(db, base: str):
    return db.execute("SELECT * FROM recordings WHERE base=?", (base,)).fetchone()

def get_lines(db, recording_id: int) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT * FROM text_lines WHERE recording_id=? ORDER BY start_dt_ms, id",
        (recording_id,))]

def set_line_speaker(db, line_id: int, speaker_name: str) -> None:
    """Auto-fix reattribution: unlike update_line, does NOT set edited=1, so
    a later re-transcription may still replace the line."""
    db.execute("UPDATE text_lines SET speaker_name=? WHERE id=?",
               (speaker_name, line_id))
    db.commit()

def merge_lines(db, keep_id: int, absorbed_ids: list[int],
                text: str, end_dt_ms: int) -> None:
    """Collapse consecutive fragments of one utterance into keep_id."""
    db.execute("UPDATE text_lines SET speaker_line=?, end_dt_ms=? WHERE id=?",
               (text, end_dt_ms, keep_id))
    db.executemany("DELETE FROM text_lines WHERE id=?",
                   [(i,) for i in absorbed_ids])
    db.commit()

def update_line(db, line_id: int, speaker_name: str, start_dt_ms: int,
                end_dt_ms: int, speaker_line: str) -> None:
    db.execute("""UPDATE text_lines SET speaker_name=?, start_dt_ms=?, end_dt_ms=?,
                  speaker_line=?, edited=1 WHERE id=?""",
               (speaker_name, start_dt_ms, end_dt_ms, speaker_line, line_id))
    db.commit()

def delete_recording(db, base: str) -> None:
    """Delete a recording; text_lines follow via ON DELETE CASCADE."""
    db.execute("DELETE FROM recordings WHERE base=?", (base,))
    db.commit()

def delete_line(db, line_id: int) -> None:
    db.execute("DELETE FROM text_lines WHERE id=?", (line_id,))
    db.commit()
