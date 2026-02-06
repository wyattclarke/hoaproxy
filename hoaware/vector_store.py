from __future__ import annotations

import logging
from pathlib import Path
import uuid
from typing import List, Sequence

from qdrant_client import QdrantClient
from qdrant_client.http.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
    PointIdsList,
)

EMBEDDING_DIMENSIONS = 1536  # OpenAI text-embedding-3-small
logger = logging.getLogger(__name__)


def build_client(
    url: str,
    api_key: str | None,
    local_path: Path | str | None = None,
) -> QdrantClient:
    local_dir = Path(local_path) if local_path is not None else None
    cleaned_url = url.strip()
    if cleaned_url.startswith("file://"):
        path_value = Path(cleaned_url.removeprefix("file://"))
        path_value.mkdir(parents=True, exist_ok=True)
        return QdrantClient(path=str(path_value))
    if not cleaned_url and local_dir is not None:
        local_dir.mkdir(parents=True, exist_ok=True)
        return QdrantClient(path=str(local_dir))

    remote = QdrantClient(url=cleaned_url, api_key=api_key)
    if local_dir is None:
        return remote
    try:
        remote.get_collections()
        return remote
    except Exception as exc:
        local_dir.mkdir(parents=True, exist_ok=True)
        logger.warning(
            "Qdrant at %s unavailable (%s); using local store at %s",
            cleaned_url,
            exc,
            local_dir,
        )
        return QdrantClient(path=str(local_dir))


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


def delete_points(
    client: QdrantClient,
    collection_name: str,
    point_ids: Sequence[str],
) -> None:
    if not point_ids:
        return
    client.delete(
        collection_name=collection_name,
        points_selector=PointIdsList(points=list(point_ids)),
        wait=True,
    )


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
