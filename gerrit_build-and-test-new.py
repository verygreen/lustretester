#!/usr/bin/env python
#
# GPL HEADER START
#
# DO NOT ALTER OR REMOVE COPYRIGHT NOTICES OR THIS FILE HEADER.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 only,
# as published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License version 2 for more details (a copy is included
# in the LICENSE file that accompanied this code).
#
# You should have received a copy of the GNU General Public License
# version 2 along with this program; If not, see
# http://www.gnu.org/licenses/gpl-2.0.html
#
# GPL HEADER END
#
# Copyright (c) 2014, Intel Corporation.
#
# Author: John L. Hammond <john.hammond@intel.com>
#
"""
Gerrit Universal Reviewer Daemon
~~~~~~ ~~~~~~~~~~ ~~~~~~~~ ~~~~~~

* Watch for new change revisions in a gerrit instance.
* Pass new revisions through builders and testers
* POST reviews back to gerrit based on results
"""

import fnmatch
import logging
import json
import os
import sys
import requests
import time
import urllib
from GerritWorkItem import GerritWorkItem
import Queue
import threading
import pwd
import re
import operator
import mybuilder
import mytester
from datetime import datetime
import dateutil.parser
import shutil
import cPickle as pickle
from pprint import pprint

def _getenv_list(key, default=None, sep=':'):
    """
    'PATH' => ['/bin', '/usr/bin', ...]
    """
    value = os.getenv(key)
    if value is None:
        return default
    else:
        return value.split(sep)

GERRIT_HOST = os.getenv('GERRIT_HOST', 'review.whamcloud.com')
GERRIT_PROJECT = os.getenv('GERRIT_PROJECT', 'fs/lustre-release')
GERRIT_BRANCH = os.getenv('GERRIT_BRANCH', 'master')
GERRIT_AUTH_PATH = os.getenv('GERRIT_AUTH_PATH', 'GERRIT_AUTH')
GERRIT_CHANGE_NUMBER = os.getenv('GERRIT_CHANGE_NUMBER', None)
GERRIT_DRYRUN = os.getenv('GERRIT_DRYRUN', None)
GERRIT_FORCEALLTESTS = os.getenv('GERRIT_FORCEALLTESTS', None)
GERRIT_BRANCHMONITORDIR = os.getenv('GERRIT_BRANCHMONITORDIR', "./branches/")
GERRIT_COMMANDMONITORDIR = os.getenv('GERRIT_COMMANDMONITORDIR', "./commands/")

# When this is set - only changes with this topic would be tested.
# good for trial runs before big deployment
GERRIT_FORCETOPIC = os.getenv('GERRIT_FORCETOPIC', None)

SAVEDSTATE_DIR="savedstate"
DONEWITH_DIR="donewith"
FAILED_POSTS_DIR="failed_posts"
LAST_BUILD_ID="LASTBUILD_ID"

StopMachine = False

# GERRIT_AUTH should contain a single JSON dictionary of the form:
# {
#     "review.example.com": {
#         "gerrit/http": {
#             "username": "example-checkpatch",
#             "password": "1234"
#         }
#     }
#     ...
# }

REVIEW_HISTORY_PATH = os.getenv('REVIEW_HISTORY_PATH', 'REVIEW_HISTORY')
STYLE_LINK = os.getenv('STYLE_LINK',
        'http://wiki.lustre.org/Lustre_Coding_Style_Guidelines')
TrivialNagMessage = 'It is recommended to add "Test-Parameters: trivial" directive to patches that do not change any running code to ease the load on the testing subsystem'
TrivialIgnoredMessage = 'Even though "Test-Parameters: trivial" was detected, this deeply suspicious bot still run some testing'

USE_CODE_REVIEW_SCORE = False

IGNORE_OLDER_THAN_DAYS = 30

build_queue = Queue.Queue()
build_condition = threading.Condition()
testing_queue = Queue.PriorityQueue()
testing_condition = threading.Condition()
managing_queue = Queue.Queue()
managing_condition = threading.Condition()
reviewer = None

fsconfig = {}

distros = ["centos7"]
architectures = ["x86_64"]

ZFS_ONLY_FILES = [ 'lustre/osd-zfs/*.[ch]', 'lustre/utils/libmount_utils_zfs.c', 'config/lustre-build-zfs.m4' ]
LDISKFS_ONLY_FILES = [
        'lustre/osd-ldiskfs/*.[ch]', 'lustre/utils/libmount_utils_ldiskfs.c',
        'ldiskfs/kernel_patches/*', 'config/lustre-build-ldiskfs.m4' ]
BUILD_ONLY_FILES = [ '*Makefile*', 'LUSTRE-VERSION-GEN', 'autogen.sh',
        'lnet/klnds/o2iblnd/*' ]
IGNORE_FILES = [ 'contrib/*', 'README', 'snmp/*', '*dkms*', '*spec*',
        'debian/*', 'rpm/*', 'Documentation/*', 'ChangeLog', 'COPYING',
        'MAINTAINERS', '*doxygen*', 'lustre-iokit/*', 'LICENSE',
        '.gitignore', 'nodist', 'lustre/doc/*', 'lustre/kernel_patches/*',
        'lnet/doc/*', 'lnet/klnds/gnilnd/*', 'lustre/tests/maloo_upload.sh',
        'lustre/tests/parallel-scale-*.sh', 'lustre/tests/setup-*.sh' ]
LNET_ONLY_FILES = [ 'lnet/*' ]
CODE_FILES = [ '*.[ch]' ]
I_DONT_KNOW_HOW_TO_TEST_THESE = [
        'contrib/lbuild/*', '*dkms*', '*spec*', 'debian/*', 'rpm/*',
        'lustre/kernel_patches/*' ]
TEST_SCRIPT_FILES = [ 'lustre/tests/*' ]


# We store all job items here
WorkList = []

def match_fnmatch_list(item, fnlist):
    for pattern in fnlist:
        if fnmatch.fnmatch(item, pattern):
            return True
    return False

def is_notknow_howto_test(filelist):
    """ Returns true if there are any changed files that
        are not noops, but we are not testing it """
    for item in sorted(filelist):
        if match_fnmatch_list(item, I_DONT_KNOW_HOW_TO_TEST_THESE):
            return True
    return False

def populate_testlist_from_array(testlist, testarray, LDiskfsOnly, ZFSOnly, DNE=True, Force=False, SSK=False, SELINUX=False):
    for item in testarray:
        def getemptytest(item):
            test = {}
            test['test'] = item[0]
            test['timeout'] = item[1]
            test['testparam'] = item[2]
            test['SSK'] = SSK
            test['SELINUX'] = SELINUX
            return test

        try:
            # See if this test is disabled
            if item[3] and not Force:
                continue
        except IndexError:
            pass # No disabled element = we are all fine
        if LDiskfsOnly:
            test = getemptytest(item)
            test['fstype'] = "ldiskfs"
            test['DNE'] = DNE
            testlist.append(test)
        if ZFSOnly:
            test = getemptytest(item)
            test['fstype'] = "zfs"
            testlist.append(test)
        if ZFSOnly and DNE and not LDiskfsOnly:
            # Need to also do DNE run
            test = getemptytest(item)
            test['fstype'] = "zfs"
            test['DNE'] = True
            testlist.append(test)
        if LDiskfsOnly and not ZFSOnly and DNE:
            # Need to capture non-DNE run for ldiskfs
            test = getemptytest(item)
            test['fstype'] = "ldiskfs"
            testlist.append(test)

    return testlist


def determine_testlist(filelist, trivial_requested):
    """ Try to guess what tests to run based on the changes """
    DoNothing = True
    NonTestFilesToo = False
    modified_test_files = []
    BuildOnly = False
    LNetOnly = False
    ZFSOnly = False
    LDiskfsOnly = False
    FullRun = False

    for item in sorted(filelist):
        # I wish there was a way to detect deleted files, but alas, not in our gerrit?
        if match_fnmatch_list(item, IGNORE_FILES):
            continue # with deletion would set BuildOnly
        DoNothing = False
        if match_fnmatch_list(item, TEST_SCRIPT_FILES):
            modified_test_files.append(os.path.basename(item).replace('.sh',''))
            continue
        NonTestFilesToo = True
        if match_fnmatch_list(item, BUILD_ONLY_FILES):
            BuildOnly = True
            continue
        if match_fnmatch_list(item, ZFS_ONLY_FILES):
            ZFSOnly = True
            continue
        if match_fnmatch_list(item, LDISKFS_ONLY_FILES):
            LDiskfsOnly = True
            continue
        if match_fnmatch_list(item, LNET_ONLY_FILES):
            LNetOnly = True
            continue
        # Otherwise unknown file = full test
        # Need to be smarter here about individual test file changes I guess?
        FullRun = True

    # For lnet only changes we'd volunteer a zfs only run
    # to ensure actual Lustre operations still work.
    if LNetOnly and not FullRun and not LDiskfsOnly and not ZFSOnly:
        ZFSOnly = True

    # Override for testing
    if GERRIT_FORCEALLTESTS:
        FullRun = True
        LNetOnly = True

    if FullRun:
        LDiskfsOnly = True
        ZFSOnly = True
        LNetOnly = True
        BuildOnly = False

    # Always reload testlists
    with open("tests/initial.json", "r") as blah:
        initialtestlist = json.load(blah)
    with open("tests/comprehensive.json", "r") as blah:
        fulltestlist = json.load(blah)
    with open("tests/lnet.json", "r") as blah:
        lnettestlist = json.load(blah)
    with open("tests/zfs.json", "r") as blah:
        zfstestlist = json.load(blah)
    with open("tests/ldiskfs.json", "r") as blah:
        ldiskfstestlist = json.load(blah)

    initial = []
    comprehensive = []

    if not DoNothing and not BuildOnly:
        if modified_test_files:
            UnknownItems = NonTestFilesToo
            foundtests = []
            for item in modified_test_files:
                Found = False
                for test in initialtestlist + fulltestlist + lnettestlist + zfstestlist + ldiskfstestlist:
                    if item == test[0]:
                        foundtests.append(test)
                        # To avoid doubletesting, mark it as disabled in the
                        # regular list too - ok to do sice we reread the list
                        # every time
                        if len(test) < 4:
                            test.append(True)
                        else:
                            test[3] = True
                        Found = True
                        break
                if not Found:
                    UnknownItems = True

            populate_testlist_from_array(initial, foundtests, True, True, Force=True)
            if not UnknownItems:
                # We just populate out test list from the changed scripts
                # we detected that we run in all possible configs
                # Force disabled tests too if we are modifying them we better
                # know how they perform
                # Also let's turn off every other test
                LNetOnly = False
                ZFSOnly = False
                LDiskfsOnly = False
                trivial_requested = True
            else:
                # Hm, not sure what to do here? Probably run everything
                # as requested in addition to modified test files?
                pass

        # Careful, if initial test list was filled above, we presume it's
        # comprehensive but if we have any other files modified that are
        # non-test - add standard initial testing too.
        if not initial or NonTestFilesToo:
            populate_testlist_from_array(initial, initialtestlist, LDiskfsOnly, ZFSOnly)

        if LNetOnly:
            # For items in this list we don't care about fs as it's supposed
            # to be fs-neutral Lnet-only stuff like lnet-selftest
            populate_testlist_from_array(comprehensive, lnettestlist, False, True, DNE=False)

        if ZFSOnly:
            # For items in this list we don't care about fs as it's supposed
            # to be fs-neutral Lnet-only stuff like lnet-selftest
            populate_testlist_from_array(comprehensive, zfstestlist, False, True)
        if LDiskfsOnly:
            # For items in this list we don't care about fs as it's supposed
            # to be fs-neutral Lnet-only stuff like lnet-selftest
            populate_testlist_from_array(comprehensive, ldiskfstestlist, True, False)

        if not trivial_requested or GERRIT_FORCEALLTESTS:
            populate_testlist_from_array(comprehensive, fulltestlist, LDiskfsOnly, ZFSOnly)

    return (DoNothing, initial, comprehensive)

def is_trivial_requested(message):
    trivial_re = re.compile("^Test-Parameters:.*trivial")
    for line in message.splitlines():
        if trivial_re.match(line):
            return True

def is_buildonly_requested(message):
    trivial_re = re.compile("^Test-Parameters:.*forbuildonly")
    for line in message.splitlines():
        if trivial_re.match(line):
            return True

def is_testonly_requested(message):
    trivial_re = re.compile("^Test-Parameters:.*fortestonly")
    for line in message.splitlines():
        if trivial_re.match(line):
            return True

def parse_checkpatch_output(out, path_line_comments, warning_count):
    """
    Parse string output out of CHECKPATCH into path_line_comments.
    Increment warning_count[0] for each warning.

    path_line_comments is { PATH: { LINE: [COMMENT, ...] }, ... }.
    """
    def add_comment(path, line, level, kind, message):
        """_"""
        logging.debug("add_comment %s %d %s %s '%s'",
                      path, line, level, kind, message)

        path_comments = path_line_comments.setdefault(path, {})
        line_comments = path_comments.setdefault(line, [])
        line_comments.append('(style) %s\n' % message)
        warning_count[0] += 1

    level = None # 'ERROR', 'WARNING'
    kind = None # 'CODE_INDENT', 'LEADING_SPACE', ...
    message = None # 'code indent should use tabs where possible'

    for line in out.splitlines():
        # ERROR:CODE_INDENT: code indent should use tabs where possible
        # #404: FILE: lustre/liblustre/dir.c:103:
        # +        op_data.op_hash_offset = hash_x_index(page->index, 0);$
        line = line.strip()
        if not line:
            level, kind, message = None, None, None
        elif line[0] == '#':
            # '#404: FILE: lustre/liblustre/dir.c:103:'
            tokens = line.split(':', 5)
            if len(tokens) != 5 or tokens[1] != ' FILE':
                continue

            path = tokens[2].strip()
            line_number_str = tokens[3].strip()
            if not line_number_str.isdigit():
                continue

            line_number = int(line_number_str)

            if path and level and kind and message:
                add_comment(path, line_number, level, kind, message)
        elif line[0] == '+':
            continue
        else:
            # ERROR:CODE_INDENT: code indent should use tabs where possible
            try:
                level, kind, message = line.split(':', 2)
            except ValueError:
                level, kind, message = None, None, None

            if level != 'ERROR' and level != 'WARNING':
                level, kind, message = None, None, None

def find_and_abort_duplicates(workitem):
    for item in WorkList:
        if item.changenr == workitem.changenr:
            item.Aborted = True

def review_input_and_score(path_line_comments, warning_count):
    """
    Convert { PATH: { LINE: [COMMENT, ...] }, ... }, [11] to a gerrit
    ReviewInput() and score
    """
    review_comments = {}

    for path, line_comments in path_line_comments.iteritems():
        path_comments = []
        for line, comment_list in line_comments.iteritems():
            message = '\n'.join(comment_list)
            path_comments.append({'line': line, 'message': message})
        review_comments[path] = path_comments

    if warning_count[0] > 0:
        score = -1
    else:
        score = +1

    if USE_CODE_REVIEW_SCORE:
        code_review_score = score
    else:
        code_review_score = 0

    if score < 0:
        return {
            'message': ('%d style warning(s).\nFor more details please see %s' %
                        (warning_count[0], STYLE_LINK)),
            'labels': {
                'Code-Review': code_review_score
                },
            'comments': review_comments,
            'notify': 'OWNER',
            }, score
    else:
        return {
            'message': 'Looks good to me.',
            'labels': {
                'Code-Review': code_review_score
                },
            'notify': 'NONE',
            }, score

def path_to_url(path):
    url = fsconfig['http_server']
    offset = fsconfig['root_path_offset']
    cut = len(offset)
    if path[:cut] != offset:
        # Somethign is really wrong here
        return "Error path substitution, server misconfiguration!"
    url += path[cut:]
    return url

def add_review_comment(WorkItem):
    """
    Convert { PATH: { LINE: [COMMENT, ...] }, ... }, [11] to a gerrit
    ReviewInput() and score
    """
    score = 0
    review_comments = {}
    try:
        commit_message = WorkItem.change['revisions'][str(WorkItem.revision)]['commit']['message']
    except:
        commit_message = ""

    if WorkItem.Aborted:
        if WorkItem.AbortDone:
            # We already printed everything in the past, just exit
            return

        WorkItem.AbortDone = True
        if WorkItem.BuildDone:
            # No messages were printed anywhere, pretend we did not even see it
            return

        message = "Newer revision detected, aborting all work on revision " + str(WorkItem.change['revisions'][str(WorkItem.revision)]['_number'])
    elif WorkItem.EmptyJob:
        if is_notknow_howto_test(WorkItem.change['revisions'][str(WorkItem.revision)]['files']):
            message = "This file contains changes that I don't know how to test or build. Skipping"
        else:
            message = 'Cannot detect any useful changes in this patch\n'
            if not is_trivial_requested(commit_message):
                message += TrivialNagMessage
    elif WorkItem.BuildDone and not WorkItem.InitialTestingStarted and not WorkItem.TestingStarted:
        # This is after initial build completion
        if WorkItem.BuildError:

            if WorkItem.BuildMessage:
                message = WorkItem.BuildMessage
            else:
                message = 'Build failed\n'
            message += ' Job output URL: ' + path_to_url(WorkItem.artifactsdir) + "/" + WorkItem.get_results_filename()
            score = -1
            review_comments = WorkItem.ReviewComments
        else:
            if WorkItem.retestiteration:
                message = "This is a retest #%d\n" % (WorkItem.retestiteration)
            else:
                message = 'Build for x86_64 centos7 successful\n'
            message += 'Job output URL: ' + path_to_url(WorkItem.artifactsdir) + '/' + WorkItem.get_results_filename() + '\n\n'
            if WorkItem.initial_tests:
                message += ' Commencing initial testing: ' + WorkItem.requested_tests_string(WorkItem.initial_tests)
            else:
                message += ' This was detected as a build-only change, no further testing would be performed by this bot.\n'
                if not is_trivial_requested(commit_message):
                    message += TrivialNagMessage
                score = 1
    elif WorkItem.InitialTestingDone and not WorkItem.TestingStarted:
        # This is after initial tests
        if WorkItem.InitialTestingError:
            message = 'Initial testing failed:\n'
            message += WorkItem.test_status_output(WorkItem.initial_tests)
            score = -1
            review_comments = WorkItem.ReviewComments
        else:
            message = 'Initial testing succeeded.\n' + WorkItem.test_status_output(WorkItem.initial_tests)
            if WorkItem.tests:
                message += '\nCommencing standard testing: \n- ' + WorkItem.requested_tests_string(WorkItem.tests)
            else:
                message += '\nNo additional testing was requested'
                score = 1
    elif WorkItem.TestingDone:
        message = ""
        if is_trivial_requested(commit_message):
            message += TrivialIgnoredMessage + '\n'
        message += 'Testing has completed '
        if WorkItem.TestingError:
            message += 'with errors!\n'
            score = -1
            review_comments = WorkItem.ReviewComments
        else:
            message += 'Successfully\n'
        message += WorkItem.test_status_output(WorkItem.tests)
    else:
        # This is one of those intermediate states like not
        # Fully complete testing round or whatnot, so don't do anything.
        #message = "Help, I don't know why I am here" + str(vars(WorkItem))
        return

    # Errors = notify owner, no errors - no need to spam people
    if score < 0:
        notify = 'OWNER'
    else:
        notify = 'NONE'

    if USE_CODE_REVIEW_SCORE:
        code_review_score = score
    else:
        code_review_score = 0

    outputdict = {
            'message': (message),
            'labels': {
                'Code-Review': code_review_score
            },
        'comments': review_comments,
        'notify': notify,
        }
    if WorkItem.change.get('branchwide', False) or not reviewer.post_review(WorkItem.change, WorkItem.revision, outputdict):
        # Ok, we had a failure posting this message, let's save it for
        # later processing
        savefile = FAILED_POSTS_DIR + "/build-" + str(WorkItem.buildnr) + "-" + str(WorkItem.changenr) + "." + str(WorkItem.revision)
        if os.path.exists(savefile + ".json"):
            attempt = 1
            while os.path.exists(savefile+ "-try" + str(attempt) + ".json"):
                attempt += 1
            savefile = savefile+ "-try" + str(attempt)

        try:
            with open(savefile + ".json", "w") as outfile:
                json.dump({'change':WorkItem.change, 'output':outputdict}, outfile, indent=4)
        except OSError:
            # Only if we cannot save
            pass

def _now():
    """_"""
    return long(time.time())


class Reviewer(object):
    """
    * Poll gerrit instance for updates to changes matching project and branch.
    * Pipe new patches through checkpatch.
    * Convert checkpatch output to gerrit ReviewInput().
    * Post ReviewInput() to gerrit instance.
    * Track reviewed revisions in history_path.
    """
    def __init__(self, host, project, branch, username, password, history_path):
        self.host = host
        self.project = project
        self.branch = branch
        self.auth = requests.auth.HTTPDigestAuth(username, password)
        self.logger = logging.getLogger(__name__)
        self.history_path = history_path
        self.history_mode = 'rw'
        self.history = {}
        self.timestamp = 0L
        self.post_enabled = True and not GERRIT_DRYRUN # XXX
        self.post_interval = 1
        self.update_interval = 120
        self.request_timeout = 60

    def _debug(self, msg, *args):
        """_"""
        self.logger.debug(msg, *args)

    def _error(self, msg, *args):
        """_"""
        self.logger.error(msg, *args)

    def _url(self, path):
        """_"""
        return 'https://' + self.host + '/a' + path

    def _get(self, path):
        """
        GET path return Response.
        """
        url = self._url(path)
        try:
            res = requests.get(url, auth=self.auth,
                               timeout=self.request_timeout)
        except Exception as exc:
            self._error("cannot GET '%s': exception = %s", url, str(exc))
            return None

        if res.status_code != requests.codes.ok:
            self._error("cannot GET '%s': reason = %s, status_code = %d",
                       url, res.reason, res.status_code)
            return None

        return res

    def _post(self, path, obj):
        """
        POST json(obj) to path, return True on success.
        """
        url = self._url(path)
        data = json.dumps(obj)
        if not self.post_enabled:
            self._debug("_post: disabled: url = '%s', data = '%s'", url, data)
            return False

        try:
            res = requests.post(url, data=data,
                                headers={'Content-Type': 'application/json'},
                                auth=self.auth, timeout=self.request_timeout)
        except Exception as exc:
            self._error("cannot POST '%s': exception = %s", url, str(exc))
            return False

        if res.status_code != requests.codes.ok:
            self._error("cannot POST '%s': reason = %s, status_code = %d",
                       url, res.reason, res.status_code)
            return False

        return True

    def load_history(self):
        """
        Load review history from history_path containing lines of the form:
        EPOCH      FULL_CHANGE_ID                         REVISION    SCORE
        1394536722 fs%2Flustre-release~master~I5cc6c23... 00e2cc75... 1
        1394536721 -                                      -           0
        1394537033 fs%2Flustre-release~master~I10be8e9... 44f7b504... 1
        1394537032 -                                      -           0
        1394537344 -                                      -           0
        ...
        """
        if 'r' in self.history_mode:
            with open(self.history_path) as history_file:
                for line in history_file:
                    epoch, change_id, revision, score = line.split()
                    if change_id == '-':
                        self.timestamp = long(float(epoch))
                    else:
                        self.history[change_id + ' ' + revision] = score

        self._debug("load_history: history size = %d, timestamp = %d",
                    len(self.history), self.timestamp)

    def write_history(self, change_id, revision, score, epoch=-1):
        """
        Add review record to history dict and file.
        """
        if GERRIT_DRYRUN:
            return
        if change_id != '-':
            self.history[change_id + ' ' + revision] = score

        if epoch <= 0:
            epoch = self.timestamp

        if 'w' in self.history_mode:
            with open(self.history_path, 'a') as history_file:
                print >> history_file, epoch, change_id, revision, score

    def in_history(self, change_id, revision):
        """
        Return True if change_id/revision was already reviewed.
        """
        return change_id + ' ' + revision in self.history

    def get_changes(self, query):
        """
        GET a list of ChangeInfo()s for all changes matching query.

        {'status':'open', '-age':'60m'} =>
          GET /changes/?q=project:...+status:open+-age:60m&o=CURRENT_REVISION =>
            [ChangeInfo()...]
        """
        query = dict(query)
        project = query.get('project', self.project)
        query['project'] = urllib.quote(project, safe='')
        branch = query.get('branch', self.branch)
        query['branch'] = urllib.quote(branch, safe='')
        if GERRIT_FORCETOPIC and not GERRIT_CHANGE_NUMBER:
            query['topic'] = GERRIT_FORCETOPIC

        path = ('/changes/?q=' +
                '+'.join(k + ':' + v for k, v in query.iteritems()) +
                '&o=CURRENT_REVISION&o=CURRENT_COMMIT&o=CURRENT_FILES')
        res = self._get(path)
        if not res:
            return []

        # Gerrit uses " )]}'" to guard against XSSI.
        return json.loads(res.content[5:])

    def post_review(self, change, revision, review_input):
        """
        POST review_input for the given revision of change.
        """
        path = '/changes/' + change['id'] + '/revisions/' + revision + '/review'
        self._debug("post_review: path = '%s'", path)
        return self._post(path, review_input)

    def change_needs_review(self, change):
        """
        * Bail if the change isn't open (status is not 'NEW').
        * Bail if we've already reviewed the current revision.
        """
        status = change.get('status')
        if status != 'NEW':
            self._debug("change_needs_review: status = %s", status)
            return False

        current_revision = change.get('current_revision')
        if not current_revision:
            return False

        # Reject too old ones
        date_created = dateutil.parser.parse(change['revisions'][str(current_revision)]['created'])
        if abs(datetime.now() - date_created).days > IGNORE_OLDER_THAN_DAYS and not GERRIT_DRYRUN:
            self._debug("change_needs_review: Created too long ago")
            return False

        # Have we already checked this revision?
        if self.in_history(change['id'], current_revision):
            #self._debug("change_needs_review: already reviewed")
            return False

        self._debug("change_needs_review: current_revision = '%s'",
                    current_revision)

        return True

    def review_change(self, change):
        """
        Review the current revision of change.
        * Pipe the patch through checkpatch(es).
        * Save results to review history.
        * POST review to gerrit.
        """
        self._debug("review_change: change = %s, subject = '%s'",
                    change['id'], change.get('subject', ''))

        current_revision = change.get('current_revision')
        self._debug("change_needs_review: current_revision = '%s'",
                    current_revision)
        if not current_revision:
            return

        try:
            commit_message = workitem.change['revisions'][str(workitem.revision)]['commit']['message']
        except:
            commit_message = ""

        if change.get('branchwide'):
            files = ['everything']
        else:
            files = change['revisions'][str(current_revision)].get('files', [])
        (DoNothing, ilist, clist) = determine_testlist(files, is_trivial_requested(commit_message))

        # For testonly changes only do very minimal testing for now
        # Or not.
        #if is_testonly_requested(commit_message):
        #    clist = []
        if is_buildonly_requested(commit_message):
            clist = []
            ilist = []
        workItem = GerritWorkItem(change, ilist, clist, fsconfig, EmptyJob=DoNothing, Reviewer=self)
        if DoNothing:
            add_review_comment(workItem)
        else:
            managing_condition.acquire()
            managing_queue.put(workItem)
            managing_condition.notify()
            managing_condition.release()

        self.write_history(change['id'], current_revision, 0)

    def update(self):
        """
        GET recently updated changes and review as needed.
        """
        new_timestamp = _now()
        age = 1 # Age
        #self._debug("update: age = %d", age)

        open_changes = self.get_changes({'status':'open',
                                         '-age':str(age) + 'h',
                                         '-label':'Code-Review=-2'})
        #self._debug("update: got %d open_changes", len(open_changes))

        for change in open_changes:
            if self.change_needs_review(change):
                self.review_change(change)
                # Don't POST more than every post_interval seconds.
                time.sleep(self.post_interval)

        self.timestamp = new_timestamp
        self.write_history('-', '-', 0)

        # See if we got any commands
        for commandfile in os.listdir(GERRIT_COMMANDMONITORDIR):
            command = {}
            try:
                with open(GERRIT_COMMANDMONITORDIR + "/" + commandfile, "r") as cmdfl:
                    try:
                        command = json.load(cmdfl)
                    except: # XXX Add some json format error here
                        pass
                    os.unlink(GERRIT_COMMANDMONITORDIR + "/" + commandfile)
            except OSError:
                pass
            if not command:
                continue
            if command.get("retest-item"):
                retestitem = str(command['retest-item'])
                self._debug("Asked to retest build id: " + retestitem)
                # This is a retest request, see if we got new test list
                # or if not - clean up old tests.
                retestfile = DONEWITH_DIR + "/" + retestitem + ".pickle"
                if not os.path.exists(retestfile):
                    self._debug("Build id: " + retestitem + " does not exist")
                    continue # no file - nothing to do. error print?

                with open(retestfile, "rb") as blah:
                    try:
                        workitem = pickle.load(blah)
                    except:
                        self._debug("Build id: " + retestitem + " cannot be loaded")
                        continue
                if not workitem.BuildDone or workitem.BuildError:
                    self._debug("Build id: " + retestitem + " has no successful build")
                    # Cannot retest a failed build
                    continue
                workitem.retestiteration += 1
                workitem.TestingDone = False
                workitem.TestingStarted = False
                workitem.TestingError = False
                workitem.InitialTestingDone = False
                workitem.InitialTestingError = False
                workitem.InitialTestingStarted = False

                try:
                    if command.get("testlist"):
                        # XXX - this is a copy from another func. need to
                        # have it all in a single place
                        with open("tests/initial.json", "r") as blah:
                            initialtestlist = json.load(blah)
                        with open("tests/comprehensive.json", "r") as blah:
                            fulltestlist = json.load(blah)
                        with open("tests/lnet.json", "r") as blah:
                            lnettestlist = json.load(blah)
                        with open("tests/zfs.json", "r") as blah:
                            zfstestlist = json.load(blah)
                        with open("tests/ldiskfs.json", "r") as blah:
                            ldiskfstestlist = json.load(blah)

                        testarray = []
                        for item in command['testlist'].split(','):
                            item = item.strip()
                            for test in initialtestlist + fulltestlist + lnettestlist + zfstestlist + ldiskfstestlist:
                                if item == test[0]:
                                    testarray.append(test)
                                    break

                        zfsonly = command.get("zfs", True)
                        ldiskfsonly = command.get("ldiskfs", True)
                        DNE = command.get("DNE", True)
                        SSK = command.get("SSK", False)
                        SELINUX = command.get("SELINUX", False)
                        workitem.tests = []
                        # Force to ensure we test what was requested even if disabled
                        workitem.initial_tests = populate_testlist_from_array([], testarray, ldiskfsonly, zfsonly, DNE=DNE, Force=True, SSK=SSK, SELINUX=SELINUX)
                    else:
                        # copy existing tests
                        for tlist in (workitem.initial_tests, workitem.tests):
                            testarray = []
                            for item in tlist:
                                titem = {}
                                for elem in ('test', 'timeout', 'testparam', 'fstype', 'DNE', 'SSK', 'SELINUX'):
                                    if item.get(elem):
                                        titem[elem] = item[elem]
                                testarray.append(titem)
                            tlist = testarray
                except : # Add some array list here?
                    self._debug("Build id: " + retestitem + " cannot update test list")

                try:
                    os.unlink(retestfile)
                except OSError:
                    # cannot delete - skip
                    self._debug("Build id: " + retestitem + " cannot delete workitem")
                    continue

                WorkList.append(workitem)
                managing_condition.acquire()
                managing_queue.put(workitem)
                managing_condition.notify()
                managing_condition.release()
            elif command.get("abort"):
                buildnr = command.get("abort")
                self._debug("Requested abort of build " + str(buildnr))
                for item in WorkList:
                    if item.buildnr == buildnr:
                        item.Aborted = True
                        self._debug("Aborted build " + str(buildnr))
                        break
            else:
                self._debug("Unknown command file contents: " + str(command));

        # Now check if we have any branches to test
        for branch in os.listdir(GERRIT_BRANCHMONITORDIR):
            try:
                with open(GERRIT_BRANCHMONITORDIR + "/" + branch, "r") as brfil:
                    subject = brfil.read()
                    subject = subject.strip()

                os.unlink(GERRIT_BRANCHMONITORDIR + "/" + branch)
            except OSError:
                subject = "Cannot read file"
            # XXX
            url = "https://git.whamcloud.com/fs/lustre-release.git/patch/" + branch
            try:
                r = requests.get(url)
                revision = r.text.split(" ", 2)[1]
                changenum = int(revision[:8], 16)
            except requests.exceptions.RequestException:
                revision = branch
                changenum = 1 # all the same - so abort-unsafe
            except ValueError: # some garbage from gitweb?
                revision = branch
                changenum = "1" # all the same - so abort-unsafe
            change = {'branch':branch, '_number':changenum, 'branchwide':True,
                    'id':branch, 'subject':subject, 'current_revision':revision }
            self.review_change(change)

    def update_single_change(self, change):

        self.load_history()

        open_changes = self.get_changes({'status':'open',
                                         'change':change})
        self._debug("update: got %d open_changes", len(open_changes))

        for change in open_changes:
            if self.change_needs_review(change):
                self.review_change(change)

    def run(self):
        """
        * Load review history.
        * Call update() every poll_interval seconds.
        """

        if self.timestamp <= 0:
            self.load_history()

        while True:
            self.update()
            time.sleep(self.update_interval)

def save_WorkItem(workitem):
    print_WorkList_to_HTML()
    if workitem not in WorkList: # already removed?
        return
    with open(SAVEDSTATE_DIR + "/" + str(workitem.buildnr) + ".pickle", "wb") as output:
        workitem.lock.acquire()
        try:
            pickle.dump(workitem, output, pickle.HIGHEST_PROTOCOL )
        except RuntimeError,v:
            pass # We just want to avoid the crash. next iteration will write it out.
        workitem.lock.release()

def donewith_WorkItem(workitem):
    print_WorkList_to_HTML()
    print("Trying to be done with buildid " + str(workitem.buildnr))
    try:
        WorkList.remove(workitem)
    except ValueError:
        pass # We are not in the list, e.g. because this is a duplicate hit for like a crash processing

    try:
        os.unlink(SAVEDSTATE_DIR + "/" + str(workitem.buildnr) + ".pickle")
    except OSError:
        pass
    with open(DONEWITH_DIR + "/" + str(workitem.buildnr) + ".pickle", "wb") as output:
        pickle.dump(workitem, output, pickle.HIGHEST_PROTOCOL)

def print_WorkList_to_HTML():
    template = """
<html>
<head><title>Testing and queue status</title></head>
<body>
<h2>{status}</h2>
<h2>Work Items status</h2>
<table border=1>
<tr><th>Build number</th><th>Description</th><th>Status</th></tr>
{workitems}
</table>
</body>
</html>
"""
    if GERRIT_DRYRUN or GERRIT_CHANGE_NUMBER:
        status = "Draining Test Queue and Stopping"
    elif StopMachine:
        status = "Stopped"
    else:
        if WorkList:
            status = "Operational / working"
        else:
            status = "Operational / Idle"
        if GERRIT_FORCETOPIC:
            status += "(Only querying changes with topic: %s)" % (GERRIT_FORCETOPIC)
        if GERRIT_FORCETOPIC:
            status += "(Only querying changes on branch: %s)" % (GERRIT_BRANCH)
    workitems = ""
    for workitem in WorkList:
        workitems += '<tr><td>'
        if workitem.artifactsdir:
            workitems += '<a href="' + workitem.artifactsdir.replace(fsconfig['root_path_offset'], "") + "/" + workitem.get_results_filename() + '">'
        workitems += str(workitem.buildnr)
        if workitem.artifactsdir:
            workitems += '</a>'
        workitems += '</td><td>'

        if workitem.artifactsdir:
            workitems += '<a href="' + workitem.artifactsdir.replace(fsconfig['root_path_offset'], "") + "/" + workitem.get_results_filename() + '">'
        workitems += workitem.change['subject']
        if workitem.artifactsdir:
            workitems += '</a>'
        workitems += '</td><td>'

        if workitem.Aborted:
            workitems += "Aborted!"
        elif workitem.TestingDone:
            workitems += "Testing done"
            if workitem.TestingError:
                workitems += " (some tests failed)"
        elif workitem.TestingStarted:
            workitems += "Comprehensive testing"
            if workitem.TestingError:
                workitems += " (some tests failed already)"
        elif workitem.InitialTestingStarted:
            workitems += "Initial testing"
            if workitem.InitialTestingError:
                workitems += " (some tests failed already)"
        elif workitem.BuildError:
            workitems += "Build failed"
        else:
            if workitem.artifactsdir:
                workitems += "Building"
            else:
                workitems += "Waiting to build"

    all_items = {'status':status, 'workitems':workitems}
    with open(fsconfig["outputs"] + "/status.html", "w") as indexfile:
        indexfile.write(template.format(**all_items))


def run_workitem_manager():
    current_build = 1

    try:
        with open(LAST_BUILD_ID, 'rb') as blah:
            current_build = int(blah.read())
    except OSError:
        pass

    logger = logging.getLogger("WorkitemManager")

    while os.path.exists(fsconfig["outputs"] + "/" + str(current_build)):
        current_build += 1

    while True:
        managing_condition.acquire()
        while managing_queue.empty():
            managing_condition.wait()
        workitem = managing_queue.get()

        managing_condition.release()
        logger.info("ref " + workitem.ref + " build " + str(workitem.buildnr)  + " popped out of queue for next steps")

        #teststr = vars(workitem)
        #pprint(teststr)
        #sys.stdout.flush()

        if workitem.buildnr is None:
            # New item, we need to build it
            workitem.buildnr = current_build
            current_build += 1

            with open(LAST_BUILD_ID, 'wb') as output:
                output.write('%d' % current_build)

            # Mark all earlier revs as aborted first before we add ourselves in
            find_and_abort_duplicates(workitem)
            # Initial workitem save
            WorkList.append(workitem)
            save_WorkItem(workitem)
            print_WorkList_to_HTML() # Here do it separately to catch the new build
            logger.info("Got new ref " + workitem.ref + " assigned buildid " + str(workitem.buildnr))
            logger.info("for ref " + workitem.ref + " initial tests: " + str(workitem.initial_tests))
            logger.info("for ref " + workitem.ref + " full tests: " + str(workitem.tests))
            workitem.Write_HTML_Status()

            if GERRIT_DRYRUN:
                donewith_WorkItem(workitem)
                continue
            build_condition.acquire()
            build_queue.put([{}, workitem])
            build_condition.notify()
            build_condition.release()
            continue

        if workitem.AbortDone:
            # Just throw this one away, we notified everybody, these are
            # just strugglers coming out
            continue

        save_WorkItem(workitem)
        # We print all the updated state changes to gerrit here, and not above, but need to
        # move it above if we want to also print the "Job picked up" sort of messages
        add_review_comment(workitem)

        workitem.Write_HTML_Status()
        if workitem.Aborted:
            # Whoops, we no longer want to do anything with this item.
            donewith_WorkItem(workitem)
            continue

        if workitem.BuildDone and workitem.BuildError:
            # We just failed the build
            # report and don't return this item anywhere
            logger.warning("ref " + workitem.ref + " build " + str(workitem.buildnr)  + " failed building")
            donewith_WorkItem(workitem)
            continue

        if workitem.BuildDone and not workitem.initial_tests:
            # Same as above, but no big tests
            logger.info("ref " + workitem.ref + " build " + str(workitem.buildnr)  + " completed build, but no tests provided")
            # XXX
            donewith_WorkItem(workitem)
            continue

        if workitem.BuildDone and not workitem.InitialTestingStarted:
            # Just finished building, need to do some initial testing
            # Create the test output dir first
            testresultsdir = workitem.artifactsdir + "/" + fsconfig["testoutputdir"]

            if not os.path.exists(testresultsdir):
                try:
                    os.mkdir(testresultsdir)
                except OSError:
                    logger.error("Huh, cannot create test results dir for ...")
                    sys.exit(1)

            workitem.testresultsdir = testresultsdir
            workitem.InitialTestingStarted = True
            testing_condition.acquire()
            # Perhaps also sort by timeout once populated?
            for testinfo in sorted(workitem.initial_tests, key=operator.itemgetter('test')):
                # save/restart logic:
                if testinfo.get('Finished', False):
                    continue
                # First 0 is priority - highest
                testing_queue.put([0, testinfo, workitem])
                testing_condition.notify()
            testing_condition.release()
            continue

        if workitem.InitialTestingStarted and not workitem.InitialTestingDone:
            # More than one initial test enqueued, just wait for more
            # completions.
            # XXX racy if some parallel thing updates the status?
            logger.warning("ref " + workitem.ref + " build " + str(workitem.buildnr)  + " only partial initial test completion")
            continue

        if workitem.InitialTestingDone and workitem.InitialTestingError:
            # Need to report it and move on,
            # Don't return the item anywhere
            logger.warning("ref " + workitem.ref + " build " + str(workitem.buildnr)  + " failed initial testing")
            donewith_WorkItem(workitem)
            continue

        if workitem.InitialTestingDone and not workitem.tests:
            # Same as above, but no big tests
            logger.info("ref " + workitem.ref + " build " + str(workitem.buildnr)  + " completed initial testing and no full tests provided")
            # XXX
            donewith_WorkItem(workitem)
            continue

        if workitem.InitialTestingDone and not workitem.TestingStarted:
            # Initial testing finished, now need to do real testing
            logger.info("ref " + workitem.ref + " build " + str(workitem.buildnr)  + " completed initial testing and switching to full testing " + str(workitem.tests))
            workitem.TestingStarted = True
            testing_condition.acquire()
            # Perhaps sort by timeout once available to run short tests
            # first?
            for testinfo in sorted(workitem.tests, key=operator.itemgetter('test', 'fstype')):
                # save/restart logic:
                if testinfo.get('Finished', False):
                    continue
                # First 100 is second priority. perhaps sort by timeout instead?
                # could lead to prolonged stragglers.
                testing_queue.put([100, testinfo, workitem])
                testing_condition.notify()
            testing_condition.release()
            continue

        if workitem.TestingDone:
            # We don't really care if it's finished in error or not, that's
            # for the reporting code to care about, but we are all done here
            logger.info("All done testing ref " + workitem.ref + " build " + str(workitem.buildnr))
            donewith_WorkItem(workitem)
            continue

#def main():
#    """_"""
if __name__ == "__main__":
    logging.basicConfig(format='%(asctime)s %(message)s', level=logging.DEBUG)

    # Start our working threads
    with open("./test-nodes-config.json") as nodes_file:
        workers = json.load(nodes_file)
    with open("./fsconfig.json") as fsconfig_file:
        fsconfig = json.load(fsconfig_file)

    try:
        testoutputowner_uid = pwd.getpwnam(fsconfig.get("testoutputowner", "green")).pw_uid
    except:
        print("Cannot find uid of test output owner " + fsconfig.get("testoutputowner", "green"))
        sys.exit(1)

    fsconfig["testoutputowneruid"] = testoutputowner_uid

    builders = []
    for distro in distros:
        for arch in architectures:
            with open("builders-" + distro + "-" + arch + ".json") as buildersfile:
                buildersinfo = json.load(buildersfile)
                for builderinfo in buildersinfo:
                    builders.append(mybuilder.Builder(builderinfo, fsconfig, build_condition, build_queue, managing_condition, managing_queue))

    for worker in workers:
        worker['thread'] = mytester.Tester(worker, fsconfig, testing_condition,\
                                           testing_queue, managing_condition, \
                                           managing_queue)

    managerthread = threading.Thread(target=run_workitem_manager, args=())
    managerthread.daemon = True
    managerthread.start()

    with open(GERRIT_AUTH_PATH) as auth_file:
        auth = json.load(auth_file)
        username = auth[GERRIT_HOST]['gerrit/http']['username']
        password = auth[GERRIT_HOST]['gerrit/http']['password']

    reviewer = Reviewer(GERRIT_HOST, GERRIT_PROJECT, GERRIT_BRANCH,
                        username, password, REVIEW_HISTORY_PATH)

    # XXX Add item loading here
    for savedstateitem in os.listdir(SAVEDSTATE_DIR):
        with open(SAVEDSTATE_DIR + "/" + savedstateitem, "rb") as blah:
            try:
                workitem = pickle.load(blah)
            except:
                # delete bad item.
                os.unlink(SAVEDSTATE_DIR + "/" + savedstateitem)

            sys.stdout.flush()
            if not workitem.BuildDone:
                # Need to clean up build dir
                try:
                    shutil.rmtree(fsconfig["outputs"] + "/" + str(workitem.buildnr))
                except OSError:
                    pass # Ok if it's not there
            elif workitem.BuildError or (workitem.InitialTestingError and workitem.InitialTestingDone) or (workitem.TestingError and workitem.TestingDone):
                pass # just insert for final notify
            elif workitem.InitialTestingStarted and not workitem.InitialTestingDone:
                # To reinsert it we just need to unmark initial testing started
                workitem.InitialTestingStarted = False
            elif workitem.TestingStarted and not workitem.TestingDone:
                # Same here
                workitem.TestingStarted = False

            WorkList.append(workitem)
            managing_condition.acquire()
            managing_queue.put(workitem)
            managing_condition.notify()
            managing_condition.release()

    print_WorkList_to_HTML()

    try:
        if GERRIT_CHANGE_NUMBER:
            print("Asking for single change " + GERRIT_CHANGE_NUMBER)
            reviewer.update_single_change(GERRIT_CHANGE_NUMBER)

            time.sleep(3)
            while WorkList:
                # Just hang in here until done
                managerthread.join(1)
        else:
            reviewer.run()
    except KeyboardInterrupt:
        StopMachine = True
        print_WorkList_to_HTML()
        sys.exit(1)

