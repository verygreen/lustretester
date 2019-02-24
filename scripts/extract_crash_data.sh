#!/bin/bash

BUILDDIR=$1
COREFILE=$2
DISTRO=$3
ARCH=$4

cd "$(dirname $0)"

if [ ! -d "$BUILDDIR" -o ! -s "$COREFILE" -o -z "$DISTRO" -o -z "$ARCH" ] ; then
	echo "Usage: $0 builddir corefile distro arch"
	exit 1
fi

SUFFIX="-${DISTRO}-${ARCH}"
COREBASE=$(dirname "${COREFILE}")

if [ ! -s "${BUILDDIR}/debug-vmlinux${SUFFIX}.xz" ] ; then
	echo "Cannot find valid debug vmlinux"
	exit 2
fi

if [ ! -s "${BUILDDIR}/source-and-binaries${SUFFIX}".tar* ] ; then
	echo "Cannot find valid sources and binaries"
	exit 2
fi

TEMPDIR=$(mktemp -d /tmp/crash-anaysis.XXXXX)


xzcat "${BUILDDIR}/debug-vmlinux${SUFFIX}.xz" >${TEMPDIR}/vmlinux
tar -C ${TEMPDIR} -a -x -f "${BUILDDIR}/source-and-binaries${SUFFIX}".tar*
mkdir ${TEMPDIR}/modules
find ${TEMPDIR} -name "*.ko" -exec mv {} ${TEMPDIR}/modules \;

echo -e "extend lustre.so\nmod -S ${TEMPDIR}/modules\nlustre -l ${TEMPDIR}/lustre.bin\nbt -l > ${TEMPDIR}/bt.crash" | crash "${COREFILE}" "${TEMPDIR}"/vmlinux > "${TEMPDIR}"/crash.out 2>&1

if [ -s "${TEMPDIR}/lustre.bin" ] ; then
	./lctl df "${TEMPDIR}/lustre.bin" >${COREFILE}-lustredebug.txt
fi
cp ${TEMPDIR}/bt.crash ${COREFILE}-decoded-bt.txt
cp ${TEMPDIR}/crash.out ${COREFILE}-crash.out

rm -rf "$TEMPDIR"
exit 0
