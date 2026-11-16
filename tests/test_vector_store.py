"""RAG SQLite 向量存储：归一化、过滤、持久化与连接复用。"""

from __future__ import annotations

import math

import pytest

from memory import rag_memory
from memory.rag_memory import RagDocument, SqliteVectorStore


def _doc(
    doc_id: str,
    vector: list[float],
    *,
    conversation_id: str = "group:1",
    timestamp: str = "2026-01-01",
    text: str = "",
) -> RagDocument:
    return RagDocument(
        id=doc_id,
        text=text or doc_id,
        vector=vector,
        conversation_id=conversation_id,
        role="user",
        timestamp=timestamp,
    )


@pytest.mark.asyncio
async def test_normalizes_vectors_and_ranks_by_dot_product(tmp_path) -> None:
    store = SqliteVectorStore(tmp_path / "rag.sqlite3")
    await store.load()
    await store.upsert_many(
        [
            _doc("diagonal", [3.0, 4.0]),
            _doc("same", [1.0, 0.0]),
            _doc("orthogonal", [0.0, 1.0]),
            _doc("opposite", [-1.0, 0.0]),
        ]
    )

    doc = next(entry for entry in store.all_entries() if entry.id == "diagonal")
    assert math.isclose(sum(value * value for value in doc.vector), 1.0)

    hits = store.top_k([1.0, 0.0], k=3)
    assert [hit.document.id for hit in hits] == ["same", "diagonal", "orthogonal"]
    assert hits[0].score == pytest.approx(1.0)
    assert hits[1].score == pytest.approx(0.6)
    assert hits[2].score == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_conversation_and_time_filters(tmp_path) -> None:
    store = SqliteVectorStore(tmp_path / "rag.sqlite3")
    await store.load()
    await store.upsert_many(
        [
            _doc("a", [1.0, 0.0], conversation_id="group:1", timestamp="2026-01-01"),
            _doc("b", [1.0, 0.0], conversation_id="group:1", timestamp="2026-02-01"),
            _doc("c", [1.0, 0.0], conversation_id="group:2", timestamp="2026-01-01"),
        ]
    )

    assert [hit.document.id for hit in store.top_k([1.0, 0.0], k=5, conversation_id="group:2")] == ["c"]
    assert [
        hit.document.id
        for hit in store.top_k([1.0, 0.0], k=5, before_ts="2026-02-01")
    ] == ["a", "c"]


@pytest.mark.asyncio
async def test_persistence_and_connection_reuse(tmp_path) -> None:
    path = tmp_path / "rag.sqlite3"
    store = SqliteVectorStore(path)
    await store.load()
    await store.upsert_many([_doc("a", [2.0, 0.0])])
    assert store._conn is not None
    await store.close()
    assert store._conn is None

    reopened = SqliteVectorStore(path)
    await reopened.load()
    assert len(reopened) == 1
    hits = reopened.top_k([1.0, 0.0], k=1)
    assert hits[0].document.id == "a"
    assert hits[0].score == pytest.approx(1.0)
    await reopened.close()


@pytest.mark.asyncio
async def test_remove_ids_and_missing_ids(tmp_path) -> None:
    store = SqliteVectorStore(tmp_path / "rag.sqlite3")
    await store.load()
    await store.upsert_many([_doc("a", [1.0, 0.0]), _doc("b", [0.0, 1.0])])

    assert await store.missing_ids(["a", "c"]) == {"c"}
    assert await store.remove_ids(["a", "missing"]) == 1
    assert len(store) == 1
    assert [hit.document.id for hit in store.top_k([1.0, 0.0], k=5)] == ["b"]
    await store.close()


@pytest.mark.asyncio
async def test_top_k_async_matches_sync_path(tmp_path) -> None:
    store = SqliteVectorStore(tmp_path / "rag.sqlite3")
    await store.load()
    await store.upsert_many([_doc("a", [1.0, 0.0]), _doc("b", [0.0, 1.0])])

    sync_hits = store.top_k([1.0, 0.0], k=2)
    async_hits = await store.top_k_async([1.0, 0.0], k=2)
    assert [(hit.document.id, hit.score) for hit in async_hits] == [
        (hit.document.id, hit.score) for hit in sync_hits
    ]
    await store.close()


@pytest.mark.asyncio
async def test_close_waits_for_cancelled_inflight_db_thread(tmp_path) -> None:
    import asyncio
    import threading

    store = SqliteVectorStore(tmp_path / "rag.sqlite3")
    await store.load()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def fake_upsert(documents) -> None:
        started.set()
        release.wait(timeout=5)
        finished.set()

    store._upsert_many_sync = fake_upsert  # type: ignore[method-assign]
    task = asyncio.create_task(store.upsert_many([_doc("a", [1.0, 0.0])]))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()

    close_task = asyncio.create_task(store.close())
    await asyncio.sleep(0.02)
    assert not close_task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await close_task
    assert finished.is_set()
    assert store._conn is None


@pytest.mark.asyncio
async def test_cancelled_upsert_still_commits_memory_and_db(tmp_path) -> None:
    import asyncio
    import threading

    path = tmp_path / "rag.sqlite3"
    store = SqliteVectorStore(path)
    await store.load()
    started = threading.Event()
    release = threading.Event()
    original = store._upsert_many_sync

    def slow_upsert(documents) -> None:
        started.set()
        release.wait(timeout=5)
        original(documents)

    store._upsert_many_sync = slow_upsert  # type: ignore[method-assign]
    task = asyncio.create_task(store.upsert_many([_doc("a", [1.0, 0.0])]))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(store) == 1
    await store.close()

    reopened = SqliteVectorStore(path)
    await reopened.load()
    assert len(reopened) == 1
    await reopened.close()


@pytest.mark.asyncio
async def test_cancelled_top_k_holds_lock_until_thread_finishes(tmp_path) -> None:
    import asyncio
    import threading

    store = SqliteVectorStore(tmp_path / "rag.sqlite3")
    await store.load()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow_top_k(query_vec, *, k, conversation_id=None, before_ts=None):
        started.set()
        release.wait(timeout=5)
        finished.set()
        return []

    store.top_k = slow_top_k  # type: ignore[method-assign]
    query_task = asyncio.create_task(store.top_k_async([1.0, 0.0], k=1))
    assert await asyncio.to_thread(started.wait, 2)
    query_task.cancel()

    upsert_task = asyncio.create_task(store.upsert_many([_doc("a", [1.0, 0.0])]))
    await asyncio.sleep(0.02)
    assert not upsert_task.done()
    release.set()
    await upsert_task
    with pytest.raises(asyncio.CancelledError):
        await query_task
    assert finished.is_set()
    assert len(store) == 1
    await store.close()


@pytest.mark.asyncio
async def test_empty_vector_is_kept_with_zero_score(tmp_path) -> None:
    store = SqliteVectorStore(tmp_path / "rag.sqlite3")
    await store.load()
    await store.upsert_many([_doc("empty", []), _doc("real", [1.0, 0.0])])

    hits = store.top_k([1.0, 0.0], k=5)
    assert [hit.document.id for hit in hits] == ["real", "empty"]
    assert hits[1].score == 0.0
    await store.close()


@pytest.mark.asyncio
async def test_zero_score_entries_outrank_negative_scores(tmp_path) -> None:
    store = SqliteVectorStore(tmp_path / "rag.sqlite3")
    await store.load()
    await store.upsert_many(
        [
            _doc("positive", [1.0, 0.0]),
            _doc("negative", [-1.0, 0.0]),
            _doc("empty", []),
            _doc("mismatch", [1.0, 0.0, 0.0]),
        ]
    )

    hits = store.top_k([1.0, 0.0], k=3)
    assert [hit.document.id for hit in hits] == ["positive", "empty", "mismatch"]
    assert [hit.score for hit in hits] == pytest.approx([1.0, 0.0, 0.0])
    await store.close()


@pytest.mark.asyncio
async def test_numpy_matrix_path_when_available(tmp_path) -> None:
    if rag_memory._np is None:
        pytest.skip("numpy 未安装，跳过矩阵检索路径")
    store = SqliteVectorStore(tmp_path / "rag.sqlite3")
    await store.load()
    await store.upsert_many([_doc("a", [1.0, 0.0])])
    assert store._matrix is not None
    assert store._matrix_rows == 1
    hits = store.top_k([1.0, 0.0], k=1)
    assert hits[0].document.id == "a"
    await store.close()


@pytest.mark.asyncio
async def test_numpy_and_pure_python_paths_agree(tmp_path, monkeypatch) -> None:
    if rag_memory._np is None:
        pytest.skip("numpy 未安装，跳过矩阵路径对照")

    import random

    random.seed(7)
    store = SqliteVectorStore(tmp_path / "rag.sqlite3")
    await store.load()
    docs = [
        RagDocument(
            id=f"d{index}",
            text="",
            vector=[random.uniform(-1, 1) for _ in range(8)],
            conversation_id=f"g:{index % 3}",
            timestamp=f"2026-01-{index % 28 + 1:02d}",
        )
        for index in range(120)
    ]
    await store.upsert_many(docs)

    numpy_module = rag_memory._np
    try:
        for case in (
            {},
            {"conversation_id": "g:1"},
            {"before_ts": "2026-01-15"},
            {"conversation_id": "g:2", "before_ts": "2026-01-20"},
        ):
            for _ in range(5):
                query = [random.uniform(-1, 1) for _ in range(8)]
                numpy_hits = store.top_k(query, k=10, **case)
                monkeypatch.setattr(rag_memory, "_np", None)
                python_hits = store.top_k(query, k=10, **case)
                monkeypatch.setattr(rag_memory, "_np", numpy_module)
                assert len(numpy_hits) == len(python_hits)
                assert [hit.score for hit in numpy_hits] == pytest.approx(
                    [hit.score for hit in python_hits], abs=1e-4
                )
                # float32 矩阵乘法与 float64 点积在 k 边界上可能把近似同分
                # 的两个条目排出不同顺序；只允许同分条目换位。
                for numpy_hit, python_hit in zip(
                    numpy_hits, python_hits, strict=True
                ):
                    if numpy_hit.document.id != python_hit.document.id:
                        assert abs(numpy_hit.score - python_hit.score) <= 1e-4
                assert all(
                    left.score >= right.score - 1e-6
                    for left, right in zip(
                        numpy_hits, numpy_hits[1:], strict=False
                    )
                )
    finally:
        monkeypatch.setattr(rag_memory, "_np", numpy_module)
        await store.close()


@pytest.mark.asyncio
async def test_pure_python_fallback_when_numpy_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(rag_memory, "_np", None)
    store = SqliteVectorStore(tmp_path / "rag.sqlite3")
    await store.load()
    await store.upsert_many(
        [
            _doc("diagonal", [3.0, 4.0]),
            _doc("same", [1.0, 0.0]),
        ]
    )
    assert store._matrix is None
    hits = store.top_k([1.0, 0.0], k=2)
    assert [hit.document.id for hit in hits] == ["same", "diagonal"]
    assert hits[1].score == pytest.approx(0.6)
    await store.close()
