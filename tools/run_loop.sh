#!/bin/sh

set -eu;

cd $(dirname $0)

while true; do
	echo "Let's do it"
	chronic ./run.sh --mode random-outdated-asset-first || { echo "run exited with $?. Sleeping some and going for the next round"; sleep 600; }
done;
