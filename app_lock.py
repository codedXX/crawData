"""Windows 和 POSIX 的进程锁；锁文件与可读取的端口元数据分开。"""
import errno
import sys
from contextlib import contextmanager


class AlreadyRunning(Exception):
    pass


@contextmanager
def instance_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if sys.platform == "win32":
            import msvcrt
            if handle.seek(0, 2) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            acquire = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(handle, fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise AlreadyRunning("岗位查询已经在运行，请切换到已打开的窗口。") from exc
            raise
        try:
            yield
        finally:
            handle.seek(0)
            release()
