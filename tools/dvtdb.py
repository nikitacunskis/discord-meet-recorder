"""Single SQLite store for Discord call transcripts.

Schema:
  recordings(id, base, dir, title, start_dt, end_dt)
  text_lines(id, recording_id, speaker_name, speaker_line,
             start_dt_ms, end_dt_ms, edited)
  record_reports(id, record_id, summary, decision, action_item)
    long format: one row carries the summary, one row per decision,
    one row per action item

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
    db.execute("DROP TABLE utterances")
    db.execute("DROP TABLE recordings")
    db.commit()
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

def _create(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS recordings(
        id INTEGER PRIMARY KEY,
        base TEXT UNIQUE NOT NULL,
        dir TEXT NOT NULL,
        start_dt TEXT,
        end_dt TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS text_lines(
        id INTEGER PRIMARY KEY,
        recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
        speaker_name TEXT NOT NULL,
        speaker_line TEXT NOT NULL,
        start_dt_ms INTEGER NOT NULL,
        end_dt_ms INTEGER NOT NULL,
        edited INTEGER NOT NULL DEFAULT 0)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_lines ON text_lines(recording_id, start_dt_ms)")
    db.execute("""CREATE TABLE IF NOT EXISTS record_reports(
        id INTEGER PRIMARY KEY,
        record_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
        summary TEXT,
        decision TEXT,
        action_item TEXT)""")

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
    return db

def set_report(db, record_id: int, summary: str,
               decisions: list, action_items: list) -> None:
    """Replace the report rows for a recording (long format: one row per fact)."""
    db.execute("DELETE FROM record_reports WHERE record_id=?", (record_id,))
    rows = [(record_id, summary or None, None, None)]
    rows += [(record_id, None, x, None) for x in decisions if x]
    rows += [(record_id, None, None, x) for x in action_items if x]
    db.executemany(
        "INSERT INTO record_reports(record_id, summary, decision, action_item) VALUES(?,?,?,?)",
        rows)
    db.commit()

def get_report(db, record_id: int) -> dict | None:
    rows = db.execute(
        "SELECT summary, decision, action_item FROM record_reports WHERE record_id=? ORDER BY id",
        (record_id,)).fetchall()
    if not rows:
        return None
    return {
        "summary": next((r["summary"] for r in rows if r["summary"]), ""),
        "decisions": [r["decision"] for r in rows if r["decision"]],
        "action_items": [r["action_item"] for r in rows if r["action_item"]],
    }

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
    """Replace a recording's transcript, preserving rows with edited=1."""
    db.execute("DELETE FROM text_lines WHERE recording_id=? AND edited=0", (recording_id,))
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
