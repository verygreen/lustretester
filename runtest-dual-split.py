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
import threading
import pwd

import mybuilder
import mytester


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

logger = setup_custom_logger('build-run-worker-test.log')

build_queue = Queue.Queue()
build_condition = threading.Condition()
testing_queue = Queue.PriorityQueue()
testing_condition = threading.Condition()
managing_queue = Queue.Queue()
managing_condition = threading.Condition()

fsconfig = {}

distros = ["centos7"]
architectures = ["x86_64"]
initialtestlist=({'test':"runtests"},{'test':"runtests",'fstype':"zfs",'DNE':True,'timeout':600})
testlist=({'test':"sanity", 'timeout':3600},{'test':"sanity",'fstype':"zfs",'DNE':True,'timeout':7200})

STARTING_BUILDNR = 1

class GerritWorkItem(object):
    def __init__(self, ref, initialtestlist, testlist):
        self.ref = ref
        self.buildnr = None
        self.BuildDone = False
        self.BuildError = False
        self.BuildMessage = ""
        self.artifactsdir = None
        self.InitialTestingStarted = False
        self.InitialTestingError = False
        self.InitialTestingDone = False
        self.TestingStarted = False
        self.TestingDone = False
        self.TestingError = False
        self.initial_tests = initialtestlist
        self.tests = testlist

    def UpdateTestStatus(self, testinfo, message, Failed=False, Crash=False,
                         ResultsDir=None, Finished=False, Timeout=False):
        # Lock here and we are fine
        if self.InitialTestingStarted and not self.InitialTestingDone:
            worklist = self.initial_tests
        elif self.TestingStarted and not self.TestingDone:
            worklist = self.tests
        else:
            logger.error("Weird state, huh?");
            pprint(self)

        updated = False
        for item in worklist:
            matched = True
            for element in testinfo:
                if item.get(element, None) != testinfo[element]:
                    matched = False
                    break
            if matched:
                updated = True
                if message is None and ResultsDir is not None:
                    item["ResultsDir"] = ResultsDir
                    break
                if Crash or Timeout:
                    Failed = True
                if Failed:
                    Finished = True
                    if not self.InitialTestingDone:
                        self.InitialTestingError = True
                    else:
                        self.TestingError = True

                item["Crash"] = Crash
                item["Timeout"] = Crash
                item["Failed"] = Failed
                item["Finished"] = Finished
                item["StatusMessage"] = message
        if not updated:
            logger.error("Passed in testinfo that I cannot match " + str(testinfo))
            pprint(testinfo)
            pprint(worklist)

        if Finished:
            for item in worklist:
                if item.get("Finished", False):
                    return
            # All entires are finished, time to mark the set
            if not self.InitialTestingDone:
                self.InitialTestingDone = True
            elif not self.TestingDone:
                self.TestingDone = True


def run_workitem_manager():
    current_build = STARTING_BUILDNR


    while os.path.exists(fsconfig["outputs"] + "/" + str(current_build)):
        current_build += 1

    while True:
        managing_condition.acquire()
        while managing_queue.empty():
            managing_condition.wait()
        workitem = managing_queue.get()
        managing_condition.release()

        teststr = vars(workItem)
        pprint(teststr)

        if workitem.buildnr is None:
            # New item, we need to build it
            workitem.buildnr = current_build
            current_build += 1

            logger.info("Got new ref " + workitem.ref + " assigned buildid " + str(workitem.buildnr))
            build_condition.acquire()
            build_queue.put([{}, workitem])
            build_condition.notify()
            build_condition.release()
            continue

        if workitem.BuildDone and workitem.BuildError:
            # We just failed the build
            # report and don't return this item anywhere
            logger.warning("ref " + workitem.ref + " build " + str(workitem.buildnr)  + " failed building")
            continue

        if workitem.BuildDone and not workitem.InitialTestingStarted:
            # Just finished building, need to do some initial testing
            # Create the test output dir first
            testresultsdir = workitem.artifactsdir + "/" + fsconfig["testoutputdir"]

            # Let's see if this is a retest and create a new dir for that
            if os.path.exists(testresultsdir):
                retry = 1
                while os.path.exists(testresultsdir + "-retry" + str(retry)):
                    retry += 1
                testresultsdir += "-retry" + str(retry)

            try:
                os.mkdir(testresultsdir)
            except OSError:
                logger.error("Huh, cannot create test results dir for ...")
                sys.exit(1)

            workitem.testresultsdir = testresultsdir
            workitem.InitialTestingStarted = True
            testing_condition.acquire()
            # First 0 is priority - highest
            for testinfo in workitem.initial_tests:
                #testinfo['initial'] = True
                testing_queue.put([0, testinfo, workitem])
                testing_condition.notify()
            testing_condition.release()
            continue

        if workitem.InitialTestingStarted and not workitem.InitialTestingDone:
            # More than one initial test enqueued, just wait for more
            # completions.
            # XXX racy if some parallel thing updates the status?
            continue

        if workitem.InitialTestingDone and workitem.InitialTestingError:
            # Need to report it and move on,
            # Don't return the item anywhere
            logger.warning("ref " + workitem.ref + " build " + str(workitem.buildnr)  + " failed initial testing")
            continue

        if workitem.InitialTestingDone and not workitem.TestingStarted:
            # Initial testing finished, now need to do real testing
            workitem.InitialTestingStarted = True
            testing_condtion.acquire()
            # First 100 is second priority. perhaps sort by timeout instead?
            # could lead to prolonged struggling.
            for testinfo in workitem.testlist:
                testinfo['initial'] = False
                testing_queue.put([100, testinfo, workitem])
                testing_condition.notify()
            testing_condition.release()
            continue

        if workitem.TestingDone:
            # We don't really care if it's finished in error or not, that's
            # for the reporting code to care about, but we are all done here
            self.logger.info("All done testing ref " + workitem.ref + " build " + workitem.buildnr + "Success: " + str(not workitem.TestingError))
            continue



if __name__ == "__main__":

    with open("./test-nodes-config.json") as nodes_file:
        workers = json.load(nodes_file)
    with open("./fsconfig.json") as fsconfig_file:
        fsconfig = json.load(fsconfig_file)

    TOUTPUTOWNER = fsconfig.get("testoutputowner", "green")
    try:
        testoutputowner_uid = pwd.getpwnam(TOUTPUTOWNER).pw_uid
    except:
        logger.error("Cannot find uid of test output owner " + TOUTPUTOWNER)
        sys.exit(1)

    fsconfig["testoutputowneruid"] = testoutputowner_uid

    builders = []

    for distro in distros:
        for arch in architectures:
            with open("./builders-" + distro + "-" + arch + ".json") as buildersfile:
                buildersinfo = json.load(buildersfile)
                for builderinfo in buildersinfo:
                    builders.append(mybuilder.Builder(builderinfo, fsconfig, build_condition, build_queue, managing_condition, managing_queue))

    for worker in workers:
        worker['thread'] = mytester.Tester(worker, fsconfig, testing_condition,\
                                           testing_queue, managing_condition, managing_queue)

    managerthread = threading.Thread(target=run_workitem_manager, args=())
    managerthread.daemon = True
    managerthread.start()

    workItem = GerritWorkItem("refs/changes/47/34147/2", initialtestlist, testlist)
    logger.info("Queued all jobs")
    managing_condition.acquire()
    managing_queue.put(workItem)
    managing_condition.notify()
    managing_condition.release()

    while True:
        managerthread.join(1)
        if workItem.TestingDone or workItem.BuildError or workItem.InitialTestingError:
            break

    logger.info("All done, bailing out")

    string = vars(workItem)
    pprint(string)

    sys.stdout.flush()
