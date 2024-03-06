#!/bin/bash
set -ex

PYTHON="$HOME"/miniconda3/bin/python
DANDISETS_PATH=/mnt/backup/dandi/dandisets-healthstatus/dandisets
MOUNT_PATH=/mnt/backup/dandi/dandisets-healthstatus/dandisets-fuse

cd "$(dirname "$0")"/..
#git reset --hard HEAD
#git clean -df

# TODO: Uncomment these two lines when setting up the cronjob:
#git checkout main
#git pull

"$PYTHON" -m virtualenv --clear venv
. venv/bin/activate
pip install ./code
#pip install 'git+https://github.com/fsspec/filesystem_spec'
pip install 'git+https://github.com/jwodder/filesystem_spec@rlock-cache'
dandisets-healthstatus check -d "$DANDISETS_PATH" -m "$MOUNT_PATH" -J 10 "$@"
dandisets-healthstatus report

# TODO: Uncomment this block when setting up the cronjob:
#git add .
#if ! git diff --quiet --cached
#then git commit -m "Automatically update health statuses"
#     git push
#else echo "No changes to commit"
#fi

datalad save -m "Results from a sweep of an archive: $*"
datalad push

# vim:set et sts=4:
