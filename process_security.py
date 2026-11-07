"""Reduce exposure through OS-generated process dumps on Linux."""
import ctypes
import sys

def harden_process():
    if sys.platform != "linux":
        return
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        raise RuntimeError("Unable to disable process dumps")
