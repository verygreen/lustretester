#!/bin/bash

. ~/bin/config

test -d "$INCOMINGDIR" || mkdir -p "$INCOMINGDIR"
test -d "$OUTGOINGDIR" || mkdir -p "$OUTGOINGDIR"

inotifywait -q -m -e moved_to "$INCOMINGDIR" | \
while read DIR EVENT FILE
do
	if [ -f ${DIR}${FILE} ] ; then
		( $COMPRESSOR -c ${DIR}${FILE} > ${OUTGOINGDIR}-${FILE}${CSUFFIX} && mv ${OUTGOINGDIR}-${FILE}${CSUFFIX} ${OUTGOINGDIR}/${FILE}${CSUFFIX} && rm ${DIR}${FILE} ) &
		:
	else
		echo "nonregular file ${FILE}. Not that anybody would see this message"
	fi

	if [ -f /tmp/compressor-quit-request ] ; then
		rm -f /tmp/compressor-quit-request
		break
	fi
done
