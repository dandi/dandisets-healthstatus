#!/usr/bin/env pipx run
# /// script
# requires-python = ">=3.8"
# dependencies = ["PyYAML"]
# ///

from __future__ import annotations
from pathlib import Path
import re
import sys
import yaml


def main() -> None:
    resultsdir = Path(sys.argv[1])
    for p in resultsdir.iterdir():
        if p.is_dir() and re.fullmatch(r"[0-9]{6}", p.name):
            statusfile = p / "status.yaml"
            with statusfile.open() as fp:
                data = yaml.safe_load(fp)
            for t in data["tests"]:
                testname = t["name"]
                for listfield, status in [
                    ("assets_nok", "fail"),
                    ("assets_ok", "pass"),
                    ("assets_timeout", "timeout"),
                ]:
                    newlist = []
                    for entry in t[listfield]:
                        if isinstance(entry, str):
                            newlist.append(
                                {
                                    "path": entry,
                                    "test": testname,
                                    "status": status,
                                }
                            )
                        else:
                            newlist.append(
                                {
                                    "path": entry["path"],
                                    "test": testname,
                                    "status": status,
                                    "versions": entry["versions"],
                                }
                            )
                    t[listfield] = newlist
            if "untested" in data:
                for entry in data["untested"]:
                    entry["status"] = "untested"
            statusfile.write_text(yaml.dump(data))


if __name__ == "__main__":
    main()
