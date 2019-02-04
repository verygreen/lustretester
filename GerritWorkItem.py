""" Gerrit Work Item definition to pass stuff around """
import sys
import os
from pprint import pprint
import threading

class GerritWorkItem(object):
    def __init__(self, change, initialtestlist, testlist, EmptyJob=False):
        self.change = change
        self.revision = change.get('current_revision')
        self.ref = change['revisions'][str(self.revision)]['ref']
        self.changenr = change['_number']
        self.buildnr = None
        self.EmptyJob = EmptyJob
        self.Aborted = False
        self.AbortDone = False
        self.BuildDone = False
        self.BuildError = False
        self.BuildMessage = ""
        self.ReviewComments = {}
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
                         ResultsDir=None, Finished=False, Timeout=False,
                         TestStdOut=None, TestStdErr=None, Subtests=None):
        # Lock here and we are fine
        if self.InitialTestingStarted and not self.InitialTestingDone:
            worklist = self.initial_tests
        elif self.TestingStarted and not self.TestingDone:
            worklist = self.tests
        else:
            logger.error("Weird state, huh?" + vars(self));

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
                if message is not None:
                    item["StatusMessage"] = message
                if TestStdOut is not None:
                    item["TestStdOut"] = TestStdOut
                if TestStdErr is not None:
                    item["TestStdErr"] = TestStdErr
                if Subtests is not None:
                    item["SubtestList"] = Subtests
            logger.info("Build " + str(self.buiildnr) + " Updated test element " + str(item))
        if not updated:
            logger.error("Build " + str(self.buiildnr) + " Passed in testinfo that I cannot match " + str(testinfo))
            pprint(testinfo)
            pprint(worklist)

        if Finished:
            for item in worklist:
                if not item.get("Finished", False):
                    return
            # All entires are finished, time to mark the set
            if not self.InitialTestingDone:
                self.InitialTestingDone = True
            elif not self.TestingDone:
                self.TestingDone = True

