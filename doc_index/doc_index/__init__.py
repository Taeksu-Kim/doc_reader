"""doc_index — keyword + vector index over doc_chunker units, in one SQLite file.

    from doc_index import Index, OpenAICompatEmbedder, files_of
    idx = Index("index.sqlite", OpenAICompatEmbedder("http://<ip>:8011/v1"))
    idx.build(["자료폴더/"])                      # incremental: unchanged files are skipped
    hits = idx.search("출장 일비 얼마", k=10)      # mode="hybrid" | "keyword" | "vector"
    files_of(hits)                                # ranked file paths

Keyword search: BM25 over Korean character bigrams (doc_index.text). Vector: Qwen3-Embedding style
(instruction on queries only). Hybrid: reciprocal rank fusion. No reranker here (yet).
"""

from doc_index.embed import Embedder, OpenAICompatEmbedder
from doc_index.store import BuildReport, Hit, Index, family_key, files_of, recency_key
from doc_index.text import normalize, tokens

__version__ = "0.1.0"

__all__ = ["BuildReport", "Embedder", "Hit", "Index", "OpenAICompatEmbedder", "family_key", "files_of", "normalize",
           "recency_key", "tokens"]
