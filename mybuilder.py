""" runtest.py - run VMs as needed to run a lustre test set
"""
import sys
import os
import threading
import logging
import shlex
import traceback
from pprint import pprint
from subprocess import Popen, PIPE, TimeoutExpired
import time

def parse_compile_error(change, stderr):
    """ Parse build error and create annotated gettit object """
    reviews = {}
    if not change.get('revisions'):
        return reviews

    files = change['revisions'][str(change['current_revision'])]['files']
    pprint(files)
    if not files:
        return reviews
    for line in stderr.splitlines():
        print("Working on: " + line)
        # lustre/llite/file.c:247:2: error: 'blah' undeclared (first use in this function)
        tokens = line.split(' ', 2)
        if len(tokens) != 3:
            continue

        tmp = tokens[0].strip().split(':', 2)
        if len(tmp) != 3:
            continue

        path = tmp[0].strip().replace('lustre/ptlrpc/../../','').replace('/home/green/git/lustre-release/', '') # also strip ptlrpc/ldlm cruft
        lineno_str = tmp[1].strip()
        if not lineno_str.isdigit():
            continue

        line_number = int(lineno_str)
        message = tokens[2].strip()
        level = tokens[1].strip()
        comment = level + " " + message

        if path not in files and "/" not in path:
            # Userspace files don't provide full path name, so
            # lets try to find it in the list
            for item in sorted(files):
                if os.path.basename(item) == path:
                    path = item
                    break

        # Let's see if it was found once more, if not - we cannot add
        # this item - gerrit would reject this comment
        if path not in files:
            print("path not in files", path)
            continue

        path_comments = reviews.setdefault(path, [])
        path_comments.append({'line':line_number, 'message': comment})

    return reviews

class Builder(object):
    def setup_custom_logger(self, name, logdir):
        formatter = logging.Formatter(fmt='%(asctime)s %(levelname)-8s %(message)s',
                                      datefmt='%Y-%m-%d %H:%M:%S')
        handler = logging.FileHandler(logdir + name, mode='a')
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
        self.logger = self.setup_custom_logger("builder-%s.log" % (self.name), self.fsinfo.get("mylogsdir", "logs") + "/")
        self.logger.info("Started daemon")
        while True:
            if self.RequestExit:
                self.logger.info("Exiting on request")
                return # painlessly terminate our thread. no locks held.
            in_cond.acquire()
            while in_queue.empty():
                # This means we cannot remove workers while build queue is
                # not empty.
                if self.OneShot or self.RequestExit:
                    self.RequestExit = True
                    in_cond.release()
                    self.logger.info("Exiting. Oneshot " + str(self.OneShot))
                    return # This terminates our thread
                in_cond.wait()

            self.Busy = True
            job = in_queue.get()
            self.logger.info("Remaining Builder items in the queue left: " + str(in_queue.qsize()))
            in_cond.release()
            buildinfo = job[0]
            workitem = job[1]
            self.logger.info("Got build job " + buildinfo.get('distro', 'NONE') + " for id " + str(workitem.buildnr))
            try:
                result = self.build_worker(buildinfo, workitem)
                self.logger.info("Finished build job " + buildinfo.get('distro', 'NONE') + " for id " + str(workitem.buildnr))
            except:
                tb = traceback.format_exc()
                self.logger.info("Exception in build job " + buildinfo.get('distro', 'NONE') + " for id " + str(workitem.buildnr) + ": " + str(sys.exc_info()))
                self.logger.info("backtrace: " + str(tb))
                result = True # No point in restarting a bad job?
                self.fatal_exceptions += 1

            if result:
                # On correct run return it to manager
                out_cond.acquire()
                out_queue.put(workitem)
                out_cond.notify()
                out_cond.release()
            else:
                # On incorrect run (VM problem or whatever) try again
                in_cond.acquire()
                in_queue.put(job)
                in_cond.notify()
                in_cond.release()
                time.sleep(10) # Sleep for a bit hoping the condition clears
                # XXX might want to make it an exponential backoff like in
                # my tester module

            self.Busy = False

    def __init__(self, builderinfo, fsinfo, in_cond, in_queue, out_cond, out_queue):
        self.command = builderinfo['run']
        self.fsinfo = fsinfo
        self.Busy = False
        self.RequestExit = False
        self.OneShot = builderinfo.get('oneshot', False)
        self.fatal_exceptions = 0
        self.daemon = threading.Thread(target=self.run_daemon, args=(in_cond, in_queue, out_cond, out_queue))
        self.daemon.daemon = True
        self.daemon.start()

    def build_worker(self, buildinfo, workitem):
        # Might need to make this per-arch?

        # Only check at the start.
        if workitem.Aborted:
            return True

        ref = workitem.ref
        buildnr = workitem.buildnr
        outdir = workitem.artifactsdir

        command = "%s %s %s %s %s %s" % (self.command, outdir, ref, buildnr, self.fsinfo["testoutputowner"], self.name)
        args = shlex.split(command)

        distro = buildinfo.get('distro', workitem.distro)

        workitem.UpdateBuildStatus(buildinfo, "", BuildStarted=True)

        try:
            env = os.environ.copy()
            env['DISTRO'] = distro

            builder = Popen(args, close_fds=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, universal_newlines=True, env=env)
        except (OSError) as details:
            self.logger.warning("Failed to run builder " + str(details))
            return False

        outs = ""
        errs = ""
        try:
            # Typically build takes 4-5 minutes, so 30 minutes should be aplenty
            # This is because we run our builders at the lowest priority and
            # so procuring enough cpu time might be hard under load.
            outs, errs = builder.communicate(timeout=1800)
        except TimeoutExpired:
            self.logger.info("Build " + str(buildnr) + " timed out, killing")
            builder.terminate()
            touts, terrs = builder.communicate()
            outs += touts
            errs += terrs
            workitem.UpdateBuildStatus(buildinfo, "Build is taking too long, aborting", Timeout=True, Failed=True, BuildStdOut=outs, BuildStdErr=errs)
            return True

        if builder.returncode != 0:
            code = builder.returncode
            self.logger.warning("Build " + str(buildnr) + " failed with code " + str(code))
            message = ""
            if code in (255, 2, 1):
                # Technically we want to put the job back into build queue
                message = "General error"
                self.logger.warning("stdout: " + outs)
                self.logger.warning("stderr: " + errs)
                return False
            elif code == 10:
                message = "git checkout error error: \n" + errs
                return False #let's retry
            elif code == 12:
                message = "Configure error: \n" + errs
            elif code == 14:
                # This is a build error, we can try to parse it
                reviewitems = parse_compile_error(workitem.change, errs)
                buildinfo['ReviewComments'] = reviewitems
                message = '%s: Compile failed\n' % (distro)
                if not reviewitems:
                    message += errs.replace('\n', '\n ')
            else:
                self.logger.warning("stdout: " + outs)
                self.logger.warning("stderr: " + errs)

            workitem.UpdateBuildStatus(buildinfo, message, Timeout=True, Failed=True, BuildStdOut=outs, BuildStdErr=errs)
        else:
            message = "Success"
            # XXX add a check that artifact exists
            workitem.UpdateBuildStatus(buildinfo, message, Finished=True, BuildStdOut=outs, BuildStdErr=errs)

        return True
