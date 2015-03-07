__author__ = 'f7b'

import time
import os
import sys

from mpi4py import MPI


MAX_TRIES = 5
SLEEP = 0.1

def readn(filename, fd, size):
    tries = 0
    ret = ''
    while len(ret) < size:
        buf = os.read(fd, size - len(ret))
        if buf == '':
            # EOF check
            return ret
        elif len(buf) == size:
            return buf
        elif len(buf) > 0:
            tries = MAX_TRIES
            ret += buf
        else:
            tries -= 1
            if tries < 0:
                sys.stderr.write("Failed to read %s" % filename)
                MPI.COMM_WORLD.Abort()

            time.sleep(SLEEP)

    return ret

def writen(filename, fd, buf):
    size = len(buf)
    n = 0
    tries = 0
    while n < size:
        rc = os.write(fd, buf[n:])
        if rc > 0:
            n += rc
            tries = MAX_TRIES
        else:
            tries -= 1
            if tries < 0:
                sys.stderr.write("Failed to write %s" % filename)
                MPI.COMM_WORLD.Abort()
            time.sleep(SLEEP)
