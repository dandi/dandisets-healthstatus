#!/usr/bin/env python3
from __future__ import annotations
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
import io
import logging
import math
import re
from signal import SIGINT
import subprocess
import sys
import textwrap
from time import sleep
from typing import Optional
import anyio
from anyio.streams.memory import MemoryObjectReceiveStream
import click
from ruamel.yaml import YAML

if sys.version_info[:2] >= (3, 8):
    from importlib.metadata import version
else:
    from importlib_metadata import version

PACKAGES_TO_VERSION = ["pynwb", "hdmf"]

PYNWB_OPEN_LOAD_NS_SCRIPT = anyio.Path(__file__).with_name("pynwb_open_load_ns.py")

WORKERS_PER_DANDISET = 64

log = logging.getLogger()


async def pynwb_open_load_ns(asset: Asset) -> TestResult:
    r = await anyio.run_process(
        [sys.executable, str(PYNWB_OPEN_LOAD_NS_SCRIPT), str(asset.filepath)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if r.returncode == 0:
        return TestResult(testname="pynwb_open_load_ns", asset=asset, success=True)
    else:
        return TestResult(
            testname="pynwb_open_load_ns",
            asset=asset,
            success=False,
            output=r.stdout.decode("utf-8", "surrogateescape"),
        )


TESTS = [pynwb_open_load_ns]


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
            "PASSED" if r.success else "FAILED",
        )
        return r


@dataclass
class TestResult:
    testname: str
    asset: Asset
    success: bool
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

    async def run(self) -> None:
        summary: dict[str, dict[str, str]] = {}
        async for dandiset in self.aiterdandisets():
            log.info("Found Dandiset %s", dandiset.identifier)
            report = await dandiset.test_assets()
            await report.dump(self.reports_root / dandiset.identifier, self.run_data)
            summary[dandiset.identifier] = report.summary()
        async with await (self.reports_root / "README.md").open("w") as fp:
            await fp.write(
                "| Dandiset | " + " | ".join(t.__name__ for t in TESTS) + " |\n"
            )
            await fp.write("| --- | " + " | ".join("---" for _ in TESTS) + " |\n")
            for did, tests in sorted(summary.items()):
                await fp.write(
                    f"| {did} | "
                    + " | ".join(tests.get(t.__name__, "\u2014") for t in TESTS)
                    + " |\n"
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
        report = DandisetReport(commit=self.commit)

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
    commit: str
    nassets: int = 0
    tests: dict[str, TestReport] = field(
        default_factory=lambda: defaultdict(TestReport)
    )

    def register_test_result(self, r: TestResult) -> None:
        if r.success:
            self.tests[r.testname].passed.append(r)
        else:
            self.tests[r.testname].failed.append(r)

    def summary(self) -> dict[str, str]:
        return {
            testname: f"{len(report.passed)} passed, {len(report.failed)} failed"
            for testname, report in self.tests.items()
        }

    async def dump(self, reportdir: anyio.Path, run_data: RunData) -> None:
        status = {
            "last_run": run_data.timestamp,
            "dandiset_version": self.commit,
            "nassets": self.nassets,
            "versions": run_data.versions,
            "tests": [
                {
                    "name": name,
                    "assets_ok": sorted(r.asset.asset_path for r in report.passed),
                    "assets_nok": sorted(r.asset.asset_path for r in report.failed),
                }
                for name, report in self.tests.items()
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
                ).open("w") as fp:
                    for r in report.failed:
                        assert r.output is not None
                        await fp.write(
                            f"Asset: {r.asset.asset_path}\nOutput:\n"
                            + textwrap.indent(r.output, " " * 4)
                            + "\n"
                        )


@dataclass
class TestReport:
    passed: list[TestResult] = field(init=False, default_factory=list)
    failed: list[TestResult] = field(init=False, default_factory=list)


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
def main(dataset_path: anyio.Path, mount_point: anyio.Path) -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.DEBUG,
    )
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
    hs = HealthStatus(
        backup_root=mount_point,
        reports_root=anyio.Path(__file__).parent,
    )
    with open("fuse.log", "wb") as fp:
        with subprocess.Popen(
            [
                "datalad",
                "-l",
                "debug",
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
                anyio.run(hs.run)
            finally:
                p.send_signal(SIGINT)


def get_package_versions() -> dict[str, str]:
    return {pkg: version(pkg) for pkg in PACKAGES_TO_VERSION}


if __name__ == "__main__":
    main()
