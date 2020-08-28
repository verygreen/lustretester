#!/bin/bash

export PATH=/usr/lib64/ccache:/usr/local/bin:/bin:/usr/bin:/usr/local/sbin:/usr/sbin:/home/green/.local/bin:/home/green/bin

SRCLOCATION=/home/green/git/lustre-release-base
TGTBUILD=/home/green/git/lustre-release
#KERNELDIR=/home/green/bk/linux-3.10.0-957.el7-debug
KERNELDIR=/home/green/bk/linux-3.10.0-1062.1.2.el7-debug
OUTDIR=/tmp/out

ARCH=$(uname -m)
. /etc/os-release
DISTRO="${ID}${VERSION_ID}"
EXTRANAME="-${DISTRO}-${ARCH}"

# log straight to the out dir for convenience I guess?
BUILDLOG=${OUTDIR}/build${EXTRANAME}.console

log() {
	echo "### "$(date +"%F %T")" ""$1" | tee -a ${BUILDLOG}
}

REF=$1
BUILDNR=$2

NCPUS=$(grep -c ^processor /proc/cpuinfo)
LAVG=$(cut -f 1 -d '.' </proc/loadavg)
# If the system is busy, e.g. with other builds or VMs or such,
# probably a good idea to reduce compile load
#if [ $LAVG -gt 2 ] ; then
#	NCPUS=$((NCPUS/2))
#fi

if [ -z "$REF" -o -z "$BUILDNR" ] ; then
	echo usage: $0 gerit_reference build_nr
	exit 2
fi

touch ${BUILDLOG} || exit -1

log "Starting to work on REF ${REF} for ${DISTRO} on ${ARCH}"


cd ${TGTBUILD}
cp -a ${SRCLOCATION}/.git . || exit 2
git reset --hard >/dev/null 2>&1

echo $REF | grep -q '^refs/' || git pull >/dev/null 2>&1
(git fetch https://review.whamcloud.com/fs/lustre-release $REF && git checkout -f FETCH_HEAD ) >>${BUILDLOG} 2>&1

RETVAL=$?

if [ $RETVAL -ne 0 ] ; then
        log "git checkout error!"
        exit 10
fi

echo "${REF}" >${OUTDIR}/REF

CONFIGUREHASH=$(cat LUSTRE-VERSION-GEN $(find -name "*.m4" -o -name "*.ac") | md5sum | cut -f1 -d " ")
log "autogen.sh"
sh autogen.sh >>${BUILDLOG} 2>&1

# See if we have cached configure runs.
# We cannot md5 all of it because it includes current git revision
# so we cut first 5 lines that contain it
# But otherwise we must ensure no autoconf files have changed.
if [ -f /tmp/confcache/${CONFIGUREHASH} ] ; then
	log "cached Configure"
	cp /tmp/confcache/${CONFIGUREHASH} ./config.cache
	# Mark it as used so we can prune old ones
	touch /tmp/confcache/${CONFIGUREHASH}
else
	UNCACHEDCONFIGURE=true
	log "Configure"
fi
./configure -C --with-linux=${KERNELDIR}  --with-zfs=/usr/local/src/zfs-0.7.13 --with-spl=/usr/local/src/spl-0.7.13 --with-zfs-devel=/usr/local --disable-shared >>${BUILDLOG} 2>&1
RETVAL=$?
if [ $RETVAL -ne 0 ] ; then
        echo "configure error!"
	tail -10 ${BUILDLOG} | sed 's/^/ /' 1>&2 # For the manager to show
	cp config.log ${OUTDIR}/config.log-${EXTRANAME}
        exit 12
fi

log "building"
make -j${NCPUS} >>${BUILDLOG} 2>&1
RETVAL=$?
if [ $RETVAL -ne 0 ] ; then
        log "build error!"
	make -j8 >/dev/null 2>${OUTDIR}/build${EXTRANAME}.stderr
	PATTERN=$(echo ${TGTBUILD}/ | sed 's/\//\\\//g')
	grep '[[:digit:]]\+:[[:digit:]]\+: ' ${OUTDIR}/build${EXTRANAME}.stderr | sed "s/^${PATTERN}//" 1>&2 # to stdout where it can be easily separated
        exit 14
fi

if [ -n "$UNCACHEDCONFIGURE" ] ; then
	cp config.cache /tmp/confcache/${CONFIGUREHASH}
fi

HASH=$(git rev-parse --short HEAD)
# We wanted to be smart with names, but what's the point if external thing does?
#NAME=$(printf "%08d" ${BUILDNR})-${HASH}-${DISTRO}-${ARCH}

# Source and debug-enabled binaries, will be compressed out of band later.
tar cf ${OUTDIR}/source-and-binaries${EXTRANAME}.tar *

# Now remove the intermediate objects and other unneeded stuff
rm -rf .git
rm -rf config*
for i in '*.c' '*.o' '*.h' '*Makefile*' '*.cmd' ; do
	find . -name "${i}" -exec rm -f {} \;
done
find -name "*.ko" -exec strip --strip-debug {} \;

rm -f ${OUTDIR}/lustre${EXTRANAME}.ssq
mksquashfs . ${OUTDIR}/lustre${EXTRANAME}.ssq -comp xz -no-exports -no-progress -noappend || exit -1

if [ ! -s ${OUTDIR}/lustre${EXTRANAME}.ssq ] ; then
	log "Somehow mksquashfs did not create the file but did not return an error either"
	exit -1
fi

# Also add kernel, initrd and compressed debug kernel
cp ${KERNELDIR}/arch/${ARCH}/boot/bzImage ${OUTDIR}/kernel${EXTRANAME} || exit -1
cp ${KERNELDIR}/initrd.img ${OUTDIR}/initrd${EXTRANAME}.img || exit -1
cp ${KERNELDIR}/vmlinux.xz ${OUTDIR}/debug-vmlinux${EXTRANAME}.xz || exit -1

log "BUILDID ${BUILDNR} ${DISTRO} for ${HASH} completed"
