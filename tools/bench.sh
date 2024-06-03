#!/bin/bash
set -ex

PYTHON="$HOME"/miniconda3/bin/python
DANDISETS_PATH="$HOME"/healthstatus-dandisets
MOUNT_PATH=/tmp/dandisets-fuse

cd "$(dirname "$0")"/..

if [ ! -e venv ]
then
    "$PYTHON" -m virtualenv venv
    . venv/bin/activate
    pip install -e './code[dandi]'
    pip install 'git+https://github.com/jwodder/filesystem_spec@rlock-cache'
else
    . venv/bin/activate
fi

dandisets-healthstatus time-mounts \
    -d "$DANDISETS_PATH" \
    -m "$MOUNT_PATH" \
    --no-update-dataset \
    --mounts fusefs,davfs2 \
    000005/sub-anm236462/sub-anm236462_ses-20140210_behavior+icephys.nwb
