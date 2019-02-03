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
# Adopted to work with smatch:
# Oleg Drokin <oleg.drokin@intel.com>
#
"""
Gerrit Checkpatch Reviewer Daemon
~~~~~~ ~~~~~~~~~~ ~~~~~~~~ ~~~~~~

* Watch for new change revisions in a gerrit instance.
* Pass new revisions through checkpatch script.
* POST reviews back to gerrit based on checkpatch output.
"""

import base64
import fnmatch
import logging
import json
import os
import requests
import subprocess
import time
import urllib

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
GERRIT_BRANCH = os.getenv('GERRIT_BRANCH', '')
GERRIT_AUTH_PATH = os.getenv('GERRIT_AUTH_PATH', 'GERRIT_AUTH')

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

CHECKPATCH_PATHS = _getenv_list('CHECKPATCH_PATHS', ['/home/green/bin/run_smatch.sh'])
CHECKPATCH_IGNORED_FILES = _getenv_list('CHECKPATCH_IGNORED_FILES', [
        'lustre/contrib/wireshark/packet-lustre.c',
        'lustre/ptlrpc/wiretest.c',
        'lustre/utils/wiretest.c',
        '*.patch'])
CHECKPATCH_IGNORED_KINDS = _getenv_list('CHECKPATCH_IGNORED_KINDS', [
        'debug:',
        'LEADING_SPACE'])
REVIEW_HISTORY_PATH = os.getenv('REVIEW_HISTORY_PATH', 'REVIEW_HISTORY')
STYLE_LINK = os.getenv('STYLE_LINK',
        'https://wiki.hpdd.intel.com/display/PUB/Coding+Guidelines')

USE_CODE_REVIEW_SCORE = False

UPDATE_INTERVAL = 300
POST_INTERVAL = 5
REQUEST_TIMEOUT = 60

def parse_checkpatch_output(out, path_line_comments, warning_count):
    """
    Parse string output out of CHECKPATCH into path_line_comments.
    Increment warning_count[0] for each warning.

    path_line_comments is { PATH: { LINE: [COMMENT, ...] }, ... }.
    """
    def add_comment(path, line, function, level, message):
        """_"""
        logging.debug("add_comment %s %d %s %s '%s'",
                      path, line, function, level, message)

        for pattern in CHECKPATCH_IGNORED_FILES:
            if fnmatch.fnmatch(path, pattern):
                return

        path_comments = path_line_comments.setdefault(path, {})
        line_comments = path_comments.setdefault(line, [])
        line_comments.append(level + ' ' + function + ':'+ message)
        warning_count[0] += 1

    level = None # 'ERROR', 'WARNING'
    message = None # 'code indent should use tabs where possible'
    function = None # function name

    for line in out.splitlines():
        # lnet/nidstrings.c:1158 libcfs_name2netstrfns() warn: always true condition '(libcfs_netstrfns[i]->nf_type >= 0) => (0-u32max >= 0)'

        line = line.strip()

        if not line:
            function, level, message = None, None, None
        else:
            # '#404: FILE: lustre/liblustre/dir.c:103:'
            tokens = line.split(' ', 3)

            if len(tokens) != 4:
                continue

            tmp = tokens[0].strip().split(':', 2)
            if len(tmp) != 2:
                continue

            path = tmp[0].strip()
            line_number_str = tmp[1].strip()
            if not line_number_str.isdigit():
                continue

            line_number = int(line_number_str)

            message = tokens[3].strip()
            level = tokens[2].strip()
            function = tokens[1].strip()

            if path and function and level and message:
                        add_comment(path, line_number, function, level, message)

def review_input_and_score(path_line_comments, warning_count, exitcode, text):
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
    elif exitcode != 0:
        score = -1
    else:
        score = +1

    if USE_CODE_REVIEW_SCORE:
        code_review_score = score
    else:
        code_review_score = 0

    if exitcode != 0:
        return {
            'message': ('Cannot build patch due to %d, messages:\n\n %s' %
                        (exitcode, text)),
            'labels': {
                'Code-Review': code_review_score
                },
            'notify': 'OWNER',
            }, score

    if score < 0:
        return {
            'message': ('%d code warning(s).\nIf you believe the warnings are incorrect, please leave a comment with your explanations as a reply to the warning.' %
                        (warning_count[0])),
            'labels': {
                'Code-Review': code_review_score
                },
            'comments': review_comments,
            'notify': 'OWNER',
            }, score
    else:
        return {
            'message': 'Code-wise looks good to me.',
            'labels': {
                'Code-Review': code_review_score
                },
            'notify': 'NONE',
            }, score


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
        self.post_enabled = True

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
            res = requests.get(url, auth=self.auth, timeout=REQUEST_TIMEOUT)
        except Exception as exc: # requests.exceptions.RequestException as exc:
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
                                auth=self.auth,
                                timeout=REQUEST_TIMEOUT)
        except Exception as exc: # requests.exceptions.RequestException as exc:
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
        if branch:
               query['branch'] = urllib.quote(branch, safe='')
        path = ('/changes/?q=' +
                '+'.join(k + ':' + v for k, v in query.iteritems()) +
                '&o=CURRENT_REVISION')
        res = self._get(path)
        if not res:
            return None

        # Gerrit uses " )]}'" to guard against XSSI.
        return json.loads(res.content[5:])

    def set_review(self, change, revision, review_input):
        """
        POST review_input for the given revision of change.
        """
        path = '/changes/' + change['id'] + '/revisions/' + revision + '/review'
        self._debug("set_review: path = '%s'", path)
        return self._post(path, review_input)

    def check_patch(self, change, revision):
        """
        Run each script in CHECKPATCH_PATHS on patch, return a
        ReviewInput() and score.
        """
        path_line_comments = {}
        warning_count = [0]

        ref = change['revisions'][str(revision)]['fetch']['ssh']['ref']

        for path in CHECKPATCH_PATHS:
            pipe = subprocess.Popen([path, ref, str(revision)],
                                    stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            out, err = pipe.communicate()
            self._debug("check_patch: path = %s, out = '%s...', err = '%s...'",
                        path, out[:80], err[:80])

            exitcode = pipe.returncode
            if exitcode == 0:
                parse_checkpatch_output(out, path_line_comments, warning_count)
            return review_input_and_score(path_line_comments, warning_count, exitcode, out)

    def review_change(self, change, force=False):
        """
        Review the current revision of change.
        * Bail if the change isn't open (status is not 'NEW').
        * GET the current revision from gerrit.
        * Bail if we've already reviewed it (unless force is True).
        * Pipe the patch through checkpatch(es).
        * Save results to review history.
        * POST review to gerrit.
        """
        self._debug("review_change: change = %s, subject = '%s'",
                    change['id'], change.get('subject', ''))

        status = change.get('status')
        if status != 'NEW':
            self._debug("review_change: status = %s", status)
            return False

        current_revision = change.get('current_revision')
        self._debug("review_change: current_revision = '%s'", current_revision)
        if not current_revision:
            return False

        # Have we already checked this revision?
        if self.in_history(change['id'], current_revision) and not force:
            self._debug("review_change: already reviewed")
            return False

        review_input, score = self.check_patch(change, current_revision)
        self._debug("review_change: score = %d", score)
        self.write_history(change['id'], current_revision, score)
        self.set_review(change, current_revision, review_input)
        # Don't POST more than every POST_INTERVAL seconds.
        time.sleep(POST_INTERVAL)

    def update(self):
        """
        GET recently updated changes and review as needed.
        """
        new_timestamp = _now()
        age = new_timestamp - self.timestamp + 60 * 60 # 1h padding
        self._debug("update: age = %d", age)

        open_changes = self.get_changes({'status':'open',
                                         '-age':str(age) + 's'})
        if open_changes is None:
            self._error("update: cannot fetch open changes")
            return

        self._debug("update: got %d open_changes", len(open_changes))

        for change in open_changes:
            self.review_change(change)

        self.timestamp = new_timestamp
        self.write_history('-', '-', 0)

    def run(self):
        """
        * Load review history.
        * Call update() every UPDATE_INTERVAL seconds.
        """

        if self.timestamp <= 0:
            self.load_history()

        while True:
            self.update()
            time.sleep(UPDATE_INTERVAL)


def main():
    """_"""
    logging.basicConfig(format='%(asctime)s %(message)s', level=logging.DEBUG)

    with open(GERRIT_AUTH_PATH) as auth_file:
        auth = json.load(auth_file)
        username = auth[GERRIT_HOST]['gerrit/http']['username']
        password = auth[GERRIT_HOST]['gerrit/http']['password']

    reviewer = Reviewer(GERRIT_HOST, GERRIT_PROJECT, GERRIT_BRANCH,
                        username, password, REVIEW_HISTORY_PATH)
    reviewer.run()


if __name__ == "__main__":
    main()
