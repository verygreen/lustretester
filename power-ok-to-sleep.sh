#!/bin/bash

if [ -z "$1" ] ; then
	echo "Usage: $0 True/False"
	exit 0
fi

if [ "$1" = "False" ] ; then # Can no longer do powersaving
	# Wake up our nodes or remove the flag if already up
	# XXX is there a small race window?
	ipmiutil power -u -N 192.168.1.211 -U ADMIN -P ADMIN | grep 'S0: working' && ssh sm-a "rm -f /var/run/tester-powersave"
	ipmiutil power -u -N 192.168.1.212 -U ADMIN -P ADMIN | grep 'S0: working' && ssh sm-b "rm -f /var/run/tester-powersave"
	ipmiutil power -u -N 192.168.1.213 -U ADMIN -P ADMIN | grep 'S0: working' && ssh sm-c "rm -f /var/run/tester-powersave"
	ipmiutil power -u -N 192.168.1.214 -U ADMIN -P ADMIN | grep 'S0: working' && ssh sm-d "rm -f /var/run/tester-powersave"
	# Actual time to powerup from here is ~160 seconds

	# rack 3
	# only takes 6s to wake up - so wake up from job script
	# ether-wake -i team0 d0:50:99:2d:2f:33

	# so cannot wake it up either

	# rack 1
	# only takes 6s to wake up - so wake up from job script
	# ether-wake -i team0 c0:3f:d5:41:e3:f8

	# newnuc
	# only takes 10 s to wake up - so wake up from job script
	# ether-wake -i team0 94:c6:91:1c:83:d0

	exit 0
fi

# For powerdown just signal on the fs somewhere so the nodes monitor
# would turn off when idle
# We do not send signals to rack 1 and 3 and newnuc, those take very little
# time to suspend/resume so just opportunistically suspend them on idle
# all the time I guess.
pdsh -R ssh -w 'sm-a,sm-b,sm-c,sm-d,intelbox2' "touch /var/run/tester-powersave"

exit 0
