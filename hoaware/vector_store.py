from __future__ import annotations

import uuid
from typing import Iterable, List, Sequence

from qdrant_client import QdrantClient
from qdrant_client.http.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
)

EMBEDDING_DIMENSIONS = 1536  # OpenAI text-embedding-3-small


def build_client(url: str, api_key: str | None) -> QdrantClient:
    return QdrantClient(url=url, api_key=api_key)


def ensure_collection(client: QdrantClient, collection_name: str) -> None:
    if client.collection_exists(collection_name=collection_name):
        return
    client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(size=EMBEDDING_DIMENSIONS, distance=Distance.COSINE),
    )


def upsert_chunks(
    client: QdrantClient,
    collection_name: str,
    chunks: Sequence[tuple[str, list[float], dict]],
) -> List[str]:
    points: List[PointStruct] = []
    point_ids: List[str] = []
    for chunk_text, vector, payload in chunks:
        point_id = uuid.uuid4().hex
        point_ids.append(point_id)
        points.append(PointStruct(id=point_id, vector=vector, payload=payload))
    client.upsert(collection_name=collection_name, points=points)
    return point_ids


def search(
    client: QdrantClient,
    collection_name: str,
    query_vector: list[float],
    limit: int = 5,
    hoa_name: str | None = None,
) -> list[dict]:
    flt = None
    if hoa_name:
        flt = Filter(
            must=[
                FieldCondition(
                    key="hoa",
                    match=MatchValue(value=hoa_name),
                )
            ]
        )
    response = client.query_points(
        collection_name=collection_name,
        query=query_vector,
        limit=limit,
        query_filter=flt,
        with_payload=True,
    )
    matches: list[dict] = []
    for point in response.points:
        matches.append(
            {
                "score": point.score,
                "payload": point.payload or {},
            }
        )
    return matches
