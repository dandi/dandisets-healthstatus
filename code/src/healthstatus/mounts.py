from __future__ import annotations
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from enum import Enum
import os
from pathlib import Path
import re
from signal import SIGINT
import subprocess
from time import sleep
import click
from ghreq import Client
import requests
from .core import log


@dataclass
class AssetInDandiset:
    dandiset_id: str
    asset_path: str

    @classmethod
    def parse(cls, s: str) -> AssetInDandiset:
        (dandiset_id, _, asset_path) = s.partition("/")
        if not asset_path or not re.fullmatch(r"[0-9]{6}", dandiset_id):
            raise ValueError(
                f"invalid asset-in-dandiset: {s!r}: must be in form"
                " <dandiset-id>/<asset-path>"
            )
        return AssetInDandiset(dandiset_id, asset_path)


@dataclass
class MountBenchmark:
    mount_name: str
    asset: AssetInDandiset
    testname: str
    elapsed: float


class Mounter(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def mount(self) -> AbstractContextManager[None]:
        ...

    @abstractmethod
    def resolve(self, asset: AssetInDandiset) -> Path:
        ...


@dataclass
class FuseMounter(Mounter):
    dataset_path: Path
    mount_path: Path
    update: bool = True
    logdir: Path = field(default_factory=Path)

    @property
    def name(self) -> str:
        return "fusefs"

    @contextmanager
    def mount(self) -> Iterator[None]:
        if not self.dataset_path.exists():
            log.info("Cloning dandi/dandisets to %s ...", self.dataset_path)
            subprocess.run(
                [
                    "datalad",
                    "clone",
                    "git@github.com:dandi/dandisets.git",
                    os.fspath(self.dataset_path),
                ],
                check=True,
            )
        elif self.update:
            update_dandisets(self.dataset_path)
        with (self.logdir / "fuse.log").open("wb") as fp:
            log.debug("Starting `datalad fusefs` process ...")
            with subprocess.Popen(
                [
                    "datalad",
                    "fusefs",
                    "-d",
                    os.fspath(self.dataset_path),
                    "--foreground",
                    "--mode-transparent",
                    os.fspath(self.mount_path),
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

    def resolve(self, asset: AssetInDandiset) -> Path:
        return self.mount_path / asset.dandiset_id / asset.asset_path


@dataclass
class DandiDavMounter(Mounter):
    mount_path: Path
    logdir: Path = field(default_factory=Path)

    @contextmanager
    def dandidav(self) -> Iterator[str]:
        with (self.logdir / "dandidav.log").open("wb") as fp:
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

    @abstractmethod
    def mount_webdav(self, url: str) -> AbstractContextManager[None]:
        ...

    @contextmanager
    def mount(self) -> Iterator[None]:
        with self.dandidav() as url:
            with self.mount_webdav(url):
                yield

    def resolve(self, asset: AssetInDandiset) -> Path:
        return self.mount_path / asset.dandiset_id / "draft" / asset.asset_path


class WebDavFSMounter(DandiDavMounter):
    @property
    def name(self) -> str:
        return "webdavfs"

    @contextmanager
    def mount_webdav(self, url: str) -> Iterator[None]:
        log.debug("Mounting webdavfs mount ...")
        subprocess.run(
            ["sudo", "mount", "-t", "webdavfs", url, os.fspath(self.mount_path)],
            check=True,
        )
        try:
            yield
        finally:
            log.debug("Unmounting webdavfs mount ...")
            subprocess.run(["sudo", "umount", os.fspath(self.mount_path)], check=True)


class DavFS2Mounter(DandiDavMounter):
    @property
    def name(self) -> str:
        return "davfs2"

    @contextmanager
    def mount_webdav(self, url: str) -> Iterator[None]:
        raise NotImplementedError


class MountType(Enum):
    FUSEFS = "fusefs"
    WEBDAVFS = "webdavfs"
    DAVFS2 = "davfs2"


def iter_mounters(
    types: set[MountType],
    dataset_path: Path | None,
    mount_path: Path,
    update_dataset: bool = True,
) -> Iterator[Mounter]:
    logdir = Path()
    if MountType.FUSEFS in types:
        if dataset_path is None:
            raise click.UsageError("--dataset-path must be set when using fusefs mount")
        yield FuseMounter(
            dataset_path=dataset_path,
            mount_path=mount_path,
            update=update_dataset,
            logdir=logdir,
        )
    if MountType.WEBDAVFS in types:
        yield WebDavFSMounter(mount_path=mount_path, logdir=logdir)
    if MountType.DAVFS2 in types:
        yield DavFS2Mounter(mount_path=mount_path, logdir=logdir)


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
