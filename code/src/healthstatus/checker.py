from __future__ import annotations
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import version
import io
import math
import os
import re
import subprocess
import sys
import textwrap
from typing import Optional
import anyio
from anyio.streams.memory import MemoryObjectReceiveStream
from ruamel.yaml import YAML
from .config import (
    MATNWB_INSTALL_DIR,
    MATNWB_SAVEDIR,
    PACKAGES_TO_VERSION,
    PYNWB_OPEN_LOAD_NS_SCRIPT,
    TIMEOUT,
    WORKERS_PER_DANDISET,
)
from .core import Outcome, log


async def run_test_command(
    testname: str,
    asset: Asset,
    command: list[str],
    env: Optional[dict[str, str]] = None,
) -> TestResult:
    if env is not None:
        env = {**os.environ, **env}
    try:
        async with anyio.fail_after(TIMEOUT):
            r = await anyio.run_process(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                env=env,
            )
    except TimeoutError:
        return TestResult(testname=testname, asset=asset, outcome=Outcome.TIMEOUT)
    else:
        if r.returncode == 0:
            return TestResult(testname=testname, asset=asset, outcome=Outcome.PASS)
        else:
            return TestResult(
                testname=testname,
                asset=asset,
                outcome=Outcome.FAIL,
                output=r.stdout.decode("utf-8", "surrogateescape"),
            )


async def pynwb_open_load_ns(asset: Asset) -> TestResult:
    return await run_test_command(
        "pynwb_open_load_ns",
        asset,
        [sys.executable, str(PYNWB_OPEN_LOAD_NS_SCRIPT), str(asset.filepath)],
    )


async def matnwb_nwbRead(asset: Asset) -> TestResult:
    return await run_test_command(
        "matnwb_nwbRead",
        asset,
        [
            "matlab",
            "-nodesktop",
            "-batch",
            (
                f"nwb = nwbRead({str(asset.filepath)!r}, 'savedir',"
                f" {MATNWB_SAVEDIR!r})"
            ),
        ],
        env={"MATLABPATH": str(MATNWB_INSTALL_DIR)},
    )


TESTS = [pynwb_open_load_ns, matnwb_nwbRead]
TEST_NAMES = [t.__name__ for t in TESTS]


@dataclass
class TestCase:
    asset: Asset
    testfunc: Callable[[Asset], Awaitable[TestResult]]

    async def run(self) -> TestResult:
        r = await self.testfunc(self.asset)
        log.info(
            "Dandiset %s, asset %s, test %s: %s",
            self.asset.dandiset_id,
            self.asset.asset_path,
            r.testname,
            r.outcome.name,
        )
        return r


@dataclass
class TestResult:
    testname: str
    asset: Asset
    outcome: Outcome
    output: Optional[str] = None


@dataclass
class RunData:
    timestamp: datetime
    versions: dict[str, str]

    @classmethod
    def get(cls) -> RunData:
        return cls(
            timestamp=datetime.now().astimezone(),
            versions=get_package_versions(),
        )


@dataclass
class HealthStatus:
    backup_root: anyio.Path
    reports_root: anyio.Path
    run_data: RunData = field(default_factory=RunData.get)

    async def run(self, dandisets: Sequence[str], dandiset_jobs: int) -> None:
        async def dowork(rec: MemoryObjectReceiveStream[Dandiset]) -> None:
            async with rec:
                async for dandiset in rec:
                    log.info("Processing Dandiset %s", dandiset.identifier)
                    report = await dandiset.test_assets()
                    await report.dump(
                        self.reports_root / "results" / dandiset.identifier,
                        self.run_data,
                    )

        async with anyio.create_task_group() as tg:
            sender, receiver = anyio.create_memory_object_stream(math.inf)
            async with receiver:
                for _ in range(dandiset_jobs):
                    tg.start_soon(dowork, receiver.clone())
            async with sender:
                if dandisets:
                    for did in dandisets:
                        log.info("Scanning Dandiset %s", did)
                        await sender.send(await self.get_dandiset(did))
                else:
                    async for ds in self.aiterdandisets():
                        log.info("Found Dandiset %s", ds.identifier)
                        await sender.send(ds)

    async def aiterdandisets(self) -> AsyncIterator[Dandiset]:
        async for p in self.backup_root.iterdir():
            if re.fullmatch(r"\d{6,}", p.name) and await p.is_dir():
                yield await Dandiset.for_path(p)

    async def get_dandiset(self, identifier: str) -> Dandiset:
        return await Dandiset.for_path(self.backup_root / identifier)


@dataclass
class Dandiset:
    identifier: str
    path: anyio.Path
    commit: str

    @classmethod
    async def for_path(cls, path: anyio.Path) -> Dandiset:
        r = await anyio.run_process(["git", "show", "-s", "--format=%H"], cwd=str(path))
        return cls(
            identifier=path.name, path=path, commit=r.stdout.decode("utf-8").strip()
        )

    async def test_assets(self) -> DandisetReport:
        report = DandisetReport(identifier=self.identifier, commit=self.commit)

        async def dowork(rec: MemoryObjectReceiveStream[TestCase]) -> None:
            async with rec:
                async for testcase in rec:
                    report.register_test_result(await testcase.run())

        async with anyio.create_task_group() as tg:
            sender, receiver = anyio.create_memory_object_stream(math.inf)
            async with receiver:
                for _ in range(WORKERS_PER_DANDISET):
                    tg.start_soon(dowork, receiver.clone())
            async with sender:
                async for asset in self.aiterassets():
                    log.info(
                        "Dandiset %s: found asset %s", self.identifier, asset.asset_path
                    )
                    report.nassets += 1
                    if asset.is_nwb():
                        for t in TESTS:
                            await sender.send(TestCase(asset=asset, testfunc=t))
        return report

    async def aiterassets(self) -> AsyncIterator[Asset]:
        def mkasset(filepath: anyio.Path) -> Asset:
            return Asset(
                dandiset_id=self.identifier,
                filepath=filepath,
                asset_path=filepath.relative_to(self.path).as_posix(),
            )

        dirs = deque([self.path])
        while dirs:
            async for p in dirs.popleft().iterdir():
                if p.name in (
                    ".dandi",
                    ".datalad",
                    ".git",
                    ".gitattributes",
                    ".gitmodules",
                ):
                    continue
                if await p.is_dir():
                    if p.suffix in (".zarr", ".ngff"):
                        yield mkasset(p)
                    else:
                        dirs.append(p)
                else:
                    yield mkasset(p)


@dataclass
class DandisetReport:
    identifier: str
    commit: str
    nassets: int = 0
    tests: dict[str, TestReport] = field(
        default_factory=lambda: defaultdict(TestReport)
    )

    def register_test_result(self, r: TestResult) -> None:
        self.tests[r.testname].by_outcome[r.outcome].append(r)

    async def dump(self, reportdir: anyio.Path, run_data: RunData) -> None:
        status = {
            "last_run": run_data.timestamp,
            "dandiset_version": self.commit,
            "nassets": self.nassets,
            "versions": run_data.versions,
            "tests": [
                {
                    "name": name,
                    "assets_ok": sorted(
                        r.asset.asset_path for r in self.tests[name].passed
                    ),
                    "assets_nok": sorted(
                        r.asset.asset_path for r in self.tests[name].failed
                    ),
                    "assets_timeout": sorted(
                        r.asset.asset_path for r in self.tests[name].timedout
                    ),
                }
                for name in TEST_NAMES
                if name in self.tests
            ],
        }
        yaml = YAML(typ="safe")
        yaml.default_flow_style = False
        out = io.StringIO()
        yaml.dump(status, out)
        await reportdir.mkdir(parents=True, exist_ok=True)
        await (reportdir / "status.yaml").write_text(out.getvalue())
        for testname, report in self.tests.items():
            if report.failed:
                async with await (
                    reportdir
                    / f"{run_data.timestamp:%Y.%m.%d.%H.%M.%S}_{testname}_errors.log"
                ).open("w", encoding="utf-8", errors="surrogateescape") as fp:
                    for r in report.failed:
                        assert r.output is not None
                        await fp.write(
                            f"Asset: {r.asset.asset_path}\nOutput:\n"
                            + textwrap.indent(r.output, " " * 4)
                            + "\n"
                        )


@dataclass
class TestReport:
    by_outcome: dict[Outcome, list[TestResult]] = field(
        init=False, default_factory=lambda: defaultdict(list)
    )

    @property
    def passed(self) -> list[TestResult]:
        return self.by_outcome[Outcome.PASS]

    @property
    def failed(self) -> list[TestResult]:
        return self.by_outcome[Outcome.FAIL]

    @property
    def timedout(self) -> list[TestResult]:
        return self.by_outcome[Outcome.TIMEOUT]


@dataclass
class Asset:
    dandiset_id: str
    filepath: anyio.Path
    asset_path: str

    def is_nwb(self) -> bool:
        return self.filepath.suffix.lower() == ".nwb"


def get_package_versions() -> dict[str, str]:
    return {pkg: version(pkg) for pkg in PACKAGES_TO_VERSION}
