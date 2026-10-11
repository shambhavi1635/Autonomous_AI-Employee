"""What the agent remembers between runs.

Three tables, because they answer three different questions and mixing
them would make all three harder to use:

    episodes   "have I done something like this before, and how did it go?"
    lessons    "what did that teach me?"
    facts      "what do I know about Globex specifically?"

Nothing here embeds anything or calls a model. This module stores and
retrieves rows; `memory/semantic.py` handles similarity, and
`core/reflector.py` decides what is worth writing. Keeping storage
separate means the memory can be inspected, seeded and tested with no API
key -- and the whole suite stays offline.

The part that is easy to get wrong is not writing memories, it is
stopping. Lessons accumulate, go stale, start contradicting each other,
and eventually crowd the planner's context with advice that was true in
September. So lessons can be superseded, they carry a usefulness score,
and retrieval is capped. An agent that only ever appends gets worse over
time, which is the opposite of the point.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.config import DATA_DIR, ensure_dirs

MEMORY_DB = DATA_DIR / "memory.sqlite"

# How many lessons the planner is shown at most. A hard cap rather than
# "everything relevant": past a handful, extra advice stops steering the
# model and starts diluting the prompt.
MAX_LESSONS_IN_PROMPT = 8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task        TEXT    NOT NULL,
    summary     TEXT    NOT NULL,
    plan_json   TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    outcome     TEXT    NOT NULL DEFAULT '',
    embedding   TEXT,
    embedder    TEXT,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS lessons (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id    INTEGER REFERENCES episodes(id),
    text          TEXT    NOT NULL UNIQUE,
    kind          TEXT    NOT NULL,
    uses          INTEGER NOT NULL DEFAULT 0,
    superseded_by INTEGER REFERENCES lessons(id),
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    subject    TEXT NOT NULL,
    predicate  TEXT NOT NULL,
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject, predicate)
);
"""


@dataclass
class Episode:
    """One finished run, as remembered."""

    id: int
    task: str
    summary: str
    plan: dict[str, Any]
    status: str
    outcome: str
    created_at: str

    @property
    def succeeded(self) -> bool:
        return self.status == "done"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Columns added after the first version of the schema shipped. CREATE
# TABLE IF NOT EXISTS does nothing to a table that already exists, so a
# new column in _SCHEMA never reaches a database someone already has --
# it works perfectly on a fresh machine and fails on yours. Each entry
# here is applied if missing.
_MIGRATIONS = [
    ("episodes", "embedder", "TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, column_type in _MIGRATIONS:
        existing = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def _connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(MEMORY_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


# ------------------------------------------------------------- episodes
def record_episode(
    task: str,
    summary: str,
    plan: dict[str, Any],
    status: str,
    outcome: str = "",
    embedding: list[float] | None = None,
    embedder: str = "",
) -> int:
    """Store a finished run. Returns its id.

    Failures are stored too, and deliberately. "I tried X and it did not
    work" is as useful to a later plan as "X worked" -- arguably more,
    since it rules something out.
    """
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO episodes (task, summary, plan_json, status, "
            "outcome, embedding, embedder, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task.strip(),
                summary.strip(),
                json.dumps(plan),
                status,
                outcome.strip(),
                json.dumps(embedding) if embedding else None,
                embedder,
                _now(),
            ),
        )
        return int(cursor.lastrowid)


def all_episodes(limit: int = 500) -> list[Episode]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM episodes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_to_episode(row) for row in rows]


def episodes_with_embeddings(
    embedder: str = "", limit: int = 500
) -> list[tuple[Episode, list[float]]]:
    """Episodes that can take part in a similarity search.

    Filtered by which embedder produced the vector. Vectors from
    different models are not comparable -- they have different
    dimensions and different geometry -- and silently mixing them means
    retrieval quietly returns nothing while looking like it worked.
    Installing sentence-transformers partway through this project made
    exactly that happen to every episode stored before it.
    """
    with _connect() as conn:
        if embedder:
            rows = conn.execute(
                "SELECT * FROM episodes WHERE embedding IS NOT NULL "
                "AND embedder = ? ORDER BY id DESC LIMIT ?",
                (embedder, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM episodes WHERE embedding IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [(_to_episode(row), json.loads(row["embedding"])) for row in rows]


def reembed_all(embed, embedder_name: str) -> int:
    """Re-embed every episode with the current embedder. Returns the count.

    Run after changing embedder. Cheap -- a few dozen short summaries --
    and it keeps old experience usable instead of stranding it.
    """
    with _connect() as conn:
        rows = conn.execute("SELECT id, summary FROM episodes").fetchall()
        for row in rows:
            conn.execute(
                "UPDATE episodes SET embedding = ?, embedder = ? WHERE id = ?",
                (json.dumps(embed(row["summary"])), embedder_name, row["id"]),
            )
        return len(rows)


def _to_episode(row: sqlite3.Row) -> Episode:
    return Episode(
        id=row["id"],
        task=row["task"],
        summary=row["summary"],
        plan=json.loads(row["plan_json"]),
        status=row["status"],
        outcome=row["outcome"],
        created_at=row["created_at"],
    )


# -------------------------------------------------------------- lessons
def add_lesson(text: str, kind: str, episode_id: int | None = None) -> int | None:
    """Record something learned. Returns the id, or None if already known.

    `text` is UNIQUE, so the same lesson learned twice is stored once.
    Without that, running the same failing task three times would put
    three copies of the same advice in front of the planner.
    """
    cleaned = text.strip()
    if not cleaned:
        return None
    with _connect() as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO lessons (episode_id, text, kind, created_at) "
                "VALUES (?, ?, ?, ?)",
                (episode_id, cleaned, kind, _now()),
            )
        except sqlite3.IntegrityError:
            return None
        return int(cursor.lastrowid)


def supersede_lesson(old_id: int, new_text: str, kind: str) -> int | None:
    """Replace a lesson that has stopped being true.

    The old row is kept, pointed at its replacement, rather than deleted.
    A memory that quietly rewrites itself cannot be audited, and "what
    did it used to believe, and when did that change?" is a question
    worth being able to answer.
    """
    new_id = add_lesson(new_text, kind)
    if new_id is None:
        return None
    with _connect() as conn:
        conn.execute(
            "UPDATE lessons SET superseded_by = ? WHERE id = ?", (new_id, old_id)
        )
    return new_id


def active_lessons(limit: int = MAX_LESSONS_IN_PROMPT) -> list[sqlite3.Row]:
    """Lessons still believed, most-used first.

    Ordering by `uses` is a crude relevance signal, but it is an honest
    one: a lesson that has been shown to the planner repeatedly has
    survived repeated chances to be wrong.
    """
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM lessons WHERE superseded_by IS NULL "
            "ORDER BY uses DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()


def active_lessons_with_source(limit: int = 50) -> list[sqlite3.Row]:
    """Lessons plus the status of the run that produced them.

    A lesson learned from a failed run deserves more scepticism than one
    learned from a success -- and a lesson learned from a run this system
    judged *wrongly* is worse than useless, because it steers every
    future plan with advice derived from a defect. One such lesson has
    already been produced here, so the UI shows the source status and
    lets a human delete it.
    """
    with _connect() as conn:
        return conn.execute(
            "SELECT l.*, e.status AS source_status, e.task AS source_task "
            "FROM lessons l LEFT JOIN episodes e ON e.id = l.episode_id "
            "WHERE l.superseded_by IS NULL "
            "ORDER BY l.uses DESC, l.id DESC LIMIT ?",
            (limit,),
        ).fetchall()


def delete_lesson(lesson_id: int) -> None:
    """Forget a lesson outright.

    Deleted rather than superseded, because this is for lessons that
    should never have been learned -- there is no history worth keeping
    of a belief that was always wrong.
    """
    with _connect() as conn:
        conn.execute("UPDATE lessons SET superseded_by = NULL "
                     "WHERE superseded_by = ?", (lesson_id,))
        conn.execute("DELETE FROM lessons WHERE id = ?", (lesson_id,))


def delete_fact(fact_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))


def mark_lessons_used(lesson_ids: list[int]) -> None:
    """Count a lesson as used when it is put in front of the planner."""
    if not lesson_ids:
        return
    with _connect() as conn:
        conn.executemany(
            "UPDATE lessons SET uses = uses + 1 WHERE id = ?",
            [(i,) for i in lesson_ids],
        )


# ---------------------------------------------------------------- facts
def remember_fact(subject: str, predicate: str, value: str) -> None:
    """Store a fact about a named thing, replacing any earlier value.

    Facts overwrite where lessons supersede. A vendor's address changing
    is not an interesting history; a belief about how to work changing
    is.
    """
    with _connect() as conn:
        conn.execute(
            "INSERT INTO facts (subject, predicate, value, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(subject, predicate) DO UPDATE SET "
            "value = excluded.value, created_at = excluded.created_at",
            (subject.strip(), predicate.strip(), value.strip(), _now()),
        )


def facts_about(subject: str) -> list[sqlite3.Row]:
    """Everything known about a subject. Matching is case-insensitive and
    partial, so 'globex' finds facts stored under 'Globex Ltd'."""
    if not subject.strip():
        return []
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM facts WHERE subject LIKE ? ORDER BY subject, predicate",
            (f"%{subject.strip()}%",),
        ).fetchall()


def all_facts() -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM facts ORDER BY subject, predicate"
        ).fetchall()


# ---------------------------------------------------------------- admin
def stats() -> dict[str, int]:
    """Counts, for the UI and for knowing whether memory is doing anything."""
    with _connect() as conn:
        return {
            "episodes": conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0],
            "successes": conn.execute(
                "SELECT COUNT(*) FROM episodes WHERE status = 'done'"
            ).fetchone()[0],
            "lessons": conn.execute(
                "SELECT COUNT(*) FROM lessons WHERE superseded_by IS NULL"
            ).fetchone()[0],
            "facts": conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
        }


def forget_everything() -> None:
    """Wipe the memory. For tests, and for starting a demo clean."""
    with _connect() as conn:
        for table in ("lessons", "episodes", "facts"):
            conn.execute(f"DELETE FROM {table}")
