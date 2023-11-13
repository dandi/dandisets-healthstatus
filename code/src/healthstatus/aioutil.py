from __future__ import annotations
from collections.abc import AsyncGenerator, Callable
import math
import sys
from typing import Awaitable, TypeVar
import anyio
from anyio.streams.memory import MemoryObjectReceiveStream

if sys.version_info[:2] >= (3, 10):
    from contextlib import aclosing
else:
    from async_generator import aclosing

T = TypeVar("T")


async def pool_tasks(
    func: Callable[[T], Awaitable[None]],
    inputs: AsyncGenerator[T, None],
    workers: int,
) -> None:
    async def dowork(rec: MemoryObjectReceiveStream[T]) -> None:
        async with rec:
            async for inp in rec:
                await func(inp)

    async with anyio.create_task_group() as tg:
        sender, receiver = anyio.create_memory_object_stream[T](math.inf)
        async with receiver:
            for _ in range(max(1, workers)):
                tg.start_soon(dowork, receiver.clone())
        async with sender, aclosing(inputs):
            async for item in inputs:
                await sender.send(item)
