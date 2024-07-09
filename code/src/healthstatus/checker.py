from __future__ import annotations
from collections import defaultdict
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import datetime
from os.path import getsize
from pathlib import Path
from random import choice
import sys
import textwrap
from typing import Optional
import anyio
from .adandi import AsyncDandiClient, DandisetInfo
from .aioutil import pool_tasks
from .config import DANDI_API_URL, WORKERS_PER_DANDISET
from .core import (
    Asset,
    AssetPath,
    AssetTestResult,
    DandisetStatus,
    Outcome,
    TestStatus,
    UntestedAsset,
    log,
)
from .tests import TESTS, Test

if sys.version_info[:2] >= (3, 10):
    from contextlib import aclosing
else:
    from async_generator import aclosing


@dataclass
class TestCase:
    dandiset_id: str
    asset: Asset
    testfunc: Test

    async def run(self) -> AssetTestResult:
        r = await self.testfunc.run(self.asset.filepath)
        log.info(
            "Dandiset %s, asset %s, test %s: %s",
            self.dandiset_id,
            self.asset.asset_path,
            self.testfunc.NAME,
            r.outcome.name,
        )
        return AssetTestResult(
            testname=self.testfunc.NAME,
            asset_path=self.asset.asset_path,
            result=r,
        )


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
    client: AsyncDandiClient = field(init=False)
    mount_point: Path
    reports_root: Path
    dandisets: tuple[str, ...]
    dandiset_jobs: int
    versions: dict[str, str]

    def __post_init__(self) -> None:
        self.client = AsyncDandiClient(api_url=DANDI_API_URL)

    async def run_all(self) -> None:
        async def dowork(dandiset: Dandiset) -> None:
            (await dandiset.test_all_assets()).dump()

        await pool_tasks(dowork, self.aiterdandisets(), self.dandiset_jobs)

    async def run_random_assets(self, mode: str) -> None:
        if mode == "random-asset":
            tester = Dandiset.test_random_asset
        elif mode == "random-outdated-asset-first":
            tester = Dandiset.test_random_outdated_asset_first
        else:
            raise ValueError(f"Invalid random asset mode: {mode!r}")

        async def dowork(dandiset: Dandiset) -> None:
            report = await tester(dandiset)
            if report is not None:
                report.dump()

        await pool_tasks(dowork, self.aiterdandisets(), self.dandiset_jobs)

    async def aiterdandisets(self) -> AsyncGenerator[Dandiset, None]:
        if self.dandisets:
            for did in self.dandisets:
                yield self.make_dandiset(await self.client.get_dandiset(did))
        else:
            async with aclosing(self.client.get_dandisets()) as ait:
                async for info in ait:
                    log.info("Found Dandiset %s", info.identifier)
                    yield self.make_dandiset(info)

    def make_dandiset(self, info: DandisetInfo) -> Dandiset:
        return Dandiset(
            identifier=info.identifier,
            draft_modified=info.draft_modified,
            path=self.mount_point / "dandisets" / info.identifier / "draft",
            reportdir=self.reports_root / "results" / info.identifier,
            versions=self.versions,
        )


@dataclass
class Dandiset:
    identifier: str
    draft_modified: datetime
    path: Path
    reportdir: Path
    versions: dict[str, str]

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
            if isinstance(res, AssetTestResult):
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
                    for t in TESTS:
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
        all_assets = [asset async for asset in self.aiterassets()]
        all_nwbs = [asset for asset in all_assets if asset.is_nwb()]
        if all_nwbs:
            report = await self.test_one_asset(choice(all_nwbs))
            report.set_asset_paths({asset.asset_path for asset in all_assets})
            return report
        else:
            log.info("Dandiset %s: no NWB assets", self.identifier)
            return None

    async def test_random_outdated_asset_first(self) -> Optional[AssetReport]:
        try:
            status = self.load_status()
        except FileNotFoundError:
            asset_paths = set()
        else:
            asset_paths = {
                path for t in status.tests for path in t.outdated_assets(self.versions)
            }
        if asset_paths:
            p = choice(list(asset_paths))
            asset = Asset(filepath=self.path / p, asset_path=p)
            report = await self.test_one_asset(asset)
            report.set_asset_paths(
                {asset.asset_path async for asset in self.aiterassets()}
            )
            return report
        else:
            log.info(
                "Dandiset %s: no outdated assets in status.yaml; selecting from"
                " all assets in Archive",
                self.identifier,
            )
            return await self.test_random_asset()

    async def test_one_asset(self, asset: Asset) -> AssetReport:
        report = AssetReport(dandiset=self)

        async def dowork(job: TestCase) -> None:
            report.register_test_result(await job.run())

        async def aiterjobs() -> AsyncGenerator[TestCase, None]:
            for t in TESTS:
                yield TestCase(asset=asset, testfunc=t, dandiset_id=self.identifier)

        await pool_tasks(dowork, aiterjobs(), WORKERS_PER_DANDISET)
        return report

    async def aiterassets(self) -> AsyncGenerator[Asset, None]:
        async with aclosing(
            self.healthstatus.client.get_asset_paths(self.identifier)
        ) as ait:
            async for path in ait:
                yield Asset(
                    filepath=self.path / path,
                    asset_path=AssetPath(path),
                )


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

    def register_test_result(self, r: AssetTestResult) -> None:
        self.tests[r.testname].by_outcome[r.outcome].append(r)

    def register_untested(self, d: UntestedAsset) -> None:
        self.untested.append(d)

    def finished(self) -> None:
        self.ended = datetime.now().astimezone()

    def as_status(self) -> DandisetStatus:
        assert self.ended is not None
        return DandisetStatus(
            dandiset=self.dandiset.identifier,
            draft_modified=self.dandiset.draft_modified,
            last_run=self.started,
            last_run_ended=self.ended,
            last_run_duration=(self.ended - self.started).total_seconds(),
            nassets=self.nassets,
            versions=self.dandiset.versions,
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

    def dump(self) -> None:
        self.dandiset.dump_status(self.as_status())
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
    by_outcome: dict[Outcome, list[AssetTestResult]] = field(
        init=False, default_factory=lambda: defaultdict(list)
    )

    @property
    def passed(self) -> list[AssetTestResult]:
        return self.by_outcome[Outcome.PASS]

    @property
    def failed(self) -> list[AssetTestResult]:
        return self.by_outcome[Outcome.FAIL]

    @property
    def timedout(self) -> list[AssetTestResult]:
        return self.by_outcome[Outcome.TIMEOUT]


@dataclass
class AssetReport:
    dandiset: Dandiset
    results: list[AssetTestResult] = field(default_factory=list)
    started: datetime = field(default_factory=lambda: datetime.now().astimezone())
    asset_paths: set[AssetPath] | None = None

    def register_test_result(self, r: AssetTestResult) -> None:
        self.results.append(r)

    def set_asset_paths(self, paths: set[AssetPath]) -> None:
        self.asset_paths = paths

    def dump(self) -> None:
        try:
            status = self.dandiset.load_status()
        except FileNotFoundError:
            status = DandisetStatus(
                dandiset=self.dandiset.identifier,
                draft_modified=self.dandiset.draft_modified,
                tests=[TestStatus(name=testname) for testname in TESTS.keys()],
                versions=self.dandiset.versions,
            )
        for r in self.results:
            status.update_asset(r, self.dandiset.versions)
        if self.asset_paths is not None:
            status.retain(self.asset_paths, self.dandiset.versions)
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
