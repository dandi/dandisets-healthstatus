from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
import logging
from typing import Optional
import anyio

log = logging.getLogger(__package__)


class Outcome(Enum):
    PASS = "pass"
    FAIL = "fail"
    TIMEOUT = "timeout"


@dataclass
class TestResult:
    testname: str
    asset: Asset
    outcome: Outcome
    output: Optional[str] = None


@dataclass
class Asset:
    filepath: anyio.Path
    asset_path: str

    def is_nwb(self) -> bool:
        return self.filepath.suffix.lower() == ".nwb"
