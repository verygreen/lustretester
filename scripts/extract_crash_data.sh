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

cleanup_crash_dir() {
	trap 0
	rm -rf ${TEMPDIR}
}

trap cleanup_crash_dir EXIT

nice -n 19 xzcat "${BUILDDIR}/debug-vmlinux${SUFFIX}.xz" >${TEMPDIR}/vmlinux || exit 3

# if .tar file exists - use it
test -f "${BUILDDIR}/source-and-binaries${SUFFIX}".tar && tar -C ${TEMPDIR} -a -x -f "${BUILDDIR}/source-and-binaries${SUFFIX}".tar

# If that failed to produce anything - switch to compressed
test -f ${TEMPDIR}/Makefile || tar -C ${TEMPDIR} -a -x -f "${BUILDDIR}/source-and-binaries${SUFFIX}".tar.* || exit 4

mkdir ${TEMPDIR}/modules
find ${TEMPDIR} -name "*.ko" -exec mv {} ${TEMPDIR}/modules \;
# XXX - copy other kernel modules here too

echo -e "extend lustre.so\nmod -S ${TEMPDIR}/modules\nlustre -l ${TEMPDIR}/lustre.bin\nbt -l > ${TEMPDIR}/bt.crash\nforeach bt -s -x > ${TEMPDIR}/bt.allthreads\n" | nice -n 19 crash "${COREFILE}" "${TEMPDIR}"/vmlinux > "${TEMPDIR}"/crash.out 2>&1

if [ -s "${TEMPDIR}/lustre.bin" ] ; then
	nice -n 19 ./lctl df "${TEMPDIR}/lustre.bin" >${COREFILE}-lustredebug.txt
fi
cp ${TEMPDIR}/crash.out "${COREFILE}"-crash.out
cp ${TEMPDIR}/bt.crash "${COREFILE}"-decoded-bt.txt
cp ${TEMPDIR}/bt.allthreads "${COREFILE}"-all_threads_traces.txt
# XXX - sort the threads to only leave unique

# Important for timeout cores
chmod 644 "${COREFILE}"

# Also need to link the debug kernel and sources-debugmodules into the target dir
ln -s "../.." $(dirname ${COREFILE})/debug-kernel-and-modules

rm -rf "$TEMPDIR"
exit 0
