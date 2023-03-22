from __future__ import annotations
from collections import defaultdict, deque
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import version
from os.path import getsize
from pathlib import Path
from random import choice
import re
import textwrap
from typing import Optional
import anyio
from .aioutil import pool_tasks
from .config import PACKAGES_TO_VERSION, WORKERS_PER_DANDISET
from .core import (
    Asset,
    DandisetStatus,
    Outcome,
    TestResult,
    TestStatus,
    UntestedAsset,
    log,
)
from .tests import TESTFUNCS, TESTS


def get_package_versions() -> dict[str, str]:
    return {pkg: version(pkg) for pkg in PACKAGES_TO_VERSION}


@dataclass
class TestCase:
    dandiset_id: str
    asset: Asset
    testfunc: Callable[[Asset], Awaitable[TestResult]]

    async def run(self) -> TestResult:
        r = await self.testfunc(self.asset)
        log.info(
            "Dandiset %s, asset %s, test %s: %s",
            self.dandiset_id,
            self.asset.asset_path,
            r.testname,
            r.outcome.name,
        )
        return r


@dataclass
class Untested:
    dandiset_id: str
    asset: Asset

    async def run(self) -> UntestedAsset:
        size = getsize(self.asset.filepath)
        r = await anyio.run_process(["file", "--brief", "-L", str(self.asset.filepath)])
        file_type = r.stdout.decode("utf-8").strip()
        r = await anyio.run_process(
            ["file", "--brief", "--mime-type", "-L", str(self.asset.filepath)]
        )
        mime_type = r.stdout.decode("utf-8").strip()
        log.info(
            "Dandiset %s, asset %s: untested, %d bytes, %s",
            self.dandiset_id,
            self.asset.asset_path,
            size,
            mime_type,
        )
        return UntestedAsset(
            asset=self.asset.asset_path,
            size=size,
            file_type=file_type,
            mime_type=mime_type,
        )


@dataclass
class HealthStatus:
    backup_root: anyio.Path
    reports_root: Path
    dandisets: tuple[str, ...]
    dandiset_jobs: int
    versions: dict[str, str] = field(default_factory=get_package_versions)

    async def run_all(self) -> None:
        async def dowork(dandiset: Dandiset) -> None:
            report = await dandiset.test_all_assets()
            report.dump(self.versions)

        await pool_tasks(dowork, self.aiterdandisets(), self.dandiset_jobs)

    async def run_random_assets(self, mode: str) -> None:
        if mode == "random-asset":

            async def tester(dandiset: Dandiset) -> Optional[AssetReport]:
                return await dandiset.test_random_asset()

        elif mode == "random-outdated-asset-first":

            async def tester(dandiset: Dandiset) -> Optional[AssetReport]:
                return await dandiset.test_random_outdated_asset_first(self.versions)

        else:
            raise ValueError(f"Invalid random asset mode: {mode!r}")

        async def dowork(dandiset: Dandiset) -> None:
            report = await tester(dandiset)
            if report is not None:
                report.dump(self.versions)

        await pool_tasks(dowork, self.aiterdandisets(), self.dandiset_jobs)

    async def aiterdandisets(self) -> AsyncGenerator[Dandiset, None]:
        if self.dandisets:
            for did in self.dandisets:
                yield await Dandiset.for_path(self.backup_root / did, self.reports_root)
        else:
            async for p in self.backup_root.iterdir():
                if re.fullmatch(r"\d{6,}", p.name) and await p.is_dir():
                    log.info("Found Dandiset %s", p.name)
                    yield await Dandiset.for_path(p, self.reports_root)


@dataclass
class Dandiset:
    identifier: str
    path: anyio.Path
    commit: str
    reports_root: Path

    @classmethod
    async def for_path(cls, path: anyio.Path, reports_root: Path) -> Dandiset:
        r = await anyio.run_process(["git", "show", "-s", "--format=%H"], cwd=str(path))
        return cls(
            identifier=path.name,
            path=path,
            commit=r.stdout.decode("utf-8").strip(),
            reports_root=reports_root,
        )

    @property
    def reportdir(self) -> Path:
        return self.reports_root / "results" / self.identifier

    @property
    def statusfile(self) -> Path:
        return self.reportdir / "status.yaml"

    def load_status(self) -> DandisetStatus:
        return DandisetStatus.from_file(self.identifier, self.statusfile)

    def dump_status(self, status: DandisetStatus) -> None:
        self.statusfile.parent.mkdir(parents=True, exist_ok=True)
        status.to_file(self.statusfile)

    async def test_all_assets(self) -> DandisetReport:
        log.info("Processing Dandiset %s", self.identifier)
        report = DandisetReport(dandiset=self)

        async def dowork(job: TestCase | Untested) -> None:
            res = await job.run()
            if isinstance(res, TestResult):
                report.register_test_result(res)
            else:
                assert isinstance(res, UntestedAsset)
                report.register_untested(res)

        async def aiterassets() -> AsyncGenerator[TestCase | Untested, None]:
            async for asset in self.aiterassets():
                log.info(
                    "Dandiset %s: found asset %s", self.identifier, asset.asset_path
                )
                report.nassets += 1
                if asset.is_nwb():
                    for t in TESTFUNCS:
                        yield TestCase(
                            asset=asset, testfunc=t, dandiset_id=self.identifier
                        )
                else:
                    yield Untested(asset=asset, dandiset_id=self.identifier)

        await pool_tasks(dowork, aiterassets(), WORKERS_PER_DANDISET)
        report.finished()
        return report

    async def test_random_asset(self) -> Optional[AssetReport]:
        log.info("Scanning Dandiset %s", self.identifier)
        all_nwbs = [asset async for asset in self.aiterassets() if asset.is_nwb()]
        if not all_nwbs:
            log.info("Dandiset %s: no NWB assets", self.identifier)
            return None
        return await self.test_one_asset(choice(all_nwbs))

    async def test_random_outdated_asset_first(
        self, versions: dict[str, str]
    ) -> Optional[AssetReport]:
        try:
            status = self.load_status()
        except FileNotFoundError:
            asset_paths = set()
        else:
            asset_paths = {
                path for t in status.tests for path in t.outdated_assets(versions)
            }
        if asset_paths:
            p = choice(list(asset_paths))
            asset = Asset(filepath=self.path / p, asset_path=p)
            return await self.test_one_asset(asset)
        else:
            log.info(
                "Dandiset %s: no outdated assets in status.yaml; selecting from"
                " all assets on disk",
                self.identifier,
            )
            return await self.test_random_asset()

    async def test_one_asset(self, asset: Asset) -> AssetReport:
        report = AssetReport(dandiset=self)

        async def dowork(job: TestCase) -> None:
            report.register_test_result(await job.run())

        async def aiterjobs() -> AsyncGenerator[TestCase, None]:
            for t in TESTFUNCS:
                yield TestCase(asset=asset, testfunc=t, dandiset_id=self.identifier)

        await pool_tasks(dowork, aiterjobs(), WORKERS_PER_DANDISET)
        return report

    async def aiterassets(self) -> AsyncIterator[Asset]:
        def mkasset(filepath: anyio.Path) -> Asset:
            return Asset(
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
                elif p.name != "dandiset.yaml":
                    yield mkasset(p)


@dataclass
class DandisetReport:
    dandiset: Dandiset
    nassets: int = 0
    tests: dict[str, TestReport] = field(
        default_factory=lambda: defaultdict(TestReport)
    )
    untested: list[UntestedAsset] = field(default_factory=list)
    started: datetime = field(default_factory=lambda: datetime.now().astimezone())
    ended: Optional[datetime] = None

    def register_test_result(self, r: TestResult) -> None:
        self.tests[r.testname].by_outcome[r.outcome].append(r)

    def register_untested(self, d: UntestedAsset) -> None:
        self.untested.append(d)

    def finished(self) -> None:
        self.ended = datetime.now().astimezone()

    def as_status(self, versions: dict[str, str]) -> DandisetStatus:
        assert self.ended is not None
        return DandisetStatus(
            dandiset=self.dandiset.identifier,
            dandiset_version=self.dandiset.commit,
            last_run=self.started,
            last_run_ended=self.ended,
            last_run_duration=(self.ended - self.started).total_seconds(),
            nassets=self.nassets,
            versions=versions,
            tests=[
                TestStatus(
                    name=name,
                    assets_ok=sorted(r.asset_path for r in self.tests[name].passed),
                    assets_nok=sorted(r.asset_path for r in self.tests[name].failed),
                    assets_timeout=sorted(
                        r.asset_path for r in self.tests[name].timedout
                    ),
                )
                for name in TESTS.keys()
                if name in self.tests
            ],
            untested=self.untested,
        )

    def dump(self, versions: dict[str, str]) -> None:
        self.dandiset.dump_status(self.as_status(versions))
        for testname, report in self.tests.items():
            if report.failed:
                with (
                    self.dandiset.reportdir
                    / f"{self.started:%Y.%m.%d.%H.%M.%S}_{testname}_errors.log"
                ).open("w", encoding="utf-8", errors="surrogateescape") as fp:
                    for r in report.failed:
                        assert r.output is not None
                        print(
                            f"Asset: {r.asset_path}\nOutput:\n"
                            + textwrap.indent(r.output, " " * 4),
                            file=fp,
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
class AssetReport:
    dandiset: Dandiset
    results: list[TestResult] = field(default_factory=list)
    started: datetime = field(default_factory=lambda: datetime.now().astimezone())

    def register_test_result(self, r: TestResult) -> None:
        self.results.append(r)

    def dump(self, versions: dict[str, str]) -> None:
        try:
            status = self.dandiset.load_status()
        except FileNotFoundError:
            status = DandisetStatus(
                dandiset=self.dandiset.identifier,
                dandiset_version=self.dandiset.commit,
                tests=[
                    TestStatus(
                        name=testname, assets_ok=[], assets_nok=[], assets_timeout=[]
                    )
                    for testname in TESTS
                ],
                versions=versions,
            )
        for r in self.results:
            status.update_asset(r, versions)
        self.dandiset.dump_status(status)
        for r in self.results:
            if r.outcome is Outcome.FAIL:
                with (
                    self.dandiset.reportdir
                    / f"{self.started:%Y.%m.%d.%H.%M.%S}_{r.testname}_errors.log"
                ).open("a", encoding="utf-8", errors="surrogateescape") as fp:
                    assert r.output is not None
                    print(
                        f"Asset: {r.asset_path}\nOutput:\n"
                        + textwrap.indent(r.output, " " * 4),
                        file=fp,
                    )
