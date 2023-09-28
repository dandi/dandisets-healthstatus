#!/bin/sh

set -eu; 

cd $(dirname $0)

while true; do 
	chronic ./run.sh --mode random-outdated-asset-first; 
done;

