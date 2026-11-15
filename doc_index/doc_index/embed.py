"""Embedding clients. Anything with ``embed(texts, query=False) -> list[list[float]]`` works."""

from __future__ import annotations

import json
import urllib.request
from typing import Protocol

# Qwen3-Embedding: queries carry an instruction, documents do not.
QUERY_TASK = "Given a request to write a business document, retrieve the company files that contain the needed facts"


class Embedder(Protocol):
    def embed(self, texts: list[str], query: bool = False) -> list[list[float]]: ...


class OpenAICompatEmbedder:
    """POST {model, input[]} -> {data[{index, embedding}]} (vLLM /v1/embeddings or the CPU server)."""

    def __init__(self, url: str, model: str = "qwen3-emb", batch: int = 16, timeout: float = 600.0,
                 query_task: str = QUERY_TASK) -> None:
        self.url = url.rstrip("/")
        if not self.url.endswith("/embeddings"):
            self.url += "/embeddings"
        self.model, self.batch, self.timeout, self.query_task = model, batch, timeout, query_task

    def embed(self, texts: list[str], query: bool = False) -> list[list[float]]:
        if query:
            texts = [f"Instruct: {self.query_task}\nQuery:{t}" for t in texts]
        out: list[list[float]] = []
        for k in range(0, len(texts), self.batch):
            body = json.dumps({"model": self.model, "input": texts[k:k + self.batch]}).encode()
            req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.load(r)["data"]
            out += [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
        return out
