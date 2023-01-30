from __future__ import annotations
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List
from pydantic import BaseModel
from ruamel.yaml import YAML
from .core import Outcome


class TestStatus(BaseModel):
    assets_nok: List[str]
    assets_ok: List[str]
    assets_timeout: List[str]
    name: str

    def counts(self) -> tuple[int, int, int]:
        return (len(self.assets_ok), len(self.assets_nok), len(self.assets_timeout))

    def asset_outcomes(self) -> Iterator[tuple[str, Outcome]]:
        for asset in self.assets_ok:
            yield (asset, Outcome.PASS)
        for asset in self.assets_nok:
            yield (asset, Outcome.FAIL)
        for asset in self.assets_timeout:
            yield (asset, Outcome.TIMEOUT)

    def summary(self) -> str:
        passed, failed, timedout = self.counts()
        if passed == failed == timedout == 0:
            return "\u2014"
        else:
            return f"{passed} passed, {failed} failed, {timedout} timed out"


class DandisetStatus(BaseModel):
    identifier: str
    dandiset_version: str
    last_run: datetime
    nassets: int
    tests: List[TestStatus]
    versions: Dict[str, str]

    @classmethod
    def from_file(cls, identifier: str, yamlfile: Path) -> DandisetStatus:
        with yamlfile.open() as fp:
            return cls.parse_obj(
                {"identifier": identifier, **YAML(typ="safe").load(fp)}
            )

    def test_counts(self, test_name: str) -> tuple[int, int, int]:
        try:
            (ts,) = (t for t in self.tests if t.name == test_name)
        except ValueError:
            return (0, 0, 0)
        return ts.counts()

    def combined_counts(self) -> tuple[int, int, int]:
        asset_outcomes = defaultdict(set)
        for ts in self.tests:
            for (asset, outcome) in ts.asset_outcomes():
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

    def summary(self) -> dict[str, str]:
        summ = defaultdict(lambda: "\u2014")
        for ts in self.tests:
            summ[ts.name] = ts.summary()
        return summ


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
