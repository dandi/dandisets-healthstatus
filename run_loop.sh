#!/bin/sh

set -eu; 

while true; do 
	chronic ./run.sh --mode random-outdated-asset-first; 
done;

