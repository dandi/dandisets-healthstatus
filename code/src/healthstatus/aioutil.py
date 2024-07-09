from __future__ import annotations
from collections.abc import AsyncGenerator, Callable, Container, Iterator
import math
import random
import ssl
import sys
from typing import Any, Awaitable, TypeVar
import anyio
from anyio.streams.memory import MemoryObjectReceiveStream
import httpx
from .core import log

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


async def arequest(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    retry_on: Container[int] = (),
    **kwargs: Any,
) -> httpx.Response:
    waits = exp_wait(attempts=15, base=2)
    kwargs.setdefault("timeout", 60)
    while True:
        try:
            r = await client.request(method, url, follow_redirects=True, **kwargs)
            r.raise_for_status()
        except (httpx.HTTPError, ssl.SSLError) as e:
            if isinstance(e, (httpx.RequestError, ssl.SSLError)) or (
                isinstance(e, httpx.HTTPStatusError)
                and (
                    e.response.status_code >= 500 or e.response.status_code in retry_on
                )
            ):
                try:
                    delay = next(waits)
                except StopIteration:
                    raise e
                log.warning(
                    "Retrying %s request to %s in %f seconds as it raised %s: %s",
                    method.upper(),
                    url,
                    delay,
                    type(e).__name__,
                    str(e),
                )
                await anyio.sleep(delay)
                continue
            else:
                raise
        return r


def exp_wait(
    base: float = 1.25,
    multiplier: float = 1,
    attempts: int | None = None,
    jitter: float = 0.1,
) -> Iterator[float]:
    """
    Returns a generator of values usable as `sleep()` times when retrying
    something with exponential backoff.

    :param float base:
    :param float multiplier: value to multiply values by after exponentiation
    :param Optional[int] attempts: how many values to yield; set to `None` to
        yield forever
    :param Optional[float] jitter: add +1 of that jitter ratio for the time
        randomly so that wait track is unique.
    :rtype: Iterator[float]
    """
    n = 0
    while attempts is None or n < attempts:
        yield (base**n * multiplier) * (1 + (random.random() - 0.5) * jitter)
        n += 1
