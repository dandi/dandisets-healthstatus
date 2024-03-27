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
from typing import Any
import click
from datalad.api import Dataset
from ghreq import Client
from .core import AssetPath, log

DANDIDAV_URL = "https://webdav.dandiarchive.org"


@dataclass
class AssetInDandiset:
    dandiset_id: str
    asset_path: AssetPath

    @classmethod
    def parse(cls, s: str) -> AssetInDandiset:
        (dandiset_id, _, asset_path) = s.partition("/")
        if not asset_path or not re.fullmatch(r"[0-9]{6}", dandiset_id):
            raise ValueError(
                f"invalid asset-in-dandiset: {s!r}: must be in form"
                " <dandiset-id>/<asset-path>"
            )
        return AssetInDandiset(dandiset_id, AssetPath(asset_path))


class MountType(Enum):
    FUSEFS = "fusefs"
    WEBDAVFS = "webdavfs"
    DAVFS2 = "davfs2"


@dataclass
class MountBenchmark:
    mount_type: MountType
    asset: AssetInDandiset
    testname: str
    elapsed: float


class Mounter(ABC):
    @property
    @abstractmethod
    def type(self) -> MountType:
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
    def type(self) -> MountType:
        return MountType.FUSEFS

    @contextmanager
    def mount(self) -> Iterator[None]:
        if not self.dataset_path.exists():
            log.info("Cloning dandi/dandisets to %s ...", self.dataset_path)
            self.dataset_path.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "datalad",
                    "clone",
                    "git@github.com:dandi/dandisets.git",
                    os.fspath(self.dataset_path),
                ],
                check=True,
            )
            get_dandisets(Dataset(self.dataset_path))
        elif self.update:
            update_dandisets(self.dataset_path)
        self.mount_path.mkdir(parents=True, exist_ok=True)
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
class WebDavFSMounter(Mounter):
    mount_path: Path

    @property
    def type(self) -> MountType:
        return MountType.WEBDAVFS

    @contextmanager
    def mount(self) -> Iterator[None]:
        log.debug("Mounting webdavfs mount ...")
        self.mount_path.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "sudo",
                "mount",
                "-t",
                "webdavfs",
                "-o",
                "allow_other",
                DANDIDAV_URL,
                os.fspath(self.mount_path),
            ],
            check=True,
        )
        try:
            yield
        finally:
            log.debug("Unmounting webdavfs mount ...")
            subprocess.run(["sudo", "umount", os.fspath(self.mount_path)], check=True)

    def resolve(self, asset: AssetInDandiset) -> Path:
        return self.mount_path / asset.dandiset_id / "draft" / asset.asset_path


@dataclass
class DavFS2Mounter(Mounter):
    mount_path: Path

    @property
    def type(self) -> MountType:
        return MountType.DAVFS2

    @contextmanager
    def mount(self) -> Iterator[None]:
        log.debug("Mounting davfs2 mount ...")
        self.mount_path.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["sudo", "mount", "-t", "davfs", DANDIDAV_URL, os.fspath(self.mount_path)],
            check=True,
        )
        try:
            yield
        finally:
            log.debug("Unmounting davfs2 mount ...")
            subprocess.run(["sudo", "umount", os.fspath(self.mount_path)], check=True)

    def resolve(self, asset: AssetInDandiset) -> Path:
        return self.mount_path / asset.dandiset_id / "draft" / asset.asset_path


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
        yield WebDavFSMounter(mount_path=mount_path)
    if MountType.DAVFS2 in types:
        yield DavFS2Mounter(mount_path=mount_path)


def update_dandisets(dataset_path: Path) -> None:
    log.info("Updating Dandisets dataset ...")
    ds = Dataset(dataset_path)
    ds.update(follow="parentds", how="ff-only", recursive=True, recursion_limit=1)
    get_dandisets(ds)


def get_dandisets(ds: Any) -> None:
    # Fetch just the public repositories from the dandisets org, and then get
    # or update just those subdatasets rather than trying to get all
    # subdatasets and failing on private/embargoed ones
    datasets = set()
    with Client() as client:
        for repo in client.paginate("/users/dandisets/repos"):
            name = repo["name"]
            if re.fullmatch("[0-9]{6}", name):
                datasets.add(name)
    for sub in ds.subdatasets(state="present"):
        name = Path(sub["path"]).relative_to(ds.pathobj).as_posix()
        datasets.discard(name)
    if datasets:
        ds.get(path=list(datasets), jobs=5, get_data=False)
