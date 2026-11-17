import asyncio

from app.kv import MemoryKV


def test_memory_kv():
    async def run():
        kv = MemoryKV()
        assert await kv.set("a", "1", ex=60, nx=True)
        assert not await kv.set("a", "2", nx=True)
        assert await kv.get("a") == "1"
        await kv.set("b", "x", ex=-1)
        assert await kv.get("b") is None
        assert await kv.delete("a") == 1
        assert await kv.get("a") is None
    asyncio.run(run())
