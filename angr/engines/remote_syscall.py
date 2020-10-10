import logging
from typing import TYPE_CHECKING

l = logging.getLogger(name=__name__)

import angr
import claripy

from ..state_plugins.inspect import BP_BEFORE, BP_AFTER
from .engine import SuccessorsMixin

if TYPE_CHECKING:
    from angr import SimState
    from angr.simos import SimUserland
    from angr.procedures.definitions import SimSyscallLibrary


BASE_SYSCALL_BYPASS_LIST = {
    'mmap',
    'munmap',
    'brk',
    # 'open',
    'write',
    'read',
    # 'close',
    'exit',
    'exit_group',
}


SYSCALL_BYPASS_LISTS = {
    'amd64': BASE_SYSCALL_BYPASS_LIST,
    'i386': BASE_SYSCALL_BYPASS_LIST,
    'mips-n32': BASE_SYSCALL_BYPASS_LIST | {
        'set_thread_area',
    },
    'mips-o32': BASE_SYSCALL_BYPASS_LIST | {
        'set_thread_area',
    },
    'mips-n64': BASE_SYSCALL_BYPASS_LIST | {
        'set_thread_area',
    }
}


#pylint:disable=abstract-method,arguments-differ
class SimEngineRemoteSyscall(SuccessorsMixin):
    """
    This mixin dispatches certain syscalls to a syscall agent that runs on another host (a local machine, a chroot jail,
    a Linux VM, a Windows VM, etc.).
    """
    def process_successors(self, successors, **kwargs):
        state: 'angr.SimState' = self.state
        if (not state.history or
                not state.history.parent or
                not state.history.parent.jumpkind or
                not state.history.parent.jumpkind.startswith('Ijk_Sys')):
            return super().process_successors(successors, **kwargs)

        l.debug("Invoking remote system call handler")
        syscall_cc = self.project.simos.syscall_cc(state)
        syscall_num = syscall_cc.syscall_num(state)

        # convert the syscall number to a concrete integer
        try:
            num = state.solver.eval_one(syscall_num)
        except angr.SimSolverError:
            if angr.sim_options.BYPASS_UNSUPPORTED_SYSCALL not in state.options:
                raise AngrUnsupportedSyscallError("Trying to perform a syscall on an emulated system which is not "
                                                  "currently cofigured to support syscalls. To resolve this, make sure "
                                                  "that your SimOS is a subclass of SimUserspace, or set the "
                                                  "BYPASS_UNSUPPORTED_SYSCALL state option.")
            if not state.solver.satisfiable():
                raise AngrUnsupportedSyscallError("The program state is not satisfiable")
            else:
                raise AngrUnsupportedSyscallError("Got a symbolic syscall number")

        # determine the abi
        syscall_abi = self._syscall_abi(state, num)

        # extract the syscall prototype
        lib: SimSyscallLibrary = angr.SIM_LIBRARIES['linux']
        syscall_name = lib.syscall_number_mapping[syscall_abi].get(num)
        if not syscall_name:
            if angr.sim_options.BYPASS_UNSUPPORTED_SYSCALL not in state.options:
                raise AngrUnsupportedSyscallError("Syscall %d for architecture %s is not found in the syscall "
                                                  "mapping" % (num, state.arch.name.lower()))
            raise NotImplementedError("what to do...")

        # check against the blacklist. for certain syscalls, we always want to use angr's support
        syscall_blacklist = SYSCALL_BYPASS_LISTS.get(syscall_abi, BASE_SYSCALL_BYPASS_LIST)
        if syscall_name in syscall_blacklist:
            # ask the next mixin in the hierarchy to process this syscall
            return super().process_successors(successors, **kwargs)

        syscall_proto = lib.get_prototype(syscall_abi, syscall_name, arch=state.arch)
        if syscall_proto is None:
            if angr.sim_options.BYPASS_UNSUPPORTED_SYSCALL not in state.options:
                raise AngrUnsupportedSyscallError("Syscall %d %s for architecture %s is not found in the syscall "
                                                  "prototypes" % (num, syscall_name, state.arch.name.lower()))
            raise NotImplementedError("what to do...")

        # extract syscall arguments
        args = [ ]
        for arg_idx, arg_type in enumerate(syscall_proto.args):
            # TODO: Use arg_type to determine what to do in a finer-grained manner
            arg = syscall_cc.arg(state, arg_idx)
            if not state.solver.symbolic(arg):
                args.append(state.solver.eval_one(arg))
            else:
                # symbolic arguments...
                return super().process_successors(successors, **kwargs)

        # create the successor
        successors.sort = 'SimProcedure'

        # fill in artifacts
        successors.artifacts['is_syscall'] = True
        successors.artifacts['name'] = syscall_name
        successors.artifacts['no_ret'] = False  # TODO
        successors.artifacts['adds_exits'] = True  # TODO

        # Update state.scratch
        state.scratch.sim_procedure = None
        state.history.recent_block_count = 1

        # inspect support
        state._inspect('syscall', BP_BEFORE, syscall_name=syscall_name)

        # talk to Bureau
        succ_state = self.project.bureau.invoke_syscall(state, num, args, syscall_cc)

        # add the successor
        successors.add_successor(succ_state, syscall_cc.return_addr.get_value(state), claripy.true,
                                 jumpkind='Ijk_Ret')

        # inspect - post execution
        state._inspect('syscall', BP_AFTER, syscall_name=syscall_name)

        successors.description = 'SimProcedure ' + syscall_name
        successors.description += ' (syscall)'
        successors.processed = True

    def _syscall_abi(self, state: 'SimState', num: int) -> str:
        """
        Determine the ABI of the current syscall.

        :param state:   The state right after the syscall jump.
        :param num:     The syscall number.
        :return:        A string for the ABI.
        """

        # attempt to get the ABI from syscall_abi, which apparently only works for AMD64
        syscall_abi = self.project.simos.syscall_abi(state)
        if syscall_abi is not None:
            return syscall_abi

        self.project.simos: SimUserland  # we really only support Linux userspace applications

        if len(self.project.simos.syscall_abis) == 1:
            # that has to be it
            return next(iter(self.project.simos.syscall_abis))

        # if there are more than one, pick the correct one
        for abi, (_, min_syscall_num, max_syscall_num) in self.project.simos.syscall_abis.items():
            if min_syscall_num <= num < max_syscall_num:
                return abi

        raise AngrSyscallError("Cannot determine the ABI for syscall %d on architecture %s." % (num, state.arch.name))


from ..errors import AngrSyscallError, AngrUnsupportedSyscallError
