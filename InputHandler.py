import re
from select import POLLHUP, POLLIN

from Constants import UPD_FROMBLOB, UPD_FROMBLOBNEXT, CMD_REQUEST, CONT_AFTER_WRITE
from HyxTalker import HyxTalker
from ProcessManager import ProcessManager
from ProcessWrapper import ProcessWrapper, LaunchArguments
from logging2 import info, warning
from utilsFolder.HeapClass import Heap, MemorySegmentInitArgs
from utilsFolder.Helper import my_help
from utilsFolder.InputReader import InputReader, InputSockReader
from utilsFolder.PaulaPoll import PaulaPoll
from utilsFolder.PollableQueue import PollableQueue


class InputHandler:

    def __init__(self, launch_args: LaunchArguments, startupfile=None, inputsock=False):
        self.inputPoll = PaulaPoll()
        self.manager = ProcessManager(launch_args, self.inputPoll)

        self.stdinQ = PollableQueue()
        self.inputPoll.register(self.stdinQ.fileno(), "userinput")
        self.reader_thread = InputReader(self.stdinQ, startupfile)
        self.sock_reader = InputSockReader(self.stdinQ) if inputsock else None

        self.hyxTalker = None
        self._errmsg_suffix = ""

    def execute(self, cmd):
        try:
            return self._execute(cmd)
        except ValueError as err:
            return str(err)

    def _execute(self, cmd):
        manager = self.manager
        procWrap = manager.getCurrentProcess()
        proc = procWrap.ptraceProcess

        result = ""
        if cmd.startswith("hyx") and not self.hyxTalker:
            _, _, cmd = cmd.partition(" ")
            result = self.init_hyx(cmd)

        elif cmd.startswith("call"):
            result = manager.callFunction(cmd)

        elif cmd.startswith("c"):  # continue
            result = manager.cont()

        elif cmd.startswith("w "):  # write
            _, _, cmd = cmd.partition(" ")
            result = manager.write(cmd)
            if CONT_AFTER_WRITE:
                if result:
                    print(result)
                result = manager.cont()

        elif cmd.startswith("fork"):
            result = self.fork(cmd)

        elif cmd.startswith("sw"):  # switch
            result = self.switch(cmd)

        elif cmd.startswith("tree"):
            result = self.switch("switch ?")

        elif cmd.startswith("b"):
            result = manager.addBreakpoint(cmd)

        elif cmd.startswith("rb"):
            _,_, cmd = cmd.partition(" ")
            result = manager.getCurrentProcess().removeBreakpoint(cmd)

        elif cmd.startswith("malloc"):
            result = manager.callFunction("call " + cmd)

        elif cmd.startswith("free"):
            result = manager.callFunction("call " + cmd)

        elif cmd.startswith("fin"):
            result = manager.finish()

        elif cmd.startswith("list b"):
            return manager.getCurrentProcess().ptraceProcess.breakpoints

        elif cmd.startswith("s"):
            result = manager.cont(singlestep=True)

        elif cmd.startswith("fam"):
            result = manager.family()

        elif cmd.startswith("maps"):
            result = manager.dumpMaps()

        elif cmd.startswith("p"):
            result = manager.print(cmd)

        elif cmd.startswith("x"):
            result = manager.examine(cmd)

        elif cmd.startswith("trace"):
            result = manager.trace_syscall(cmd)

        elif cmd.startswith("getsegment") and False:
            _, _, cmd = cmd.partition(" ")
            result = manager.getCurrentProcess().get_own_segment()

        elif cmd.startswith("where"):
            result = manager.getCurrentProcess().where()

        elif cmd.startswith("name"):
            _, _, cmd = cmd.partition(" ")
            pid, _, name = cmd.partition(" ")

            # first give pid, then name. if you only give name,
            # first partition (currently pid) is actually name
            if name:
                pid = int(pid)
            else:
                name, pid = pid, 0
            result = manager.name_process(name, pid)

        elif cmd.startswith("?"):
            my_help(cmd)

        else:
            result = "use ? for a list of available commands"

        return result if result else ""

    def inputLoop(self):
        print("type ? for help")
        while True:
            skip_hyx_update = False
            poll_result = self.inputPoll.poll()
            assert len(poll_result) > 0

            if len(poll_result) == 1:
                name, fd, event = poll_result[0]
                if name == "hyx":
                    skip_hyx_update = self.handle_hyx(event)
                elif name == "userinput":
                    self.handle_stdin()
                elif "-out" in name:
                    self.handle_procout(name, fd, event)

                elif "-err" in name:
                    self.handle_stderr(event)

            else:  # this happens when two sockets are written to at the "same" time
                for name, fd, event in poll_result:
                    if "-out" in name:
                        self.handle_procout(name, fd, event)
                        break
                    elif "-err" in name:
                        self.handle_stderr(name)
                        break

                info(poll_result)

            if self.hyxTalker:
                try:
                    self.hyxTalker.updateHyx()
                except ValueError as e:
                    warning("encountered %s when updating hyx" % e)
                    self._switch_hyxtalker()

    def handle_stderr(self, event):
        stderr_prefix = "[ERR] %s"
        msg = stderr_prefix % self.manager.getCurrentProcess().read(0x1000, "err")
        print(msg)
        if self.hyxTalker:
            self.hyxTalker.sendMessage(msg + self._errmsg_suffix)
            self._errmsg_suffix = ""

    # this is called when a new line has been put to the stdinQ
    def handle_stdin(self):
        cmd = self.stdinQ.get()[:-1]  # remove newline
        assert isinstance(cmd, str)

        result = self.execute(cmd)
        if result:
            print(result)

    def handle_hyx(self, event):
        """Handles incoming updates/ command requests from hyx etc
            :return True if hyx shouldnt be  """
        hyxtalker = self.hyxTalker

        if event & POLLHUP:  # sock closed
            remaining_data = hyxtalker.hyxsock.recv(1000)
            if remaining_data:
                print(remaining_data)
            self.delete_hyx()
            return
        if event != POLLIN:
            raise NotImplementedError("unknown event: %s" % event)

        check = hyxtalker.hyxsock.recv(1)
        if check == CMD_REQUEST:
            cmd = hyxtalker.recvCommand()
            print("%s   (hyx)" % cmd)

            if cmd.strip().startswith("fork"):
                # if this would not be done, hyx would interpret the new heap (sent when forking) as the result from the command
                hyxtalker.sendCommandResponse("forking")
                result = self.execute(cmd)
                print(result)
            else:
                result = self.execute(cmd)
                print(result)
                hyxtalker.sendCommandResponse(result)

        elif check == UPD_FROMBLOB or check == UPD_FROMBLOBNEXT:
            hyxtalker.getUpdate(isNextByte=(check == UPD_FROMBLOBNEXT))

        else:
            warning(check, event)
            raise NotImplementedError

    def handle_procout(self, name, fd, event):
        procWrap = self.manager.getCurrentProcess()
        assert isinstance(procWrap, ProcessWrapper)
        read_bytes = procWrap.out_pipe.read(4096)
        if self.sock_reader:
            self.sock_reader.acc_sock.send(read_bytes)

        print("[OUT] %s" % read_bytes)
        if self.hyxTalker:
            self.hyxTalker.sendMessage("[OUT] %s" % read_bytes)


    def delete_hyx(self):
        self.hyxTalker.destroy(rootsock=True)
        self.hyxTalker = None

    def init_hyx(self, cmd: str):
        """open a segment with Hyx. You can specify the permissions of the segment, default is rwp.
       You can use slicing syntax, [1:-3] will open the segment starting with an offset of 0x1000, ending 0x3000 bytes before actual send of segment
       You can also trim the segment to start at the first page that has some non-zero bytes in it.

       Example use:
       hyx heap [f:]     omits the first fifteen pages
       hyx stack [i:i]   removes "boring" (zero-filled) pages from the start and end
       hyx libc rp"""
        currentProcess = self.manager.getCurrentProcess()
        args = INIT_HYX_ARGS.match(cmd)

        if not args:
            init_args = MemorySegmentInitArgs("heap", "rwp", 0, 0, False, False)
        else:
            segment = args.group(1)
            permissions = args.group(2)
            if permissions is None:
                permissions = "rwp"

            # if sliceoffsets are specified, convert the strings to int
            convert_func = lambda slice_str: int(slice_str, 16) * 0x1000 if slice_str else 0
            start, stop = map(convert_func, [args.group(4), args.group(6)])

            init_args = MemorySegmentInitArgs(segment, permissions, start, stop,
                                              start_nonzero=bool(args.group(5)),
                                              stop_nonzero=bool(args.group(7))
                                              )

        try:
            heap = Heap(currentProcess, init_args)
        except ValueError as e:
            return str(e)

        self.hyxTalker = HyxTalker(heap, self.inputPoll)

    def fork(self, cmd):
        manager = self.manager
        currProc = manager.getCurrentProcess()

        # make sure there is a new child after forking, switch to new child
        children_count = len(currProc.children)
        result = manager.fork(cmd)
        if len(currProc.children) > children_count:
            self._switch_hyxtalker()

        return result

    def switch(self, cmd):
        manager = self.manager
        _, _, cmd = cmd.partition(" ")
        result = manager.switchProcess(cmd)
        self._switch_hyxtalker()

        return result

    def _switch_hyxtalker(self):
        if not self.hyxTalker:
            return

        newProc = self.manager.getCurrentProcess()
        if newProc.heap:
            newHeap = newProc.heap
        else:
            args = self.hyxTalker.heap.args
            newHeap = Heap(newProc, args)

        self.hyxTalker.heap = newHeap
        self.hyxTalker.sendNewHeap(newHeap.start, newHeap.stop)

        msg = "switched to %d" % newProc.getPid()
        self.hyxTalker.sendMessage(msg)

        self._errmsg_suffix = "   " + msg   # next time stderr is printed, add this


INIT_HYX_ARGS = re.compile(
    r"([\w./-]+)"  # name of library
    r"(?:\s+([rwxps]+))?"  # permissions
    r"( ?\["  # slicing
    r"([0-9a-fA-F]*)"
    r"(i?)"  # i for start_nonzero
    r":"
    r"(-?[0-9a-fA-F]*)"
    r"(i?)"
    r"\])?"
)

if __name__ == "__main__":
    path_to_hack = "/home/jasper/university/barbeit/dummy/a.out"
    path_to_hack = "/home/jasper/university/barbeit/dummy/minimalloc"
    path_to_hack = "/home/jasper/university/bx/pwn/oldpwn/pwn18/vuln"
    path_to_hack = "demo/vuln"
    from ProcessWrapper import LaunchArguments

    import pwn

    pwn.context.log_level = "DEBUG"

    args = LaunchArguments([path_to_hack], False)

    i = InputHandler(args)
    i.inputLoop()
