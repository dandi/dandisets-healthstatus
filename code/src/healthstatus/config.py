from pathlib import Path
import anyio

MATNWB_INSTALL_DIR = Path("matnwb")  # in current working directory

MATNWB_SAVEDIR = "/mnt/fast/dandi/dandisets-healthstatus"

PACKAGES_TO_VERSION = ["pynwb", "hdmf"]

PYNWB_OPEN_LOAD_NS_SCRIPT = anyio.Path(__file__).with_name("pynwb_open_load_ns.py")

TIMEOUT = 3600

WORKERS_PER_DANDISET = 1
