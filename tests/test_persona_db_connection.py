"""PersonaDB 长连接复用与关闭行为。"""

from __future__ import annotations

import pytest

from mind.db import PersonaDB


@pytest.mark.asyncio
async def test_connection_is_reused_and_closed(tmp_path) -> None:
    db = PersonaDB(tmp_path / "persona.sqlite3")
    await db.load()
    first = db._conn
    assert first is not None

    await db.write_important({"note": "hello"})
    assert db._conn is first
    assert await db.read_important() == {"note": "hello"}

    await db.close()
    assert db._conn is None
    # close 幂等。
    await db.close()


@pytest.mark.asyncio
async def test_reopens_after_close(tmp_path) -> None:
    db = PersonaDB(tmp_path / "persona.sqlite3")
    await db.load()
    await db.write_important({"note": "hello"})
    await db.close()

    assert await db.read_important() == {"note": "hello"}
    assert db._conn is not None
    await db.close()
