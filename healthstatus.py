#!/usr/bin/env python3
from __future__ import annotations
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import io
import logging
import math
import os
import pathlib
import re
from shutil import rmtree
from signal import SIGINT
import subprocess
import sys
import tempfile
import textwrap
from time import sleep
from typing import Optional
import anyio
from anyio.streams.memory import MemoryObjectReceiveStream
import click
import requests
from ruamel.yaml import YAML

if sys.version_info[:2] >= (3, 8):
    from importlib.metadata import version
else:
    from importlib_metadata import version

MATNWB_INSTALL_DIR = pathlib.Path(__file__).with_name("matnwb")

MATNWB_SAVEDIR = "/mnt/fast/dandi/dandisets-healthstatus"

PACKAGES_TO_VERSION = ["pynwb", "hdmf"]

PYNWB_OPEN_LOAD_NS_SCRIPT = anyio.Path(__file__).with_name("pynwb_open_load_ns.py")

TIMEOUT = 3600

WORKERS_PER_DANDISET = 5

log = logging.getLogger()


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


class Outcome(Enum):
    PASS = "pass"
    FAIL = "fail"
    TIMEOUT = "timeout"


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

    async def run(self, dandiset_jobs: int) -> None:
        all_reports: list[DandisetReport] = []

        async def dowork(rec: MemoryObjectReceiveStream[Dandiset]) -> None:
            async with rec:
                async for dandiset in rec:
                    log.info("Processing Dandiset %s", dandiset.identifier)
                    report = await dandiset.test_assets()
                    await report.dump(
                        self.reports_root / dandiset.identifier, self.run_data
                    )
                    all_reports.append(report)

        async with anyio.create_task_group() as tg:
            sender, receiver = anyio.create_memory_object_stream(math.inf)
            async with receiver:
                for _ in range(dandiset_jobs):
                    tg.start_soon(dowork, receiver.clone())
            async with sender:
                async for dandiset in self.aiterdandisets():
                    log.info("Found Dandiset %s", dandiset.identifier)
                    await sender.send(dandiset)

        async with await (self.reports_root / "README.md").open("w") as fp:
            dandiset_qtys = {
                Outcome.PASS: 0,
                Outcome.FAIL: 0,
                Outcome.TIMEOUT: 0,
            }
            asset_qtys = {
                Outcome.PASS: 0,
                Outcome.FAIL: 0,
                Outcome.TIMEOUT: 0,
            }
            test_summaries = {tn: TestSummary(tn) for tn in TEST_NAMES}
            for r in all_reports:
                for tn in TEST_NAMES:
                    passed, failed, timedout = r.tests[tn].counts()
                    asset_qtys[Outcome.PASS] += passed
                    asset_qtys[Outcome.FAIL] += failed
                    asset_qtys[Outcome.TIMEOUT] += timedout
                    if failed:
                        dandiset_qtys[Outcome.FAIL] += 1
                    if timedout:
                        dandiset_qtys[Outcome.TIMEOUT] += 1
                    if not failed and not timedout:
                        dandiset_qtys[Outcome.PASS] += 1
                    test_summaries[tn].register(r.identifier, r.tests[tn])
            await fp.write(
                "| Test / (Dandisets/assets)"
                f" | Passed ({dandiset_qtys[Outcome.PASS]}/{asset_qtys[Outcome.PASS]})"
                f" | Failed ({dandiset_qtys[Outcome.FAIL]}/{asset_qtys[Outcome.FAIL]})"
                f" | Timed Out ({dandiset_qtys[Outcome.TIMEOUT]}"
                f"/{asset_qtys[Outcome.TIMEOUT]}) |\n"
            )
            await fp.write("| --- | --- | --- | --- |\n")
            for tn in TEST_NAMES:
                await fp.write(test_summaries[tn].as_row() + "\n")
            await fp.write("\n")
            await fp.write("| Dandiset | " + " | ".join(TEST_NAMES) + " |\n")
            await fp.write("| --- | " + " | ".join("---" for _ in TESTS) + " |\n")
            for did, tests in sorted((r.identifier, r.summary()) for r in all_reports):
                await fp.write(
                    f"| {did} | " + " | ".join(tests[tn] for tn in TEST_NAMES) + " |\n"
                )

    async def aiterdandisets(self) -> AsyncIterator[Dandiset]:
        async for p in self.backup_root.iterdir():
            if re.fullmatch(r"\d{6,}", p.name) and await p.is_dir():
                r = await anyio.run_process(
                    ["git", "show", "-s", "--format=%H"], cwd=str(p)
                )
                yield Dandiset(
                    identifier=p.name, path=p, commit=r.stdout.decode("utf-8").strip()
                )


@dataclass
class Dandiset:
    identifier: str
    path: anyio.Path
    commit: str

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

    def summary(self) -> dict[str, str]:
        return {testname: self.tests[testname].summary() for testname in TEST_NAMES}

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

    def counts(self) -> tuple[int, int, int]:
        return (len(self.passed), len(self.failed), len(self.timedout))

    def summary(self) -> str:
        passed, failed, timedout = self.counts()
        if passed == failed == timedout == 0:
            return "\u2014"
        else:
            return f"{passed} passed, {failed} failed, {timedout} timed out"


@dataclass
class TestSummary:
    name: str
    dandisets_passed: int = 0
    assets_passed: int = 0
    dandisets_failed: dict[str, int] = field(default_factory=dict)
    assets_failed: int = 0
    dandisets_timedout: dict[str, int] = field(default_factory=dict)
    assets_timedout: int = 0

    def register(self, dandiset_id: str, report: TestReport) -> None:
        passed, failed, timedout = report.counts()
        self.assets_passed += passed
        if failed:
            self.dandisets_failed[dandiset_id] = failed
            self.assets_failed += failed
        if timedout:
            self.dandisets_timedout[dandiset_id] = timedout
            self.assets_timedout += timedout
        if not failed and not timedout:
            self.dandisets_passed += 1

    def as_row(self) -> str:
        s = f"| {self.name} | "
        if self.dandisets_passed:
            s += f"{self.dandisets_passed}/{self.assets_passed}"
        else:
            s += "\u2014"
        s += " | "
        if self.dandisets_failed:
            s += f"{len(self.dandisets_failed)}/{self.assets_failed}: " + ", ".join(
                f"[{did}]({did}/status.yaml)/{failed}"
                for did, failed in sorted(self.dandisets_failed.items())
            )
        else:
            s += "\u2014"
        if self.dandisets_timedout:
            s += f"{len(self.dandisets_timedout)}/{self.assets_timedout}: " + ", ".join(
                f"[{did}]({did}/status.yaml)/{timedout}"
                for did, timedout in sorted(self.dandisets_timedout.items())
            )
        else:
            s += "\u2014"
        s += " |"
        return s


@dataclass
class Asset:
    dandiset_id: str
    filepath: anyio.Path
    asset_path: str

    def is_nwb(self) -> bool:
        return self.filepath.suffix.lower() == ".nwb"


@click.command()
@click.option(
    "-d",
    "--dataset-path",
    type=click.Path(file_okay=False, exists=True, path_type=anyio.Path),
    required=True,
)
@click.option(
    "-m",
    "--mount-point",
    type=click.Path(file_okay=False, exists=True, path_type=anyio.Path),
    required=True,
)
@click.option(
    "-J",
    "--dandiset-jobs",
    type=int,
    default=1,
    help="Number of Dandisets to process at once",
    show_default=True,
)
def main(dataset_path: anyio.Path, mount_point: anyio.Path, dandiset_jobs: int) -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.DEBUG,
    )
    log.info("Updating Dandisets dataset ...")
    subprocess.run(
        [
            "datalad",
            "update",
            "-d",
            str(dataset_path),
            "--follow",
            "parentds",
            "--how=ff-only",
            "-r",
            "-R1",
        ],
        check=True,
    )
    subprocess.run(
        ["datalad", "get", "-d", str(dataset_path), "-r", "-R1", "-J5", "-n"],
        check=True,
    )
    matnwb_version = install_matnwb()
    hs = HealthStatus(
        backup_root=mount_point,
        reports_root=anyio.Path(__file__).parent,
    )
    hs.run_data.versions["matnwb"] = matnwb_version
    with open("fuse.log", "wb") as fp:
        with subprocess.Popen(
            [
                "datalad",
                "fusefs",
                "-d",
                str(dataset_path),
                "--foreground",
                "--mode-transparent",
                str(mount_point),
            ],
            stdout=fp,
            stderr=fp,
        ) as p:
            sleep(3)
            try:
                anyio.run(hs.run, dandiset_jobs)
            finally:
                p.send_signal(SIGINT)


def get_package_versions() -> dict[str, str]:
    return {pkg: version(pkg) for pkg in PACKAGES_TO_VERSION}


def install_matnwb() -> str:
    # Returns the matnwb version
    if MATNWB_INSTALL_DIR.exists():
        rmtree(MATNWB_INSTALL_DIR)
    MATNWB_INSTALL_DIR.mkdir(parents=True)
    with requests.Session() as s:
        log.info("Fetching latest matnwb version ...")
        r = s.get(
            "https://api.github.com/repos/NeurodataWithoutBorders/matnwb/releases/latest"
        )
        r.raise_for_status()
        data = r.json()
        version = data["tag_name"]
        assert isinstance(version, str)
        log.info("Found version %s", version)
        with tempfile.NamedTemporaryFile() as fp:
            with s.get(data["tarball_url"], stream=True) as r:
                r.raise_for_status()
                for chunk in r.iter_content(65535):
                    fp.write(chunk)
            subprocess.run(
                [
                    "tar",
                    "zxf",
                    fp.name,
                    "-C",
                    str(MATNWB_INSTALL_DIR),
                    "--strip-components=1",
                ],
                check=True,
            )
    subprocess.run(
        [
            "matlab",
            "-nodesktop",
            "-sd",
            str(MATNWB_INSTALL_DIR),
            "-batch",
            "generateCore()",
        ],
        check=True,
    )
    return version


if __name__ == "__main__":
    main()
