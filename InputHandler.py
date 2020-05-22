from utilsFolder.PaulaPoll import PaulaPoll
from ProcessManager import ProcessManager
from utilsFolder.PollableQueue import PollableQueue
from signal import SIGWINCH
import re

from threading import Thread
from utilsFolder.InputReader import InputReader
from HeapClass import Heap, MemorySegmentInitArgs

from ProcessWrapper import ProcessWrapper, LaunchArguments
from HyxTalker import HyxTalker
from utilsFolder.Parsing import parseInteger
from Helper import my_help


class InputHandler:

    def __init__(self, launch_args:LaunchArguments, startupfile=None):
        self.inputPoll = PaulaPoll()
        self.manager = ProcessManager(launch_args, self.inputPoll)

        self.stdinQ = PollableQueue()
        self.inputPoll.register(self.stdinQ.fileno(), "userinput")
        self.reader_thread = InputReader(self.stdinQ, startupfile)

        self.hyxTalker = None

    def execute(self, cmd):
        try:
            return self._execute(cmd)
        except ValueError as err:
            return str(err)

    def _execute(self, cmd):
        manager = self.manager
        procWrap = manager.getCurrentProcess()
        proc= procWrap.ptraceProcess

        result = ""
        if cmd.startswith("hyx") and not self.hyxTalker:
            _, _, cmd = cmd.partition(" ")
            result=self.init_hyx(cmd)

        elif cmd.startswith("c"):  # continue
            result = manager.cont()

        elif cmd.startswith("w"):
            result = manager.write(cmd[2:].encode() + b"\n")  # TODO

        elif cmd.startswith("fork"):
            result = self.fork()

        elif cmd.startswith("proclist"):
            print(manager.processList)

        elif cmd.startswith("sw"):  # switch
            _, _, cmd = cmd.partition(" ")
            result = manager.switchProcess(cmd)

        elif cmd.startswith("b"):

            result = manager.addBreakpoint(cmd)

        elif cmd.startswith("malloc"):
            _, _, val = cmd.partition(" ")
            val = parseInteger(val, procWrap)
            result = manager.malloc(val)

        elif cmd.startswith("free"):
            _, _, pointer = cmd.partition(" ")
            pointer = parseInteger(pointer, procWrap)
            result = manager.free(pointer)

        elif cmd.startswith("fin"):
            result = manager.finish()

        elif cmd.startswith("try"):
            result = manager.tryFunction(cmd.split(" ")[1], cmd.split(" ")[2:])

        elif cmd.startswith("list b"):
            print(manager.getCurrentProcess().ptraceProcess.breakpoints)

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

        elif cmd.startswith("getsegment"):
            _,_, cmd= cmd.partition(" ")

            result = manager.getCurrentProcess().get_own_segment()

        elif cmd.startswith("?"):
            my_help(cmd)

        else:
            result= "use ? for a list of available commands"


        return result if result else ""

    def inputLoop(self):

        print("type ? for help")
        while True:
            pollresult = self.inputPoll.poll()
            assert len(pollresult) > 0

            if len(pollresult) == 1:
                name, pollfd, event = pollresult[0]
                if name == "hyx":
                    self.handle_hyx(event)
                elif name == "userinput":
                    self.handle_stdin(pollfd, event)
                elif "-out" in name:
                    self.handle_procout(name, pollfd, event)

                elif "-err" in name:
                    self.handle_stderr(event)

            else:  # this happens when two sockets are written to at the "same" time
                for name, pollfd, event in pollresult:
                    if "-out" in name:
                        self.handle_procout(name, pollfd, event)
                        break
                    elif "-err" in name:
                        self.handle_stderr(name,pollfd, event)

                print(pollresult)
                # raise NotImplementedError

            if self.hyxTalker:
                self.hyxTalker.updateHyx()

    def handle_stderr(self, event):
        print("got this on stderr")
        print(self.manager.getCurrentProcess().read(0x1000, "err"))

    # this is called when a new line has been put to the stdinQ
    def handle_stdin(self, fd, event):
        cmd = self.stdinQ.get()[:-1]  # remove newline
        assert isinstance(cmd, str)

        if event == SIGWINCH:
            return

        result=self.execute(cmd)
        if result:
            print(result)

    def handle_hyx(self, event):
        hyxtalker = self.hyxTalker

        from select import POLLHUP, POLLIN
        from Constants import UPD_FROMBLOB, UPD_FROMBLOBNEXT, CMD_REQUEST

        if event & POLLHUP:
            print("hyx closed, remaining data = %s" % hyxtalker.hyxsock.recv(1000))
            self.delete_hyx()
            return
        if event != POLLIN:
            print(event)
            raise NotImplementedError

        check = hyxtalker.hyxsock.recv(1)
        if check == CMD_REQUEST:
            cmd = hyxtalker.recvCommand()
            print("%s   (hyx) " % cmd)
            result = self.execute(cmd)
            print(result)
            hyxtalker.sendCommandResponse(result)

        elif check == UPD_FROMBLOB or check == UPD_FROMBLOBNEXT:
            hyxtalker.getUpdate(isNextByte=(check == UPD_FROMBLOBNEXT))

        else:
            print(check, event)
            raise NotImplementedError

    def handle_procout(self, name, fd, event):
        procWrap = self.manager.getCurrentProcess()
        assert isinstance(procWrap, ProcessWrapper)
        print("proc %s wrote: " % name, procWrap.out_pipe.read(4096))

    def delete_hyx(self):
        self.hyxTalker.destroy(rootsock=True)
        self.hyxTalker = None

    def init_hyx(self, cmd="heap rw"):
        """open a segment with Hyx. You can specify the permissions of the segment, default is rwp.
       You can use slicing syntax, [1:-3] will open the segment starting with an offset of 0x1000
       You can also trim the segment to start at the first page that has some non-zero bytes in it.

       Example use:
       hyx heap [2:]
       hyx stack [i:i]
       hyx libc rp"""
        currentProcess = self.manager.getCurrentProcess()
        args = INIT_HYX_ARGS.match(cmd)

        if not args:
            return "could not match this \nexample use: hyx libc rwx [a1:] this will load the libc segment with rwx " \
                   "permissions starting at offset 0xA1000 "

        segment = args.group(1)
        permissions = args.group(2)

        # if sliceoffsets are specified, convert the strings to int
        convert_func = lambda slice_str: int(slice_str, 16)*0x1000 if slice_str else 0
        start, stop = map(convert_func, [args.group(4), args.group(6)])

        if not segment:
            segment = "heap"

        if not permissions:
            permissions = "rwp"

        init_args = MemorySegmentInitArgs(segment, permissions, start, stop,
                                          start_nonzero=bool(args.group(5)),
                                          stop_nonzero=bool(args.group(7))
                                          )

        try:
            heap = Heap(currentProcess, init_args)
        except ValueError as e:
            return str(e)

        print(heap.file_path)

        self.hyxTalker = HyxTalker(self.manager.socketname, heap, self.inputPoll)

    def fork(self):
        manager = self.manager
        currProc = manager.getCurrentProcess()
        children_count = len(currProc.children)
        result = manager.fork()
        if len(currProc.children) <= children_count:
            return result

        new_child = currProc.children[-1]
        self.hyxTalker.heap = Heap(new_child)

        return result


INIT_HYX_ARGS = re.compile(
    r"([\w./-]+)?"  # name of library
    r" ([rwxps]+)?"  # permissions
    r"( ?\["  # slicing
    r"([0-9a-fA-F]*)"   
    r"(i?)" # i for start_nonzero
    r":"
    r"(-?[0-9a-fA-F]*)"
    r"(i?)"
    r"\])?"
)

if __name__ == "__main__":
    path_to_hack = "/home/jasper/university/barbeit/dummy/a.out"
    path_to_hack = "/home/jasper/university/barbeit/dummy/minimalloc"
    from ProcessWrapper import LaunchArguments
    args=LaunchArguments(path_to_hack, False)

    i = InputHandler(args)
    i.inputLoop()
