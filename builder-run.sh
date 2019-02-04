#!/bin/bash

# Add parsing of parameters

exec systemd-nspawn -M "$5" -q --read-only --bind=$1:/tmp/out --bind-ro=/home/green/build-and-test/bin-x86:/home/green/bin --tmpfs=/home/green/git/lustre-release:mode=777,size=3G -D /exports/centos7-base -u $4 /home/green/bin/run_build.sh $2 $3
