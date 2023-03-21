from __future__ import annotations
import logging
from operator import attrgetter
import os
from pathlib import Path
import re
from shutil import rmtree
from signal import SIGINT
import subprocess
import sys
import tempfile
from time import sleep
import anyio
import click
import requests
from .checker import HealthStatus
from .config import MATNWB_INSTALL_DIR
from .core import Asset, DandisetStatus, Outcome, TestSummary, log
from .tests import TESTS


@click.group()
def main() -> None:
    pass


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
    type=click.Choice(["all", "random-asset"]),
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
        reports_root=anyio.Path(os.getcwd()),
        dandisets=dandisets,
        dandiset_jobs=dandiset_jobs,
    )
    hs.versions["matnwb"] = matnwb_version
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
                elif mode == "random-asset":
                    anyio.run(hs.run_random_assets)
                else:
                    raise AssertionError(f"Unexpected mode: {mode!r}")
            finally:
                p.send_signal(SIGINT)


@main.command()
def report() -> None:
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
    all_statuses = []
    test_summaries = {tn: TestSummary(tn) for tn in TESTS.keys()}
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
            for tn in TESTS.keys():
                test_summaries[tn].register(p.name, status.test_counts(tn))
            all_statuses.append(status)
    with open("README.md", "w") as fp:
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
        print("| Dandiset | " + " | ".join(TESTS.keys()) + " | Untested |", file=fp)
        print("| --- | " + " | ".join("---" for _ in TESTS) + " | --- |", file=fp)
        for s in sorted(all_statuses, key=attrgetter("dandiset")):
            print(s.as_row(), file=fp)


@main.command()
@click.argument("testname", type=click.Choice(list(TESTS.keys())))
@click.argument(
    "files",
    nargs=-1,
    type=click.Path(exists=True, dir_okay=False, path_type=anyio.Path),
)
def test_files(testname: str, files: tuple[anyio.Path]) -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.DEBUG,
    )
    if "matnwb" in testname.lower():
        install_matnwb()
    testfunc = TESTS[testname]
    ok = True
    for f in files:
        asset = Asset(filepath=f, asset_path=str(f))
        log.info("Testing %s ...", f)
        r = anyio.run(testfunc, asset)
        if r.output is not None:
            print(r.output, end="")
        log.info("%s: %s", f, r.outcome.name)
        if r.outcome is not Outcome.PASS:
            ok = False
    sys.exit(0 if ok else 1)


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
            fp.flush()
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
