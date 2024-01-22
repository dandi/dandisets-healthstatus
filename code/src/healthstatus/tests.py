from __future__ import annotations
from collections.abc import Iterator
from dataclasses import dataclass, field
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import ClassVar, Protocol
import anyio
from .config import MATNWB_INSTALL_DIR, PYNWB_OPEN_LOAD_NS_SCRIPT, TIMEOUT
from .core import Outcome, TestResult, log
from .util import MatNWBInstaller


class Test(Protocol):
    NAME: ClassVar[str]

    def prepare(self) -> None:
        ...

    async def run(self, path: Path) -> TestResult:
        ...


@dataclass
class TestRegistry:
    tests: dict[str, Test] = field(init=False, default_factory=dict)

    def register(self, klass: type[Test]) -> type[Test]:
        self.tests[klass.NAME] = klass()
        return klass

    def __iter__(self) -> Iterator[Test]:
        return iter(self.tests.values())

    def get(self, name: str) -> Test:
        return self.tests[name]

    def keys(self) -> Iterator[str]:
        return iter(self.tests.keys())


TESTS = TestRegistry()

TIMED_TESTS = TestRegistry()


@TESTS.register
@TIMED_TESTS.register
class PyNwbTest:
    NAME: ClassVar[str] = "pynwb_open_load_ns"

    def prepare(self) -> None:
        pass

    async def run(self, path: Path) -> TestResult:
        return await run_test_command(
            [sys.executable, os.fspath(PYNWB_OPEN_LOAD_NS_SCRIPT), os.fspath(path)],
        )


@TESTS.register
@TIMED_TESTS.register
class MatNwbTest:
    NAME: ClassVar[str] = "matnwb_nwbRead"

    def prepare(self) -> None:
        MatNWBInstaller(MATNWB_INSTALL_DIR).install(update=True)

    async def run(self, path: Path) -> TestResult:
        return await run_test_command(
            ["matlab", "-nodesktop", "-batch", f"nwb = nwbRead({str(path)!r})"],
            env={"MATLABPATH": os.fspath(MATNWB_INSTALL_DIR)},
        )


@TIMED_TESTS.register
class DandiLsTest:
    NAME: ClassVar[str] = "dandi_ls"

    def prepare(self) -> None:
        pass

    async def run(self, path: Path) -> TestResult:
        return await run_test_command(
            ["dandi", "ls", "--", os.fspath(path)],
            env={"DANDI_CACHE": "ignore"},
        )


async def run_test_command(
    command: list[str], env: dict[str, str] | None = None
) -> TestResult:
    if env is not None:
        env = {**os.environ, **env}
    elapsed: float | None = None
    try:
        with anyio.fail_after(TIMEOUT):
            start = time.perf_counter()
            log.debug("Running: %s", shlex.join(command))
            r = await anyio.run_process(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                env=env,
            )
            end = time.perf_counter()
        elapsed = end - start
    except TimeoutError:
        return TestResult(outcome=Outcome.TIMEOUT)
    else:
        if r.returncode == 0:
            return TestResult(outcome=Outcome.PASS, elapsed=elapsed)
        else:
            return TestResult(
                outcome=Outcome.FAIL,
                output=r.stdout.decode("utf-8", "surrogateescape"),
            )
