from __future__ import annotations
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import logging
from operator import attrgetter
from pathlib import Path
from typing import Any, Dict, List, Literal, NewType, Optional, Sequence
from pydantic import BaseModel, Field, field_validator
import yaml
from .yamllineno import load_yaml_lineno

log = logging.getLogger(__package__)

AssetPath = NewType("AssetPath", str)


class Outcome(Enum):
    PASS = "pass"
    FAIL = "fail"
    TIMEOUT = "timeout"


@dataclass
class TestResult:
    outcome: Outcome
    elapsed: float
    output: str | None = None  # Only set if outcome is FAIL


@dataclass(frozen=True)
class Asset:
    filepath: Path
    asset_path: AssetPath

    def is_nwb(self) -> bool:
        return self.filepath.suffix.lower() == ".nwb"


@dataclass
class AssetTestResult:
    testname: str
    asset_path: AssetPath
    result: TestResult

    @property
    def outcome(self) -> Outcome:
        return self.result.outcome

    @property
    def output(self) -> str | None:
        return self.result.output


class AssetEntry(BaseModel):
    path: AssetPath
    test: str
    status: Outcome
    versions: Optional[Dict[str, str]] = None

    def to_asset_test_info(self, versions: dict[str, str]) -> AssetTestInfo:
        if self.versions is not None:
            versions = self.versions
        return AssetTestInfo(
            asset_path=self.path,
            testname=self.test,
            versions=versions,
            outcome=self.status,
        )


class TestStatus(BaseModel):
    name: str
    assets_nok: Sequence[AssetEntry] = Field(default_factory=list)
    assets_nok_lineno: Optional[int] = Field(default=None, exclude=True)
    assets_ok: Sequence[AssetEntry] = Field(default_factory=list)
    assets_ok_lineno: Optional[int] = Field(default=None, exclude=True)
    assets_timeout: Sequence[AssetEntry] = Field(default_factory=list)
    assets_timeout_lineno: Optional[int] = Field(default=None, exclude=True)

    def counts(self) -> tuple[int, int, int]:
        return (len(self.assets_ok), len(self.assets_nok), len(self.assets_timeout))

    def asset_outcomes(self) -> Iterator[tuple[str, Outcome]]:
        for asset in self.assets_ok:
            yield (asset.path, Outcome.PASS)
        for asset in self.assets_nok:
            yield (asset.path, Outcome.FAIL)
        for asset in self.assets_timeout:
            yield (asset.path, Outcome.TIMEOUT)

    def all_assets(self) -> Iterator[AssetPath]:
        for asset in [*self.assets_ok, *self.assets_nok, *self.assets_timeout]:
            yield asset.path

    def outdated_assets(self, current_versions: dict[str, str]) -> Iterator[AssetPath]:
        for asset in [*self.assets_ok, *self.assets_nok, *self.assets_timeout]:
            if asset.versions != current_versions:
                yield asset.path

    def update_asset(
        self,
        asset_path: AssetPath,
        outcome: Outcome,
        versions: Optional[dict[str, str]],
    ) -> None:
        self.assets_ok = [a for a in self.assets_ok if a.path != asset_path]
        self.assets_nok = [a for a in self.assets_nok if a.path != asset_path]
        self.assets_timeout = [a for a in self.assets_timeout if a.path != asset_path]
        if outcome is Outcome.PASS:
            alist = self.assets_ok
        elif outcome is Outcome.FAIL:
            alist = self.assets_nok
        elif outcome is Outcome.TIMEOUT:
            alist = self.assets_timeout
        else:
            raise AssertionError(f"Unexpected outcome {outcome!r}")
        alist.append(
            AssetEntry(
                path=asset_path,
                test=self.name,
                status=outcome,
                versions=versions,
            )
        )
        alist.sort(key=attrgetter("path"))

    def summary(self, pagelink: str) -> str:
        passed, failed, timedout = self.counts()
        if passed == failed == timedout == 0:
            return "\u2014"
        else:
            parts = []
            if passed:
                parts.append(f"[{passed} passed]({pagelink}#L{self.assets_ok_lineno})")
            else:
                parts.append("0 passed")
            if failed:
                parts.append(f"[{failed} failed]({pagelink}#L{self.assets_nok_lineno})")
            else:
                parts.append("0 failed")
            if timedout:
                parts.append(
                    f"[{timedout} timed out]({pagelink}#L{self.assets_timeout_lineno})"
                )
            else:
                parts.append("0 timed out")
            return ", ".join(parts)


class UntestedAsset(BaseModel):
    asset: AssetPath
    size: int
    file_type: str
    mime_type: str
    status: Literal["untested"] = "untested"


class DandisetStatus(BaseModel):
    dandiset: str
    draft_modified: Optional[datetime] = None
    last_run: Optional[datetime] = None
    last_run_ended: Optional[datetime] = None
    last_run_duration: Optional[float] = None
    nassets: Optional[int] = None
    tests: List[TestStatus]
    untested: List[UntestedAsset] = Field(default_factory=list)
    untested_lineno: int = Field(default=0, exclude=True)
    versions: Dict[str, str]

    @field_validator("versions", mode="before")
    @classmethod
    def _rmlinenos(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: v
                for k, v in value.items()
                if isinstance(k, str) and not k.endswith("_lineno")
            }
        else:
            return value

    @classmethod
    def from_file(cls, yamlfile: Path) -> DandisetStatus:
        with yamlfile.open() as fp:
            return cls.model_validate(load_yaml_lineno(fp))

    def to_file(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        jsonable = self.model_dump(mode="json")
        path.write_text(yaml.dump(jsonable))

    def update_asset(self, res: AssetTestResult, versions: dict[str, str]) -> None:
        try:
            t = next(t for t in self.tests if t.name == res.testname)
        except StopIteration:
            t = TestStatus(name=res.testname)
            self.tests.append(t)
        if versions == self.versions:
            vdict = None
        else:
            vdict = versions
        t.update_asset(res.asset_path, res.outcome, vdict)
        if vdict is not None:
            self.prune_versions(versions)

    def retain(
        self, asset_paths: set[AssetPath], current_versions: dict[str, str]
    ) -> None:
        for t in self.tests:
            t.assets_ok = [a for a in t.assets_ok if a.path in asset_paths]
            t.assets_nok = [a for a in t.assets_nok if a.path in asset_paths]
            t.assets_timeout = [a for a in t.assets_timeout if a.path in asset_paths]
        self.nassets = len(asset_paths)
        self.prune_versions(current_versions)

    def prune_versions(self, current_versions: dict[str, str]) -> None:
        if all(
            a.versions == current_versions
            for t in self.tests
            for a in [*t.assets_ok, *t.assets_nok, *t.assets_timeout]
        ):
            for t in self.tests:
                for alist in [t.assets_ok, t.assets_nok, t.assets_timeout]:
                    for a in alist:
                        a.versions = None
            self.versions = current_versions

    def test_counts(self, test_name: str) -> tuple[int, int, int]:
        try:
            (ts,) = (t for t in self.tests if t.name == test_name)
        except ValueError:
            return (0, 0, 0)
        return ts.counts()

    def combined_counts(self) -> tuple[int, int, int]:
        asset_outcomes = defaultdict(set)
        for ts in self.tests:
            for asset, outcome in ts.asset_outcomes():
                asset_outcomes[asset].add(outcome)
        passed = 0
        failed = 0
        timedout = 0
        for outcomes in asset_outcomes.values():
            if Outcome.FAIL in outcomes:
                failed += 1
            if Outcome.TIMEOUT in outcomes:
                timedout += 1
            if outcomes == {Outcome.PASS}:
                passed += 1
        return (passed, failed, timedout)

    def as_row(self) -> str:
        from .tests import TESTS

        pagelink = f"results/{self.dandiset}/status.yaml"
        tss = {ts.name: ts.summary(pagelink) for ts in self.tests}
        s = f"| [{self.dandiset}]({pagelink}) | " + " | ".join(
            tss.get(tn, "\u2014") for tn in TESTS.keys()
        )
        if self.untested:
            s += f" | [{len(self.untested)}]({pagelink}#L{self.untested_lineno})"
        else:
            s += " | \u2014"
        s += " |"
        return s

    def iter_each_asset_test(self) -> Iterator[AssetTestInfo]:
        for t in self.tests:
            for asset in t.assets_ok:
                yield asset.to_asset_test_info(self.versions)
            for asset in t.assets_nok:
                yield asset.to_asset_test_info(self.versions)
            for asset in t.assets_timeout:
                yield asset.to_asset_test_info(self.versions)


@dataclass
class TestSummary:
    name: str
    dandisets_passed: int = 0
    assets_passed: int = 0
    dandisets_failed: dict[str, int] = field(default_factory=dict)
    assets_failed: int = 0
    dandisets_timedout: dict[str, int] = field(default_factory=dict)
    assets_timedout: int = 0

    def register(self, dandiset_id: str, counts: tuple[int, int, int]) -> None:
        passed, failed, timedout = counts
        self.assets_passed += passed
        if failed:
            self.dandisets_failed[dandiset_id] = failed
            self.assets_failed += failed
        if timedout:
            self.dandisets_timedout[dandiset_id] = timedout
            self.assets_timedout += timedout
        if not failed and not timedout and passed:
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
                f"[{did}](results/{did}/status.yaml)/{failed}"
                for did, failed in sorted(self.dandisets_failed.items())
            )
        else:
            s += "\u2014"
        s += " | "
        if self.dandisets_timedout:
            s += f"{len(self.dandisets_timedout)}/{self.assets_timedout}: " + ", ".join(
                f"[{did}](results/{did}/status.yaml)/{timedout}"
                for did, timedout in sorted(self.dandisets_timedout.items())
            )
        else:
            s += "\u2014"
        s += " |"
        return s


@dataclass
class AssetTestInfo:
    asset_path: AssetPath
    testname: str
    versions: dict[str, str]
    outcome: Outcome
