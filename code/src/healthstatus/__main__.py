from __future__ import annotations
from collections import Counter, defaultdict
from csv import DictWriter
from enum import Enum
import logging
from operator import attrgetter
from pathlib import Path
import re
import sys
import textwrap
from typing import Generic, Optional, TypeVar
import anyio
import click
from packaging.version import Version
from .checker import AssetReport, Dandiset, HealthStatus
from .core import AssetPath, AssetTestResult, DandisetStatus, Outcome, TestSummary, log
from .mounts import (
    AssetInDandiset,
    DavFS2Mounter,
    MountBenchmark,
    MountType,
    iter_mounters,
)
from .tests import TESTS, TIMED_TESTS

E = TypeVar("E", bound="Enum")


class EnumSet(click.ParamType, Generic[E]):
    # The enum's values must be strs.
    name = "enumset"

    def __init__(self, klass: type[E]) -> None:
        self.klass = klass

    def convert(
        self,
        value: str | set[E],
        param: click.Parameter | None,
        ctx: click.Context | None,
    ) -> set[E]:
        if not isinstance(value, str):
            return value
        selected = set()
        for v in re.split(r"\s*,\s*", value):
            try:
                selected.add(self.klass(v))
            except ValueError:
                self.fail(f"{value!r}: invalid option {v!r}", param, ctx)
        return selected

    def get_metavar(self, _param: click.Parameter) -> str:
        return "[" + ",".join(v.value for v in self.klass) + "]"


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def main() -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.DEBUG,
    )


@main.command()
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
    type=click.Path(file_okay=False, exists=True, path_type=Path),
    help="Directory at which to mount Dandisets",
    required=True,
)
@click.option(
    "--mode",
    type=click.Choice(["all", "random-asset", "random-outdated-asset-first"]),
    help=(
        "Strategy for selecting which assets to test on.  'all' - Test all"
        " assets. 'random-asset' - Test one randomly-selected NWB per Dandiset."
        "  'random-outdated-asset-first' - Test one randomly-selected NWB per"
        " Dandiset that has not been tested with the latest package versions,"
        " falling back to 'random-asset' if there are no such assets"
    ),
    default="all",
    show_default=True,
)
@click.argument("dandisets", nargs=-1)
def check(
    mount_point: Path,
    dandiset_jobs: int,
    dandisets: tuple[str, ...],
    mode: str,
) -> None:
    """Run health status tests on Dandiset assets and record the results"""
    pkg_versions = {}
    for t in TESTS:
        pkg_versions.update(t.prepare())
    hs = HealthStatus(
        backup_root=mount_point,
        reports_root=Path.cwd(),
        dandisets=dandisets,
        dandiset_jobs=dandiset_jobs,
        versions=pkg_versions,
    )
    mnt = DavFS2Mounter(mount_path=mount_point)
    with mnt.mount():
        if mode == "all":
            anyio.run(hs.run_all)
        elif mode in ("random-asset", "random-outdated-asset-first"):
            anyio.run(hs.run_random_assets, mode)
        else:
            raise AssertionError(f"Unexpected mode: {mode!r}")


@main.command()
def report() -> None:
    """Produce a README.md file summarizing `check` results"""
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
        print(
            "| --- | " + " | ".join("---" for _ in TESTS.keys()) + " | --- |", file=fp
        )
        for s in sorted(all_statuses, key=attrgetter("dandiset")):
            print(s.as_row(), file=fp)


@main.command()
@click.option(
    "--save-results",
    is_flag=True,
    help="Record test results for Dandiset assets",
)
@click.argument("testname", type=click.Choice(list(TESTS.keys())))
@click.argument(
    "files",
    nargs=-1,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
def test_files(testname: str, files: tuple[Path, ...], save_results: bool) -> None:
    """Run a single health status test on some number of files"""
    pkg_versions = {}
    for t in TESTS:
        pkg_versions.update(t.prepare(minimal=t.NAME != testname))
    testfunc = TESTS.get(testname)
    ok = True
    dandiset_cache: dict[Path, tuple[Dandiset, set[AssetPath]]] = {}
    for f in files:
        if save_results and (path := find_dandiset(Path(f))) is not None:
            try:
                dandiset, asset_paths = dandiset_cache[path]
            except KeyError:
                dandiset = Dandiset(
                    path=path, reports_root=Path.cwd(), versions=pkg_versions
                )
                asset_paths = anyio.run(dandiset.get_asset_paths)
                dandiset_cache[path] = (dandiset, asset_paths)
            report = AssetReport(dandiset=dandiset)
            ap = AssetPath(Path(f).relative_to(path).as_posix())
        else:
            report = None
            asset_paths = None
            ap = None
        log.info("Testing %s ...", f)
        r = anyio.run(testfunc.run, f)
        if r.output is not None:
            print(r.output, end="")
        log.info("%s: %s", f, r.outcome.name)
        if r.outcome is not Outcome.PASS:
            ok = False
        if save_results:
            assert report is not None
            assert asset_paths is not None
            assert ap is not None
            atr = AssetTestResult(
                testname=testname,
                asset_path=AssetPath(ap),
                result=r,
            )
            report.register_test_result(atr)
            report.dump(asset_paths)
    sys.exit(0 if ok else 1)


@main.command()
@click.option(
    "-d",
    "--dataset-path",
    type=click.Path(file_okay=False, path_type=Path),
    help=(
        "Directory containing a clone of dandi/dandisets.  Required when using"
        " the fusefs mount."
    ),
)
@click.option(
    "-m",
    "--mount-point",
    type=click.Path(file_okay=False, exists=True, path_type=Path),
    help="Directory at which to mount Dandisets",
    required=True,
)
@click.option(
    "-M",
    "--mounts",
    type=EnumSet(MountType),
    help="Comma-separated list of mount types to test against",
    default=set(MountType),
    show_default="all",
)
@click.option(
    "--update-dataset/--no-update-dataset",
    help="Whether to update the dandi/dandisets clone before fuse-mounting",
    default=True,
    show_default=True,
)
@click.argument(
    "assets",
    nargs=-1,
    type=AssetInDandiset.parse,
    metavar="DANDISET_ID/ASSET_PATH ...",
)
def time_mounts(
    assets: tuple[AssetInDandiset, ...],
    dataset_path: Path | None,
    mount_point: Path,
    mounts: set[MountType],
    update_dataset: bool,
) -> None:
    """Run timed tests on Dandiset assets using various mounting technologies"""
    for t in TIMED_TESTS:
        t.prepare()
    results = []
    for mounter in iter_mounters(
        types=mounts,
        dataset_path=dataset_path,
        mount_path=mount_point,
        update_dataset=update_dataset,
    ):
        log.info("Testing with mount type: %s", mounter.type.value)
        with mounter.mount():
            for a in assets:
                log.info(
                    "Testing Dandiset %s, asset %s ...", a.dandiset_id, a.asset_path
                )
                fpath = mounter.resolve(a)
                for tfunc in TIMED_TESTS:
                    log.info("Running test %r", tfunc.NAME)
                    r = anyio.run(tfunc.run, fpath)
                    if r.outcome is Outcome.PASS:
                        assert r.elapsed is not None
                        log.info("Test passed in %f seconds", r.elapsed)
                        results.append(
                            MountBenchmark(
                                mount_type=mounter.type,
                                asset=a,
                                testname=tfunc.NAME,
                                elapsed=r.elapsed,
                            )
                        )
                    elif r.outcome is Outcome.FAIL:
                        assert r.output is not None
                        log.error(
                            "Test failed; output:\n\n%s\n",
                            textwrap.indent(r.output, " " * 4),
                        )
                        sys.exit(1)
                    else:
                        assert r.outcome is Outcome.TIMEOUT
                        log.error("Test timed out")
                        sys.exit(1)
    csvout = DictWriter(
        sys.stdout, ["mount_type", "dandiset", "asset", "test", "time_sec"]
    )
    csvout.writeheader()
    for res in results:
        csvout.writerow(
            {
                "mount_type": res.mount_type.value,
                "dandiset": res.asset.dandiset_id,
                "asset": res.asset.asset_path,
                "test": res.testname,
                "time_sec": res.elapsed,
            }
        )


@main.command()
@click.argument("testname", type=click.Choice(list(TIMED_TESTS.keys())))
@click.argument(
    "files",
    nargs=-1,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
def time_files(testname: str, files: tuple[Path, ...]) -> None:
    """Run a single timed test on some number of files"""
    testfunc = TIMED_TESTS.get(testname)
    testfunc.prepare()
    for f in files:
        log.info("Testing %s ...", f)
        r = anyio.run(testfunc.run, f)
        if r.outcome is Outcome.PASS:
            assert r.elapsed is not None
            log.info("Test passed in %f seconds", r.elapsed)
        elif r.outcome is Outcome.FAIL:
            assert r.output is not None
            log.error(
                "Test failed; output:\n\n%s\n", textwrap.indent(r.output, " " * 4)
            )
            sys.exit(1)
        else:
            assert r.outcome is Outcome.TIMEOUT
            log.error("Test timed out")
            sys.exit(1)


def find_dandiset(asset: Path) -> Optional[Path]:
    if not asset.is_absolute():
        asset = asset.absolute()
    for p in asset.parents:
        if (p / "dandiset.yaml").exists():
            return p
    return None


if __name__ == "__main__":
    main()
