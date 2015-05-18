import os
import pwd
import errno
import asyncio
import shlex
from functools import partial

from time import time, sleep

from cutil.logging import warn, info, set_log_level
from cproc.watcher import InitChildWatcher
from cutil.syslog import SyslogServer

import signal

class SubProcess(object):

    pid = None
    name = None

    _user_uid = None
    _user_gid = None
    _user_env = None
    _proc = None

    @classmethod
    @asyncio.coroutine
    def spawn(cls, args=None, user=None, service=None, wait=False):
        print("spawn: {0}".format((args, user, service)))
        sp = cls(args, user)
        if service:
            sp.configure(service)
        yield from sp.run()
        if wait:
            yield from sp.wait()

    def __init__(self, args=None, user=None):
        super(SubProcess, self).__init__()
        self._prog_args = args
        if user:
            self._setup_user(user)

    def configure(self, service):
        args = None
        self.name = service.name
        if service.command:
            assert not (service.command and (service.bin or service.args)), "bin/args and command config are mutually-exclusive"
            args = shlex.split(service.command)
        elif service.bin:
            args = [service.bin] + shlex.split(service.args or '')
        else:
            raise Exception("No command or arguments provided for service")
        self._prog_args = args

    def _setup_subprocess(self):
        if self._user_uid:
            os.setgid(self._user_gid)
            os.setuid(self._user_uid)
            os.environ.update(self._user_env)

    def _setup_user(self, user):
        """
        Execute set-up for the new process.
        """
        pwrec = pwd.getpwnam(user)
        self._user_uid = pwrec.pw_uid
        self._user_gid = pwrec.pw_gid
        self._user_env = {
            'HOME':       pwrec.pw_dir,
            'LOGNAME':    user,
            'USER':       user,
        }
            
    @asyncio.coroutine
    def run(self):
        args = self._prog_args
        assert args, "No arguments provided to SubProcess.run()"
        info("Running %s... " % " ".join(args))
        create = asyncio.create_subprocess_exec(*self._prog_args, preexec_fn=self._setup_subprocess)
        proc = self._proc = yield from create
        print("CREATED PROCESS", proc)

    @asyncio.coroutine
    def wait(self):
        proc = self._proc
        if not proc:
            raise Exception("Process not started, can't wait")
        yield from proc.wait()
        info("Process status for pid={0} is '{1}'".format(proc.pid, proc.returncode))

        

class TopLevelProcess(object):
             
    _cls_singleton = None

    exit_when_no_processes = True
    kill_all_timeout = 5
    send_sighup = False

    _ignore_signals = False
    _all_killed = False
    _killing_system = False
    _enable_exit = False

    def __init__(self):
        policy = asyncio.get_event_loop_policy()
        w = self._watcher = InitChildWatcher()
        policy.set_child_watcher(w)
        w.add_no_processes_handler(self._no_processes)
        self.loop.add_signal_handler(signal.SIGTERM, self.kill_system)

    @classmethod
    def sharedInstance(cls):
        "Return a singleton object for this class."
        if not cls._cls_singleton:
            cls._cls_singleton = TopLevelProcess()
        return cls._cls_singleton

    @property
    def debug(self):
        return asyncio.get_event_loop().get_debug()
    @debug.setter
    def debug(self, val):
        asyncio.get_event_loop().set_debug(val)

    @property
    def loop(self):
        return asyncio.get_event_loop()

    def _no_processes(self):
        print("NO PROCESSES!", self)
        self._all_killed = True
        if self._enable_exit and self.exit_when_no_processes:
            print("FORCING EXIT")
            self._enable_exit = False
            self.loop.call_later(0.5, self.loop.stop)

    def kill_system(self):
        if self._killing_system:
            return

        info("KILLING SYSTEM")
        self._killing_system = True

        try:
            os.kill(-1, signal.SIGTERM) # first try a sig term
            if self.send_sighup:
                os.kill(-1, signal.SIGHUP)
        except ProcessLookupError:
            info("No processes remain when attempting to kill system, just stop.")
            self._no_processes()
            return

        self.loop.call_later(self.kill_all_timeout, self._check_kill_all)

    def _check_kill_all(self):
        if self._all_killed:
            return

        info("Some processes remain after {0}secs.  Forcing kill".format(self.kill_all_timeout))

        try:
            os.kill(-1, signal.SIGKILL)
        except ProcessLookupError:
            info("No processes when attempting to force quit")
            self._no_processes()
            return

    def activate_result(self, future):
        print("DISPATCH RESULT", future)

    def activate(self, cr):
       future = asyncio.async(cr)
       future.add_done_callback(self.activate_result)
       return future

    def run(self, args, user=None, wait=False):
        return SubProcess.spawn(args, user, wait=wait)

    def run_event_loop(self):
        "Sets up the event loop and runs it."

        self._syslog = SyslogServer().run()
        self.activate(self._syslog)

        self.loop.run_forever()
        self.loop.close()

    @asyncio.coroutine
    def run_services(self, config):
        "Run services from the speicified config (an instance of cutil.config.Configuration)"
        slist = config.get_services().get_startup_list()
        info("RUN SERVICES: {0}".format(slist))
        for s in slist:
            if not s.enabled:
                continue
            info("RUNNING SERVICE: " + str(s))
            try:
                yield from SubProcess.spawn(service=s)
            except Exception as ex:
                print("PROCESS COULD NOT BE STARTED", str(ex), type(ex))
                if isinstance(ex, FileNotFoundError) and s.optional:
                    print("OPTIONAL SERVICE BINARY MISSING, WE IGNORE IT")
                elif s.ignore_failures:
                    print("IGNORING FAILURE AND CONTINUING")
                else:
                    raise

        self._enable_exit = True
