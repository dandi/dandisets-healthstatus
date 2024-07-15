from __future__ import annotations
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib.metadata import version
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import ClassVar, Protocol
import anyio
from .config import (
    MATNWB_INSTALL_DIR,
    PACKAGES_TO_VERSION,
    PYNWB_OPEN_LOAD_NS_SCRIPT,
    TIMEOUT,
)
from .core import Outcome, TestResult, log
from .util import MatNWBInstaller


class Test(Protocol):
    NAME: ClassVar[str]

    def prepare(self, minimal: bool = False) -> dict[str, str]:
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

    def prepare(self, minimal: bool = False) -> dict[str, str]:  # noqa: U100
        return {pkg: version(pkg) for pkg in PACKAGES_TO_VERSION}

    async def run(self, path: Path) -> TestResult:
        return await run_test_command(
            [sys.executable, os.fspath(PYNWB_OPEN_LOAD_NS_SCRIPT), os.fspath(path)],
        )


@TESTS.register
@TIMED_TESTS.register
class MatNwbTest:
    NAME: ClassVar[str] = "matnwb_nwbRead"

    def prepare(self, minimal: bool = False) -> dict[str, str]:
        installer = MatNWBInstaller(MATNWB_INSTALL_DIR)
        if minimal:
            installer.download(update=False)
        else:
            installer.install(update=True)
        return {"matnwb": installer.get_version()}

    async def run(self, path: Path) -> TestResult:
        return await run_test_command(
            ["matlab", "-nodesktop", "-batch", f"nwb = nwbRead({str(path)!r})"],
            env={"MATLABPATH": os.fspath(MATNWB_INSTALL_DIR)},
        )


@TIMED_TESTS.register
class DandiLsTest:
    NAME: ClassVar[str] = "dandi_ls"

    def prepare(self, minimal: bool = False) -> dict[str, str]:  # noqa: U100
        return {}

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
    log.debug("Running: %s", shlex.join(command))
    start = time.perf_counter()
    try:
        with anyio.fail_after(TIMEOUT):
            r = await anyio.run_process(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                env=env,
            )
    except TimeoutError:
        end = time.perf_counter()
        return TestResult(outcome=Outcome.TIMEOUT, elapsed=end - start)
    else:
        end = time.perf_counter()
        if r.returncode == 0:
            return TestResult(outcome=Outcome.PASS, elapsed=end - start)
        else:
            return TestResult(
                outcome=Outcome.FAIL,
                elapsed=end - start,
                output=r.stdout.decode("utf-8", "surrogateescape"),
            )
