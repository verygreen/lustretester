#!/bin/bash

# arguments: outdir ref buildnr userid uniqprocessname

DISTRO=${DISTRO:-"centos7"}

ROOTDIR="/exports/${DISTRO}-base"
if [ ! -d ${ROOTDIR} -o ! -d distros/${DISTRO} ] ; then
	echo "Wrong distro $DISTRO"
	exit 1
fi

# Add parsing of parameters

# XXX - we run the builder at the lowest priority so that it does not disrupt
# parallel VMs
exec nice -n 10 systemd-nspawn -M "$5" -q --read-only --bind=$1:/tmp/out --bind-ro=/home/green/build-and-test/distros/${DISTRO}/bin-x86:/home/green/bin --tmpfs=/home/green/git/lustre-release:mode=777,size=3G -D ${ROOTDIR} -u $4 /home/green/bin/run_build.sh $2 $3
