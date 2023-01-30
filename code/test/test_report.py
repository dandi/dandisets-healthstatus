from pathlib import Path
from shutil import copytree
from traceback import format_exception
from click.testing import CliRunner, Result
from healthstatus.__main__ import main

DATA_DIR = Path(__file__).with_name("data")


def show_result(r: Result) -> str:
    if r.exception is not None:
        assert isinstance(r.exc_info, tuple)
        return "".join(format_exception(*r.exc_info))
    else:
        return r.output


def test_report() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        copytree(DATA_DIR / "report-input", "results")
        r = runner.invoke(main, ["report"], standalone_mode=False)
        assert r.exit_code == 0, show_result(r)
        with open("README.md") as fp:
            readme = fp.read()
    expected = (DATA_DIR / "report-output.md").read_text()
    assert readme == expected
