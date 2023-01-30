from __future__ import annotations
import logging
import os
from shutil import rmtree
from signal import SIGINT
import subprocess
import tempfile
from time import sleep
import anyio
import click
import requests
from .checker import HealthStatus
from .config import MATNWB_INSTALL_DIR
from .core import log


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
@click.argument("dandisets", nargs=-1)
def check(
    dataset_path: anyio.Path,
    mount_point: anyio.Path,
    dandiset_jobs: int,
    dandisets: tuple[str],
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
                anyio.run(hs.run, dandisets, dandiset_jobs)
            finally:
                p.send_signal(SIGINT)


@main.command()
def report() -> None:
    r"""
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
            passed, failed, timedout = r.combined_counts()
            asset_qtys[Outcome.PASS] += passed
            asset_qtys[Outcome.FAIL] += failed
            asset_qtys[Outcome.TIMEOUT] += timedout
            dandiset_qtys[Outcome.PASS] += bool(not failed and not timedout)
            dandiset_qtys[Outcome.FAIL] += bool(failed)
            dandiset_qtys[Outcome.TIMEOUT] += bool(timedout)
            for tn in TEST_NAMES:
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
                f"| [{did}][results/{did}/status.yaml] | "
                + " | ".join(tests[tn] for tn in TEST_NAMES)
                + " |\n"
            )
    """
    raise NotImplementedError


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
