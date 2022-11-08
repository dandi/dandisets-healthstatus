import sys
import pynwb

with pynwb.NWBHDF5IO(sys.argv[1], "r", load_namespaces=True) as reader:
    nwbfile = reader.read()
assert repr(nwbfile)
assert str(nwbfile)
