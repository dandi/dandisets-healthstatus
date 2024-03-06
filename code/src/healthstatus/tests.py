from __future__ import annotations
import os
import subprocess
import sys
from typing import Optional
import anyio
from .config import MATNWB_INSTALL_DIR, PYNWB_OPEN_LOAD_NS_SCRIPT, TIMEOUT
from .core import Asset, Outcome, TestResult


async def run_test_command(
    testname: str,
    asset: Asset,
    command: list[str],
    env: Optional[dict[str, str]] = None,
) -> TestResult:
    if env is not None:
        env = {**os.environ, **env}
    try:
        with anyio.fail_after(TIMEOUT):
            r = await anyio.run_process(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                env=env,
            )
    except TimeoutError:
        return TestResult(
            testname=testname, asset_path=asset.asset_path, outcome=Outcome.TIMEOUT
        )
    else:
        if r.returncode == 0:
            return TestResult(
                testname=testname, asset_path=asset.asset_path, outcome=Outcome.PASS
            )
        else:
            return TestResult(
                testname=testname,
                asset_path=asset.asset_path,
                outcome=Outcome.FAIL,
                output=r.stdout.decode("utf-8", "surrogateescape"),
            )


async def pynwb_open_load_ns(asset: Asset) -> TestResult:
    return await run_test_command(
        "pynwb_open_load_ns",
        asset,
        [sys.executable, str(PYNWB_OPEN_LOAD_NS_SCRIPT), str(asset.filepath)],
    )


async def matnwb_nwbRead(asset: Asset) -> TestResult:
    return await run_test_command(
        "matnwb_nwbRead",
        asset,
        [
            "matlab",
            "-nodesktop",
            "-batch",
            f"nwb = nwbRead({str(asset.filepath)!r})",
        ],
        env={"MATLABPATH": str(MATNWB_INSTALL_DIR)},
    )


TESTFUNCS = [pynwb_open_load_ns, matnwb_nwbRead]

TESTS = {t.__name__: t for t in TESTFUNCS}
