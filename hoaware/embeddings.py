from __future__ import annotations

from typing import Iterable, List, Sequence

from openai import OpenAI


def batch_embeddings(
    texts: Sequence[str],
    client: OpenAI,
    model: str,
    batch_size: int = 32,
) -> List[list[float]]:
    vectors: List[list[float]] = []
    for i in range(0, len(texts), batch_size):
        chunk = texts[i : i + batch_size]
        response = client.embeddings.create(model=model, input=list(chunk))
        for item in response.data:
            vectors.append(item.embedding)
    return vectors
