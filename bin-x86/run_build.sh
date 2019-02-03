#!/bin/bash

export PATH=/usr/lib64/ccache:/usr/local/bin:/bin:/usr/bin:/usr/local/sbin:/usr/sbin:/home/green/.local/bin:/home/green/bin

SRCLOCATION=/home/green/git/lustre-release-base
TGTBUILD=/home/green/git/lustre-release
KERNELDIR=/home/green/bk/linux-3.10.0-957.el7-debug
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

if [ -z "$REF" -o -z "$BUILDNR" ] ; then
	echo usage: $0 gerit_reference build_nr
	exit 2
fi

touch ${BUILDLOG} || exit -1

log "Starting to work on REF ${REF} for ${DISTRO} on ${ARCH}"


cd ${TGTBUILD}
cp -a ${SRCLOCATION}/.git .
git reset --hard >/dev/null 2>&1

(git fetch http://review.whamcloud.com/fs/lustre-release $REF && git checkout -f FETCH_HEAD ) >>${BUILDLOG} 2>&1

RETVAL=$?

if [ $RETVAL -ne 0 ] ; then
        echo git checkout error!
        exit 10
fi

log "autogen.sh"
sh autogen.sh >>${BUILDLOG} 2>&1
log "Configure"
./configure --with-linux=${KERNELDIR}  --with-zfs=/usr/local/src/zfs-0.7.11 --with-spl=/usr/local/src/spl-0.7.11 --with-zfs-devel=/usr/local --disable-shared >>${BUILDLOG} 2>&1
RETVAL=$?
if [ $RETVAL -ne 0 ] ; then
        echo configure error!
        exit 12
fi

log "building"
make -j8 >>${BUILDLOG} 2>&1
RETVAL=$?
if [ $RETVAL -ne 0 ] ; then
        echo build error!
        exit 14
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

mksquashfs . ${OUTDIR}/lustre${EXTRANAME}.ssq -comp xz -no-exports -no-progress

# Also add kernel, initrd and compressed debug kernel
cp ${KERNELDIR}/arch/${ARCH}/boot/bzImage ${OUTDIR}/kernel${EXTRANAME}
cp ${KERNELDIR}/initrd.img ${OUTDIR}/initrd${EXTRANAME}.img
cp ${KERNELDIR}/vmlinux.xz ${OUTDIR}/debug-vmlinux${EXTRANAME}.xz

log "BUILDID ${BUILDNR} for ${HASH} completed"
