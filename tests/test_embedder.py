import numpy as np

from vault_engine.embedder import MockEmbedder, embed_dim_for_model


def test_mock_embedder_deterministic():
    e = MockEmbedder(dim=8)
    v1 = e.encode(["hello"])[0]
    v2 = e.encode(["hello"])[0]
    np.testing.assert_array_equal(v1, v2)
    assert v1.shape == (8,)


def test_mock_embedder_different_text_different_vectors():
    e = MockEmbedder(dim=8)
    v1, v2 = e.encode(["hello", "world"])
    assert not np.array_equal(v1, v2)


def test_mock_embedder_batch_shape():
    e = MockEmbedder(dim=4)
    out = e.encode(["a", "b", "c"])
    assert out.shape == (3, 4)


def test_embed_dim_for_model_known():
    assert embed_dim_for_model("mixedbread-ai/mxbai-embed-large-v1") == 1024
