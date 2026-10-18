"""The search index (spec 5.6): one SQLite FTS5 table over the names, captions and texts of every record.

A record enters it when it is stored, by its name and caption (page 0), and its text joins page by page once a
reader has read it (pages 1 and on). Callers write inside their own transaction, so a record and its index rows land
together or not at all. SQLite's tokenizer folds the marks on Latin letters (é, š); the index folds two letters more,
in what it is given and in what it is asked (finding I7): the Cyrillic yo, which OCR and whisper mostly write as ye,
and the Serbian d with stroke, which has no mark to remove and is written "dj" without one.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

RANK_ROWS = 2000  # the best index rows looked at: enough to find the best records when one has many pages
SNIPPET_TOKENS = 16
# yo and Yo as ye and Ye; d and D with stroke as dj and Dj
FOLDED = (("\u0451", "\u0435"), ("\u0401", "\u0415"), ("\u0111", "dj"), ("\u0110", "Dj"))
_FOLD = str.maketrans(dict(FOLDED))
_BM25 = "bm25(evidence_fts, 1.0, 2.0, 2.0)"  # a name or a caption weighs twice a page


def fold(text: str) -> str:
    return text.translate(_FOLD)


def fold_sql(expression: str) -> str:
    """fold() in SQL, for the rows the index takes from the database itself."""
    for letter, plain in FOLDED:
        expression = f"replace({expression}, char({ord(letter)}), char({', '.join(str(ord(c)) for c in plain)}))"
    return expression


def index_names(conn: sqlite3.Connection, evidence_id: str, name: str | None, caption: str | None) -> None:
    conn.execute(
        "INSERT INTO evidence_fts(body, name, caption, evidence_id, page) VALUES ('', ?, ?, ?, 0)",
        (fold(name or ""), fold(caption or ""), evidence_id),
    )


def index_pages(conn: sqlite3.Connection, evidence_id: str, texts: Sequence[str]) -> None:
    """Pages 1 and on; a page without text has no row."""
    conn.executemany(
        "INSERT INTO evidence_fts(body, name, caption, evidence_id, page) VALUES (?, '', '', ?, ?)",
        [(fold(text), evidence_id, number) for number, text in enumerate(texts, 1) if text],
    )


@dataclass(frozen=True)
class Hit:
    evidence_id: str
    page: int  # the best matching page of its text; 0: only the record's name or caption matched
    snippet: str | None  # the words around the match on that page, as the index keeps them; None for page 0


def find(
    conn: sqlite3.Connection, spaces: Sequence[str], states: Sequence[str], stems: Sequence[str], limit: int
) -> list[Hit]:
    """The records whose names, captions or texts have words that begin with one of the stems, best first (bm25, a
    name or caption weighing twice a page), each once, at its best page of text when one matches (finding M5). The
    space filter is in the query itself, before the ranking (spec 5.6). A stem is letters and digits only, so it can
    be quoted as it is."""
    query = " OR ".join(f'"{fold(stem)}"*' for stem in stems)
    rows = conn.execute(
        "SELECT f.evidence_id, f.page FROM evidence_fts f JOIN evidence e ON e.id = f.evidence_id "
        f"WHERE evidence_fts MATCH ? AND e.space_id IN ({','.join('?' * len(spaces))}) "
        f"AND e.state IN ({','.join('?' * len(states))}) ORDER BY {_BM25} LIMIT ?",
        (query, *spaces, *states, RANK_ROWS),
    ).fetchall()
    best: dict[str, int] = {}
    for evidence_id, page in rows:
        best.setdefault(evidence_id, page)
        if len(best) == limit:
            break
    return [_text_hit(conn, query, evidence_id, page) for evidence_id, page in best.items()]


def _text_hit(conn: sqlite3.Connection, query: str, evidence_id: str, page: int) -> Hit:
    """The record at `page` with the words around the match there; found by its name or caption, at its best page of
    text that matches too."""
    row = conn.execute(
        f"SELECT page, snippet(evidence_fts, 0, '', '', '…', ?) FROM evidence_fts "
        f"WHERE evidence_fts MATCH ? AND evidence_id = ? AND {'page = ?' if page else 'page > ?'} "
        f"ORDER BY {_BM25} LIMIT 1",
        (SNIPPET_TOKENS, query, evidence_id, page),
    ).fetchone()
    return Hit(evidence_id, page, None) if row is None else Hit(evidence_id, int(row[0]), str(row[1]))
