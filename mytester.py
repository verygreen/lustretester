""" runtest.py - run VMs as needed to run a lustre test set
"""
import sys
import os
import fcntl
import time
import threading
import logging
import Queue
import shlex
import json
import shutil
import yaml
from pprint import pprint
from subprocess32 import Popen, PIPE, TimeoutExpired
import mycrashanalyzer

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

    def match_console_string(self, string):
        # Right now we assume the output cannot be changing as we are called
        # at the end. This migth change eventually I guess
        if not os.path.exists(self.consolelogfile):
            return False
        if os.stat(self.consolelogfile).st_size > len(self.consoleoutput):
            try:
                with open(self.consolelogfile, "r") as consolefile:
                    self.consoleoutput = consolefile.read()
            except OSError:
                return False
        return string in self.consoleoutput

    def is_alive(self):
        if self.process is not None:
            self.process.poll()
            if self.process.returncode is not None:
                return False
        return False

    def wait_for_login(self):
        """ Returns error as string or None if all is fine. No timeout handling """

        fd = self.process.stdout.fileno()
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        deadlinetime = time.time() + 180 # IF a node did not come up in 3 minutes, something is wrong with it anyway
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
            if "login:" in string:
                # Restore old blocking behavior
                fcntl.fcntl(fd, fcntl.F_SETFL, fl)
                return None
        # Hm, the loop ended somehow?
        self.process.terminate()
        return "Timed Out waiting for login prompt"

    def terminate(self):
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
        self.logger = self.setup_custom_logger("tester-%s.log" % (self.name))
        self.logger.info("Started daemon")
        # Would be great to verify all nodes are operational here, but
        # Alas, need kernels and initrds for that. Perhaps require some
        # default ones in json config?
        sleep_on_error = 15
        while True:
            in_cond.acquire()
            while in_queue.empty():
                in_cond.wait()

            job = in_queue.get()
            self.logger.info("Remaining Testing items in the queue left: " + str(in_queue.qsize()))
            self.Busy = True
            in_cond.release()
            priority = job[0] # Not really used here
            testinfo = job[1]
            workitem = job[2]
            self.logger.info("Got job buildid " + str(workitem.buildnr) + " test " + str(testinfo) )
            result = self.test_worker(testinfo, workitem)
            # same the test output if any
            if self.testresultsdir and self.testouts:
                try:
                    with open(self.testresultsdir + "/test.stdout", "w") as sout:
                        sout.write(self.testouts)
                except OSError:
                    pass # what can we do
            if result:
                sleep_on_error = 15 # Reset the backoff time after successful run
                self.collect_syslogs()
                self.update_permissions()
                self.logger.info("Finished job buildid " + str(workitem.buildnr) + " test " + testinfo['test'] + '-' + testinfo['fstype'] )
                # If we had a crash or timeout, a separate item was
                # started that would process it and return to queue.
                if (self.CrashDetected and self.crashfiles) or self.TimeoutDetected:
                    self.logger.info("crash detected or timeout, they will post their stuff separately")
                    pass
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
                in_queue.put([priority, testinfo, workitem])
                in_cond.notify()
                in_cond.release()
                self.logger.info("Going to sleep for " + str(sleep_on_error) + "seconds")
                time.sleep(sleep_on_error)
                self.logger.info("Woke after sleep")
                sleep_on_error *= 2

            self.Busy = False


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
        self.CrashDetected = False # This is for wen core was detected
        self.Crashed = False # This is when something died
        self.TimeoutDetected = False
        self.crashfiles = []
        self.testerrs = ''
        self.testouts = ''
        self.startTime = 0
        self.out_cond = out_cond
        self.out_queue = out_queue
        self.daemon = threading.Thread(target=self.run_daemon, args=(in_cond, in_queue, out_cond, out_queue))
        self.daemon.daemon = True
        self.daemon.start()

    def update_permissions(self):
        """ Update all files to be readable in the test dir """
        for filename in os.listdir(self.testresultsdir):
            path = self.testresultsdir + "/" + filename
            if not os.path.isdir(path):
                try:
                    os.chmod(path, 0644)
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
                os.chmod(self.testresultsdir + "/" + node + ".syslog.log", 0644)
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

        for crash in os.listdir(crashdirname):
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
                    if result.returncode is not 0:
                        self.logger.warning("Failed processing of core file " + filename + " to " + outputlocationpathprefix + "vmcore with " + outs + " and " + errs)
                    else:
                        try:
                            os.chmod(outputlocationpathprefix + "vmcore", 0644)
                        except OSError:
                            pass # What can we do?
                        self.CrashDetected = True
                        crashfilename = outputlocationpathprefix + "vmcore"

                vmcore_flat.close()

        # Now remove the crash data if we detected something
        if self.CrashDetected:
            shutil.rmtree(crashdirname)
        elif self.Crashed:
            try:
                shutil.copytree(crashdirname, outputlocationpathprefix + crashdirname + "-unprocessed")
            except:
                self.logger.warning("Cannot move for analysis, making local copy")
                try:
                    os.rename(crashdirname, crashdirname + "-unprocessed")
                except:
                    pass

        return crashfilename

    def get_duration(self):
        return int(time.time() - self.startTime)

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

    def test_worker(self, testinfo, workitem,
                    clientdistro="centos7", serverdistro="centos7"):
        """ Returns False if the test should be retried because had some unrelated problemi """
        self.init_new_run()
        artifactdir = workitem.artifactsdir
        outdir = workitem.testresultsdir

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
                pass

        timeout = testinfo.get("timeout", -1)
        if timeout == -1:
            timeout = 7*3600 # we are willing to wait up to 7 hours for unknown tests
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
            self.logger.error("Our build artifacts are missing?")
            workitem.UpdateTestStatus("Build artifacts missing", Failed=True)
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
            os.chmod(testresultsdir, 01755)
        except OSError:
            self.logger.error("Huh, cannot create test results dir: " + testresultsdir)
            return False

        # To know where to copy syslogs and core files
        self.testresultsdir = testresultsdir

        server = Node(self.servernetname, testresultsdir)
        client = Node(self.clientnetname, testresultsdir)

        workitem.UpdateTestStatus(testinfo, None, ResultsDir=testresultsdir)

        # SELinux check here since it needs a command line argument

        try:
            server.process = Popen([self.serverruncommand, server.name, serverkernel, serverinitrd, serverbuild, testresultsdir], close_fds=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, universal_newlines=True)
        except (OSError) as details:
            self.logger.warning("Failed to run server " + str(details))
            return False

        try:
            client.process = Popen([self.clientruncommand, client.name, clientkernel, clientinitrd, clientbuild, testresultsdir], close_fds=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, universal_newlines=True)
        except (OSError) as details:
            self.logger.warning("Failed to run client " + str(details))
            server.terminate()
            return False
        # Now we need to wait until both have booted and gave us login prompt
        if server.wait_for_login() is not None:
            client.terminate()
            #pprint(server.errs)
            self.logger.warning("Server did not show login prompt " + str(server.errs) + " " + str(server.outs) + " " + str([self.serverruncommand, server.name, serverkernel, serverinitrd, serverbuild, testresultsdir]))
            return False
        if client.wait_for_login() is not None:
            server.terminate()
            #pprint(client.errs)
            self.logger.warning("Client did not show login prompt" + str(client.errs) + " " + str(client.outs) + " " + str([self.clientruncommand, client.name, clientkernel, clientinitrd, clientbuild, testresultsdir]))
            return False

        if workitem.Aborted:
            server.terminate()
            client.terminate()
            return True

        # Now mount NFS in VM
        try:
            command = "ssh -o StrictHostKeyChecking=no root@" + self.clientnetname + \
                    " 'mkdir /tmp/testlogs ; mount 192.168.10.252:/" + \
                    testresultsdir + " /tmp/testlogs -t nfs'"
            args = shlex.split(command)
            setupprocess = Popen(args, close_fds=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, universal_newlines=True)
            outs, errs = setupprocess.communicate(timeout=100) # XXX timeout handling
            self.testouts += outs
            self.testerrs += errs
            if setupprocess.returncode is not 0:
                self.logger.warning("Failed to setup test environment: " + self.testerrs + " " + self.testouts)
                server.terminate()
                client.terminate()
                return False
        except OSError:
            self.logger.warning("Failed to run test setup " + str(details))
            server.terminate()
            client.terminate()
            return False
        except TimeoutExpired:
            self.logger.warning("Timed out mounting nfs")
            setupprocess.terminate()
            server.terminate()
            client.terminate()
            return False

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
            # XXX - this stuff should be in some config
            args = ["ssh", "-tt", "-o", "StrictHostKeyChecking=no", "root@" + self.clientnetname,
                    'PDSH="pdsh -S -Rssh -w" mds_HOST=' + self.servernetname +
                    " ost_HOST=" + self.servernetname + " MDSDEV1=/dev/vdc " +
                    "OSTDEV1=/dev/vde OSTDEV2=/dev/vdf LOAD_MODULES_REMOTE=true " +
                    "FSTYPE=" + fstype + DNEStr + SSKSTR + SELINUXSTR +
                    "MDSSIZE=0 OSTSIZE=0 " +
                    "MGSSIZE=0 " + ENVPARAMS + " "
                    "NAME=ncli /home/green/git/lustre-release/lustre/tests/auster -D /tmp/testlogs/ -r -k " + testscript + " " + TESTPARAMS ]
            testprocess = Popen(args, close_fds=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, universal_newlines=True)
        except (OSError) as details:
            self.logger.warning("Failed to run test " + str(details))
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
        while testprocess.returncode is None: # XXX add a timer
            try:
                # This is a very ugly workaround to the fact that when you call
                # communicate with timeout, it polls(!!!) the FDs of the subprocess
                # at an insane rate resulting in huge cpu hog. So only let it
                # poll once per call and we do our sleeping ourselves.
                # XXX - perhaps consider doing some sort of a manual select call?
                time.sleep(5) # every 5 seconds, not ideal because that becomes our latency
                outs, errs = testprocess.communicate(timeout=0.01) # cannot have 0 somehow
            except (TimeoutExpired, ValueError):
                if workitem.Aborted:
                    self.logger.warning("job for buildid " + str(workitem.buildnr) + " aborted")
                    testprocess.terminate()
                    server.terminate()
                    client.terminate()
                    return True

                self.testouts += outs
                self.testerrs += errs
                if not server.check_node_alive():
                    self.logger.info(server.name + " died while processing test job")
                    self.error = True
                    self.Crashed = True
                    message = "Server crashed"
                    break
                if not client.check_node_alive():
                    self.logger.info(client.name + " died while processing test job")
                    message = "Client crashed"
                    self.Crashed = True
                    self.error = True
                    break

                # See if any fatal errors happened that would allow us to
                # terminate job sooner as we know it's not healthy anymore
                for item in console_errors:
                    if item.get('error') and item.get('fatal'):
                        for node in [server, client]:
                            if node.match_console_string(item['error']):
                                self.logger.warning("Matched fatal error in logs: " + item['error'] + ' on node ' + node.name)
                                self.error = True
                                message = "Fatal Error " + item['error'] + " on " + str(node.name)
                                corefile = node.dump_core("fatalerror")
                                counter = 0
                                while node.check_node_alive():
                                    if counter > 60:
                                        self.logger.warning("Timeout waiting for crashdump generation on fatalerror on " + str(node.name))
                                        break
                                    counter += 1
                                    time.sleep(5)
                                # XXX copy arch and distro from testinfo/nodestruct
                                mycrashanalyzer.Crasher(self.fsinfo, corefile, testinfo, clientdistro, self.clientarch, workitem, message, TIMEOUT=True, COND=self.out_cond, QUEUE=self.out_queue)
                        # Cannot break from the above loop
                        if self.error:
                            break
                if self.error:
                    break # the above break only breaks from the for loop

                # Also timeout
                if time.time() > deadlinetime:
                    self.logger.warning("Job timed out, terminating")
                    self.error = True
                    message = "Timeout"
                    self.TimeoutDetected = True
                    # Now lets dump qemu crashdumps of the server and client
                    clientcore = client.dump_core("timeout")
                    servercore = server.dump_core("timeout")
                    counter = 0
                    while client.check_node_alive() and server.check_node_alive():
                        if counter > 60:
                            self.logger.warning("Timeout waiting for crashdump generation on timeout")
                            break
                        counter += 1
                        time.sleep(5)
                    if clientcore:
                        mycrashanalyzer.Crasher(self.fsinfo, clientcore, testinfo, clientdistro, self.clientarch, workitem, message, TIMEOUT=True, COND=self.out_cond, QUEUE=self.out_queue)
                    if servercore:
                        mycrashanalyzer.Crasher(self.fsinfo, servercore, testinfo, serverdistro, self.serverarch, workitem, message, TIMEOUT=True, COND=self.out_cond, QUEUE=self.out_queue)
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
                    self.logger.warning("Clash with portmap, restarting")
                    server.terminate()
                    client.terminate()
                    return False

                # We also have this "File exists" error out of nowhere at times,
                # seems to be some generic failure, so skip it.
                if ".ko: File exists" in self.testouts:
                    self.logger.warning("Cannot insert module Fle Exists, restarting")
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
                    self.logger.info(server.name + " kdump starting while processing test job")
                    self.error = True
                    message = "Server crashed"
                    self.Crashed = True
                    # wait for kdump to finish
                    while server.is_alive():
                        if server.match_console_string(kdump_end_message):
                            self.logger.info(server.name + " kdump done")
                            break
                        if counter > 60: # 5 minutes max for crashdump
                            self.logger.info(server.name + " kdump timeout")
                            warnings += "(crashdump timeout)"
                            break
                        time.sleep(5)
                        counter += 1
                elif client.match_console_string(kdump_start_message):
                    self.logger.info(client.name + " kdump starting while processing test job")
                    self.error = True
                    message = "Client crashed"
                    self.Crashed = True
                    while client.is_alive():
                        if client.match_console_string(kdump_end_message):
                            self.logger.info(client.name + " kdump done")
                            break
                        if counter > 60: # 5 minutes max for crashdump
                            self.logger.info(client.name + " kdump timeout")
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
                    with open(yamlfile, "r") as fl:
                        fldata = fl.read()
                        testresults = yaml.load(fldata.replace('\\', ''))
                except (OSError, ImportError) as e:
                    self.logger.error("Exception when trying to read results.yml: " + str(e))
                else:
                    for yamltest in testresults.get('Tests', []):
                        if yamltest.get('name', '') != testscript:
                            self.logger.warning("Skipping unexpected test results for " + yamltest.get('name', 'EMPTYNAME'))
                            continue
                        try:
                            for subtest in yamltest.get('SubTests', []):
                                if subtest.get('status', '') == "FAIL":
                                    failedsubtests += subtest['name'].replace('test_', '') + "("
                                    if subtest.get('error'):
                                        failedsubtests += subtest['error']
                                    else:
                                        failedsubtests += "ret " + str(subtest['return_code'])
                                    failedsubtests += ") "
                                elif subtest.get('status', '') == "SKIP":
                                    skippedsubtests += subtest['name'].replace('test_', '') + "("
                                    skippedsubtests += str(subtest.get('error')) + ") "
                        except TypeError:
                            pass # Well, here's empty list for you I guess

                        if yamltest.get('status', '') == "FAIL":
                            Failure = True
                            message = "Failure"
                        elif yamltest.get('status', '') == "SKIP":
                            message = "Skipped"

            if testprocess.returncode is not 0:
                Failure = True
                message += " Test script terminated with error " + str(testprocess.returncode)
            elif not Failure and not message:
                message = "Success"

        duration = int(time.time() - self.startTime)
        message += "(" + str(duration) + "s)"

        matched_server_errors = []
        matched_client_errors = []
        for item in console_errors:
            if item.get('error') and item.get('message'):
                if server.match_console_string(item['error']):
                    matched_server_errors.append(item['message'])
                if client.match_console_string(item['error']):
                    matched_client_errors.append(item['message'])
        if matched_server_errors:
            warnings += "(Server: " + ",".join(matched_server_errors) + ")"
        if matched_client_errors:
            warnings += "(Client: " + ",".join(matched_client_errors) + ")"

        # Probably should make it a configurable item too?
        if ": double free or corruption " in self.testouts:
            warnings += "(userspace memcorruption)"
        elif "Backtrace: " in self.testouts:
            warnings += "(userspace backtrace - please investigate)"

        self.logger.info("Job finished with code " + str(testprocess.returncode) + " and message " + message)
        # XXX Also need to add yaml parsing of results with subtests.

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
            mycrashanalyzer.Crasher(self.fsinfo, crashname, testinfo, serverdistro, self.serverarch, workitem, message, COND=self.out_cond, QUEUE=self.out_queue)

        crashname = self.collect_crashdump(client)
        if crashname:
            mycrashanalyzer.Crasher(self.fsinfo, crashname, testinfo, clientdistro, self.clientarch, workitem, message, COND=self.out_cond, QUEUE=self.out_queue)

        if self.CrashDetected:
            # These would post stuff separately
            return True

        # We crashed, but did not find the crash file, huh?
        if self.Crashed:
            self.logger.warning("job for buildid " + str(workitem.buildnr) + " We had a crash " + message + "but no crashdumps?")

        workitem.UpdateTestStatus(testinfo, message, Finished=True, Crash=self.CrashDetected, TestStdOut=self.testouts, TestStdErr=self.testerrs, Failed=Failure, Subtests=failedsubtests, Skipped=skippedsubtests, Warnings=warnings)

        return True
