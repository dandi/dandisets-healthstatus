from enum import Enum
import logging

log = logging.getLogger(__package__)


class Outcome(Enum):
    PASS = "pass"
    FAIL = "fail"
    TIMEOUT = "timeout"
