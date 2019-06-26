""" Gerrit Work Item definition to pass stuff around """
import sys
import os
from pprint import pprint
import threading
import operator
import subprocess
import pickle as pickle

class GerritWorkItem(object):
    def __init__(self, change, builds, initialtestlist, testlist, fsconfig, EmptyJob=False, Reviewer=None, DISTRO=None):
        self.change = change
        self.revision = change.get('current_revision')
        if change.get('branchwide'):
            self.ref = self.revision
        else:
            self.ref = change['revisions'][str(self.revision)]['ref']
        self.changenr = change['_number']
        self.buildnr = None
        self.Reviewer = Reviewer
        self.fsconfig = fsconfig
        self.EmptyJob = EmptyJob
        if DISTRO:
            self.distro = DISTRO
        else:
            self.distro = fsconfig['defaultdistro']
        self.Aborted = False
        self.AbortDone = False
        self.BuildDone = False
        self.BuildError = False
        if not builds: # Mostly for special request builds
            self.builds = [{'distro':self.distro}]
        else:
            self.builds = builds

        self.ReviewComments = {}
        self.artifactsdir = ""
        self.InitialTestingStarted = False
        self.InitialTestingError = False
        self.InitialTestingDone = False
        self.TestingStarted = False
        self.TestingDone = False
        self.TestingError = False
        self.initial_tests = initialtestlist
        self.tests = testlist
        self.lock = threading.Lock()
        self.retestiteration = 0
        self.crash_ids_reported = []

    def __getstate__(self):
        state = self.__dict__.copy()
        del state['lock']
        del state['Reviewer'] # no posts on restarts?
        return state
    def __setstate__(self, state):
        self.__dict__.update(state)
        self.lock = threading.Lock()
        self.Reviewer = None
        if not self.__dict__.get('retestiteration'):
            self.retestiteration = 0
        if not self.__dict__.get('distro'):
            self.distro = "centos7" # only matters for old items
        if not self.__dict__.get('builds'):
            self.builds = [{"distro":"centos7",'BuildMessage':self.__dict__.get('BuildMessage')}] # only matters for old items
        if not self.__dict__.get('ReviewComments'):
            self.ReviewComments = {} # XXX for a bug.


    def get_results_filename(self):
        if self.retestiteration:
            htmlfile = "results-retry%d.html" % (self.retestiteration)
        else:
            htmlfile = "results.html"
        return htmlfile

    def get_base_url(self):
        url = self.fsconfig['http_server']
        offset = self.fsconfig['root_path_offset']
        cut = len(offset)
        path = self.artifactsdir
        if path[:cut] != offset:
            return "Error path substitution, server misconfiguration!"
        url += path[cut:]
        return url

    def get_url_for_test(self, testinfo):
        return self.get_base_url() + testinfo.get('ResultsDir', '').replace(self.artifactsdir, '')

    # This just prints a rate-limited message
    def post_immediate_review_comment(self, message, review, newid):
        if newid in self.crash_ids_reported:
            # We printed message about this one already, do nothing.
            return
        if not self.Reviewer: # Well, no object = nothing to do
            print("No reviewer")
            return
        if not review or not message:
            print("message or review empty")
            return # Not printing empty reviews

        if self.Reviewer.post_review(self.change, self.revision, {'message':message, 'notify':'OWNER', 'labels':{'Code-Review':0}, 'comments':review}):
            self.crash_ids_reported.append(newid)
        else:
            print("Failure posting review")

    def UpdateBuildStatus(self, buildinfo, message, Failed=None,
                          Finished=None, Timeout=None,
                          BuildStdOut=None, BuildStdErr=None):
        self.lock.acquire()
        buildinfo['BuildMessage'] = message
        if Failed or Timeout:
            Finished = True
            self.BuildError = True
        if Failed != None:
            buildinfo['Failed'] = Failed
        if Finished != None:
            buildinfo['Finished'] = Finished
        if Timeout != None:
            buildinfo['Timeout'] = Timeout

        if BuildStdOut:
            buildinfo['stdout'] = BuildStdOut
        if BuildStdErr:
            buildinfo['stderr'] = BuildStdErr

        if Finished:
            unfinished = False
            for build in self.builds:
                if not build.get('Finished'):
                    unfinished = True
                    break
            if not unfinished:
                self.BuildDone = True
                print("Finished all builds for id: " + str(self.buildnr))
                # Also need to chown results dir back to root
                try:
                    os.chown(self.artifactsdir, 0, -1)
                except OSError:
                    pass # not thta it wver was a problem, but just in case

        self.lock.release()


    def UpdateTestStatus(self, testinfo, message, Failed=False, Crash=False,
                         ResultsDir=None, Finished=False, Timeout=False,
                         TestStdOut=None, TestStdErr=None, Subtests=None,
                         Skipped=None, Warnings=None):
        self.lock.acquire()
        if self.InitialTestingStarted and not self.InitialTestingDone:
            worklist = self.initial_tests
        elif self.TestingStarted and not self.TestingDone:
            worklist = self.tests
        else:
            print("Weird state, huh?" + str(vars(self)));
            if testinfo in self.initial_tests:
                worklist = self.initial_tests
            elif testinfo in self.tests:
                worklist = self.tests
            else:
                print("Totally unknown testinfo: " + str(testinfo))
                worklist = []

        item = testinfo # no need to search for it
        if message is None and ResultsDir is not None:
            item["ResultsDir"] = ResultsDir
        else:
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
            if message is not None and not message in item.get("StatusMessage", ""):
                item["StatusMessage"] = message
            if TestStdOut is not None:
                item["TestStdOut"] = TestStdOut
            if TestStdErr is not None:
                item["TestStdErr"] = TestStdErr
            if Subtests:
                item["SubtestList"] = Subtests
            if Skipped:
                item["SkippedSubtests"] = Skipped
            if Warnings:
                if item.get("Warnings"):
                    item["Warnings"] += Warnings
                else:
                    item["Warnings"] = Warnings

        print("Build " + str(self.buildnr) + " Updated test element " + str(item))
        sys.stdout.flush() # Make sure it's visible in its entirety over a pipe
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
        if Finished and self.fsconfig.get("testdone-cb"):
            args = [self.fsconfig["testdone-cb"], str(Failed), str(Timeout), str(Crash), str(self.buildnr), str(testinfo.get("ResultsDir"))]
            try:
                subprocess.call(args)
            except OSError as e:
                print("Error running testset callback for " + str(args))

    def testresults_as_html(self, tests):
        htmlteststable = '<table border="1"><tr><th>Test</th><th>Status/results</th><th>Extra info</th></tr>'
        for test in sorted(tests, key=operator.itemgetter('test', 'fstype')):
            htmlteststable += '<tr><td>'
            htmlteststable += test['name'] + '@' + test['fstype']
            if test.get('DNE', False):
                htmlteststable += '+DNE'
            if test.get('SSK', False):
                htmlteststable += '+SharedKey'
            if test.get('SELINUX', False):
                htmlteststable += '+SELinux'
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
                if test.get('NewWarnings'):
                    htmlteststable += '<div style="background-color:red;">' + "".join(test['NewWarnings']) + '</div>'
                if test.get("Warnings"):
                    htmlteststable += '<div style="background-color:yellow;">' + test['Warnings'] + '</div>'
            else: # Not finished, if results dir is set, then we at least started
                if test.get('ResultsDir'):
                    htmlteststable += 'Running'
                if test.get('NewWarnings'):
                    htmlteststable += '<div style="background-color:red;">' + "".join(test['NewWarnings']) + '</div>'
                if test.get("Warnings"):
                    htmlteststable += '<div style="background-color:yellow;">' + test['Warnings'] + '</div>'

            if test.get('ResultsDir'):
                htmlteststable += '</a>'

            htmlteststable += '</td><td>'
            if test.get("Failed", False):
                newstuff = test.get('NewFailures')
                if newstuff:
                    htmlteststable += '<div style="background-color:red;">' + " ".join(newstuff) + "</div>"
                htmlteststable += "<div>" + test.get('SubtestList', '') + "</div>"
                oldstuff = test.get('OldFailures')
                if oldstuff:
                    htmlteststable += '<div style="background-color:yellow;">' + " ".join(oldstuff) + "</div>"
            else:
                htmlteststable += test.get('SkippedSubtests', '')
            htmlteststable += '</td></tr>'

        htmlteststable += '</table>'
        return htmlteststable

    def Write_HTML_Status(self):
        if not self.artifactsdir:
            # Did not even finish compile yet
            return
        if self.change.get('branchwide'):
            change = '<a href="https://git.whamcloud.com/fs/lustre-release.git/shortlog/%s">Then tip of %s branch "%s"</a>' % (self.change['current_revision'], self.change['branch'], self.change['subject'])
        else:
            # XXX - need to somehow pass in GERRIT_HOST
            change = '<a href="http://review.whamcloud.com/%d">%d rev %d: %s</a>' % (self.changenr, self.changenr, self.change['revisions'][str(self.revision)]["_number"], self.change['subject'])
        all_items = {'build':self.buildnr, 'change':change}
        template = """
<html>
<head><title>Results for build #{build} {change}</title></head>
<body>
{abortedmessage}
<h2>Results for build #{build} {change}</h2>
<h3>Overall build status: {buildstatus}</h3>
<table border=1>
<tr><th>Distro</th><th>details</th></tr>
{buildinfo}
</table>
{initialtesting}
{fulltesting}
</body>
</html>
"""
        if self.Aborted:
            abortedmessage = '<h1>This testrun was ABORTED! Likely due to a newer version of a patch. Below data is not going to progress anymore</h1>'
        else:
            abortedmessage = ''
        all_items['abortedmessage'] = abortedmessage

        if not self.BuildDone:
            buildstatus = "Ongoing"
        elif self.BuildError:
            buildstatus = "Failure"
        else:
            buildstatus = "Success"
        all_items['buildstatus'] = buildstatus

        buildinfo = ""
        for build in self.builds:
            buildinfo += "<tr><td>%s</td>" % (build['distro'])
            buildinfo += '<td><a href="build-%s-x86_64.console">' % (build['distro'])
            if build.get('BuildMessage'):
                buildinfo += build['BuildMessage']
            elif build.get('BuildStarted'):
                buildinfo += "Ongoing"
            else:
                buildinfo += "Waiting"
            buildinfo += "</a></td></tr>"

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

        htmlfile = "/" + self.get_results_filename()

        try:
            with open(self.artifactsdir + htmlfile, "w") as indexfile:
                indexfile.write(template.format(**all_items))
        except:
            pass


    def requested_tests_string(self, tests):
        testlist = ""
        self.lock.acquire()
        for test in sorted(tests, key=operator.itemgetter('test', 'fstype')):
            testlist += test['name'] + '@' + test['fstype']
            if test.get('DNE', False):
                testlist += '+DNE'
            if test.get('SSK', False):
                testlist += '+SharedKey'
            if test.get('SELINUX', False):
                testlist += '+SELinux'
            testlist += " "
        self.lock.release()
        return testlist

    def test_status_output(self, tests):
        passedtests = ""
        failedtests = ""
        skippedtests = ""
        warningtests = ""
        newfailures = ""
        self.lock.acquire()
        for test in sorted(tests, key=operator.itemgetter('test', 'fstype')):
            testname = test['name'] + '@' + test['fstype']
            if test.get('DNE', False):
                testname += '+DNE'
            if test.get('SSK', False):
                testname += '+SharedKey'
            if test.get('SELINUX', False):
                testname += '+SELinux'

            if test.get('NewWarnings'):
                warningtests += testname + "(" + ",".join(test['NewWarnings']) + ") "
            if not test['Failed']:
                if test.get('Skipped'):
                    skippedtests += testname + " "
                else:
                    passedtests += testname + " "
            else:
                newstuff = test.get('NewFailures')
                if newstuff:
                    newfailures += "- " + testname + ":" + ", ".join(newstuff) + "\n"
                failedtests += "> " + testname + " "
                if not test.get('StatusMessage', ''):
                    if test['Timeout']:
                        failedtests += " Timed out"
                    elif test['Crash']:
                        failedtests += " Crash"
                    else:
                        failedtests += " Failed"
                else:
                    failedtests += test['StatusMessage']

                if test.get('SubtestList', ''):
                    failedtests += "\n- " + test['SubtestList']
                # Only print one URL at theend for everything
                #resultsdir = test.get('ResultsDir')
                #if resultsdir:
                #    url = resultsdir.replace(self.fsconfig['root_path_offset'], self.fsconfig['http_server'])
                #    failedtests += "\n- " + url + '/'
                failedtests += '\n'
        self.lock.release()

        testlist = ""
        if newfailures:
            testlist += "IMPORTANT: these tests appear to be new failures unique to this patch\n" + newfailures + "\n"
        if failedtests:
            testlist += "\n" + failedtests
        if warningtests:
            testlist += "\nTests with NEW Warning messages:\n- " + warningtests + "\n"
        if passedtests:
            testlist += "\nSucceeded:\n- " + passedtests + "\n"
        if skippedtests:
            testlist += "\nSkipped:\n- " + skippedtests + "\n"

        allresults = self.artifactsdir + "/" + self.get_results_filename()
        testlist += "\n(" + self.distro +")All results and logs: " + allresults.replace(self.fsconfig['root_path_offset'], self.fsconfig['http_server'])

        return testlist

    def get_current_text_status(self):
        """ Return text status of this item """
        if self.Aborted:
            status = "Aborted!"
        elif self.TestingDone:
            status = "Testing done"
            if self.TestingError:
                status += " (some tests failed)"
        elif self.TestingStarted:
            status = "Comprehensive testing"
            if self.TestingError:
                status += " (some tests failed already)"
        elif self.InitialTestingStarted:
            status = "Initial testing"
            if self.InitialTestingError:
                status += " (some tests failed already)"
        elif self.BuildError:
            status = "Build failed"
        else:
            if self.artifactsdir:
                status = "Building"
            else:
                status = "Waiting to build"
        return status

    def get_saved_name(self):
        name = str(self.buildnr)
        if self.retestiteration:
            name += "-" + str(self.retestiteration)
        name += ".pickle"
        return name

    def save(self, path):
        """ Saves the item with common name """
        name = self.get_saved_name()

        with open(path + "/" + name, "wb") as output:
            self.lock.acquire()
            try:
                pickle.dump(self, output, pickle.HIGHEST_PROTOCOL )
            except RuntimeError:
                pass # We just want to avoid the crash. next iteration will write it out.
            self.lock.release()
