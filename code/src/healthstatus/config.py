from pathlib import Path

DANDI_API_URL = "https://api.dandiarchive.org/api"

MATNWB_INSTALL_DIR = Path("matnwb")  # in current working directory

PACKAGES_TO_VERSION = ["pynwb", "hdmf"]

PYNWB_OPEN_LOAD_NS_SCRIPT = Path(__file__).with_name("pynwb_open_load_ns.py")

TIMEOUT = 3600

WORKERS_PER_DANDISET = 1
