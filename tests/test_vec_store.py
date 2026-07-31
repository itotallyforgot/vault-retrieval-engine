from pathlib import Path

import numpy as np
import pytest

from vault_engine.stores.vec_store import (
    EmbeddingModelMismatch,
    VaultPathMismatch,
    VecStore,
)


def test_vec_store_upsert_and_search(tmp_path: Path):
    db = tmp_path / "v.db"
    store = VecStore(db_path=db, dim=8, model_name="test/model")
    store.open()
    try:
        v1 = np.ones(8, dtype=np.float32)
        v2 = -np.ones(8, dtype=np.float32)
        store.upsert("alpha", 0, "hello", "abc", v1)
        store.upsert("beta", 0, "world", "def", v2)
        hits = store.search(np.ones(8, dtype=np.float32), top_k=2)
        assert hits[0].page_slug == "alpha"
        assert hits[0].chunk_idx == 0
        assert hits[0].content == "hello"
        assert hits[0].checksum == "abc"
    finally:
        store.close()


def test_vec_store_skip_unchanged_checksum(tmp_path: Path):
    db = tmp_path / "v.db"
    store = VecStore(db_path=db, dim=8, model_name="test/model")
    store.open()
    try:
        v = np.ones(8, dtype=np.float32)
        assert store.upsert("p", 0, "t", "abc", v) is True
        assert store.upsert("p", 0, "t", "abc", v) is False  # unchanged
        assert store.upsert("p", 0, "t2", "def", v) is True  # checksum changed
    finally:
        store.close()


def test_vec_store_delete_page_chunks(tmp_path: Path):
    db = tmp_path / "v.db"
    store = VecStore(db_path=db, dim=8, model_name="test/model")
    store.open()
    try:
        v = np.ones(8, dtype=np.float32)
        store.upsert("p", 0, "a", "1", v)
        store.upsert("p", 1, "b", "2", v)
        store.upsert("q", 0, "c", "3", v)
        store.delete_page("p")
        hits = store.search(v, top_k=10)
        assert all(h.page_slug != "p" for h in hits)
        assert any(h.page_slug == "q" for h in hits)
    finally:
        store.close()


def test_vec_store_records_model_fingerprint(tmp_path: Path):
    db = tmp_path / "v.db"
    store = VecStore(db_path=db, dim=8, model_name="m1")
    store.open()
    try:
        assert store.embedding_fingerprint() == ("m1", 8)
    finally:
        store.close()


def test_vec_store_rejects_mismatched_model(tmp_path: Path):
    db = tmp_path / "v.db"
    store = VecStore(db_path=db, dim=8, model_name="m1")
    store.open()
    store.close()

    # Re-open with a different model -> mismatch.
    bad = VecStore(db_path=db, dim=8, model_name="m2")
    with pytest.raises(EmbeddingModelMismatch):
        bad.open()


def test_vec_store_rejects_mismatched_dim(tmp_path: Path):
    db = tmp_path / "v.db"
    store = VecStore(db_path=db, dim=8, model_name="m1")
    store.open()
    store.close()

    bad = VecStore(db_path=db, dim=16, model_name="m1")
    with pytest.raises(EmbeddingModelMismatch):
        bad.open()


def test_vec_store_get_checksums_for_page(tmp_path: Path):
    db = tmp_path / "v.db"
    store = VecStore(db_path=db, dim=8, model_name="m1")
    store.open()
    try:
        v = np.ones(8, dtype=np.float32)
        store.upsert("p", 0, "a", "csum-a", v)
        store.upsert("p", 1, "b", "csum-b", v)
        store.upsert("q", 0, "c", "csum-c", v)
        cs = store.get_checksums("p")
        assert cs == {0: "csum-a", 1: "csum-b"}
        assert store.get_checksums("missing") == {}
    finally:
        store.close()


def test_vec_store_delete_chunk_removes_only_that_chunk(tmp_path: Path):
    db = tmp_path / "v.db"
    store = VecStore(db_path=db, dim=8, model_name="m1")
    store.open()
    try:
        v = np.ones(8, dtype=np.float32)
        store.upsert("p", 0, "a", "1", v)
        store.upsert("p", 1, "b", "2", v)
        assert store.delete_chunk("p", 0) is True
        assert store.get_checksums("p") == {1: "2"}
        # Idempotent: deleting again is a no-op.
        assert store.delete_chunk("p", 0) is False
    finally:
        store.close()


def test_vec_store_iter_chunks_for_page_returns_vectors(tmp_path: Path):
    db = tmp_path / "v.db"
    store = VecStore(db_path=db, dim=4, model_name="m1")
    store.open()
    try:
        v0 = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        v1 = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
        v_other = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)
        store.upsert("p", 0, "a", "csum-a", v0)
        store.upsert("p", 1, "b", "csum-b", v1)
        store.upsert("q", 0, "c", "csum-c", v_other)
        rows = store.iter_chunks_for_page("p")
        assert len(rows) == 2
        idxs = sorted(r[0] for r in rows)
        assert idxs == [0, 1]
        # Round-trip preserved through float32 (de)serialization.
        by_idx = {idx: vec for idx, vec in rows}
        np.testing.assert_allclose(by_idx[0], v0)
        np.testing.assert_allclose(by_idx[1], v1)
        # Unknown page → empty.
        assert store.iter_chunks_for_page("missing") == []
    finally:
        store.close()


def test_vec_store_force_reset_clears_state(tmp_path: Path):
    db = tmp_path / "v.db"
    store = VecStore(db_path=db, dim=8, model_name="m1")
    store.open()
    store.upsert("p", 0, "a", "1", np.ones(8, dtype=np.float32))
    store.close()

    # Different model + force=True -> wipe and accept.
    new = VecStore(db_path=db, dim=8, model_name="m2")
    new.open(force_reset=True)
    try:
        assert new.embedding_fingerprint() == ("m2", 8)
        assert new.search(np.ones(8, dtype=np.float32), top_k=10) == []
    finally:
        new.close()


# --- FTS5 / BM25 lexical channel (E3) ------------------------------------


def test_search_lexical_finds_keyword_match(tmp_path: Path):
    store = VecStore(db_path=tmp_path / "v.db", dim=8, model_name="m")
    store.open()
    try:
        store.upsert("alpha", 0, "the quick brown fox jumps", "c1", np.ones(8, dtype=np.float32))
        store.upsert("beta", 0, "a lazy sleeping dog", "c2", -np.ones(8, dtype=np.float32))
        hits = store.search_lexical("fox", top_k=5)
        assert [h.page_slug for h in hits] == ["alpha"]
        assert hits[0].content == "the quick brown fox jumps"
    finally:
        store.close()


def test_search_lexical_ranks_by_bm25(tmp_path: Path):
    store = VecStore(db_path=tmp_path / "v.db", dim=8, model_name="m")
    store.open()
    try:
        store.upsert("a", 0, "alpha alpha alpha topic", "c1", np.ones(8, dtype=np.float32))
        store.upsert("b", 0, "alpha mentioned once here", "c2", np.ones(8, dtype=np.float32))
        store.upsert("c", 0, "nothing relevant at all", "c3", np.ones(8, dtype=np.float32))
        hits = store.search_lexical("alpha", top_k=5)
        slugs = [h.page_slug for h in hits]
        assert slugs[:2] == ["a", "b"]  # 'c' has no match
        assert "c" not in slugs
        # Scores are best-first (ascending BM25).
        assert hits[0].distance <= hits[1].distance
    finally:
        store.close()


def test_search_lexical_neutralizes_fts_operators(tmp_path: Path):
    """A query containing FTS5 operator characters must not raise — they are
    quoted into bare terms."""
    store = VecStore(db_path=tmp_path / "v.db", dim=8, model_name="m")
    store.open()
    try:
        store.upsert("a", 0, "safe content about security", "c1", np.ones(8, dtype=np.float32))
        for q in ('NEAR("x")', "alpha OR beta", "foo*", 'bad "quote', "(paren", "col:on"):
            hits = store.search_lexical(q, top_k=5)  # must not raise
            assert isinstance(hits, list)
        assert store.search_lexical("   ", top_k=5) == []
        assert store.search_lexical("***", top_k=5) == []
    finally:
        store.close()


def test_fts_index_stays_in_sync_on_delete(tmp_path: Path):
    store = VecStore(db_path=tmp_path / "v.db", dim=8, model_name="m")
    store.open()
    try:
        store.upsert("alpha", 0, "uniqueword content", "c1", np.ones(8, dtype=np.float32))
        assert [h.page_slug for h in store.search_lexical("uniqueword")] == ["alpha"]
        # delete_chunk removes the FTS row too.
        store.delete_chunk("alpha", 0)
        assert store.search_lexical("uniqueword") == []
        # Re-add then delete_page.
        store.upsert("alpha", 0, "uniqueword again", "c2", np.ones(8, dtype=np.float32))
        assert [h.page_slug for h in store.search_lexical("uniqueword")] == ["alpha"]
        store.delete_page("alpha")
        assert store.search_lexical("uniqueword") == []
    finally:
        store.close()


def test_fts_index_updates_on_content_change(tmp_path: Path):
    """Re-upserting a chunk with new text must drop the old text from FTS."""
    store = VecStore(db_path=tmp_path / "v.db", dim=8, model_name="m")
    store.open()
    try:
        store.upsert("alpha", 0, "originalword here", "c1", np.ones(8, dtype=np.float32))
        store.upsert("alpha", 0, "replacedword here", "c2", -np.ones(8, dtype=np.float32))
        assert store.search_lexical("originalword") == []  # old text gone
        assert [h.page_slug for h in store.search_lexical("replacedword")] == ["alpha"]
    finally:
        store.close()


def test_vec_store_stamps_vault_path_on_first_open(tmp_path: Path):
    db = tmp_path / "v.db"
    vault = tmp_path / "vault-a"
    store = VecStore(db_path=db, dim=8, model_name="m1", vault_path=vault)
    store.open()
    try:
        assert store._conn is not None
        row = store._conn.execute("SELECT vault_path FROM store_meta WHERE singleton=1").fetchone()
        assert row[0] == str(vault)
    finally:
        store.close()


def test_vec_store_rejects_second_vault_sharing_one_cache(tmp_path: Path):
    """The cross-vault leak, at the store layer.

    Two vaults pointed at one embeddings.db used to silently share a corpus:
    vault B's search returned vault A's chunk text. The store now refuses the
    second vault instead of mixing them.
    """
    db = tmp_path / "v.db"
    first = VecStore(db_path=db, dim=8, model_name="m1", vault_path=tmp_path / "vault-a")
    first.open()
    first.close()

    second = VecStore(db_path=db, dim=8, model_name="m1", vault_path=tmp_path / "vault-b")
    with pytest.raises(VaultPathMismatch) as e:
        second.open()
    # The message has to name both vaults and both escape hatches, or the user
    # cannot tell a genuine second vault from a moved one.
    assert str(tmp_path / "vault-a") in str(e.value)
    assert str(tmp_path / "vault-b") in str(e.value)
    assert "--cache" in str(e.value)
    assert "--force" in str(e.value)


def test_vec_store_adopts_pre_stamp_database(tmp_path: Path):
    """A store built before the stamp existed adopts its current vault.

    Existing users must not re-embed just because they upgraded.
    """
    db = tmp_path / "v.db"
    old = VecStore(db_path=db, dim=8, model_name="m1")  # no vault_path: no stamp
    old.open()
    old.upsert("p", 0, "a", "csum-a", np.ones(8, dtype=np.float32))
    old.close()

    upgraded = VecStore(db_path=db, dim=8, model_name="m1", vault_path=tmp_path / "vault-a")
    upgraded.open()
    try:
        assert upgraded.get_checksums("p") == {0: "csum-a"}  # nothing was wiped
    finally:
        upgraded.close()


def test_vec_store_all_slugs(tmp_path: Path):
    db = tmp_path / "v.db"
    store = VecStore(db_path=db, dim=8, model_name="m1")
    store.open()
    try:
        v = np.ones(8, dtype=np.float32)
        store.upsert("p", 0, "a", "1", v)
        store.upsert("p", 1, "b", "2", v)
        store.upsert("q", 0, "c", "3", v)
        assert store.all_slugs() == {"p", "q"}
    finally:
        store.close()
