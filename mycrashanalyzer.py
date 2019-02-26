""" Threads for analyzing crashdumps
"""
import sys
import os
import time
import threading
import logging
import Queue
import subprocess32
import shlex
import json
import re
import psycopg2
from pprint import pprint
from subprocess32 import Popen, PIPE, TimeoutExpired

### Important - we need transform_null_equals = on in postgresql.conf or =null logic breaks

# XXX load them from json?
crashstarters = ["SysRq : Trigger a crash",
                 "BUG: unable to handle kernel paging request",
                 "BUG: unable to handle kernel NULL pointer dereference",
                 "NMI watchdog: BUG: soft lockup - CPU",
                 "WARNING: MMP writes to pool",
                 "Kernel panic - not syncing: Out of memory",
                 "kernel BUG at ",
                 "general protection fault:" #,
                 "Synchronous External Abort:", # And now ARM stuff
                 "Unable to handle kernel NULL pointer dereference",
                 "unable to handle kernel paging request",
                 "watchdog: BUG: soft lockup - "
                 ]
blacklisted_bt_funcs = [ "libcfs_call_trace", "dump_stack", "lbug_with_loc",
                         "0xffffffffffffffff", "ret_from_fork_nospec_begin",
                         "ret_from_fork_nospec_end"] 
crashenders = ["Code: ", "Kernel panic - not syncing: LBUG", "Starting crashdump kernel...", "DWARF2 unwinder stuck at", "Leftover inexact backtrace" ]
lustremodules = [ "[ldiskfs]", "[ldiskfs]", "[lnet]", "[lnet_selftest]", "[ko2iblnd]", "[ksocklnd]", "[ost]", "[lvfs]", "[fsfilt_ldiskfs]", "[mgs]", "[fid]", "[lod]", "[llog_test]", "[obdclass]", "[ptlrpc_gss]", "[ptlrpc]", "[obdfilter]", "[mdc]", "[mdt]", "[nodemap]", "[mdd]", "[mgc]", "[fld]", "[cmm]", "[osd_ldiskfs]", "[lustre]", "[obdecho]", "[osp]", "[lov]", "[mds]", "[lfsck]", "[lquota]", "[ofd]", "[kinode]", "[osc]", "[lmv]", "[osd_zfs]", "[libcfs]" ]


def extract_crash_from_dmesg(crashfile):
    crashlog = crashfile.read()

    lasttestline = None
    entirecrash = ""
    lasttestlogs = ""
    abbreviated_backtrace = ""
    recording_crash = False
    recording_backtrace = False
    stop_crash_recording = False
    crashfunction = None
    crashtrigger = None
    for line in crashlog.splitlines():
        line = line.strip()
        # skip empty lines
        if not line:
            continue
        # Strip initial timestamp
        if line[0] == '[':
            index = line.find(']')
            if index > 0:
                line = line[index+2:]
                if not line: # and skip empty line again
                    continue
        else:
            # Sometimes we have extra blak lines so Trace and such stuff
            # ends up on a line of itself, so lets assume that if
            # we have anything recorded, the line will work as is
            if not crashtrigger:
                continue # Skip nonkernel lines
        if not recording_crash:
            for crashline in crashstarters:
                if line.startswith(crashline):
                    entirecrash += line + "\n"
                    recording_crash = True
                    crashtrigger = crashline # For uniformity
                    break
            if recording_crash:
                continue
            # Now lustre specific stuff
            pattern = re.compile("LustreError: \d+:\d+:\([a-zA-Z0-9_\.]+:\d+:(\w+)\(\)\) (ASSERTION\( .* \) failed)")
            result = pattern.match(line)
            if not result:
                pattern = re.compile("LustreError: \d+:\d+:\([a-zA-Z0-9_\.]+:\d+:(\w+)\(\)\) (LBUG)")
                result = pattern.match(line)
            if result:
                entirecrash += line + "\n"
                crashtrigger = result.group(2)
                crashfunction = result.group(1)
                recording_crash = True
                continue

            # Now to see if it is a start of a new test
            if "Lustre: DEBUG MARKER: == " in line:
                lasttestline = line.replace('Lustre: DEBUG MARKER: == ', '')
                index = lasttestline.find('==') # some people forget spaces
                if index > 0:
                    lasttestline = lasttestline[:index].strip()
                lasttestlogs = ""
            elif lasttestline: # If in a known test - record all output
                lasttestlogs += line + "\n"
            else:
                if 'Lustre: Lustre: Build Version' in line or \
                    'libcfs: loading out-of-tree module taints kernel' in line:
                    # a bit of a hack to catch early failures
                    lasttestline = 'Module load'
                    lasttestlogs = line + '\n'
        else:
            # It's also ok if the crash ends with the file
            # Like in case of ooms and such
            for crashline in crashenders:
                if crashline in line:
                    recording_crash = False
                    recording_backtrace = False
                    stop_crash_recording = True
                    break

            if stop_crash_recording:
                break
            entirecrash += line + "\n"

            if recording_backtrace:
                bttokens = line.strip().split(' ', 3)
                if not bttokens[0].startswith('[<'):
                    # sometimes we get no address
                    if not "+0x" in bttokens[0]:
                        continue # Not an address so some cruft
                    bttokens.insert(0, "[<fakeaddress>]")
                if len(bttokens) < 2:
                    continue
                if bttokens[1] != '?':
                    # strip address and parts/isra/.. stuff
                    function = bttokens[1].split('+')[0].split('.')[0]
                    if function in blacklisted_bt_funcs:
                        continue
                    abbreviated_backtrace += function
                    #if len(bttokens) >= 3: # module name
                    #    abbreviated_backtrace += " " + bttokens[2]
                    abbreviated_backtrace += '\n'
            elif not crashfunction:
                pattern = re.compile("IP: \[<\w+>\] (\w+).*\+0x")
                result = pattern.match(line)
                if result:
                    crashfunction = result.group(1)
                    continue
                pattern = re.compile("RIP: \d+:\[<\w+>\]  \[<\w+>\] (\w+).*\+0x")
                result = pattern.match(line)
                if result:
                    crashfunction = result.group(1)
                    continue
                pattern = re.compile("PC is at (\w+)\+0x")
                result = pattern.match(line)
                if result:
                    crashfunction = result.group(1)
                    continue
            if line == 'Call Trace:' or line == 'Call trace:':
                recording_backtrace = True
            if crashfunction and line.startswith("LR is at "):
                # Special ARM handling for backtraces
                tokens = line.replace("LR is at ", "").split(" ")
                index = tokens[0].find("+")
                if len(tokens) < 3 and index > 0:
                    abbreviated_backtrace += tokens[0][:index]
                    #if len(tokens) == 2 and tokens[1].startswith('['):
                    #    # module name
                    #    abbreviated_backtrace += " " + tokens[1]
                    abbreviated_backtrace += "\n"


    return (lasttestline, entirecrash, lasttestlogs, crashtrigger, crashfunction, abbreviated_backtrace)

def is_known_crash(lasttest, crashtrigger, crashfunction, crashbt, fullbt, lasttestlogs, DBCONN=None):
    # Always load fresh definitions
    dbconn = DBCONN
    try:
        # XXX - read coonfig
        if not dbconn:
            dbconn = psycopg2.connect(dbname="crashinfo", user="crashinfo", password="blah", host="localhost")

        cur = dbconn.cursor()
        EXTRACONDS = ""
        # if we have no test info, cannot match for test so skip
        if not lasttest:
            EXTRACONDS += " AND testline IS NOT NULL"
        # If we have no test logs, cannot matc for inlogs, so skip
        if not lasttestlogs:
            EXTRACONDS += " AND inlogs IS NOT NULL"
        cur.execute("SELECT testline, inlogs, infullbt, bug, extrainfo FROM known_crashes where reason=%s AND func=%s" + EXTRACONDS +" AND strpos(%s, backtrace) > 0 ORDER BY testline DESC, inlogs DESC", (crashtrigger, crashfunction, crashbt))
        rows = cur.fetchall()
        cur.close()
    except psycopg2.DatabaseError:
        return (None, None)
    finally:
        if not DBCONN and dbconn:
            dbconn.close()

    for row in rows:
        if row[0] and lasttest and not row[0] in lasttest: # mandatory testline
            continue
        if row[1] and lasttestlogs:
            for line in row[1].splitlines():
                if not line in lasttestlogs:
                    # Mandatory line match in test output did not trigger
                    continue
        if row[2]:
            for line in row[2].splitlines():
                if not line in fullbt:
                    # Mandatory line match in full bt did not trigger
                    continue

        return (row[3], row[4])

    return (None, None)

def add_known_crash(lasttest, crashtrigger, crashfunction, crashbt, inlogs, infullbt, bug, extrainfo, DBCONN=None):
    dbconn = DBCONN

    if not bug:
        return False

    if not crashfunction:
        crashfunction = None
    if not lasttest:
        lasttest = None
    if not inlogs:
        inlogs = None
    if not crashbt:
        crashbt = None
    if not infullbt:
        infullbt = None
    try:
        if not dbconn:
            dbconn = psycopg2.connect(dbname="crashinfo", user="crashinfo", password="blah", host="localhost")
        cur = dbconn.cursor()
        # first ensure we don't have any new ones
        cur.execute("SELECT id FROM known_crashes WHERE reason=%s AND func=%s AND testline=%s AND strpos(backtrace, %s) > 0 AND inlogs=%s AND infullbt=%s", (crashtrigger, crashfunction, lasttest, crashbt, inlogs, infullbt))
        if cur.rowcount > 0:
            id = cur.fetchone()[0]
            print("Huh, adding a known crash that is already matching what we have at id: " + id)
            return False
        cur.execute("INSERT INTO known_crashes(reason, func, testline, backtrace, inlogs, infullbt, bug, extrainfo) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", (crashtrigger, crashfunction, lasttest, crashbt, inlogs, infullbt, bug, extrainfo))

        dbconn.commit()
        cur.close()
    except psycopg2.DatabaseError as e:
        print("Cannot insert new entry " + str(e))
        return False # huh, and what am I supposed to do here?
    finally:
        if not DBCONN and dbconn:
            dbconn.close()

    return True

def add_new_crash(lasttest, crashtrigger, crashfunction, crashbt, fullcrash, testlogs, link, DBCONN=None):
    """ Check if we have a matching crash and add it, if we have a new one,
        add a new one """
    newid = 0
    numreports = 0
    if not crashfunction:
        crashfunction = None
    if not lasttest:
        lasttest = None
    dbconn = DBCONN
    try:
        if not dbconn:
            dbconn = psycopg2.connect(dbname="crashinfo", user="crashinfo", password="blah", host="localhost")
        cur = dbconn.cursor()
        # First let's see if we have a matching crash
        cur.execute("SELECT new_crashes.id, count(triage.newcrash_id) as hitcounts FROM new_crashes, triage WHERE new_crashes.reason=%s AND new_crashes.func=%s AND new_crashes.backtrace=%s AND new_crashes.id = triage.newcrash_id group by new_crashes.id", (crashtrigger, crashfunction, crashbt))
        if cur.rowcount > 1:
            print("Error! not supposed to have more than one matching row in new crashes")
        if cur.rowcount > 0:
            row = cur.fetchone()
            newid = row[0]
            numreports = row[1]
        else:
            # Need to add it
            cur.execute("INSERT INTO new_crashes(reason, func, backtrace) VALUES(%s, %s, %s) RETURNING id", (crashtrigger, crashfunction, crashbt))
            newid = cur.fetchone()[0]

        cur.execute("INSERT INTO triage(link, testline, fullcrash, testlogs, newcrash_id) VALUES (%s, %s, %s, %s, %s)", (link, lasttest, fullcrash, testlogs, newid))
        dbconn.commit()
        cur.close()
    except psycopg2.DatabaseError as e:
        print(str(e))
        return (0, 0) # huh, and what am I supposed to do here?
    finally:
        if not DBCONN and dbconn:
            dbconn.close()

    return (newid, numreports)


class Crasher(object):
    def setup_custom_logger(self, name):
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

    def run_daemon(self, in_cond, in_queue, out_cond, out_queue):
        self.name = threading.current_thread().name
        self.logger = self.setup_custom_logger("crasher-%s.log" % (self.name))
        self.logger.info("Started crasher daemon")
        while True:
            in_cond.acquire()
            while in_queue.empty():
                in_cond.wait()
            self.Busy = True
            job = in_queue.get()
            self.logger.info("Remaining Crasher items in the queue left: " + str(in_queue.qsize()))
            in_cond.release()
            crashfilename = job[0]
            testinfo = job[1]
            distro = job[2]
            arch = job[3]
            workitem = job[4]
            self.logger.info("Got crash job for id " + str(workitem.buildnr))
            result = self.crash_worker(crashfilename, testinfo, distro, arch, workitem)
            self.logger.info("Finished crash job for id " + str(workitem.buildnr))
            out_cond.acquire()
            out_queue.put(workitem)
            out_cond.notify()
            out_cond.release()
            self.Busy = False

    def __init__(self, fsconfig, in_cond, in_queue, out_cond, out_queue):
        self.Busy = False
        self.fsconfig = fsconfig
        self.daemon = threading.Thread(target=self.run_daemon, args=(in_cond, in_queue, out_cond, out_queue))
        self.daemon.daemon = True
        self.daemon.start()

    def crash_worker(self, crashfilename, testinfo, distro, arch, workitem):
        # We probably don't want to work on aborted stuff?
        if workitem.Aborted:
            return True

        if not testinfo.get('ResultsDir', ""):
            self.logger.error("Got crash job, but no ResultsDir set?")
            return True

        try:
            with open("crash_processor.json", "r") as blah:
                crashprocessorinfo = json.load(blah)
        except OSError: # no file?
            return False

        command = "%s %s %s %s %s" % (crashprocessorinfo['command'], workitem.artifactsdir, crashfilename, distro, arch)
        args = shlex.split(command)

        try:
            processor = Popen(args, close_fds=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, universal_newlines=True)
        except (OSError) as details:
            self.logger.warning("Failed to run crash processor " + str(details))
            self.put_error("Failed to run crash processor", workitem)
            return False

        # We will not give any timeout here since we assume it's a well
        # mannered local job
        outs, errs = processor.communicate()

        if processor.returncode is not 0:
            self.logger.warning("Build " + str(workitem.buildnr) + " failed with code " + str(processor.returncode))
        else:
            self.logger.info("Build " + str(workitem.buildnr) + " done initial processing")

        try:
            with open(crashfilename + "-dmesg.txt", "r") as crashfile:
                (lasttestline, entirecrash, lasttestlogs, crashtrigger, crashfunction, abbreviated_backtrace) = extract_crash_from_dmesg(crashfile)
        except OSError:
            self.logger.warning("Build " + str(workitem.buildnr) + "no crash dmesg file")
            lasttestline = ""
            entirecrash = ""

        if not entirecrash: # Huh? empty crash?
            self.logger.error("Cannot extract crash message")
            return True # no useful data anyway

        # set the bt somewhere and triage it for newedness.
        (bug, extrainfo) = is_known_crash(lasttestline, crashtrigger, crashfunction, abbreviated_backtrace, entirecrash, lasttestlogs)
        if bug is not None:
            # Ok, there was a match, just append it to old message and move on
            message = "%s" % (bug)
            if extrainfo:
                message += "(%s)" % (extrainfo)
            testinfo['SubtestList'] = message
            return True # No need to look into decoded bt, this is a known crash

        # Need to generate our link
        resultsdir = testinfo.get('ResultsDir')
        if resultsdir:
            url = resultsdir.replace(self.fsconfig['root_path_offset'], self.fsconfig['http_server'])
        else:
            self.logger.warning("Build " + str(workitem.buildnr) + " no url?")
            url = "Build " + str(workitem.buildnr)

        # Lets record this new or previously seen crash and record status of it
        (newid, numreports) = add_new_crash(lasttestline, crashtrigger, crashfunction, abbreviated_backtrace, entirecrash, lasttestlogs, url)
        if newid: # 0 means there was some error
            message = "(Untriaged #%d, seen %d times before)" % (newid, numreports)
            testinfo['SubtestList'] = message
            if numreports > 20: # Frequently hit failure, don't bother posting below
                return True
        else:
            print("DB error")
            return True

        # Now let's see if any changes in this changeset were in this crash
        # based on filename only.
        # Of course we need to keep in mind that there are changes for branches
        # and those have no filenames
        if  workitem.change.get('revisions'):
            files = workitem.change['revisions'][str(workitem.change['current_revision'])]['files']
        else:
            # debug files = ['lustre/osc/osc_object.c']
            return # Nowhere to post changes, bail out

        try:
            with open(crashfilename + "-decoded-bt.txt", "r") as crashfile:
                crashlog = crashfile.read()
        except OSError:
            self.logger.warning("Build " + str(workitem.buildnr) + " no decoded crash bt?")
            return True # No crash bt so cannot decode, bail out

        lines = crashlog.splitlines()
        reviews = {}
        i = 1 # Skip first line
        while i < len(lines):
            line = lines[i].strip()
            i += 1
            # Skip spurious file info and exceptions
            if line[0] != '#':
                continue
            tokens = line.split(' ', 5)
            if len(tokens) < 6: # No kernel module info - skip
                i += 1 # Kernel always have debug info in my case, so skip it too
                continue
            if tokens[5] in lustremodules:
                # Ok, it's a lustre module, let's make sure it's not
                # LBUG itself
                if tokens[2] == "lbug_with_loc" and tokens[5] == "[libcfs]":
                    i += 1 # skip source line too
                    continue

                function = tokens[2]
                # Ok, now we know we have a lustre line, let's populate the item
                tokens = lines[i].strip().split(' ', 1)
                i += 1
                # Sanity check:
                if not tokens[0].startswith("/") or not tokens[1].isdigit():
                    continue # not a file and line info, huh?
                # Config variable!
                filename = tokens[0].replace("/home/green/git/lustre-release/", "")
                # Strip final colon
                nsym = len(filename)
                filename = filename[:nsym-1]
                fileline = int(tokens[1])
                if filename in files: # We got our first hit, so we'll record here
                    path_comments = reviews.setdefault(filename, [])
                    comment = "Crash with latest lustre function %s in backtrace called here:\n\n " % (function)
                    path_comments.append({'line':fileline, 'message': comment + entirecrash})

        if reviews: # there's at least some match and we have not seen it too much - let's print it as immediate message comment?
            message = "Crash in %s@%s" % (testinfo['test'], testinfo['fstype'])
            if testinfo.get('DNE', False):
                message += "+DNE"

            workitem.post_immediate_review_comment(message, reviews)
        else:
            # For now it still might be unrelated so... Just do nothing?
            pass

        return True
