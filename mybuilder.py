""" runtest.py - run VMs as needed to run a lustre test set
"""
import sys
import os
import fcntl
import time
import threading
import logging
import Queue
import subprocess32
import shlex
import json
from pprint import pprint
from subprocess32 import Popen, PIPE, TimeoutExpired


class Builder(object):
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
        self.logger = self.setup_custom_logger("builder-%s.log" % (threading.current_thread().name))
        self.logger.info("Started daemon")
        while True:
            in_cond.acquire()
            while in_queue.empty():
                in_cond.wait()

            self.Busy = True
            job = in_queue.get()
            in_cond.release()
            builddata = job[0] # Unused for now
            workitem = job[1]
            self.logger.info("Got build job for id " + str(workitem.buildnr))
            result = self.build_worker(builddata, workitem)
            out_cond.acquire()
            out_queue.put(workitem)
            out_cond.notify()
            out_cond.release()
            self.Busy = False

    def __init__(self, builderinfo, fsinfo, in_cond, in_queue, out_cond, out_queue):
        self.command = builderinfo['run']
        self.fsinfo = fsinfo
        self.Busy = False
        self.daemon = threading.Thread(target=self.run_daemon, args=(in_cond, in_queue, out_cond, out_queue))
        self.daemon.daemon = True
        self.daemon.start()

    def put_error(self, statusmessage, workitem):
        workitem.BuildMessage = statusmessage
        workitem.BuildError = True
        workitem.BuildDone = True

    def build_worker(self, builddata, workitem):
        # Might need to make this per-arch?
        # XXX Also add distro here

        ref = workitem.ref
        buildnr = workitem.buildnr
        outdir = self.fsinfo["outputs"] + "/" + str(buildnr)
        workitem.artifactsdir = outdir
        try:
            os.mkdir(outdir)
        except:
            self.logger.error("Build dir already exists, huh?")
            self.put_error("Build dir already exists", workitem)
            return

        command = "%s %s %s %s %s" % (self.command, outdir, ref, buildnr, self.fsinfo["testoutputowner"])
        args = shlex.split(command)

        # XXX exception handling?
        os.chown(outdir, self.fsinfo["testoutputowneruid"], -1)

        try:
            builder = Popen(args, close_fds=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, universal_newlines=True)
        except (OSError) as details:
            self.logger.warning("Failed to run builder " + str(details))
            self.put_error("Failed to run builder", workitem)
            return

        try:
            # Typically build takes 4-5 minutes, so 10 minutes should be aplenty
            outs, errs = builder.communicate(timeout=600)
        except TimeoutExpired:
            self.logger.info("Build " + str(buildnr) + " timed out, killing")
            builder.terminate()
            touts, terrs = builder.communicate()
            self.put_error("Build is taking too long", workitem)

        if builder.returncode is not 0:
            self.logger.warning("Build " + str(buildnr) + " failed")
            self.put_error("Build failed", workitem)

        # XXX add a check that artifact exists


        # And finally chown back to root
        os.chown(outdir, 0, -1)

        workitem.BuildDone = True
        workitem.BuildMessage = "Success"

        return outs
