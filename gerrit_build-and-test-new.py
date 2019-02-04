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
import subprocess
import time
import urllib
from GerritWorkItem import GerritWorkItem
import Queue
import threading
import pwd
import re
import mybuilder
import mytester
from datetime import datetime
import dateutil.parser
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

SAVEDSTATE_DIR="savedstate"
DONEWITH_DIR="donewith"
LAST_BUILD_ID="LASTBUILD_ID"

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
managerthread = None
reviewer = None

fsconfig = {}

distros = ["centos7"]
architectures = ["x86_64"]
#initialtestlist=({'test':"runtests"},{'test':"runtests",'fstype':"zfs",'DNE':True,'timeout':600})
initialtestlist = [("runtests", 600)]
#testlist=({'test':"sanity", 'timeout':3600},{'test':"sanity",'fstype':"zfs",'DNE':True,'timeout':7200})
#testlist=[("sanity", 7200)]
testlist=[]

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
        'lnet/doc/*', 'lnet/klnds/gnilnd/*' ]
LNET_ONLY_FILES = [ 'lnet/*' ]
CODE_FILES = [ '*.[ch]' ]

def match_fnmatch_list(item, list):
    for pattern in list:
        if fnmatch.fnmatch(item, pattern):
            return True
    return False

def determine_testlist(filelist, trivial_requested):
    """ Try to guess what tests to run based on the changes """
    DoNothing = True
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

    if LDiskfsOnly and ZFSOnly:
        FullRun = True

    if LNetOnly and not FullRun and not LDiskfsOnly and not ZFSOnly:
        LNetOnly = True

    initial = []
    comprehensive = []

    if not DoNothing:
        for item in initialtestlist:
            if FullRun or LDiskfsOnly:
                test = {}
                test['test'] = item[0]
                test['fstype'] = "ldiskfs"
                test['timeout'] = item[1]
                initial.append(test)
            if ZFSOnly or FullRun:
                test = {}
                test['test'] = item[0]
                test['fstype'] = "zfs"
                test['timeout'] = item[1]
                test['DNE'] = True
                initial.append(test)
            if ZFSOnly and not FullRun:
                # Need to also do non-DNE run
                test = {}
                test['test'] = item[0]
                test['fstype'] = "zfs"
                test['timeout'] = item[1]
                initial.append(test)
            if LDiskfsOnly and not FullRun:
                # Need to capture DNE run for ldiskfs
                test = {}
                test['test'] = item[0]
                test['fstype'] = "ldiskfs"
                test['timeout'] = item[1]
                test['DNE'] = True
                initial.append(test)

        for item in testlist:
            if FullRun or LDiskfsOnly:
                test = {}
                test['test'] = item[0]
                test['fstype'] = "ldiskfs"
                test['timeout'] = item[1]
                comprehensive.append(test)
            if ZFSOnly or FullRun:
                test = {}
                test['test'] = item[0]
                test['fstype'] = "zfs"
                test['timeout'] = item[1]
                test['DNE'] = True
                comprehensive.append(test)
            if ZFSOnly and not FullRun:
                # Need to also do non-DNE run
                test = {}
                test['test'] = item[0]
                test['fstype'] = "zfs"
                test['timeout'] = item[1]
                comprehensive.append(test)
            if LDiskfsOnly and not FullRun:
                # Need to capture DNE run for ldiskfs
                test = {}
                test['test'] = item[0]
                test['fstype'] = "ldiskfs"
                test['timeout'] = item[1]
                test['DNE'] = True
                comprehensive.append(test)

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
    trivial_re = re.compile("^Test-Parameters:.*fortestdonly")
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

    if WorkItem.EmptyJob:
        message = 'Cannot detect any useful changes in this patch\n'
        if not is_trivial_requested(commit_message):
            message += TrivialNagMessage
    elif WorkItem.BuildDone and not WorkItem.InitialTestingStarted and not WorkItem.TestingStarted:
        # This is after initial build completion
        if WorkItem.BuildError:
            message = 'Build failed\nInsert some useful info and URL here'
            score = -1
            review_comments = WorkItem.ReviewComments
        else:
            message = 'Build for x86_64 centos7 successful\n'
            if WorkItem.initial_tests:
                message += 'Commencing initial testing ADD TESTLIST HERE'
            else:
                message += 'This was detected as a build-only change, no further testing would be performed by this bot.\n'
            if not is_trivial_requested(commit_message):
                message += TrivialNagMessage
                score = 1
    elif WorkItem.InitialTestingDone and not WorkItem.TestingStarted:
        # This is after initial tests
        if WorkItem.InitialTestingError:
            message = 'Initial testing failed\nINSERT USEFUL INFO AND LINKS HERE'
            score = -1
            review_comments = WorkItem.ReviewComments
        else:
            message = 'Initial testing succeeded\nINSERT TESTLIST\n'
            if WorkItem.tests:
                message += 'Commencing standard testing ADD TESTLIST'
            else:
                message += 'No additional testing was requested'
                score = 1
    elif WorkItem.TestingDone:
        message = ""
        if is_trivial_requested(commit_message):
            message += TrivialIgnoredMessage
        message += 'Testing has completed '
        if WorkItem.TestingError:
            message += 'with errors!\n'
            message += 'ADD SOME USEFUL INFO HERE\n'
            score = -1
            review_comments = WorkItem.ReviewComments
        else:
            message += 'Successfully\n'
        message += 'Add some useful links and info here'
    else:
        message = "Help, I don't know why I am here" + str(vars(WorkItem))

    # Errors = notify owner, no errors - no need to spam people
    if score < 0:
        notify = 'OWNER'
    else:
        notify = 'NONE'

    if USE_CODE_REVIEW_SCORE:
        code_review_score = score
    else:
        code_review_score = 0

    reviewer.post_review (WorkItem.change, WorkItem.revision, {
        'message': (message),
        'labels': {
            'Code-Review': code_review_score
            },
        'comments': review_comments,
        'notify': notify,
        })



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
        self.post_enabled = False # XXX
        self.post_interval = 5
        self.update_interval = 300
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

    def check_patch(self, patch):
        """
        Run each script in CHECKPATCH_PATHS on patch, return a
        ReviewInput() and score.
        """
        path_line_comments = {}
        warning_count = [0]

        for path in CHECKPATCH_PATHS:
            pipe = subprocess.Popen([path] + CHECKPATCH_ARGS,
                                    stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            out, err = pipe.communicate(patch)
            self._debug("check_patch: path = %s, out = '%s...', err = '%s...'",
                        path, out[:80], err[:80])
            parse_checkpatch_output(out, path_line_comments, warning_count)

        return review_input_and_score(path_line_comments, warning_count)

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
        self._debug("change_needs_review: current_revision = '%s'",
                    current_revision)
        if not current_revision:
            return False

        # Reject too old ones
        date_created = dateutil.parser.parse(change['revisions'][str(current_revision)]['created'])
        if abs(datetime.now() - date_created).days > IGNORE_OLDER_THAN_DAYS:
            self._debug("change_needs_review: Created too long ago")
            return False

        # Have we already checked this revision?
        if self.in_history(change['id'], current_revision):
            self._debug("change_needs_review: already reviewed")
            return False


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

        (DoNothing, ilist, clist) = determine_testlist(change['revisions'][str(current_revision)]['files'], is_trivial_requested(commit_message))

        # For testonly changes only do very minimal testing for now
        if is_testonly_requested(commit_message):
            clist = []
        if is_buildonly_requested(commit_message):
            clist = []
            ilist = []
        workItem = GerritWorkItem(change, ilist, clist, EmptyJob=DoNothing)
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
        age = new_timestamp - self.timestamp + 60 * 60 # 1h padding
        self._debug("update: age = %d", age)

        open_changes = self.get_changes({'status':'open',
                                         '-age':str(age) + 's'})
        self._debug("update: got %d open_changes", len(open_changes))

        for change in open_changes:
            if self.change_needs_review(change):
                self.review_change(change)
                # Don't POST more than every post_interval seconds.
                time.sleep(self.post_interval)

        self.timestamp = new_timestamp
        self.write_history('-', '-', 0)

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
    with open(SAVEDSTATE_DIR + "/" + str(workitem.buildnr) + ".pickle", "wb") as output:
        pickle.dump(workitem, output, pickle.HIGHEST_PROTOCOL )

def donewith_WorkItem(workitem):
    try:
        os.unlink(SAVEDSTATE_DIR + "/" + str(workitem.buildnr) + ".pickle")
    except OSError:
        pass
    with open(DONEWITH_DIR + "/" + str(workitem.buildnr) + ".pickle", "wb") as output:
        pickle.dump(workitem, output, pickle.HIGHEST_PROTOCOL)

def run_workitem_manager():
    current_build = 1

    try:
        with open(LAST_BUILD_ID, 'rb') as input:
            current_build = f.read('%d')
    except:
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

        teststr = vars(workitem)
        pprint(teststr)

        if workitem.buildnr is None:
            # New item, we need to build it
            workitem.buildnr = current_build
            current_build += 1

            with open(LAST_BUILD_ID, 'wb') as input:
                f.write('%d' % current_build)

            # Initial workitem save
            save_WorkItem(workitem)
            logger.info("Got new ref " + workitem.ref + " assigned buildid " + str(workitem.buildnr))
            build_condition.acquire()
            build_queue.put([{}, workitem])
            build_condition.notify()
            build_condition.release()
            continue

        save_WorkItem(workitem)
        # We print all the updated state changes to gerrit here, and not above, but need to
        # move it above if we want to also print the "Job picked up" sort of messages
        add_review_comment(workitem)

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
            workitem.TestingStarted = True
            testing_condition.acquire()
            # First 100 is second priority. perhaps sort by timeout instead?
            # could lead to prolonged struggling.
            for testinfo in workitem.tests:
                #testinfo['initial'] = False
                testing_queue.put([100, testinfo, workitem])
                testing_condition.notify()
            testing_condition.release()
            continue

        if workitem.TestingDone:
            # We don't really care if it's finished in error or not, that's
            # for the reporting code to care about, but we are all done here
            logger.info("All done testing ref " + workitem.ref + " build " + workitem.buildnr + "Success: " + str(not workitem.TestingError))
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
                                           testing_queue, managing_condition, managing_queue)

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

    if GERRIT_CHANGE_NUMBER:
        reviewer.update_single_change(GERRIT_CHANGE_NUMBER)
    else:
        reviewer.run()


