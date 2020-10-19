#!/bin/bash

# arguments: outdir ref buildnr userid uniqprocessname

BASENAME=$(cd $(dirname $0); echo $PWD)
DISTRO=${DISTRO:-"centos7"}
GITSOURCE=${GITSOURCE:-"/home/green/git/lustre-release"}
CONFCACHESOURCE=${CONFCACHESOURCE:-"/exports/testreports/confcache/${DISTRO}"}
REMOTEHOST=${REMOTEHOST:-"intelbox2.virtnet"}
NBDSERVER=${NBDSERVER:-"fatbox1.virtnet"}

if ! ping -n -t 2 -c 1 ${REMOTEHOST} >/dev/null; then
	echo "Cannot reach remote builder host, aborting"
	exit 1
fi
if ! nbd-client -l ${NBDSERVER} | grep -q -x $DISTRO ; then
	echo "NBD server is not serving our distro ${DISTRO}, exiting"
	exit 1
fi

# Add parsing of parameters

BSCRIPT=$(gzip -9c ${BASENAME}/distros/${DISTRO}/bin-x86/run_build.sh | base64 -w0)
ssh -q -o StrictHostKeyChecking=no root@${REMOTEHOST} "mkdir -p /tmp/build.$$/git" || exit 1
scp -q -o StrictHostKeyChecking=no -r ${GITSOURCE}/.git root@${REMOTEHOST}:"/tmp/build.$$/git/" || exit 1

exec ssh -q -o StrictHostKeyChecking=no root@${REMOTEHOST} "modprobe nbd ; echo $BSCRIPT | base64 -d | zcat > /tmp/build.$$/run_build.sh ; chmod +x /tmp/build.$$/run_build.sh;"'NODE=$(nbd-client -N '${DISTRO}' -p -b 4096 '${NBDSERVER}' | tail -1 | sed "s/.*\/dev/\/dev/") ;'"systemd-nspawn -M ${5}.builder.localnet -P -q --read-only --bind=$1:/tmp/out \
	--bind-ro=/tmp/build.$$:/home/green/bin \
	--bind-ro=/tmp/build.$$/git:/home/green/git/lustre-release-base \
	--bind=${CONFCACHESOURCE}:/tmp/confcache \
	--tmpfs=/home/green/git/lustre-release:mode=777,size=3G -i "'$NODE'" \
	-u $4 /home/green/bin/run_build.sh $2 $3 ;"'RETVAL=$?'"; rm -rf /tmp/build.$$"';sync;nbd-client -d $NODE;exit $RETVAL'
