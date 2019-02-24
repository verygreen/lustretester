import os
import mycrashanalyzer
import json
import threading
import Queue
import time
import cPickle as pickle

managing_queue = Queue.Queue()
managing_condition = threading.Condition()
crashing_queue = Queue.Queue()
crashing_condition = threading.Condition()


if __name__ == "__main__":
    with open("./fsconfig.json") as fsconfig_file:
        fsconfig = json.load(fsconfig_file)
    crasher = mycrashanalyzer.Crasher(fsconfig, crashing_condition, crashing_queue, managing_condition, managing_queue)
    with open("donewith/72.pickle", "rb") as blah:
        workitem = pickle.load(blah)

    for test in workitem.tests:
        if test['test'] != "sanity-gss":
            continue
        if test.get("Crash"):
            resultsdir = test['ResultsDir']
            print("found a crashed test in " + resultsdir)
            for fname in os.listdir(resultsdir):
                if not fname.endswith("-vmcore"):
                    continue
                path = resultsdir + "/" + fname
                print("Queueing Core " + path)
                crashing_condition.acquire()
                crashing_queue.put((path, test, "centos7", "x86_64", workitem))
                crashing_condition.notify()
                crashing_condition.release()
                time.sleep(1000)

