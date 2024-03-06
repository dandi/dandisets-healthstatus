from __future__ import annotations
from collections import Counter, defaultdict
import logging
import os
from operator import attrgetter
from pathlib import Path
import re
from signal import SIGINT
import subprocess
import sys
from time import sleep
from typing import Optional
import anyio
import click
from packaging.version import Version
from .checker import AssetReport, Dandiset, HealthStatus
from .config import MATNWB_INSTALL_DIR
from .core import Asset, DandisetStatus, Outcome, TestSummary, log
from .tests import TESTS
from .util import MatNWBInstaller, get_package_versions


@click.group()
def main() -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.DEBUG,
    )


@main.command()
@click.option(
    "-d",
    "--dataset-path",
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
@click.option(
    "-m",
    "--mount-point",
    type=click.Path(file_okay=False, exists=True, path_type=anyio.Path),
    required=True,
)
@click.option(
    "--mode",
    type=click.Choice(["all", "random-asset", "random-outdated-asset-first"]),
    default="all",
    show_default=True,
)
@click.argument("dandisets", nargs=-1)
def check(
    dataset_path: anyio.Path,
    mount_point: anyio.Path,
    dandiset_jobs: int,
    dandisets: tuple[str, ...],
    mode: str,
) -> None:
    log.info("Updating Dandisets dataset ...")
    # We have embargoed dandisets which should not be cloned
    # so we want to prevent git asking password and have to avoid non-0 exit
    # from datalad
    # Ref: https://github.com/dandi/dandisets-healthstatus/issues/73
    kw = dict(
        env=dict(GIT_TERMINAL_PROMPT='0', **os.environ),
        check=False,
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
        **kw
    )
    subprocess.run(
        ["datalad", "get", "-d", str(dataset_path), "-r", "-R1", "-J5", "-n"],
        **kw
    )
    installer = MatNWBInstaller(MATNWB_INSTALL_DIR)
    installer.install(update=True)
    hs = HealthStatus(
        backup_root=mount_point,
        reports_root=Path.cwd(),
        dandisets=dandisets,
        dandiset_jobs=dandiset_jobs,
    )
    hs.versions["matnwb"] = installer.get_version()
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
                if mode == "all":
                    anyio.run(hs.run_all)
                elif mode in ("random-asset", "random-outdated-asset-first"):
                    anyio.run(hs.run_random_assets, mode)
                else:
                    raise AssertionError(f"Unexpected mode: {mode!r}")
            finally:
                p.send_signal(SIGINT)


@main.command()
def report() -> None:
    dandiset_qtys: Counter[Outcome] = Counter()
    asset_qtys: Counter[Outcome] = Counter()
    # Mapping from package names to package versions to test outcomes to
    # quantities:
    version_qtys: dict[str, dict[str, Counter[Outcome]]] = defaultdict(
        lambda: defaultdict(Counter)
    )
    all_statuses = []
    test_summaries = {tn: TestSummary(tn) for tn in TESTS.keys()}
    assets_seen = 0
    for p in Path("results").iterdir():
        if re.fullmatch(r"\d{6,}", p.name) and p.is_dir():
            status = DandisetStatus.from_file(p.name, p / "status.yaml")
            passed, failed, timedout = status.combined_counts()
            asset_qtys[Outcome.PASS] += passed
            asset_qtys[Outcome.FAIL] += failed
            asset_qtys[Outcome.TIMEOUT] += timedout
            dandiset_qtys[Outcome.PASS] += bool(not failed and not timedout and passed)
            dandiset_qtys[Outcome.FAIL] += bool(failed)
            dandiset_qtys[Outcome.TIMEOUT] += bool(timedout)
            for ati in status.iter_each_asset_test():
                assets_seen += 1
                for pkg, ver in ati.versions.items():
                    version_qtys[pkg][ver][ati.outcome] += 1
            for tn in TESTS.keys():
                test_summaries[tn].register(p.name, status.test_counts(tn))
            all_statuses.append(status)
            assets_seen += len(status.untested)
    with open("README.md", "w") as fp:
        print("# Versions (passed/failed/timed out/not tested)", file=fp)
        for pkg, data in sorted(version_qtys.items()):
            print(f"- {pkg}: ", end="", file=fp)
            for i, (ver, outcomes) in enumerate(
                sorted(data.items(), key=lambda it: Version(it[0]), reverse=True)
            ):
                if i != 0:
                    print(", ", end="", file=fp)
                passed = outcomes[Outcome.PASS]
                failed = outcomes[Outcome.FAIL]
                timedout = outcomes[Outcome.TIMEOUT]
                not_tested = assets_seen - sum(outcomes.values())
                print(
                    f"{ver} ({passed}/{failed}/{timedout}/{not_tested})",
                    end="",
                    file=fp,
                )
            print(file=fp)
        print(file=fp)
        print("# Summary", file=fp)
        print(
            "| Test / (Dandisets/assets)"
            f" | Passed ({dandiset_qtys[Outcome.PASS]}/{asset_qtys[Outcome.PASS]})"
            f" | Failed ({dandiset_qtys[Outcome.FAIL]}/{asset_qtys[Outcome.FAIL]})"
            f" | Timed Out ({dandiset_qtys[Outcome.TIMEOUT]}"
            f"/{asset_qtys[Outcome.TIMEOUT]}) |",
            file=fp,
        )
        print("| --- | --- | --- | --- |", file=fp)
        for tn in TESTS.keys():
            print(test_summaries[tn].as_row(), file=fp)
        print(file=fp)
        print("# By Dandiset", file=fp)
        print("| Dandiset | " + " | ".join(TESTS.keys()) + " | Untested |", file=fp)
        print("| --- | " + " | ".join("---" for _ in TESTS) + " | --- |", file=fp)
        for s in sorted(all_statuses, key=attrgetter("dandiset")):
            print(s.as_row(), file=fp)


@main.command()
@click.option("--save-results", is_flag=True)
@click.argument("testname", type=click.Choice(list(TESTS.keys())))
@click.argument(
    "files",
    nargs=-1,
    type=click.Path(exists=True, dir_okay=False, path_type=anyio.Path),
)
def test_files(
    testname: str, files: tuple[anyio.Path, ...], save_results: bool
) -> None:
    versions = get_package_versions()
    installer = MatNWBInstaller(MATNWB_INSTALL_DIR)
    if "matnwb" in testname.lower():
        installer.install(update=True)
    else:
        installer.download(update=False)
    versions["matnwb"] = installer.get_version()
    testfunc = TESTS[testname]
    ok = True
    dandiset_cache: dict[Path, tuple[Dandiset, set[str]]] = {}
    for f in files:
        if save_results and (path := find_dandiset(Path(f))) is not None:
            try:
                dandiset, asset_paths = dandiset_cache[path]
            except KeyError:
                dandiset = Dandiset(
                    path=anyio.Path(path), reports_root=Path.cwd(), versions=versions
                )
                asset_paths = anyio.run(dandiset.get_asset_paths)
                dandiset_cache[path] = (dandiset, asset_paths)
            report = AssetReport(dandiset=dandiset)
            ap = Path(f).relative_to(path).as_posix()
        else:
            report = None
            asset_paths = None
            ap = str(f)
        asset = Asset(filepath=f, asset_path=ap)
        log.info("Testing %s ...", f)
        r = anyio.run(testfunc, asset)
        if r.output is not None:
            print(r.output, end="")
        log.info("%s: %s", f, r.outcome.name)
        if r.outcome is not Outcome.PASS:
            ok = False
        if save_results:
            assert report is not None
            assert asset_paths is not None
            report.register_test_result(r)
            report.dump(asset_paths)
    sys.exit(0 if ok else 1)


def find_dandiset(asset: Path) -> Optional[Path]:
    if not asset.is_absolute():
        asset = asset.absolute()
    for p in asset.parents:
        if (p / "dandiset.yaml").exists():
            return p
    return None


if __name__ == "__main__":
    main()
