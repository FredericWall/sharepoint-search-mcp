from mcp_server.embeddings import Embedder


def test_local_embedder_returns_correct_dimension(fake_model_path):
    emb = Embedder(provider="local", model=fake_model_path)
    vec = emb.embed("SharePoint knowledge search")
    assert len(vec) == 384
