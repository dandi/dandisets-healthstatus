from __future__ import annotations
from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import re
from signal import SIGINT
import subprocess
from time import sleep
from ghreq import Client
import requests
from .core import log


@contextmanager
def fused(
    dataset_path: str | os.PathLike[str],
    mount_path: str | os.PathLike[str],
    update: bool = False,
    logdir: Path | None = None,
) -> Iterator[None]:
    if update:
        update_dandisets(Path(dataset_path))
    if logdir is None:
        logdir = Path()
    with (logdir / "fuse.log").open("wb") as fp:
        log.debug("Starting `datalad fusefs` process ...")
        with subprocess.Popen(
            [
                "datalad",
                "fusefs",
                "-d",
                os.fspath(dataset_path),
                "--foreground",
                "--mode-transparent",
                os.fspath(mount_path),
            ],
            stdout=fp,
            stderr=fp,
        ) as p:
            sleep(3)
            try:
                yield
            finally:
                log.debug("Terminating `datalad fusefs` process ...")
                p.send_signal(SIGINT)


def update_dandisets(dataset_path: Path) -> None:
    # Importing this at the top of the file leads to some weird import error
    # when running tests:
    from datalad.api import Dataset

    log.info("Updating Dandisets dataset ...")
    # Fetch just the public repositories from the dandisets org, and then get
    # or update just those subdatasets rather than trying to get all
    # subdatasets and failing on private/embargoed ones
    datasets = set()
    with Client() as client:
        for repo in client.paginate("/users/dandisets/repos"):
            name = repo["name"]
            if re.fullmatch("[0-9]{6}", name):
                datasets.add(name)
    ds = Dataset(dataset_path)
    ds.update(follow="parentds", how="ff-only", recursive=True, recursion_limit=1)
    for sub in ds.subdatasets(state="present"):
        name = Path(sub["path"]).relative_to(dataset_path).as_posix()
        datasets.discard(name)
    if datasets:
        ds.get(path=list(datasets), jobs=5, get_data=False)


@contextmanager
def dandidav(logdir: Path | None = None) -> Iterator[str]:
    if logdir is None:
        logdir = Path()
    with (logdir / "dandidav.log").open("wb") as fp:
        log.debug("Starting `dandidav` process ...")
        with subprocess.Popen(["dandidav"], stdout=fp, stderr=fp) as p:
            try:
                url = "http://127.0.0.1:8080"
                for _ in range(10):
                    try:
                        requests.get(url, timeout=1)
                    except requests.RequestException:
                        sleep(1)
                    else:
                        break
                else:
                    raise RuntimeError("WebDAV server did not start up time")
                yield url
            finally:
                log.debug("Terminating `dandidav` process ...")
                p.send_signal(SIGINT)


@contextmanager
def webdavfs(url: str, mount_path: str | os.PathLike[str]) -> Iterator[None]:
    log.debug("Mounting webdavfs mount ...")
    subprocess.run(
        ["sudo", "mount", "-t", "webdavfs", url, os.fspath(mount_path)],
        check=True,
    )
    try:
        yield
    finally:
        log.debug("Unmounting webdavfs mount ...")
        subprocess.run(["sudo", "umount", os.fspath(mount_path)], check=True)


@contextmanager
def davfs2(url: str, mount_path: str | os.PathLike[str]) -> Iterator[None]:
    raise NotImplementedError
