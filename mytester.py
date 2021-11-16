""" runtest.py - run VMs as needed to run a lustre test set
"""
import sys
import os
import io
import fcntl
import time
import threading
import logging
import re
import shlex
import json
import shutil
import traceback
import yaml
from pprint import pprint
from subprocess import Popen, PIPE, TimeoutExpired
import mycrashanalyzer
from mytestdatadb import process_results
from mytestdatadb import process_warning
from mytuplesorter import TupleSortingOn0
import myyamlsanitizer

class Node(object):
    def __init__(self, name, outputdir):
        self.name = name # Node name
        # XXX we probably want a better way eventually.
        self.consolelogfile = outputdir + "/" + name + "-console.txt"
        self.outputdir = outputdir
        self.process = None # Popen object
        self.outs = '' # full accumulated stdout output
        self.errs = '' # full accumulated stderr output
        self.consoleoutput = ""
        self.consolelogdesc = None
        self.last_test_line_time = time.time()

    def match_console_string(self, string):
        # Right now we assume the output cannot be changing as we are called
        # at the end. This migth change eventually I guess
        if not os.path.exists(self.consolelogfile):
            return False

        # If console output is not empty that means we have opened this
        # file in the past and there's no need to reopen it again
        # because it was closed by terminate or some such
        if not self.consolelogdesc and not self.consoleoutput:
            try:
                self.consolelogdesc = io.open(self.consolelogfile, "r", encoding = "ISO-8859-1")
            except OSError:
                return False
            fd = self.consolelogdesc.fileno()
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        try:
            newdata = self.consolelogdesc.read()
        except ValueError: # File was closed already
            newdata = ""

        # If this process is already dead, no new data can come so
        # let's close this preemptively
        if not self.is_alive():
            self.consolelogdesc.close() # Ok to do many times

        if "Lustre: DEBUG MARKER: == " in newdata:
            self.last_test_line_time = time.time()

        self.consoleoutput += newdata

        return string in self.consoleoutput

    def is_alive(self):
        if self.process is not None:
            self.process.poll()
            return self.process.returncode is None # None = did not terminate yet
        return False

    def wait_for_login(self):
        """ Returns error as string or None if all is fine. No timeout handling """

        fd = self.process.stdout.fileno()
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        deadlinetime = time.time() + 300 # IF a node did not come up in 5 minutes, something is wrong with it anyway. Only this long because initial nfs mount for client state is somewhat slow.
        while time.time() <= deadlinetime:
            try:
                string = self.process.stdout.read()
            except:
                string = ''
                time.sleep(1)
            else:
                self.outs += string
                #pprint(string)

            self.process.poll()
            if self.process.returncode is not None:
                # Capture stderr too
                string = self.process.stderr.read()
                self.errs += string
                return "Process died"
            if "Entering emergency mode. Exit the shell to continue" in self.outs:
                print("Emergency mode shell detected!")
                return "Emergency shell"
            # Happens in fedora and rhel8 at times.
            if "nbd: nbd0 already in use" in self.outs:
                return "nbd0 is in use"
            if "login:" in self.outs:
                # Restore old blocking behavior
                fcntl.fcntl(fd, fcntl.F_SETFL, fl)
                return None
        # Hm, the loop ended somehow?
        self.process.terminate()
        return "Timed Out waiting for login prompt"

    def terminate(self):
        if self.consolelogdesc is not None:
            self.consolelogdesc.close() # Safe to do many times
        if self.process is None or self.process.returncode is not None:
            return
        try:
            self.process.terminate()
        except OSError: # Already dead? ignore
            pass
        # This can actually hang too if the process refuses to die.
        # 3 minutes sounds like an extreme, but we want to avoid making
        # it available until it's truly dead or until the timoeut has triggered.
        # Helps us to save crashdumps and whatnot I guess
        try:
            outs, errs = self.process.communicate(timeout=180)
            self.outs += outs
            self.errs += errs
        except TimeoutExpired:
            print(self.name + " did not die after terminate, leaving it be")

    def returncode(self):
        self.process.poll()
        return self.process.returncode

    def check_node_alive(self):
        self.process.poll()
        if self.process.returncode is None:
            return True

        outs, errs = self.process.communicate()
        self.outs += outs
        self.errs += errs
        return False

    def dump_core(self, prefix):
        """ This dumps core and asks the qemu to quit, assumes qemu """
        if not self.process or not self.check_node_alive():
            return None
        outs = ''
        errs = ''
        corename = '%s/%s-%s-core' % (self.outputdir, self.name, prefix)
        # \1c is to trigger monitor mode
        command = '\1c\ndump-guest-memory -l %s\nquit\n' % (corename)
        try:
            outs, errs = self.process.communicate(input=command, timeout=1)
        except TimeoutExpired:
            pass
        self.outs += outs
        self.errs += errs
        return corename

class Tester(object):
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
        self.logger = self.setup_custom_logger("tester-%s.log" % (self.name), self.fsinfo.get("mylogsdir", "logs") + "/")
        self.logger.info("Started daemon")
        # Would be great to verify all nodes are operational here, but
        # Alas, need kernels and initrds for that. Perhaps require some
        # default ones in json config?
        sleep_on_error = 15
        while True:
            if self.RequestExit:
                self.logger.info("Exiting on request")
                return # painlessly terminate our thread. no locks held.
            in_cond.acquire()
            while in_queue.empty():
                # This means we cannot remove workers while work queue is
                # not empty.
                if self.OneShot or self.RequestExit:
                    self.RequestExit = True
                    in_cond.release()
                    self.logger.info("Exiting. Oneshot " + str(self.OneShot))
                    return # This terminates our thread
                in_cond.wait()

            job = in_queue.get()
            self.logger.info("Remaining Testing items in the queue left: " + str(in_queue.qsize()))
            self.Busy = True
            in_cond.release()
            priority = job[0] # Not really used here
            testinfo = job[1]
            workitem = job[2]
            self.logger.info("Got job buildid " + str(workitem.buildnr) + " test " + str(testinfo))
            try:
                result = self.test_worker(testinfo, workitem)
                # save the test output if any
                if self.testresultsdir and self.testouts:
                    try:
                        with open(self.testresultsdir + "/test.stdout", "w") as sout:
                            sout.write(self.testouts)
                        with open(self.testresultsdir + "/test.stderr", "w") as serr:
                            serr.write(self.testerrs)
                    except OSError:
                        pass # what can we do
            except:
                tb = traceback.format_exc()
                self.logger.info("Exception in job buildid " + str(workitem.buildnr) + " " + testinfo['name'] + '-' + testinfo['fstype'] + ": " + str(sys.exc_info()))
                self.logger.info("backtrace: " + str(tb))
                result = True # No point in restarting a bad job?
                              # Note it would probably hang forever in the
                              # queue requiring a restart of some sort
                self.fatal_exceptions += 1

            if result:
                sleep_on_error = 15 # Reset the backoff time after successful run
                self.collect_syslogs()
                self.update_permissions()
                self.logger.info("Finished job buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'])
                # If we had a crash or timeout, a separate item was
                # started that would process it and return to queue.
                if (self.CrashDetected and self.crashfiles) or self.TimeoutDetected:
                    self.logger.info("crash detected or timeout, they will post their stuff separately")
                else:
                    out_cond.acquire()
                    out_queue.put(workitem)
                    out_cond.notify()
                    out_cond.release()
            else:
                # We had some problem with our VMs or whatnot, return
                # the job to the pool for retrying and sleep for some time
                failcount = testinfo.get("failcount", 0) + 1
                testinfo["failcount"] = failcount
                self.logger.info("Failed to test job buildid " + str(workitem.buildnr) + " test " + str(testinfo) + " #" + str(failcount))
                in_cond.acquire()
                in_queue.put(TupleSortingOn0((priority, testinfo, workitem)))
                in_cond.notify()
                in_cond.release()
                self.Invalid = True
                self.logger.info("Going to sleep for " + str(sleep_on_error) + "seconds")
                time.sleep(sleep_on_error)
                self.logger.info("Woke after sleep")
                sleep_on_error *= 2
                # Cap sleep on error at 10 minutes
                if sleep_on_error > 600:
                    sleep_on_error = 600
                self.Invalid = False

            self.Busy = False
            self.cleanup_after_run()


    def __init__(self, workerinfo, fsinfo, in_cond, in_queue, out_cond, out_queue):
        self.name = workerinfo['name']
        self.serverruncommand = workerinfo['serverrun']
        self.clientruncommand = workerinfo['clientrun']
        self.servernetname = workerinfo['servername']
        self.clientnetname = workerinfo['clientname']
        self.serverarch = workerinfo.get('serverarch', "invalid")
        self.clientarch = workerinfo.get('clientarch', "invalid")
        self.testresultsdir = ""
        self.fsinfo = fsinfo
        self.Busy = False
        self.Invalid = False
        self.RequestExit = False
        # Oneshot means exit as soon as there are no more queued test items.
        self.OneShot = workerinfo.get('oneshot', False)
        self.CrashDetected = False # This is for wen core was detected
        self.Crashed = False # This is when something died
        self.TimeoutDetected = False
        self.error = False
        self.crashfiles = []
        self.testerrs = ''
        self.testouts = ''
        self.startTime = 0
        self.fatal_exceptions = 0
        self.out_cond = out_cond
        self.out_queue = out_queue
        self.daemon = threading.Thread(target=self.run_daemon, args=(in_cond, in_queue, out_cond, out_queue))
        self.daemon.daemon = True
        self.daemon.start()

    def update_permissions(self):
        """ Update all files to be readable in the test dir """
        if not os.path.exists(self.testresultsdir):
            return
        for filename in os.listdir(self.testresultsdir):
            path = self.testresultsdir + "/" + filename
            if not os.path.isdir(path):
                try:
                    os.chmod(path, 0o644)
                except OSError:
                    pass # what can we do

    def collect_syslogs(self):
        for node in [self.servernetname, self.clientnetname]:
            syslogfilename = self.fsinfo["syslogdir"] + "/" + node + ".syslog.log"

            if not os.path.exists(syslogfilename):
                self.logger.warning("Attempting to collect missing syslog for " + node)
                return
            shutil.copy(syslogfilename, self.testresultsdir + "/")
            try:
                os.chmod(self.testresultsdir + "/" + node + ".syslog.log", 0o644)
            except OSError:
                pass

    def collect_crashdump(self, node):
        crashfilename = None
        if node.returncode() is None:
            return None # It's still alive, so no crashdumps

        crashdirname = self.fsinfo["crashdumps"] + "/" + node.name
        if not os.path.exists(crashdirname):
            return None

        if not os.path.isdir(crashdirname):
            self.logger.warning("crashdir location not a dir " + crashdirname)

        outputlocationpathprefix = self.testresultsdir + "/" + node.name + "-"

        haveCrashfiles = False
        for crash in os.listdir(crashdirname):
            haveCrashfiles = True
            for item in ["vmcore-dmesg.txt", "vmcore"]:
                filename = crashdirname + "/" + crash + "/" + item
                if os.path.exists(filename):
                    shutil.copy(filename, outputlocationpathprefix + item)
            filename = crashdirname + "/" + crash + "/vmcore.flat"
            if os.path.exists(filename):
                vmcore_flat = open(filename)
                try:
                    result = Popen("makedumpfile -R '" + outputlocationpathprefix + "vmcore'", shell=True, stdin=vmcore_flat, stdout=PIPE, stderr=PIPE)
                except OSError as e:
                    self.logger.warning("Error trying to capture corefile " + str(e))
                else:
                    outs, errs = result.communicate()
                    if result.returncode != 0:
                        self.logger.warning("Failed processing of core file " + filename + " to " + outputlocationpathprefix + "vmcore with " + outs + " and " + errs)
                    else:
                        try:
                            os.chmod(outputlocationpathprefix + "vmcore", 0o644)
                        except OSError:
                            pass # What can we do?
                        self.CrashDetected = True
                        crashfilename = outputlocationpathprefix + "vmcore"

                vmcore_flat.close()

        # Now remove the crash data if we detected something
        if self.CrashDetected:
            shutil.rmtree(crashdirname)
        elif haveCrashfiles:
            if not self.Crashed:
                self.logger.warning("Not marked crashed, but have a crash file?")

            try:
                shutil.copytree(crashdirname, outputlocationpathprefix + "unprocessed")
            except:
                self.logger.warning("Cannot move for analysis, making local copy")
                try:
                    os.rename(crashdirname, crashdirname + "-unprocessed")
                except:
                    pass

        return crashfilename

    def match_test_output(self, testname, patterns):
        """ search for array of patterns in test output for testname
            and return matching patterns
        """
        matches = []
        filename = self.testresultsdir + "/" + testname + ".suite_log." + self.clientnetname + ".log"
        if not os.path.exists(filename):
            self.logger.warning("suite log " + filename + " does not exist in match_test_output")
            return matches

        if os.stat(filename).st_size == 0:
            self.logger.warning("suite log is empty in match_test_output")
            return matches

        try:
            with open(filename, "r", encoding = "ISO-8859-1") as suitefd:
                suitelog = suitefd.read()
        except OSError:
            return matches

        for pattern in patterns:
            if pattern.get('string', 'blahblah') in suitelog:
                matches.append(pattern)
        return matches

    def get_duration(self):
        return int(time.time() - self.startTime)

    def cleanup_after_run(self):
        self.testerrs = ''
        self.testouts = ''
        self.crashfiles = []

    def init_new_run(self):
        self.testerrs = ''
        self.testouts = ''
        self.CrashDetected = False
        self.Crashed = False
        self.TimeoutDetected = False
        self.crashfiles = []
        self.error = False
        self.startTime = time.time()
        # Cleanup old crashdumps and syslogs
        for nodename in [self.servernetname, self.clientnetname]:
            try:
                shutil.rmtree(self.fsinfo["crashdumps"] + "/" + nodename)
            except OSError:
                pass # Not there, who cares
            try:
                with open(self.fsinfo["syslogdir"] + "/" + nodename + ".syslog.log", 'wb') as f:
                    f.truncate(0)
            except:
                pass # duh, no syslog file yet?

    def test_worker(self, testinfo, workitem):
        """ Returns False if the test should be retried because had some unrelated problemi """
        self.init_new_run()
        artifactdir = workitem.artifactsdir
        outdir = workitem.testresultsdir
        distro = testinfo.get("forcedistro", workitem.distro)
        clientdistro = testinfo.get("clientdistro", distro)
        serverdistro = testinfo.get("serverdistro", distro)

        # XXX Incorporate distro into this somehow?
        testscript = testinfo.get("test", None)
        if testscript is None:
            workitem.UpdateTestStatus(testinfo, "Invalid testinfo!", Failed=True)
            return True # No point in retrying an invalid test

        if workitem.Aborted:
            return True

        if testinfo.get("failcount", 0) > 30: # arbitrary high number
            workitem.UpdateTestStatus(testinfo, "Cannot get this item to successfully run for 30 times! Giving up", Failed=True)
            return True

        console_errors = []
        if os.path.exists("console_errors_lookup.json"):
            try:
                with open("console_errors_lookup.json", "r") as errfile:
                    console_errors = json.load(errfile)
            except: # any error really
                self.logger.error("Failure loading console errors description?")

        timeout = testinfo.get("timeout", -1)
        if timeout == -1:
            timeout = 7*3600 # we are willing to wait up to 7 hours for unknown tests
        # 1 hour by default for a single test to sit there doing nothing before timing out.
        single_subtest_timeout = testinfo.get("singletimeout", 3600)

        fstype = testinfo.get("fstype", "ldiskfs")
        DNE = testinfo.get("DNE", False)
        SELINUX = testinfo.get("SELINUX", False)
        SSK = testinfo.get("SSK", False)
        serverkernel = artifactdir +"/kernel-%s-%s" % (serverdistro, self.serverarch)
        clientkernel = artifactdir +"/kernel-%s-%s" % (clientdistro, self.clientarch)
        serverinitrd = artifactdir +"/initrd-%s-%s.img" % (serverdistro, self.serverarch)
        clientinitrd = artifactdir +"/initrd-%s-%s.img" % (clientdistro, self.clientarch)

        # testscript is just the script, but there might be a symbolic name too
        testresultsdir = outdir + "/" + testinfo.get('name', testscript) + "-" + fstype
        if DNE:
            testresultsdir += "-DNE"
        if SSK:
            testresultsdir += "-SSK"
        if SELINUX:
            testresultsdir += "-SELINUX"

        testresultsdir += "-" + serverdistro + "_" + self.serverarch
        testresultsdir += "-" + clientdistro + "_" + self.clientarch

        clientbuild = artifactdir + "/lustre-" + clientdistro + "-" + self.clientarch + ".ssq"
        serverbuild = artifactdir + "/lustre-" + serverdistro + "-" + self.serverarch + ".ssq"

        if not os.path.exists(serverbuild) or not os.path.exists(clientbuild) or \
           not os.path.exists(serverkernel) or not os.path.exists(serverinitrd) or \
           not os.path.exists(clientkernel) or not os.path.exists(clientinitrd):
            self.logger.error("Our build artifacts are missing for build " + str(workitem.buildnr))
            self.logger.error("server build " + serverbuild + ": " + str(os.path.exists(serverbuild)))
            self.logger.error("client build " + clientbuild + ": " + str(os.path.exists(clientbuild)))
            self.logger.error("server kernel " + serverkernel + ": " + str(os.path.exists(serverkernel)))
            self.logger.error("client kernel " + clientkernel + ": " + str(os.path.exists(clientkernel)))
            self.logger.error("server initrd " + serverinitrd + ": " + str(os.path.exists(serverinitrd)))
            self.logger.error("client initrd " + clientinitrd + ": " + str(os.path.exists(clientinitrd)))
            workitem.UpdateTestStatus(testinfo, "Build artifacts missing", Failed=True)
            return True # If we don't see 'em, nobody can see 'em

        # Let's see if this is a retest and create a new dir for that
        if os.path.exists(testresultsdir):
            retry = 1
            while os.path.exists(testresultsdir + "-retry" + str(retry)):
                retry += 1
            testresultsdir += "-retry" + str(retry)

        # Make output test results dir:
        try:
            os.mkdir(testresultsdir)
            # Make it writeable for the tests
            os.chown(testresultsdir, self.fsinfo["testoutputowneruid"], -1)
            # Don't let them delete other files
            os.chmod(testresultsdir, 0o1755)
        except OSError:
            self.logger.error("Huh, cannot create test results dir: " + testresultsdir)
            return False

        # To know where to copy syslogs and core files
        self.testresultsdir = testresultsdir

        server = Node(self.servernetname, testresultsdir)
        client = Node(self.clientnetname, testresultsdir)

        workitem.UpdateTestStatus(testinfo, None, ResultsDir=testresultsdir)

        # SELinux check here since it needs a command line argument

        env = os.environ.copy()
        if testinfo.get("vmparams"):
            VMPARAMS = dict(re.findall(r'(\S+)=(".*?"|\S+)', testinfo['vmparams']))
            for p in VMPARAMS:
                env[p] = VMPARAMS[p]

        try:
            env['DISTRO'] = serverdistro
            server.process = Popen([self.serverruncommand, server.name, serverkernel, serverinitrd, serverbuild, testresultsdir], close_fds=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, universal_newlines=True, env=env)
        except (OSError) as details:
            self.logger.warning("Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'] + " Failed to run server " + str(details))
            return False

        try:
            env['DISTRO'] = clientdistro
            client.process = Popen([self.clientruncommand, client.name, clientkernel, clientinitrd, clientbuild, testresultsdir], close_fds=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, universal_newlines=True, env=env)
        except (OSError) as details:
            self.logger.warning("Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'] + " Failed to run client " + str(details))
            server.terminate()
            return False
        # Now we need to wait until both have booted and gave us login prompt
        if server.wait_for_login() is not None:
            client.terminate()
            #pprint(server.errs)
            self.logger.warning("Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'] + " Server did not show login prompt " + str(server.errs) + " " + str(server.outs) + " " + str([self.serverruncommand, server.name, serverkernel, serverinitrd, serverbuild, testresultsdir]))
            return False
        if client.wait_for_login() is not None:
            server.terminate()
            #pprint(client.errs)
            self.logger.warning("Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'] + " Client did not show login prompt" + str(client.errs) + " " + str(client.outs) + " " + str([self.clientruncommand, client.name, clientkernel, clientinitrd, clientbuild, testresultsdir]))
            return False

        if workitem.Aborted:
            server.terminate()
            client.terminate()
            return True

        # Now perform initial preparations like starting kdump and mount NFS in VM
        try:
            command = "ssh -o StrictHostKeyChecking=no root@" + self.clientnetname + \
                    " 'systemctl start kdump ; mkdir /tmp/testlogs ; mount 192.168.200.253:/" + \
                    testresultsdir + " /tmp/testlogs -t nfs'"
            args = shlex.split(command)
            setupprocess = Popen(args, close_fds=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, universal_newlines=True)
            outs, errs = setupprocess.communicate(timeout=600) # XXX timeout handling
            self.testouts += outs
            self.testerrs += errs
            if setupprocess.returncode != 0:
                self.logger.warning("Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'] + " Failed to setup test environment: " + self.testerrs + " " + self.testouts)
                server.terminate()
                client.terminate()
                return False
        except OSError as details:
            self.logger.warning("Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'] + " Failed to run test setup " + str(details))
            server.terminate()
            client.terminate()
            return False
        except TimeoutExpired:
            self.logger.warning("Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'] + " Timed out mounting nfs")
            setupprocess.terminate()
            server.terminate()
            client.terminate()
            return False

        del setupprocess

        if workitem.Aborted:
            self.logger.warning("job for buildid " + str(workitem.buildnr) + " aborted")
            server.terminate()
            client.terminate()
            return True

        try:
            if DNE:
                DNEStr = " MDSDEV2=/dev/vdd MDSCOUNT=2 "
            else:
                DNEStr = " "

            if SSK:
                SSKSTR = " SHARED_KEY=true "
            else:
                SSKSTR = " "
            if SELINUX:
                SELINUXSTR = " " # XXX
            else:
                SELINUXSTR = " "

            TESTPARAMS = testinfo.get('testparam', '')
            ENVPARAMS = testinfo.get('env', '')
            AUSTERPARAMS = testinfo.get('austerparam', '')
            # XXX - this stuff should be in some config
            args = ["ssh", "-tt", "-o", "ServerAliveInterval=0", "-o",
                    "StrictHostKeyChecking=no", "root@" + self.clientnetname,
                    'PDSH="pdsh -S -Rssh -w" mds_HOST=' + self.servernetname +
                    " ost_HOST=" + self.servernetname + " MDSDEV1=/dev/vdc " +
                    "OSTDEV1=/dev/vde OSTDEV2=/dev/vdf LOAD_MODULES_REMOTE=true " +
                    "FSTYPE=" + fstype + DNEStr + SSKSTR + SELINUXSTR +
                    "MDSSIZE=0 OSTSIZE=0 " +
                    "MGSSIZE=0 " + ENVPARAMS + " "
                    "NAME=ncli /home/green/git/lustre-release/lustre/tests/auster -D /tmp/testlogs/ -r -k " + AUSTERPARAMS + " " + testscript + " " + TESTPARAMS]
            testprocess = Popen(args, close_fds=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, universal_newlines=True)
        except (OSError) as details:
            self.logger.warning("Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'] + " Failed to run test " + str(details))
            server.terminate()
            client.terminate()
            return False


        # XXX Up to this point, if there are any crashdumps, they would not
        # be captured. This is probably fine because Lustre was not involved
        # yet? From this point on all failures are assumed to be test-related

        self.startTime = time.time()
        deadlinetime = self.startTime + timeout
        message = ""
        warnings = ""
        matched_suite_errors = []
        while testprocess.returncode is None: # XXX add a timer
            try:
                # This is a very ugly workaround to the fact that when you call
                # communicate with timeout, it polls(!!!) the FDs of the subprocess
                # at an insane rate resulting in huge cpu hog. So only let it
                # poll once per call and we do our sleeping ourselves.
                # XXX - perhaps consider doing some sort of a manual select call?
                time.sleep(5) # every 5 seconds, not ideal because that becomes our latency
                outs = "" # avoid repeats
                errs = ""
                outs, errs = testprocess.communicate(timeout=0.01) # cannot have 0 somehow
            except ValueError:
                testprocess.poll()
                break # dead already?
            except TimeoutExpired:
                if workitem.Aborted:
                    self.logger.warning("job for buildid " + str(workitem.buildnr) + " aborted")
                    testprocess.terminate()
                    server.terminate()
                    client.terminate()
                    return True

                self.testouts += outs
                self.testerrs += errs
                if not server.check_node_alive():
                    # See if ssh timed out and we need to restart
                    if "Timeout, server" in server.errs:
                        self.logger.info(server.name + " ssh session died, need to restart")
                        testprocess.terminate()
                        client.terminate()
                        return False

                    self.logger.info(server.name + " died while processing test job Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'])
                    self.error = True
                    self.Crashed = True
                    message = "Server crashed"
                    break
                if not client.check_node_alive():
                    # See if ssh timed out and we need to restart
                    if "Timeout, server" in client.errs:
                        self.logger.info(client.name + " ssh session died, need to restart")
                        testprocess.terminate()
                        server.terminate()
                        return False

                    self.logger.info(client.name + " died while processing test job Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'])
                    message += "Client crashed"
                    self.Crashed = True
                    self.error = True
                    break

                # See if any fatal errors happened that would allow us to
                # terminate job sooner as we know it's not healthy anymore
                for item in console_errors:
                    if item.get('error') and item.get('fatal'):
                        for node in [server, client]:
                            if node.match_console_string(item['error']):
                                self.logger.warning("Matched fatal error in logs: " + item['error'] + ' on node ' + node.name + " Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'])
                                self.error = True
                                message = 'Fatal Error "' + item['error'] + '" on ' + str(node.name)
                                corefile = node.dump_core("fatalerror")
                                counter = 0
                                while node.check_node_alive():
                                    if counter > 60:
                                        self.logger.warning("Timeout waiting for crashdump generation on fatalerror on " + str(node.name) + " Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'])
                                        break
                                    counter += 1
                                    time.sleep(5)
                                mycrashanalyzer.crasher_add_work(self.fsinfo, corefile, testinfo, clientdistro, self.clientarch, workitem, message, TIMEOUT=True, COND=self.out_cond, QUEUE=self.out_queue)
                        # Cannot break from the above loop
                        if self.error:
                            break
                if self.error:
                    break # the above break only breaks from the for loop

                # Also timeout both full test and single subtest
                if (time.time() > deadlinetime) or \
                   (time.time() - client.last_test_line_time > single_subtest_timeout):
                    self.logger.warning("Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'] + " Job timed out, terminating")
                    self.error = True
                    message = "Timeout"
                    self.TimeoutDetected = True
                    # Now lets dump qemu crashdumps of the server and client
                    clientcore = client.dump_core("timeout")
                    servercore = server.dump_core("timeout")
                    counter = 0
                    while client.check_node_alive() and server.check_node_alive():
                        if counter > 60:
                            self.logger.warning("Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'] + " Timeout waiting for crashdump generation on timeout")
                            break
                        counter += 1
                        time.sleep(5)
                    if clientcore:
                        mycrashanalyzer.crasher_add_work(self.fsinfo, clientcore, testinfo, clientdistro, self.clientarch, workitem, message, TIMEOUT=True, COND=self.out_cond, QUEUE=self.out_queue)
                    if servercore:
                        mycrashanalyzer.crasher_add_work(self.fsinfo, servercore, testinfo, serverdistro, self.serverarch, workitem, message, TIMEOUT=True, COND=self.out_cond, QUEUE=self.out_queue)
                    # XXX kick some additional analyzer for backtraces or such
                    break
            else:
                self.testouts += outs
                self.testerrs += errs
                # We finished normally, let's see if it was an invalid
                # run.
                # Add a config file if this list is to grow
                portmap_error = "port 988: port already in use"
                if server.match_console_string(portmap_error) or client.match_console_string(portmap_error):
                    self.logger.warning("Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'] + " Clash with portmap, restarting")
                    server.terminate()
                    client.terminate()
                    return False

                # We also have this "File exists" error out of nowhere at times,
                # seems to be some generic failure, so skip it.
                if ".ko: File exists" in self.testouts:
                    self.logger.warning("Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'] + " Cannot insert module file Exists, restarting")
                    server.terminate()
                    client.terminate()
                    return False

                # Match test suite output for signs of neessary restart.
                # Also see if we can print various warnings.
                if os.path.exists("suite_errors_lookup.json"):
                    suite_errors = []
                    try:
                        with open("suite_errors_lookup.json", "r") as errfile:
                            suite_errors = json.load(errfile)
                    except: # any error really
                        self.logger.error("Failure loading suite errors description?")
                    else:
                        matched_suite_errors = self.match_test_output(testscript, suite_errors)
                        for match in matched_suite_errors:
                            # self.get_duration() < 300 and ?
                            if match.get("fatal"):
                                self.logger.warning("Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'] + " matched suite error pattern " + match.get("name", "no name"))
                                server.terminate()
                                client.terminate()
                                return False

                # It's also possible either a client or server are dead or
                # are dying (crashdumping), need to check for it here
                #kdump_start_message = "Starting Kdump Vmcore Save Service"
                # Need to catch early booting message to be sure, so grab
                # part of the kdump kernel commandline instead
                kdump_start_message = "irqpoll nr_cpus=1 reset_devices"
                kdump_end_message = "kdump: saving vmcore complete"
                counter = 0
                if server.match_console_string(kdump_start_message):
                    self.logger.info(server.name + " kdump starting while processing test job Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'])
                    self.error = True
                    message = "Server crashed"
                    self.Crashed = True
                    # wait for kdump to finish
                    while server.is_alive():
                        if server.match_console_string(kdump_end_message):
                            self.logger.info(server.name + " kdump done Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'])
                            break
                        if counter > 60: # 5 minutes max for crashdump
                            self.logger.info(server.name + " kdump timeout Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'])
                            warnings += "(crashdump timeout)"
                            break
                        time.sleep(5)
                        counter += 1
                if client.match_console_string(kdump_start_message):
                    self.logger.info(client.name + " kdump starting while processing test job Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'])
                    self.error = True
                    message += "Client crashed"
                    self.Crashed = True
                    while client.is_alive():
                        if client.match_console_string(kdump_end_message):
                            self.logger.info(client.name + " kdump done Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'])
                            break
                        if counter > 60: # 5 minutes max for crashdump
                            self.logger.info(client.name + " kdump timeout Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'])
                            warnings += "(crashdump timeout)"
                            break
                        time.sleep(5)

        if workitem.Aborted:
            self.logger.warning("job for buildid " + str(workitem.buildnr) + " aborted")
            try:
                testprocess.terminate()
            except OSError:
                pass # No such process?
            server.terminate()
            client.terminate()
            # Don't bother collecting logs
            return True

        failedsubtests = ""
        skippedsubtests = ""
        if self.error:
            Failure = True
            try:
                testprocess.terminate()
            except OSError:
                pass # No such process?
            else:
                try:
                    outs, errs = testprocess.communicate()
                    self.testouts += outs
                    self.testerrs += errs
                except:
                    pass # did it die?

        else:
            # Don't go here if we had a panic, it's unimportant.
            yamlfile = testresultsdir + '/results.yml'
            Failure = False
            if os.path.exists(yamlfile):
                try:
                    with open(yamlfile, "r", encoding = "ISO-8859-1") as fl:
                        fldata = fl.read()
                        try:
                            testresults = yaml.safe_load(fldata)
                        except (ImportError, yaml.parser.ParserError,yaml.scanner.ScannerError):
                            # If yaml is invalid we need to sanitize it
                            testresults = yaml.safe_load(myyamlsanitizer.sanitize(fldata))
                except (OSError, ImportError, yaml.parser.ParserError, UnicodeDecodeError, yaml.scanner.ScannerError) as e:
                    warnings += "(yaml read error" + str(e) + ", check logs)"
                    self.logger.error("Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'] + " Exception when trying to read results.yml: " + str(e))
                else:
                    try:
                        for yamltest in testresults.get('Tests', []):
                            if yamltest.get('name', '') != testscript:
                                self.logger.warning("Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'] + " Skipping unexpected test results for " + yamltest.get('name', 'EMPTYNAME'))
                                continue

                            if yamltest.get('status', '') == "FAIL":
                                Failure = True
                                message = "Failure"
                            elif yamltest.get('status', '') == "SKIP":
                                message = "Skipped"

                            if not yamltest.get('SubTests', []):
                                continue # no subtests?

                            for subtest in yamltest.get('SubTests', []):
                                if not subtest.get('status'):
                                    if (testscript != "sanity-dom") or (subtest['name'] not in ("test_sanity", "test_sanityn")):
                                        subtest['status'] = "FAIL"
                                        if not subtest.get('error'):
                                            subtest['error'] = "No status. Crash?"
                                if subtest.get('status', '') == "FAIL":
                                    if workitem.change.get('updated_tests'):
                                        if subtest['name'] in workitem.change['updated_tests'].get(testscript, []):
                                            workitem.AddedTestFailure = True
                                            msg = "Test script %s subtest %s that was touched by this patch failed with '%s'. This is just a heads up on first fatal failure and a full report would be posted on test completion. See the results link above if you want intermediate results." % (testscript, subtest['name'], subtest.get('error', ""))
                                            workitem.post_immediate_review_comment(msg, {}, 0)
                                    failedsubtests += subtest['name'].replace('test_', '') + "("
                                    if subtest.get('error'):
                                        failedsubtests += subtest['error'].replace('\\', '')
                                    else:
                                        failedsubtests += "ret " + str(subtest['return_code'])
                                    failedsubtests += ") "
                                elif subtest.get('status', '') == "SKIP":
                                    skippedsubtests += subtest['name'].replace('test_', '') + "("
                                    skippedsubtests += str(subtest.get('error')) + ") "

                    except TypeError:
                        pass # Well, here's empty list for you I guess
                    # second pass for bug db, we probably might want to do it a single pass?
                    # Skip "Special" testsets
                    if not "-special" in testinfo.get('name', "nope"):
                        new, old = process_results(testresults, workitem, workitem.get_url_for_test(testinfo), testinfo['fstype'])
                        if new:
                            testinfo['NewFailures'] = new
                        if old:
                            testinfo['OldFailures'] = old

            if testprocess.returncode != 0:
                Failure = True
                message += " Test script terminated with error " + str(testprocess.returncode)
            elif not Failure and not message:
                message = "Success"

        duration = self.get_duration()
        message += "(" + str(duration) + "s)"

        # See if there was anything in error logs
        matched_server_errors = []
        matched_client_errors = []
        uniq_warns = []

        oldwarns = []
        for match in matched_suite_errors:
            if match.get("warn"):
                warnmsg = match.get("name", "no name")
                uniq = process_warning(testinfo['name'], warnmsg,
                                       workitem.change,
                                       workitem.get_url_for_test(testinfo),
                                       testinfo['fstype'])
                if uniq:
                    uniq_warns.append(warnmsg)
                else:
                    oldwarns.append(warnmsg)
        if oldwarns:
            warnings += "(Scripts: " + ",".join(oldwarns) + ")"

        for item in console_errors:
            if item.get('error') and item.get('message'):
                if server.match_console_string(item['error']):
                    matched_server_errors.append(item['message'])
                if client.match_console_string(item['error']):
                    matched_client_errors.append(item['message'])
        if matched_server_errors:
            oldwarns = []
            for warn in matched_server_errors:
                warnmsg = "Server: " + warn
                uniq = process_warning(testinfo['name'], warnmsg,
                                       workitem.change,
                                       workitem.get_url_for_test(testinfo),
                                       testinfo['fstype'])
                if uniq:
                    uniq_warns.append(warnmsg)
                else:
                    oldwarns.append(warn)
            if oldwarns:
                warnings += "(Server: " + ",".join(oldwarns) + ")"
        if matched_client_errors:
            oldwarns = []
            for warn in matched_client_errors:
                warnmsg = "Client: " + warn
                uniq = process_warning(testinfo['name'], warnmsg,
                                       workitem.change,
                                       workitem.get_url_for_test(testinfo),
                                       testinfo['fstype'])
                if uniq:
                    uniq_warns.append(warnmsg)
                else:
                    oldwarns.append(warn)
            if oldwarns:
                warnings += "(Client: " + ",".join(oldwarns) + ")"

        # Probably should make it a configurable item too?
        if ": double free or corruption " in self.testouts:
            warnmsg = "userspace memcorruption"
            uniq = process_warning(testinfo['name'], warnmsg,
                                   workitem.change,
                                   workitem.get_url_for_test(testinfo),
                                   testinfo['fstype'])
            if uniq:
                uniq_warns.append(warnmsg)
            else:
                warnings += "(%s)" % (warnmsg)
        elif "Backtrace: " in self.testouts:
            warnmsg = "userspace backtrace - please investigate"
            uniq = process_warning(testinfo['name'], warnmsg,
                                   workitem.change,
                                   workitem.get_url_for_test(testinfo),
                                   testinfo['fstype'])
            if uniq:
                uniq_warns.append(warnmsg)
            else:
                warnings += "(%s)" % (warnmsg)

        if uniq_warns:
            testinfo['NewWarnings'] = uniq_warns

        self.logger.info("Buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'] + " Job finished with code " + str(testprocess.returncode) + " and message " + message)
        # XXX Also need to add yaml parsing of results with subtests.

        del testprocess

        #pprint(self.testerrs)

        # Now kill the client and server
        server.terminate()
        #pprint(souts)
        #pprint(serrs)
        client.terminate()
        #pprint(couts)
        #pprint(cerrs)

        # See if we have any crashdumps
        crashname = self.collect_crashdump(server)
        if crashname:
            mycrashanalyzer.crasher_add_work(self.fsinfo, crashname, testinfo, serverdistro, self.serverarch, workitem, message, COND=self.out_cond, QUEUE=self.out_queue)

        crashname = self.collect_crashdump(client)
        if crashname:
            mycrashanalyzer.crasher_add_work(self.fsinfo, crashname, testinfo, clientdistro, self.clientarch, workitem, message, COND=self.out_cond, QUEUE=self.out_queue)

        if self.CrashDetected:
            # These would post stuff separately
            return True

        # We crashed, but did not find the crash file, huh?
        if self.Crashed:
            self.logger.warning("job for buildid " + str(workitem.buildnr) + " test " + testinfo['name'] + '-' + testinfo['fstype'] + " We had a crash " + message + "but no crashdumps?")
            self.logger.warning("client stderr: " + client.errs)
            self.logger.warning("server stderr: " + server.errs)

        del client
        del server

        workitem.UpdateTestStatus(testinfo, message, Finished=True, Crash=self.CrashDetected, TestStdOut=self.testouts, TestStdErr=self.testerrs, Failed=Failure, Subtests=failedsubtests, Skipped=skippedsubtests, Warnings=warnings)

        return True
