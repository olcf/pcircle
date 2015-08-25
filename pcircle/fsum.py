#!/usr/bin/env python
#
# author: Feiyi Wang
#
# This program provides a MPI-based parallization for checksumming
#
#
from __future__ import print_function
import os
import hashlib
import argparse
import stat
import sys
import signal
import cPickle as pickle

from mpi4py import MPI
from cStringIO import StringIO

from circle import Circle
from _version import get_versions
from task import BaseTask
from utils import bytes_fmt, timestamp2, conv_unit
from fwalk import FWalk
from cio import readn
from fdef import ChunkSum
from globals import G
import utils
from pcircle.mpihelper import tally_hosts

ARGS = None
__version__ = get_versions()['version']

del get_versions


def sig_handler():
    # catch keyboard, do nothing
    # eprint("\tUser cancelled ... cleaning up")
    sys.exit(1)


def parse_args():
    parser = argparse.ArgumentParser(description="fsum")
    parser.add_argument("-v", "--version", action="version", version="{version}".format(version=__version__))
    parser.add_argument("--loglevel", default="WARN", help="log level")
    parser.add_argument("path", default=".", help="path")
    parser.add_argument("-i", "--interval", type=int, default=10, help="interval")
    parser.add_argument("-o", "--output", default="sha1-%s.sig" % timestamp2(), help="sha1 output file")
    parser.add_argument("--chunksize", help="chunk size (K, M, G, T)")
    parser.add_argument("--use-store", action="store_true", help="Use persistent store")
    parser.add_argument("--export-block-signatures", action="store_true", help="export block-level signatures")

    return parser.parse_args()


class Checksum(BaseTask):
    def __init__(self, circle, treewalk, chunksize, totalsize=0):
        BaseTask.__init__(self, circle)
        self.circle = circle
        self.treewalk = treewalk
        self.totalsize = totalsize
        self.workcnt = 0
        self.chunkq = []
        self.chunksize = chunksize

        # debug
        self.d = {"rank": "rank %s" % circle.rank}
        self.wtime_started = MPI.Wtime()
        self.wtime_ended = None

        # reduce
        self.vsize = 0

        self.logger = utils.getLogger(__name__)

        if self.circle.rank == 0:
            print("Start parallel checksumming ...")

    def create(self):

        if G.use_store:
            while self.treewalk.flist.qsize > 0:
                fitems, _ = self.treewalk.flist.mget(G.DB_BUFSIZE)
                for fi in fitems:
                    if stat.S_ISREG(fi.st_mode):
                        self.enq_file(fi)  # where chunking takes place
                self.treewalk.flist.mdel(G.DB_BUFSIZE)

        else:
            for fi in self.treewalk.flist:
                if not os.path.islink(fi.path) and stat.S_ISREG(fi.st_mode):
                    self.enq_file(fi)

                    # right after this, we do first checkpoint

                    # if self.checkpoint_file:
                    #    self.do_no_interrupt_checkpoint()
                    #    self.checkpoint_last = MPI.Wtime()

    def enq_file(self, f):
        """
        f[0] path f[1] mode f[2] size - we enq all in one shot
        CMD = copy src  dest  off_start  last_chunk
        """
        chunks = f.st_size / self.chunksize
        remaining = f.st_size % self.chunksize

        workcnt = 0

        if f.st_size == 0:  # empty file
            ck = ChunkSum(f.path)
            self.enq(ck)
            self.logger.debug("%s" % ck, extra=self.d)
            workcnt += 1
        else:
            for i in range(chunks):
                ck = ChunkSum(f.path)
                ck.offset = i * self.chunksize
                ck.length = self.chunksize
                self.enq(ck)
                self.logger.debug("%s" % ck, extra=self.d)
            workcnt += chunks

        if remaining > 0:
            # send remainder
            ck = ChunkSum(f.path)
            ck.offset = chunks * self.chunksize
            ck.length = remaining
            self.enq(ck)
            self.logger.debug("%s" % ck, extra=self.d)
            workcnt += 1

        # tally work cnt
        self.workcnt += workcnt

    # def process(self):
    #     ck = self.deq()
    #     self.logger.debug("process: %s" % ck, extra=self.d)
    #     blocks = ck.length / self.chunksize
    #     remaining = ck.length % self.chunksize
    #
    #     chunk_digests = StringIO()
    #     try:
    #         fd = os.open(ck.filename, os.O_RDONLY)
    #     except OSError as e:
    #         self.logger.warn("%s, Skipping ... " % e, extra=self.d)
    #         return
    #
    #     os.lseek(fd, ck.offset, os.SEEK_SET)
    #
    #     for i in range(blocks):
    #         chunk_digests.write(hashlib.sha1(readn(fd, self.chunksize)).hexdigest())
    #
    #     if remaining > 0:
    #         chunk_digests.write(hashlib.sha1(readn(fd, remaining)).hexdigest())
    #
    #     ck.digest = chunk_digests.getvalue()
    #
    #     self.chunkq.append(ck)
    #
    #     self.vsize += ck.length
    #     try:
    #         os.close(fd)
    #     except Exception as e:
    #         self.logger.warn(e, extra=self.d)

    def process(self):
        ck = self.deq()
        try:
            fd = os.open(ck.filename, os.O_RDONLY)
        except OSError as e:
            self.logger.warn("%s, Skipping ... " % e, extra=self.d)
            return

        os.lseek(fd, ck.offset, os.SEEK_SET)
        digest = hashlib.sha1()
        blocksize = 4*1024*1024 # 4MiB block
        blockcount = ck.length / blocksize
        remaining = ck.length % blocksize
        for _ in xrange(blockcount):
            digest.update(readn(fd, blocksize))
        if remaining > 0:
            digest.update(readn(fd, remaining))
        try:
            os.close(fd)
        except Exception as e:
            self.logger.warn(e, extra=self.d)
        ck.digest = digest.hexdigest()
        self.chunkq.append(ck)
        self.vsize += ck.length

    def reduce_init(self, buf):
        buf['vsize'] = self.vsize

    def reduce_report(self, buf):
        out = ""
        if self.totalsize != 0:
            out += "%.2f %% block checksummed, " % (100 * float(buf['vsize']) / self.totalsize)

        out += "%s bytes done" % bytes_fmt(buf['vsize'])
        print(out)

    def reduce_finish(self, buf):
        # self.reduce_report(buf)
        pass

    def reduce(self, buf1, buf2):
        buf1['vsize'] += buf2['vsize']
        return buf1

    def epilogue(self):
        self.wtime_ended = MPI.Wtime()
        if self.circle.rank == 0:
            print("")
            if self.totalsize == 0:
                return
            time = self.wtime_ended - self.wtime_started
            rate = float(self.totalsize) / time
            print("Checksumming Completed In: %.2f seconds" % time)
            print("Average Rate: %s/s\n" % bytes_fmt(rate))


def _read_in_blocks(chunks, chunksize=26214):
    """ deprecated, do_checksum() return signature directly"""
    idx = 0
    total = len(chunks)
    buf = StringIO()

    while True:
        if idx == total:
            break
        buf.write(chunks[idx].digest)
        idx += 1
        if idx % chunksize == 0 or idx == total:
            yield hashlib.sha1(buf.getvalue()).hexdigest()
            buf = StringIO()


def do_checksum(chunks):
    buf = StringIO()
    h = hashlib.sha1()
    for chunk in chunks:
        buf.write(chunk.digest)
        # idx += 1
        # if idx % blocks == 0 or idx == len(chunks):
        #     h.update(buf.getvalue())
        #     buf = StringIO()
    h.update(buf.getvalue())
    return h.hexdigest()


def export_checksum(chunks):
    fullpath = os.path.abspath(ARGS.output)
    ex_base = os.path.basename(fullpath).split(".")[0] + ".checksums"
    ex_dir = os.path.dirname(fullpath)
    ex_path = os.path.join(ex_dir, ex_base)

    for c in chunks:
        c.filename = os.path.relpath(c.filename, start=ARGS.path)

    with open(ex_path, "wb") as f:
        pickle.dump(chunks, f, pickle.HIGHEST_PROTOCOL)

    return os.path.basename(ex_path)


def export_checksum2(chunks, ofile):
    # fullpath = os.path.abspath(ofile)
    # ex_base = os.path.basename(fullpath).split(".")[0] + ".checksums"
    # ex_dir = os.path.dirname(fullpath)
    # ex_path = os.path.join(ex_dir, ex_base)

    with open(ofile, "a") as f:
        f.write("----block checksums----\n")
        for c in chunks:
            f.write("%s!@%s\n" % (c, c.digest))

    return ofile

def parse_and_bcast():
    global ARGS
    parse_flags = True
    if MPI.COMM_WORLD.rank == 0:
        try:
            ARGS = parse_args()
        except Exception:
            parse_flags = False
    parse_flags = MPI.COMM_WORLD.bcast(parse_flags)
    if parse_flags:
        ARGS = MPI.COMM_WORLD.bcast(ARGS)
    else:
        sys.exit(0)

    if MPI.COMM_WORLD.rank == 0 and ARGS.loglevel == "debug":
        print(ARGS)


def main():
    global ARGS
    signal.signal(signal.SIGINT, sig_handler)

    ARGS = parse_args()
    parse_and_bcast()

    G.loglevel = ARGS.loglevel
    G.use_store = ARGS.use_store

    root = os.path.abspath(ARGS.path)
    hosts_cnt = tally_hosts()
    circle = Circle(reduce_interval=ARGS.interval)

    if circle.rank == 0:
        print("Running Parameters:\n")
        print("\t{:<20}{:<20}".format("FSUM version:", __version__))
        print("\t{:<20}{:<20}".format("Num of hosts:", hosts_cnt))
        print("\t{:<20}{:<20}".format("Num of processes:", MPI.COMM_WORLD.Get_size()))
        print("\t{:<20}{:<20}".format("Root path:", root))

    fwalk = FWalk(circle, root)
    circle.begin(fwalk)
    if G.use_store:
        fwalk.flushdb()
    totalsize = fwalk.epilogue()
    circle.finalize(reduce_interval=ARGS.interval)

    # by default, we use adaptive chunksize
    chunksize = utils.calc_chunksize(totalsize)
    if ARGS.chunksize:
        chunksize = conv_unit(ARGS.chunksize)

    if circle.rank == 0:
        print("Chunksize = ", chunksize)

    fcheck = Checksum(circle, fwalk, chunksize, totalsize)

    circle.begin(fcheck)
    circle.finalize()

    if circle.rank == 0:
        sys.stdout.write("\nAggregating ... ")

    chunkl = circle.comm.gather(fcheck.chunkq)

    if circle.rank == 0:
        chunks = [item for sublist in chunkl for item in sublist]
        chunks.sort()
        sys.stdout.write("%s chunks\n" % len(chunks))
        sha1val = do_checksum(chunks)
        with open(ARGS.output, "w") as f:
            f.write("sha1: %s\n" % sha1val)
            f.write("chunksize: %s\n" % chunksize)
            f.write("fwalk version: %s\n" % __version__)
            f.write("src: %s\n" % root)
            f.write("date: %s\n" % utils.current_time())
            f.write("totalsize: %s\n" % totalsize)

        print("\nSHA1: %s" % sha1val)
        print("Signature file: [%s]" % ARGS.output)
        if ARGS.export_block_signatures:
            export_checksum2(chunks, ARGS.output)
            print("Exporting block signatures ... \n")

    fcheck.epilogue()


if __name__ == "__main__":
    main()
