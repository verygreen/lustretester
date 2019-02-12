""" Gerrit Work Item definition to pass stuff around """
import sys
import os
from pprint import pprint
import threading

class GerritWorkItem(object):
    def __init__(self, change, initialtestlist, testlist, EmptyJob=False):
        self.change = change
        self.revision = change.get('current_revision')
        if change.get('branch'):
            self.ref = change['branch']
        else:
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
        self.lock = threading.Lock()

    def UpdateTestStatus(self, testinfo, message, Failed=False, Crash=False,
                         ResultsDir=None, Finished=False, Timeout=False,
                         TestStdOut=None, TestStdErr=None, Subtests=None,
                         Skipped=None):
        self.lock.acquire()
        if self.InitialTestingStarted and not self.InitialTestingDone:
            worklist = self.initial_tests
        elif self.TestingStarted and not self.TestingDone:
            worklist = self.tests
        else:
            print("Weird state, huh?" + vars(self));

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
                item["Timeout"] = Timeout
                item["Failed"] = Failed
                item["Finished"] = Finished
                if message is not None:
                    item["StatusMessage"] = message
                if TestStdOut is not None:
                    item["TestStdOut"] = TestStdOut
                if TestStdErr is not None:
                    item["TestStdErr"] = TestStdErr
                if Subtests:
                    item["SubtestList"] = Subtests
                if Skipped:
                    item["SkippedSubtests"] = Skipped
        if not updated:
            print("Build " + str(self.buildnr) + " Passed in testinfo that I cannot match " + str(testinfo))
            pprint(testinfo)
            pprint(worklist)
        else:
            print("Build " + str(self.buildnr) + " Updated test element " + str(item))

        if Finished:
            for item in worklist:
                if not item.get("Finished", False):
                    self.lock.release()
                    return
            # All entires are finished, time to mark the set
            if not self.InitialTestingDone:
                self.InitialTestingDone = True
            elif not self.TestingDone:
                self.TestingDone = True

        self.lock.release()

    def requested_tests_string(self, tests):
        testlist = ""
        self.lock.acquire()
        for test in sorted(tests, key=operator.itemgetter('test', 'fstype')):
            testlist += test['test'] + '@' + test['fstype']
            if test.get('DNE', False):
                testlist += '+DNE'
            testlist += " "
        self.lock.release()
        return testlist

    def test_status_output(tests):
        passedtests = ""
        failedtests = ""
        skippedtests = ""
        self.lock.acquire()
        for test in sorted(tests, key=operator.itemgetter('test', 'fstype')):
            testname = test['test'] + '@' + test['fstype']
            if test.get('DNE', False):
                testname += '+DNE'
            testname += " "

            if not test['Failed']:
                if test.get('Skipped'):
                    skippedtests += testname
                else:
                    passedtests += testname
            else:
                failedtests += "> " + testname
                if not test.get('StatusMessage', ''):
                    if test['Timeout']:
                        failedtests += " Timed out"
                    elif test['Crash']:
                        failedtests += " Crash"
                    else:
                        failedtests += " Failed"
                failedtests += " " + test['StatusMessage']
                if test.get('SubtestList', ''):
                    failedtests += "\n- " + test['SubtestList']
                resultsdir = test.get('ResultsDir')
                if resultsdir:
                    failedtests += "\n- " + path_to_url(resultsdir) + '/'
                failedtests += '\n'
        self.lock.release()

        testlist = ""
        if failedtests:
            testlist = "\n" + failedtests
        if passedtests:
            testlist += "\nSucceeded:\n- " + passedtests + "\n"
        if skippedtests:
            testlist += "\nSkipped:\n- " + skippedtests + "\n"

        return testlist
