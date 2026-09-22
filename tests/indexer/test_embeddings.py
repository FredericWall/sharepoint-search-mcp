from indexer.embeddings import Embedder


def test_local_embedder_returns_correct_dimension(fake_model_path):
    emb = Embedder(provider="local", model=fake_model_path)
    vec = emb.embed("SharePoint knowledge search")
    assert len(vec) == 384
    assert all(isinstance(x, float) for x in vec)


def test_embed_batch_returns_list_of_vectors(fake_model_path):
    emb = Embedder(provider="local", model=fake_model_path)
    vecs = emb.embed_batch(["text eins", "text zwei"])
    assert len(vecs) == 2
    assert len(vecs[0]) == 384


def test_similar_texts_have_high_cosine_similarity(fake_model_path):
    import numpy as np
    emb = Embedder(provider="local", model=fake_model_path)
    a = np.array(emb.embed("data quality reporting"))
    b = np.array(emb.embed("reporting on data quality"))
    c = np.array(emb.embed("the weather is sunny today"))
    cos_ab = a @ b / (np.linalg.norm(a) * np.linalg.norm(b))
    cos_ac = a @ c / (np.linalg.norm(a) * np.linalg.norm(c))
    assert cos_ab > cos_ac
