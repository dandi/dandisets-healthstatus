#!/bin/bash

set -eu

cd "$(mktemp -d ${TMPDIR:-/tmp}/matnwb-XXXXXXX)"
pwd

ds="$1"
f="$2"

shift 
shift

git clone https://github.com/dandisets/$ds
( cd $ds; 
  echo "Version of the dandiset: ";
  git describe --always
  git annex get "$f"
)

mkdir out

git clone https://github.com/NeurodataWithoutBorders/matnwb
cd matnwb

set -x
for v in "$@"; do 
	git checkout $v; 
	echo "Running using: "; git describe --tags; 
	# Original way.
	# matlab -nodesktop -sd $PWD -batch 'generateCore()'; 
	# MATLABPATH=$PWD:$PWD/../out time matlab -nodesktop -batch "f='../$ds/$f'; disp(util.getSchemaVersion(f)); nwb = nwbRead(f, 'savedir', '../out')" || echo "Exited with $?"

	# Recommended later way for testing reading only
	MATLABPATH=$PWD time matlab -nodesktop -batch "f='../$ds/$f'; disp(util.getSchemaVersion(f)); nwb = nwbRead(f)" || echo "Exited with $?"
done

pwd
