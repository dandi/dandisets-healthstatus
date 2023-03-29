from __future__ import annotations
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
import subprocess
import requests
from .config import PACKAGES_TO_VERSION
from .core import log


def get_package_versions() -> dict[str, str]:
    return {pkg: version(pkg) for pkg in PACKAGES_TO_VERSION}


@dataclass
class MatNWBInstaller:
    install_dir: Path

    def get_latest_tag(self) -> str:
        r = requests.get(
            "https://api.github.com/repos/NeurodataWithoutBorders/matnwb/releases/latest"
        )
        r.raise_for_status()
        version = r.json()["tag_name"]
        assert isinstance(version, str)
        return version

    def download(self, update: bool) -> bool:
        # Returns True if checked-out clone contents changed
        if not self.install_dir.exists():
            self.install_dir.mkdir(parents=True)
            log.info("Cloning NeurodataWithoutBorders/matnwb")
            subprocess.run(
                [
                    "git",
                    "clone",
                    "https://github.com/NeurodataWithoutBorders/matnwb.git",
                    str(self.install_dir),
                ],
                check=True,
            )
            subprocess.run(
                ["git", "checkout", self.get_latest_tag()],
                cwd=str(self.install_dir),
                check=True,
            )
            return True
        elif update:
            server_version = self.get_latest_tag()
            clone_version = self.get_version()
            if server_version != clone_version:
                log.info("Updating NeurodataWithoutBorders/matnwb")
                subprocess.run(["git", "fetch"], cwd=str(self.install_dir), check=True)
                subprocess.run(
                    ["git", "checkout", server_version],
                    cwd=str(self.install_dir),
                    check=True,
                )
                return True
            else:
                return False
        else:
            return False

    def install(self, update: bool = True) -> None:
        if self.download(update):
            log.info("Installing NeurodataWithoutBorders/matnwb")
            subprocess.run(
                ["git", "clean", "-dxf"], cwd=str(self.install_dir), check=True
            )
            subprocess.run(
                [
                    "matlab",
                    "-nodesktop",
                    "-sd",
                    str(self.install_dir),
                    "-batch",
                    "generateCore()",
                ],
                check=True,
            )

    def get_version(self) -> str:
        return subprocess.run(
            ["git", "describe", "--tags"],
            cwd=str(self.install_dir),
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
