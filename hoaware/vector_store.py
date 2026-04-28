from __future__ import annotations

import logging
import os
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
    OptimizersConfigDiff,
    HnswConfigDiff,
)

EMBEDDING_DIMENSIONS = 1536  # OpenAI text-embedding-3-small
logger = logging.getLogger(__name__)

_cached_client: QdrantClient | None = None
_cached_client_key: tuple | None = None


def _qdrant_disabled() -> bool:
    return os.environ.get("HOA_DISABLE_QDRANT", "").strip().lower() in {"1", "true", "yes"}


def build_client(
    url: str,
    api_key: str | None,
    local_path: Path | str | None = None,
) -> QdrantClient | None:
    # Read path is sqlite-vec (db.vector_search). Qdrant is only the legacy
    # write mirror. Set HOA_DISABLE_QDRANT=1 on memory-constrained hosts to
    # skip the embedded Qdrant entirely (mmap'd local segments otherwise
    # accumulate against the cgroup memory cap).
    if _qdrant_disabled():
        return None

    global _cached_client, _cached_client_key
    cache_key = (url, api_key, str(local_path) if local_path is not None else None)
    if _cached_client is not None and _cached_client_key == cache_key:
        return _cached_client

    local_dir = Path(local_path) if local_path is not None else None
    cleaned_url = url.strip()
    if cleaned_url.startswith("file://"):
        path_value = Path(cleaned_url.removeprefix("file://"))
        path_value.mkdir(parents=True, exist_ok=True)
        client = QdrantClient(path=str(path_value))
    elif not cleaned_url and local_dir is not None:
        local_dir.mkdir(parents=True, exist_ok=True)
        client = QdrantClient(path=str(local_dir))
    else:
        remote = QdrantClient(url=cleaned_url, api_key=api_key)
        if local_dir is None:
            client = remote
        else:
            try:
                remote.get_collections()
                client = remote
            except Exception as exc:
                local_dir.mkdir(parents=True, exist_ok=True)
                logger.warning(
                    "Qdrant at %s unavailable (%s); using local store at %s",
                    cleaned_url,
                    exc,
                    local_dir,
                )
                client = QdrantClient(path=str(local_dir))

    _cached_client = client
    _cached_client_key = cache_key
    return client


def ensure_collection(client: QdrantClient | None, collection_name: str) -> None:
    if client is None:
        return
    if client.collection_exists(collection_name=collection_name):
        return
    client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(
            size=EMBEDDING_DIMENSIONS,
            distance=Distance.COSINE,
            on_disk=True,
        ),
        optimizers_config=OptimizersConfigDiff(
            memmap_threshold=0,  # Use mmap for all segments
        ),
        hnsw_config=HnswConfigDiff(
            on_disk=True,  # HNSW index on disk too
        ),
    )
    # Create payload index on "hoa" for efficient filtering in the unified collection
    try:
        client.create_payload_index(
            collection_name=collection_name,
            field_name="hoa",
            field_schema="keyword",
        )
    except Exception:
        pass  # Index may already exist


def upsert_chunks(
    client: QdrantClient | None,
    collection_name: str,
    chunks: Sequence[tuple[str, list[float], dict]],
) -> List[str]:
    if client is None:
        # Qdrant disabled: still return placeholder ids so callers can record
        # one row per chunk in the chunks table.
        return [uuid.uuid4().hex for _ in chunks]
    points: List[PointStruct] = []
    point_ids: List[str] = []
    for chunk_text, vector, payload in chunks:
        point_id = uuid.uuid4().hex
        point_ids.append(point_id)
        points.append(PointStruct(id=point_id, vector=vector, payload=payload))
    client.upsert(collection_name=collection_name, points=points)
    return point_ids


def delete_points(
    client: QdrantClient | None,
    collection_name: str,
    point_ids: Sequence[str],
) -> None:
    if client is None or not point_ids:
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


def points_exist(
    client: QdrantClient | None,
    collection_name: str,
    point_ids: Sequence[str],
) -> bool:
    """Check whether all given point ids exist in the target collection."""
    if client is None or not point_ids:
        return False
    try:
        points = client.retrieve(
            collection_name=collection_name,
            ids=list(point_ids),
            with_payload=False,
            with_vectors=False,
        )
    except Exception:
        return False
    return len(points) == len(point_ids)
