""" runtest.py - run VMs as needed to run a lustre test set
"""
import sys
import os
import fcntl
import time
import threading
import logging
import Queue
import subprocess32
import shlex
import json
from pprint import pprint
from subprocess32 import Popen, PIPE, TimeoutExpired


def setup_custom_logger(name):
    formatter = logging.Formatter(fmt='%(asctime)s %(levelname)-8s %(message)s',
                                  datefmt='%Y-%m-%d %H:%M:%S')
    handler = logging.FileHandler(name, mode='a')
    handler.setFormatter(formatter)
    screen_handler = logging.StreamHandler(stream=sys.stdout)
    screen_handler.setFormatter(formatter)
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.addHandler(screen_handler)
    return logger

logger = setup_custom_logger('build-run-worker.log')



def build_worker(ref, buildnr, outdir):
    # Might need to make this per-arch?
    # XXX Also add distro here
    command = "systemd-nspawn -q --read-only --bind=%s:/tmp/out --bind-ro=/home/green/build-and-test/bin-x86:/home/green/bin --tmpfs=/home/green/git/lustre-release:mode=777,size=3G -D /exports/centos7-base -u green /home/green/bin/run_build.sh %s %d" % (outdir, ref, buildnr)
    args = shlex.split(command)

    # XXX chown to build user, hardcoded to green for now
    # XXX exception handling?
    os.chown(outdir, 1000, -1)

    try:
        builder = Popen(args, close_fds=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, universal_newlines=True)
    except (OSError) as details:
        logger.warning("Failed to run builder " + str(details))
        return "Builder error"

    try:
        # Typically build takes 4-5 minutes, so 10 minutes should be aplenty
        outs, errs = builder.communicate(timeout=600)
    except TimeoutExpired:
        logger.info("Build " + str(buildid) + " timed out, killing")
        builder.terminate()
        touts, terrs = builder.communicate()

    if builder.returncode is not 0:
        logger.warning("Build " + str(buildid) + " failed")
        return "Build failed"

    # XXX add a check that artifact exists

    return outs

def wait_for_login(process):
    """ Returns error as string or None if all is fine. No timeout handling """

    fd = process.stdout.fileno()
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    while True:
        try:
            string = process.stdout.read()
        except:
            string = ''
            time.sleep(1)
        else:
            pprint(string)

        process.poll()
        if process.returncode is not None:
            return "Process died"
        if "login:" in string:
            return None
    # Hm, the loop ended somehow?
    process.poll()
    return "terminated"

def test_worker(workerinfo, artifactdir, outdir, testinfo,
                clientdistro="centos7", serverdistro="centos7"):
    # XXX Incorporate distro into this somehow?
    serverruncommand = workerinfo['serverrun']
    clientruncommand = workerinfo['clientrun']
    servernetname = workerinfo['servername']
    clientnetname = workerinfo['clientname']

    testname = testinfo.get("test", None)
    if testname is None:
        return "Invalid testinfo!"
    timeout = testinfo.get("timeout", 300)
    fstype = testinfo.get("fstype", "ldiskfs")
    DNE = testinfo.get("DNE", False)
    serverarch = workerinfo.get('serverarch', "invalid")
    clientarch = workerinfo.get('clientarch', "invalid")

    serverkernel = artifactdir +"/kernel-%s-%s" % (serverdistro, serverarch)
    clientkernel = artifactdir +"/kernel-%s-%s" % (clientdistro, clientarch)
    serverinitrd = artifactdir +"/initrd-%s-%s.img" % (serverdistro, serverarch)
    clientinitrd = artifactdir +"/initrd-%s-%s.img" % (clientdistro, clientarch)

    testresultsdir = outdir + "/" + testname + "-" + fstype
    if DNE:
        testresultsdir += "-DNE"

    testresultsdir += "-" + serverdistro + "_" + serverarch
    testresultsdir += "-" + clientdistro + "_" + clientarch

    clientbuild = artifactdir + "/lustre-" + clientdistro + "-" + clientarch + ".ssq"
    serverbuild = artifactdir + "/lustre-" + serverdistro + "-" + serverarch + ".ssq"

    if not os.path.exists(serverbuild) or not os.path.exists(clientbuild) or \
       not os.path.exists(serverkernel) or not os.path.exists(serverinitrd) or \
       not os.path.exists(clientkernel) or not os.path.exists(clientinitrd):
        logger.error("Our build artifacts are missing?")
        sys.exit(1)

    # Make output test results dir:
    try:
        os.mkdir(testresultsdir)
    except OSError:
        logger.error("Huh, cannot create test results dir")
        sys.exit(1)

    try:
        server = Popen([serverruncommand, servernetname, serverkernel, serverinitrd, serverbuild, testresultsdir], close_fds=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, universal_newlines=True)
    except (OSError) as details:
        logger.warning("Failed to run server " + str(details))
        return "Server error"

    try:
        client = Popen([clientruncommand, clientnetname, clientkernel, clientinitrd, clientbuild, testresultsdir], close_fds=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, universal_newlines=True)
    except (OSError) as details:
        logger.warning("Failed to run client " + str(details))
        server.terminate()
        return "Client error"
    # Now we need to wait until both have booted and gave us login prompt
    if wait_for_login(server) is not None:
        client.terminate()
        error = server.stderr.read()
        pprint(error)
        return "Server died with " + str(server.returncode)
    if wait_for_login(client) is not None:
        server.terminate()
        error = client.stderr.read()
        pprint(error)
        return "Client died with " + str(client.returncode)

    try:
        if DNE:
            DNEStr = " MDSDEV2=/dev/vdd MDSCOUNT=2 "
        else:
            DNEStr = " "
        args = ["ssh", "-tt", "-o", "StrictHostKeyChecking=no", "root@" + clientnetname,
                'PDSH="pdsh -S -Rssh -w" mds_HOST=' + servernetname +
                " ost_HOST=" + servernetname + " MDSDEV1=/dev/vdc " +
                "OSTDEV1=/dev/vde OSTDEV2=/dev/vdf LOAD_MODULES_REMOTE=true " +
                "FSTYPE=" + fstype + DNEStr +
                "/home/green/git/lustre-release/lustre/tests/auster -r " +
                testname ]
        testprocess = Popen(args, close_fds=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, universal_newlines=True)
    except (OSError) as details:
        logger.warning("Failed to run test " + str(details))
        return "Testload error"

    # XXX add a loop here to preiodically test that our servers are alive
    # and also to ensure we don't need to abandon the test for whatever reason
    try:
        outs, errs = testprocess.communicate(timeout=timeout)
    except TimeoutExpired:
        logger.warning("Job timed out, terminating");
        testprocess.terminate()
        outs, errs = testprocess.communicate()

    testprocess.poll()
    logger.info("Job finished with code " + str(testprocess.returncode))
    pprint(errs)

    # Now kill the client and server
    server.terminate()
    souts, serrs = server.communicate()
    #pprint(souts)
    #pprint(serrs)
    client.terminate()
    couts, cerrs = client.communicate()
    #pprint(couts)
    #pprint(cerrs)

    return outs

if __name__ == "__main__":

    with open("./test-nodes-config.json") as nodes_file:
        workers = json.load(nodes_file)
    with open("./fsconfig.json") as fsconfig_file:
        fsconfig = json.load(fsconfig_file)

    worker = workers[0]
    OUTPUTS = fsconfig.get("outputs", "/tmp/work")
    CRASHESDIR = fsconfig.get("crashdumps", "/tmp/crash")
    TOUTPUTDIR = fsconfig.get("testoutputdir", "testresults")

    testlist=({'test':"runtests"},{'test':"runtests",'fstype':"zfs",'DNE':True,'timeout':600})

    buildnr = 1
    ref = "refs/changes/47/34147/2"

    # XXX
    while os.path.exists(OUTPUTS + "/" + str(buildnr)):
        buildnr += 1

    outdir = OUTPUTS + "/" + str(buildnr)

    try:
        os.mkdir(outdir)
    except:
        logger.error("Build dir already exists, huh?")
        sys.exit(1)

    result = build_worker(ref, buildnr, outdir)
    pprint(result)

    testresultsdir = outdir + "/" + TOUTPUTDIR
    try:
        os.mkdir(testresultsdir)
    except OSError:
        logger.error("Huh, cannot create test results dir")
        sys.exit(1)

    for test in testlist:
        result = test_worker(worker, outdir, testresultsdir, test)

        pprint(result)
        # Copy out results and whatnot?

    logger.info("All done, bailing out")

    sys.stdout.flush()
