#!/bin/bash
set -ex

PYTHON="$HOME"/miniconda3/bin/python
DANDISETS_PATH=/mnt/backup/dandi/dandisets-healthstatus/dandisets
MOUNT_PATH=/mnt/backup/dandi/dandisets-healthstatus/dandisets-fuse

cd "$(dirname "$0")"
#git reset --hard HEAD
#git clean -df
git checkout main
git pull

"$PYTHON" -m virtualenv --clear venv
. venv/bin/activate
pip install -q -r requirements.txt
python update.py -d "$DANDISETS_PATH" -m "$MOUNT_PATH"

git add .
if ! git diff --quiet --cached
then git commit -m "Automatically update health statuses"
     git push
else echo "No changes to commit"
fi

# vim:set et sts=4:
