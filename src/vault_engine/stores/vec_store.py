"""sqlite-vec adapter for chunk embeddings.

Schema:
- chunks         : vec0 virtual table with auxiliary columns.
- chunk_meta     : tracks (page_slug, chunk_idx) -> rowid + checksum.
- embedding_meta : single-row table recording (model_name, dim) — the
                   fingerprint that gates open() against silent vector-space
                   corruption when the configured model changes.
"""

from __future__ import annotations

import re
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import sqlite_vec


@dataclass
class VecHit:
    page_slug: str
    chunk_idx: int
    content: str
    checksum: str
    distance: float


class EmbeddingModelMismatch(RuntimeError):
    """Raised when an existing store was built with a different embedding model.

    The caller must either revert to the prior model or pass `force_reset=True`
    to wipe the store and rebuild from vault truth.
    """


class VecStore:
    def __init__(self, db_path: Path, dim: int, model_name: str) -> None:
        self.db_path = Path(db_path)
        self.dim = dim
        self.model_name = model_name
        self._conn: sqlite3.Connection | None = None

    def open(self, force_reset: bool = False) -> None:
        if force_reset and self.db_path.exists():
            self.db_path.unlink()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING vec0(
                embedding FLOAT[{self.dim}],
                +page_slug TEXT,
                +chunk_idx INTEGER,
                +content TEXT,
                +checksum TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunk_meta (
                page_slug TEXT NOT NULL,
                chunk_idx INTEGER NOT NULL,
                checksum TEXT NOT NULL,
                rowid INTEGER NOT NULL,
                PRIMARY KEY (page_slug, chunk_idx)
            )
            """
        )
        # FTS5 lexical index over chunk text (BM25). This is the third
        # retrieval channel (vector / topology / lexical): exact-keyword and
        # word-order matching that the bag-of-words embedder cannot do (it
        # scores "X is safe" ~= "X is not safe"). Its rowid mirrors the
        # ``chunks`` rowid so deletes stay coordinated. page_slug / chunk_idx
        # are UNINDEXED (stored, not tokenized) so a hit can name its chunk.
        self._conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                content,
                page_slug UNINDEXED,
                chunk_idx UNINDEXED
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS embedding_meta (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                model_name TEXT NOT NULL,
                dim INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur = self._conn.execute("SELECT model_name, dim FROM embedding_meta WHERE singleton=1")
        row = cur.fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO embedding_meta(singleton, model_name, dim) VALUES (1, ?, ?)",
                (self.model_name, self.dim),
            )
        else:
            stored_model, stored_dim = row[0], row[1]
            if stored_model != self.model_name or stored_dim != self.dim:
                self.close()
                raise EmbeddingModelMismatch(
                    f"vec store at {self.db_path} was built with "
                    f"model={stored_model!r} dim={stored_dim}, but engine "
                    f"is configured for model={self.model_name!r} dim={self.dim}. "
                    "Pass force_reset=True (or run `vault-engine reindex --force`) "
                    "to wipe the store and rebuild."
                )
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def embedding_fingerprint(self) -> tuple[str, int]:
        assert self._conn is not None
        cur = self._conn.execute("SELECT model_name, dim FROM embedding_meta WHERE singleton=1")
        row = cur.fetchone()
        return (row[0], row[1])

    def _serialize(self, v: np.ndarray) -> bytes:
        if v.dtype != np.float32:
            v = v.astype(np.float32)
        return struct.pack(f"{self.dim}f", *v.tolist())

    def upsert(
        self,
        page_slug: str,
        chunk_idx: int,
        content: str,
        checksum: str,
        embedding: np.ndarray,
    ) -> bool:
        """Insert or replace. Returns True if changed, False if unchanged.

        Wrapped in a transaction so a crash mid-write never leaves chunks
        and chunk_meta out of sync.
        """
        assert self._conn is not None
        cur = self._conn.execute(
            "SELECT checksum, rowid FROM chunk_meta WHERE page_slug=? AND chunk_idx=?",
            (page_slug, chunk_idx),
        )
        row = cur.fetchone()
        if row and row[0] == checksum:
            return False

        blob = self._serialize(embedding)
        with self._conn:
            if row:
                existing_rowid = row[1]
                self._conn.execute("DELETE FROM chunks WHERE rowid=?", (existing_rowid,))
                self._conn.execute(
                    "DELETE FROM chunk_meta WHERE page_slug=? AND chunk_idx=?",
                    (page_slug, chunk_idx),
                )
                # Keep the lexical index in lock-step with the vector store.
                self._conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (existing_rowid,))
            cur = self._conn.execute(
                "INSERT INTO chunks(embedding, page_slug, chunk_idx, content, checksum) VALUES(?, ?, ?, ?, ?)",
                (blob, page_slug, chunk_idx, content, checksum),
            )
            new_rowid = cur.lastrowid
            self._conn.execute(
                "INSERT INTO chunk_meta(page_slug, chunk_idx, checksum, rowid) VALUES(?, ?, ?, ?)",
                (page_slug, chunk_idx, checksum, new_rowid),
            )
            # Mirror the chunk into the FTS index under the SAME rowid.
            self._conn.execute(
                "INSERT INTO chunks_fts(rowid, content, page_slug, chunk_idx) VALUES(?, ?, ?, ?)",
                (new_rowid, content, page_slug, chunk_idx),
            )
        return True

    def delete_page(self, page_slug: str) -> int:
        """Drop every chunk for ``page_slug``. Wrapped in a transaction."""
        assert self._conn is not None
        cur = self._conn.execute("SELECT rowid FROM chunk_meta WHERE page_slug=?", (page_slug,))
        rowids = [r[0] for r in cur.fetchall()]
        with self._conn:
            for rid in rowids:
                self._conn.execute("DELETE FROM chunks WHERE rowid=?", (rid,))
                self._conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (rid,))
            self._conn.execute("DELETE FROM chunk_meta WHERE page_slug=?", (page_slug,))
        return len(rowids)

    def delete_chunk(self, page_slug: str, chunk_idx: int) -> bool:
        """Drop a single chunk. Returns True if a row was removed, False if absent.

        Wrapped in a transaction.
        """
        assert self._conn is not None
        cur = self._conn.execute(
            "SELECT rowid FROM chunk_meta WHERE page_slug=? AND chunk_idx=?",
            (page_slug, chunk_idx),
        )
        row = cur.fetchone()
        if row is None:
            return False
        rowid = row[0]
        with self._conn:
            self._conn.execute("DELETE FROM chunks WHERE rowid=?", (rowid,))
            self._conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (rowid,))
            self._conn.execute(
                "DELETE FROM chunk_meta WHERE page_slug=? AND chunk_idx=?",
                (page_slug, chunk_idx),
            )
        return True

    def iter_chunks_for_page(self, page_slug: str) -> list[tuple[int, np.ndarray]]:
        """Return [(chunk_idx, vector), ...] for every chunk belonging to page_slug.

        Used by the inference layer to mean-pool chunks into a page-level
        vector for semantic-similarity edge inference. Empty list if the page
        is unknown.
        """
        assert self._conn is not None
        cur = self._conn.execute(
            """
            SELECT chunk_idx, embedding
            FROM chunks
            WHERE page_slug=?
            ORDER BY chunk_idx
            """,
            (page_slug,),
        )
        out: list[tuple[int, np.ndarray]] = []
        for row in cur.fetchall():
            chunk_idx = row[0]
            blob = row[1]
            vec = np.frombuffer(blob, dtype=np.float32).copy()
            out.append((chunk_idx, vec))
        return out

    def get_checksums(self, page_slug: str) -> dict[int, str]:
        """Return {chunk_idx: checksum} for all chunks belonging to page_slug.

        The Indexer uses this to identify which chunks actually need re-encoding
        before calling the (expensive) embedder. Empty dict if the page is
        unknown.
        """
        assert self._conn is not None
        cur = self._conn.execute(
            "SELECT chunk_idx, checksum FROM chunk_meta WHERE page_slug=?",
            (page_slug,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}

    def search(self, query_vec: np.ndarray, top_k: int = 10) -> list[VecHit]:
        assert self._conn is not None
        blob = self._serialize(query_vec)
        cur = self._conn.execute(
            """
            SELECT page_slug, chunk_idx, content, checksum, distance
            FROM chunks
            WHERE embedding MATCH ?
            ORDER BY distance
            LIMIT ?
            """,
            (blob, top_k),
        )
        return [
            VecHit(
                page_slug=row[0],
                chunk_idx=row[1],
                content=row[2],
                checksum=row[3],
                distance=row[4],
            )
            for row in cur.fetchall()
        ]

    @staticmethod
    def _fts_query(query: str) -> str:
        """Turn free text into a safe FTS5 MATCH expression.

        Each alphanumeric token is wrapped in double quotes so FTS5 treats it
        as a bare string, neutralising query operators a user (or a page) might
        inject (``"``, ``*``, ``:``, ``NEAR``, ``AND``/``OR``, parentheses).
        Tokens are OR-ed so any keyword can match — recall over precision at
        the channel level; RRF and the other channels handle precision.
        Returns ``""`` when the query has no usable tokens.
        """
        tokens = re.findall(r"\w+", query.lower())
        if not tokens:
            return ""
        return " OR ".join(f'"{t}"' for t in tokens)

    def search_lexical(self, query: str, top_k: int = 10) -> list[VecHit]:
        """BM25 keyword search over chunk text (the lexical channel).

        Complements the vector channel: exact-term and word-order matching the
        bag-of-words embedder cannot do. ``distance`` carries the BM25 score
        (SQLite returns it negative, more-negative = better), so ascending sort
        is best-first, matching the vector channel's lower-is-better convention.
        Empty/whitespace/operator-only queries return ``[]``.
        """
        assert self._conn is not None
        match = self._fts_query(query)
        if not match:
            return []
        cur = self._conn.execute(
            """
            SELECT page_slug, chunk_idx, content, bm25(chunks_fts) AS score
            FROM chunks_fts
            WHERE chunks_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (match, top_k),
        )
        return [
            VecHit(
                page_slug=row[0],
                chunk_idx=row[1],
                content=row[2],
                checksum="",  # FTS rows don't carry the chunk checksum
                distance=row[3],
            )
            for row in cur.fetchall()
        ]
