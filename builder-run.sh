#!/bin/bash

# Add parsing of parameters

# XXX - we run the builder at the lowest priority so that it does not disrupt
# parallel VMs
exec nice -n 19 systemd-nspawn -M "$5" -q --read-only --bind=$1:/tmp/out --bind-ro=/home/green/build-and-test/bin-x86:/home/green/bin --tmpfs=/home/green/git/lustre-release:mode=777,size=3G -D /exports/centos7-base -u $4 /home/green/bin/run_build.sh $2 $3
