import time
from PaulaPipe import Pipe

from ptrace.debugger.process import PtraceProcess
from ptrace.debugger.process_event import ProcessExecution
import pwn
from subprocess import Popen
from utils import path_launcher

from HeapClass import Heap

from ptrace.func_call import FunctionCallOptions

class ProcessWrapper:
    """Provides an easy way to redirect stdout and stderr using pipes. Write to the processes STDIN and read from STDOUT at any time! """

    def __init__(self, args=None, debugger=None, redirect=False, parent=None, ptraceprocess=None):

        self.syscall_options = FunctionCallOptions(
            write_types=True,
            write_argname=True,
            write_address=True,
        )

        if args:
            assert debugger is not None
            assert not parent
            # create three pseudo terminals
            self.in_pipe = Pipe()
            self.out_pipe = Pipe()
            self.err_pipe = Pipe()

            self.stdin_buf = b""
            self.stdout_buf = b""
            self.stderr_buf = b""

            # if we want to redirect, tell the subprocess to write to our pipe, else it will print to normal stdout
            if redirect:
                stdout_arg = self.out_pipe.writeobj
                stderr_arg = self.err_pipe.writeobj
            else:
                stdout_arg = None
                stderr_arg = None

            self.popen_obj = Popen(args, stdin=self.in_pipe.readobj, stdout=stdout_arg, stderr=stderr_arg)

            self.debugger = debugger
            self.ptraceProcess = self.setupPtraceProcess()  # launches actual program, halts immediately

            self.heap = None  # Heap(self.ptraceProcess.pid)

        # this is used when a process is forked by user
        else:
            assert isinstance(parent, ProcessWrapper) and isinstance(ptraceprocess, PtraceProcess)
            self.in_pipe = parent.in_pipe  # TODO
            self.out_pipe = parent.out_pipe
            self.err_pipe = parent.err_pipe

            self.stdin_buf = parent.stdin_buf
            self.stdout_buf = parent.stdout_buf
            self.stderr_buf = parent.stderr_buf

            self.debugger = parent.debugger
            self.ptraceProcess = ptraceprocess

            try:
                self.heap = Heap(self.ptraceProcess.pid)
            except KeyError:
                self.heap = None

    def setupPtraceProcess(self):
        from ptrace.debugger.debugger import PtraceDebugger

        assert isinstance(self.debugger, PtraceDebugger)
        ptrace_proc = self.debugger.addProcess(self.popen_obj.pid, is_attached=False, seize=True)
        ptrace_proc.interrupt()  # seize does not automatically interrupt the process
        ptrace_proc.setoptions(self.debugger.options)

        launcher_ELF = pwn.ELF(path_launcher)  # get ready to launch
        ad = launcher_ELF.symbols["go"]
        ptrace_proc.writeBytes(ad, b"gogo")

        ptrace_proc.cont()
        assert isinstance(ptrace_proc.waitEvent(), ProcessExecution)  # execve syscall is hit

        ptrace_proc.syscall()
        ptrace_proc.waitSyscall()
        result = ptrace_proc.getreg("orig_rax")

        print("initial execve returned %d" % result)

        return ptrace_proc

    def addBreakpoints(self, *bp_list):
        def addSingleBP(breakpoint):
            self.ptraceProcess.crepaulaateBreakpoint(breakpoint)

        for bp in bp_list:
            addSingleBP(bp)

    def writeToBuf(self, text):
        """write to the processes stdin buffer, awaiting read syscall"""
        self.stdin_buf += text

    def writeBufToPipe(self, n: int):
        """write N bytes to the stdin pipe"""
        towrite = self.stdin_buf[0:n]
        self.stdin_buf = self.stdin_buf[n:]
        return self.in_pipe.write(towrite)

    def read(self, n, channel="out"):
        if channel == "out":
            ret = self.out_pipe.read(n)
            self.stdout_buf += ret
            return ret
        elif channel == "err":
            ret = self.err_pipe.read(n)
            self.stderr_buf += ret
            return ret
        else:
            raise KeyError

    def getfileno(self, which):
        if which == "err":
            return self.err_pipe.fileno("read")
        elif which == "out":
            return self.out_pipe.fileno("read")
        elif which == "in":
            return self.in_pipe.fileno("write")
        else:
            raise KeyError("specify which")

    def getPid(self):
        return self.ptraceProcess.pid

    def setHeap(self):
        if self.heap is None:
            self.heap = Heap(self.getPid())

    def forkProcess(self):
        from copy import copy, deepcopy
        def copySyscallstate(child: PtraceProcess, parent: PtraceProcess):
            from ptrace.debugger.syscall_state import SyscallState
            state = SyscallState(child)
            state.next_event = parent.syscall_state.next_event
            state.syscall = copy(parent.syscall_state.syscall)
            state.syscall.process = child

            child.syscall_state = state

        process = self.ptraceProcess

        ip = process.getInstrPointer()
        regs = process.getregs()
        print("orig_rax = %d" % process.getreg("orig_rax"))
        print("rax = %d" % process.getreg("rax"))
        print(" ip = %#x" % ip)


        if False:
            state = process.syscall_state
            syscall = state.event(self.syscall_options)
            print(syscall.format())


        at_syscall_entry = process.syscall_state.next_event == "exit"  # if process is about to syscall, dont inject
        if at_syscall_entry:  # user wants to fork just before a syscall
            print("user forks just before syscall")
            process.setreg("orig_rax", 57)
        else:
            print("normal fork")
            original = process.readBytes(ip, len(inject))
            process.setreg("rax",57)
            process.writeBytes(ip, inject)

        process.singleStep()  # continue till fork happended
        event = process.waitEvent()
        print("got event_stop", event, "pid=", process.pid)
        from ptrace.debugger.process_event import NewProcessEvent
        assert isinstance(event, NewProcessEvent)

        print("ip after getting newprocessevent= %#x" % process.getInstrPointer())
        if not at_syscall_entry:
            pass

        process.syscall()  # exit syscall
        process.waitSyscall()
        if at_syscall_entry:
            process.syscall()
            process.waitSyscall()

        print("ip after finishing syscall= %#x" % process.getInstrPointer())

        #print(process.getreg("orig_rax"))
        #child_pid = process.getreg("rax")  # result of fork

        #if child_pid > (1 << 63) or child_pid <= process.pid:
        #    raise ValueError("fork returned something unexpected. childpid=%d" % child_pid)

        # restore state in parent and child process
        child = process.debugger.list[-1]
        assert child.getreg("rax") == 0  # successfull fork

        #process.syscall()
        #process.waitSyscall()

        process.setregs(regs)
        child.setregs(regs)



        if at_syscall_entry:
            assert process.syscall_state.next_event== "exit"
        else:
            process.writeBytes(ip, original)
            child.writeBytes(ip, original)

        #copySyscallstate(child, process)

        return ProcessWrapper(parent=self, ptraceprocess=child)


codeWriteEax = """
syscall
"""

inject = pwn.asm(codeWriteEax, arch="amd64")
