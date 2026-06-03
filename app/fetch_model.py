"""Pre-fetch (vendor) the semantic embedding model into the local cache dir.

Run this ONCE on a machine with internet access, then ship the resulting
``embedding_cache_dir`` (default ``./models/fastembed``) with the deployment so
semantic retrieval runs fully offline / air-gapped:

    FOI_RETRIEVAL_PROVIDER=semantic python -m app.fetch_model

After this, set ``FOI_EMBEDDING_OFFLINE=true`` in production to forbid any
runtime network fetch (load fails fast if the model isn't present).
"""
from __future__ import annotations

from .config import get_settings
from .services import embedding


def main() -> None:
    s = get_settings()
    print(f"Fetching '{s.embedding_model}' into '{s.embedding_cache_dir}' ...")
    emb = embedding._FastEmbedEmbedder(
        s.embedding_model, s.embedding_dim, cache_dir=s.embedding_cache_dir)
    vec = emb.embed_one("warm-up probe to force the model download")
    assert len(vec) == s.embedding_dim, (len(vec), s.embedding_dim)
    print(f"OK — model cached; embedding dim={len(vec)}. "
          f"Ship '{s.embedding_cache_dir}' with the deployment and set "
          f"FOI_EMBEDDING_OFFLINE=true for air-gap.")


if __name__ == "__main__":
    main()
