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
from types import TracebackType
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
from .util import nowstamp

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
        async def dowork(dst: DandisetTester) -> None:
            await dst.test_all_assets()
            dst.reporter.dump()

        await pool_tasks(dowork, self.aiterdandisets(), self.dandiset_jobs)

    async def run_random_assets(self, mode: str) -> None:
        if mode == "random-asset":
            tester = DandisetTester.test_random_asset
        elif mode == "random-outdated-asset-first":
            tester = DandisetTester.test_random_outdated_asset_first  # type: ignore[assignment]
        else:
            raise ValueError(f"Invalid random asset mode: {mode!r}")

        async def dowork(dst: DandisetTester) -> None:
            if await tester(dst):
                dst.reporter.dump()

        await pool_tasks(dowork, self.aiterdandisets(), self.dandiset_jobs)

    async def aiterdandisets(self) -> AsyncGenerator[DandisetTester, None]:
        async def inner() -> AsyncGenerator[DandisetInfo, None]:
            if self.dandisets:
                for did in self.dandisets:
                    yield await self.client.get_dandiset(did)
            else:
                async with aclosing(self.client.get_dandisets()) as ait:
                    async for info in ait:
                        log.info("Found Dandiset %s", info.identifier)
                        yield info

        async with aclosing(inner()) as ait:
            async for info in ait:
                yield DandisetTester(
                    reporter=DandisetReporter(
                        identifier=info.identifier,
                        draft_modified=info.draft_modified,
                        reportdir=self.reports_root / "results" / info.identifier,
                        versions=self.versions,
                    ),
                    mount_path=(
                        self.mount_point / "dandisets" / info.identifier / "draft"
                    ),
                    client=self.client,
                )


@dataclass
class DandisetTester:
    reporter: DandisetReporter
    mount_path: Path
    client: AsyncDandiClient

    @property
    def identifier(self) -> str:
        return self.reporter.identifier

    async def test_all_assets(self) -> None:
        log.info("Processing Dandiset %s", self.identifier)
        with self.reporter.session() as report:

            async def dowork(job: TestCase | Untested) -> None:
                res = await job.run()
                if isinstance(res, AssetTestResult):
                    report.register_test_result(res)
                else:
                    assert isinstance(res, UntestedAsset)
                    report.register_untested(res)

            async def aitercases() -> AsyncGenerator[TestCase | Untested, None]:
                async for asset in self.aiterassets():
                    log.info(
                        "Dandiset %s: found asset %s", self.identifier, asset.asset_path
                    )
                    if asset.is_nwb():
                        for t in TESTS:
                            yield TestCase(
                                asset=asset, testfunc=t, dandiset_id=self.identifier
                            )
                    else:
                        yield Untested(asset=asset, dandiset_id=self.identifier)

            await pool_tasks(dowork, aitercases(), WORKERS_PER_DANDISET)

    async def test_random_asset(self, all_assets: list[Asset] | None = None) -> bool:
        # Returns True if anything tested
        log.info("Scanning Dandiset %s", self.identifier)
        if all_assets is None:
            all_assets = [asset async for asset in self.aiterassets()]
        all_nwbs = [asset for asset in all_assets if asset.is_nwb()]
        if all_nwbs:
            await self.test_one_asset(choice(all_nwbs))
            self.reporter.set_asset_paths({asset.asset_path for asset in all_assets})
            return True
        else:
            log.info("Dandiset %s: no NWB assets", self.identifier)
            return False

    async def test_random_outdated_asset_first(self) -> bool:
        # Returns True if anything tested
        all_assets = [asset async for asset in self.aiterassets()]
        all_asset_paths = {asset.asset_path for asset in all_assets}
        if outdated := (self.reporter.outdated_assets() & all_asset_paths):
            p = choice(list(outdated))
            asset = Asset(filepath=self.mount_path / p, asset_path=p)
            await self.test_one_asset(asset)
            self.reporter.set_asset_paths(all_asset_paths)
            return True
        else:
            log.info(
                "Dandiset %s: no outdated assets in status.yaml; selecting from"
                " all assets in Archive",
                self.identifier,
            )
            return await self.test_random_asset(all_assets)

    async def test_one_asset(self, asset: Asset) -> None:
        async def dowork(job: TestCase) -> None:
            self.reporter.register_test_result(await job.run())

        async def aiterjobs() -> AsyncGenerator[TestCase, None]:
            for t in TESTS:
                yield TestCase(asset=asset, testfunc=t, dandiset_id=self.identifier)

        await pool_tasks(dowork, aiterjobs(), WORKERS_PER_DANDISET)

    async def aiterassets(self) -> AsyncGenerator[Asset, None]:
        async with aclosing(self.client.get_asset_paths(self.identifier)) as ait:
            async for path in ait:
                yield Asset(
                    filepath=self.mount_path / path,
                    asset_path=AssetPath(path),
                )


@dataclass
class DandisetReporter:
    identifier: str
    draft_modified: datetime | None
    reportdir: Path
    versions: dict[str, str]
    status: DandisetStatus = field(init=False)
    errors: list[TestError] = field(init=False, default_factory=list)
    started: datetime = field(init=False, default_factory=nowstamp)

    def __post_init__(self) -> None:
        try:
            self.status = DandisetStatus.from_file(self.statusfile)
        except FileNotFoundError:
            self.status = DandisetStatus(
                dandiset=self.identifier,
                draft_modified=self.draft_modified,
                tests=[TestStatus(name=testname) for testname in TESTS.keys()],
                versions=self.versions,
            )
        else:
            self.status.draft_modified = self.draft_modified

    @property
    def statusfile(self) -> Path:
        return self.reportdir / "status.yaml"

    def dump(self) -> None:
        self.status.to_file(self.statusfile)
        errors_by_test = defaultdict(list)
        for e in self.errors:
            errors_by_test[e.testname].append(e)
        for testname, errors in errors_by_test.items():
            with (
                self.reportdir
                / f"{self.started:%Y.%m.%d.%H.%M.%S}_{testname}_errors.log"
            ).open("w", encoding="utf-8", errors="surrogateescape") as fp:
                for e in errors:
                    print(
                        f"Asset: {e.asset}\nOutput:\n"
                        + textwrap.indent(e.output, " " * 4),
                        file=fp,
                    )

    def outdated_assets(self) -> set[AssetPath]:
        return {
            path for t in self.status.tests for path in t.outdated_assets(self.versions)
        }

    def session(self) -> DandisetSession:
        return DandisetSession(self)

    def register_test_result(self, r: AssetTestResult) -> None:
        self.status.update_asset(r, self.versions)
        if r.outcome is Outcome.FAIL:
            assert r.output is not None
            self.errors.append(
                TestError(
                    testname=r.testname,
                    asset=r.asset_path,
                    output=r.output,
                )
            )

    def set_asset_paths(self, paths: set[AssetPath]) -> None:
        self.status.retain(paths, self.versions)


@dataclass
class DandisetSession:
    reporter: DandisetReporter
    tests: dict[str, TestReport] = field(
        init=False, default_factory=lambda: defaultdict(TestReport)
    )
    untested: list[UntestedAsset] = field(init=False, default_factory=list)
    errors: list[TestError] = field(init=False, default_factory=list)
    ended: Optional[datetime] = field(init=False, default=None)

    def __enter__(self) -> DandisetSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.ended = datetime.now().astimezone()
            self.reporter.status = self.as_status()
            self.reporter.errors = self.errors

    def register_test_result(self, r: AssetTestResult) -> None:
        self.tests[r.testname].register(r)
        if r.outcome is Outcome.FAIL:
            assert r.output is not None
            self.errors.append(
                TestError(
                    testname=r.testname,
                    asset=r.asset_path,
                    output=r.output,
                )
            )

    def register_untested(self, d: UntestedAsset) -> None:
        self.untested.append(d)

    @property
    def nassets(self) -> int:
        return len(
            {p for report in self.tests.values() for p in report.asset_paths()}
            | {ua.asset for ua in self.untested}
        )

    def as_status(self) -> DandisetStatus:
        assert self.ended is not None
        return DandisetStatus(
            dandiset=self.reporter.identifier,
            draft_modified=self.reporter.draft_modified,
            last_run=self.reporter.started,
            last_run_ended=self.ended,
            last_run_duration=(self.ended - self.reporter.started).total_seconds(),
            nassets=self.nassets,
            versions=self.reporter.versions,
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


@dataclass
class TestReport:
    by_outcome: dict[Outcome, list[AssetTestResult]] = field(
        init=False, default_factory=lambda: defaultdict(list)
    )

    def register(self, r: AssetTestResult) -> None:
        self.by_outcome[r.outcome].append(r)

    @property
    def passed(self) -> list[AssetTestResult]:
        return self.by_outcome[Outcome.PASS]

    @property
    def failed(self) -> list[AssetTestResult]:
        return self.by_outcome[Outcome.FAIL]

    @property
    def timedout(self) -> list[AssetTestResult]:
        return self.by_outcome[Outcome.TIMEOUT]

    def asset_paths(self) -> set[AssetPath]:
        return {atr.asset_path for lst in self.by_outcome.values() for atr in lst}


@dataclass
class TestError:
    testname: str
    asset: AssetPath
    output: str
