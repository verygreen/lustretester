""" Gerrit Work Item definition to pass stuff around """
import sys
import os
from pprint import pprint
import threading
import operator

class GerritWorkItem(object):
    def __init__(self, change, initialtestlist, testlist, fsconfig, EmptyJob=False):
        self.change = change
        self.revision = change.get('current_revision')
        if change.get('branch'):
            self.ref = change['branch']
        else:
            self.ref = change['revisions'][str(self.revision)]['ref']
        self.changenr = change['_number']
        self.buildnr = None
        self.fsconfig = fsconfig
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

    def __getstate__(self):
        state = self.__dict__.copy()
        del state['lock']
        return state
    def __setstate__(self, state):
        self.__dict__.update(state)
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
                if self.Aborted:
                    item["Aborted"] = True
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
            self.Write_HTML_Status()

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

    def testresults_as_html(self, tests):
        htmlteststable = '<table border="1"><tr><th>Test</th><th>Status/results</th><th>Extra info</th></tr>'
        for test in sorted(tests, key=operator.itemgetter('test', 'fstype')):
            htmlteststable += '<tr><td>'
            htmlteststable += test['test'] + '@' + test['fstype']
            if test.get('DNE', False):
                htmlteststable += '+DNE'
            # Ugh, double checking
            color = ""
            if test.get('Finished', False):
                if test["Failed"]:
                    color = 'bgcolor="pink"'
                elif "Skipped" in test.get('StatusMessage', ''): # XXX - add real state
                    color = 'bgcolor="yellow"'
                else:
                    color = 'bgcolor="lightgreen"'

            htmlteststable += '</td><td ' + color + '>'
            if test.get('ResultsDir'):
                htmlteststable += '<a href="' + test.get('ResultsDir').replace(self.artifactsdir + '/', '') + '/">'
            if test.get('Finished', False):
                if test.get('StatusMessage', ''):
                    htmlteststable += test['StatusMessage']
                elif test['Timeout']:
                    htmlteststable += 'Timed Out'
                elif test['Crash']:
                    htmlteststable += 'Crashed'
                elif test["Failed"]:
                    htmlteststable += 'Failed'
                elif test.get("Aborted", False):
                    htmlteststable += 'Aborted'
                else:
                    htmlteststable += 'Success'
            else: # Not finished, if results dir is set, then we at least started
                if test.get('ResultsDir'):
                    htmlteststable += 'Running'

            if test.get('ResultsDir'):
                htmlteststable += '</a>'

            htmlteststable += '</td><td>'
            if test.get("Failed", False):
                htmlteststable += test.get('SubtestList', '')
            else:
                htmlteststable += test.get('SkippedSubtests', '')
            htmlteststable += '</td></tr>'

        htmlteststable += '</table>'
        return htmlteststable

    def Write_HTML_Status(self):
        if not self.artifactsdir:
            # Did not even finish compile yet
            return
        if self.change.get('branch'):
            change = "tip of %s branch" % (self.change['branch'])
        else:
            # XXX - need to somehow pass in GERRIT_HOST
            change = '<a href="http://review.whamcloud.com/%d">%d rev %d: %s</a>' % (self.change.changenr, self.change.changenr, self.change['revisions'][str(self.revision)]["_number"], self.change['subject'])
        all_items = {'build':self.buildnr, 'change':change}
        template = """
<html>
<head>Results for build #{build} {change}</head>
<body>
{abortedmessage}
<h2>Results for build #{build} {change}</h2>
{buildinfo}
{initialtesting}
{fulltesting}
</body>
"""
        if self.Aborted:
            abortedmessage = '<h1>This testrun was ABORTED! Likely due to a newer version of a patch. Below data is not going to progress anymore</h1>'
        else:
            abortedmessage = ''
        all_items['abortedmessage'] = abortedmessage

        if not self.BuildDone:
            buildstatus = "Ongoing"
        elif self.BuildError:
            if self.BuildMessage:
                buildstatus = self.BuildMessage
            else:
                buildstatus = "Error"
        else:
            buildstatus = "Success"
        # XXX - hardcoded arch/distro
        buildinfo = '<h3>Build %s <a href="build-centos7-x86_64.console">build console</a></h3>' % (buildstatus)
        all_items['buildinfo'] = buildinfo

        if self.initial_tests:
            if self.InitialTestingStarted:
                if not self.InitialTestingDone:
                    initialtesting = '<h3>Initial testing: Running</h3><p>'
                elif self.InitialTestingError:
                    initialtesting = '<h3>Initial testing: Failure</h3><p>'
                elif self.InitialTestingDone:
                    initialtesting = '<h3>Initial testing: Success</h3><p>'
            else:
                initialtesting = '<h3>Initial testing: Not started</h3><p>'
            initialtesting += self.testresults_as_html(self.initial_tests)
        else:
            initialtesting = '<h3>Initial testing: Not planned</h3><p>'

        all_items['initialtesting'] = initialtesting

        if self.tests:
            if self.TestingStarted:
                if not self.TestingDone:
                    testing = '<h3>Comprehensive testing: Running</h3><p>'
                elif self.TestingError:
                    testing = '<h3>Comprehensive testing: Failure</h3><p>'
                elif self.TestingDone:
                    testing = '<h3>Comprehensive testing: Success</h3><p>'
            else:
                testing = '<h3>Comprehensive testing: Not started</h3><p>'
            testing += self.testresults_as_html(self.tests)
        else:
            testing = '<h3>Comprehensive testing: Not planned</h3><p>'

        all_items['fulltesting'] = testing

        try:
            with open(self.artifactsdir + "/results.html", "w") as indexfile:
                indexfile.write(template.format(**all_items))
        except:
            pass


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

    def test_status_output(self, tests):
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
                    url = resultsdir.replace(self.fsconfig['root_path_offset'], self.fsconfig['http_server'])
                    failedtests += "\n- " + url + '/'
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
