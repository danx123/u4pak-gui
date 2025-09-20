#!/usr/bin/env python
# coding=UTF-8
#
# Copyright (c) 2014 Mathias Panzenböck
#
# The GUI portion was added by Danx using PySide6. (c) 2025
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
# #
# THIS SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE, AND NON-INFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES, OR
# OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT, OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from __future__ import annotations, with_statement, division, print_function

import os
import io
import sys
import hashlib
import zlib
import math
import argparse
import traceback
import mmap
import weakref
import stat

from struct import unpack as st_unpack, pack as st_pack
from collections import OrderedDict
from io import DEFAULT_BUFFER_SIZE
from binascii import hexlify
from typing import NamedTuple, Optional, Tuple, List, Dict, Set, Iterable, Iterator, Callable, IO, Any, Union

# --- Integrasi PySide6 ---
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QFileDialog, QLineEdit, QTabWidget, QTextEdit,
    QTreeView, QProgressBar, QLabel, QCheckBox, QComboBox, QListWidget,
    QMessageBox, QHeaderView, QListWidgetItem
)
from PySide6.QtGui import QStandardItemModel, QStandardItem, QFont
from PySide6.QtCore import QObject, QThread, Signal, Qt

try:
	import llfuse # type: ignore
except ImportError:
	HAS_LLFUSE = False
else:
	HAS_LLFUSE = True

HAS_STAT_NS = hasattr(os.stat_result, 'st_atime_ns')

# ==============================================================================
# == KODE INTI DARI u4pak.py (sedikit dimodifikasi untuk integrasi GUI) ==
# ==============================================================================

__all__ = 'read_index', 'pack'

def highlevel_sendfile(outfile: io.BufferedWriter, infile: io.BufferedReader, offset: int, size: int) -> None:
	infile.seek(offset,0)
	buf_size = DEFAULT_BUFFER_SIZE
	buf = bytearray(buf_size)
	while size > 0:
		if size >= buf_size:
			n = infile.readinto(buf) or 0
			if n < buf_size:
				raise IOError("unexpected end of file")
			outfile.write(buf)
			size -= buf_size
		else:
			data = infile.read(size) or b''
			if len(data) < size:
				raise IOError("unexpected end of file")
			outfile.write(data)
			size = 0

if hasattr(os, 'sendfile'):
	def os_sendfile(outfile: io.BufferedWriter, infile: io.BufferedReader, offset: int, size: int) -> None:
		try:
			out_fd = outfile.fileno()
			in_fd  = infile.fileno()
		except:
			highlevel_sendfile(outfile, infile, offset, size)
		else:
			# size == 0 has special meaning for some sendfile implentations
			if size > 0:
				os.sendfile(out_fd, in_fd, offset, size)
	sendfile = os_sendfile
else:
	sendfile = highlevel_sendfile

def raise_check_error(ctx: Optional[Record], message: str) -> None:
	if ctx is None:
		raise ValueError(message)

	elif isinstance(ctx, Record):
		raise ValueError("%s: %s" % (ctx.filename, message))

	else:
		raise ValueError("%s: %s" % (ctx, message))

class FragInfo(object):
	__slots__ = '__frags', '__size'

	__size: int
	__frags: List[Tuple[int, int]]

	def __init__(self, size: int, frags: Optional[List[Tuple[int, int]]] = None) -> None:
		self.__size  = size
		self.__frags = []
		if frags:
			for start, end in frags:
				self.add(start, end)

	@property
	def size(self) -> int:
		return self.__size

	def __iter__(self) -> Iterator[Tuple[int, int]]:
		return iter(self.__frags)

	def __len__(self) -> int:
		return len(self.__frags)

	def __repr__(self) -> str:
		return 'FragInfo(%r,%r)' % (self.__size, self.__frags)

	def add(self, new_start: int, new_end: int) -> None:
		if new_start >= new_end:
			return

		elif new_start >= self.__size or new_end > self.__size:
			raise IndexError("range out of bounds: (%r, %r]" % (new_start, new_end))

		frags = self.__frags
		for i, (start, end) in enumerate(frags):
			if new_end < start:
				frags.insert(i, (new_start, new_end))
				return

			elif new_start <= start:
				if new_end <= end:
					frags[i] = (new_start, end)
					return

			elif new_start <= end:
				if new_end > end:
					new_start = start
			else:
				continue

			j = i+1
			n = len(frags)
			while j < n:
				next_start, next_end = frags[j]
				if next_start <= new_end:
					j += 1
					if next_end > new_end:
						new_end = next_end
						break
				else:
					break

			frags[i:j] = [(new_start, new_end)]
			return

		frags.append((new_start, new_end))

	def invert(self) -> FragInfo:
		inverted = FragInfo(self.__size)
		append   = inverted.__frags.append
		prev_end = 0

		for start, end in self.__frags:
			if start > prev_end:
				append((prev_end, start))
			prev_end = end

		if self.__size > prev_end:
			append((prev_end, self.__size))

		return inverted

	def free(self) -> int:
		free     = 0
		prev_end = 0

		for start, end in self.__frags:
			free += start - prev_end
			prev_end = end

		free += self.__size - prev_end

		return free

class Pak(object):
	__slots__ = ('version', 'index_offset', 'index_size', 'footer_offset', 'index_sha1', 'mount_point', 'records')

	version: int
	index_offset: int
	index_size: int
	footer_offset: int
	index_sha1: bytes
	mount_point: Optional[str]
	records: List[Record]

	def __init__(self, version: int, index_offset: int, index_size: int, footer_offset: int, index_sha1: bytes, mount_point: Optional[str] = None, records: Optional[List[Record]] = None) -> None:
		self.version       = version
		self.index_offset  = index_offset
		self.index_size    = index_size
		self.footer_offset = footer_offset
		self.index_sha1    = index_sha1
		self.mount_point   = mount_point
		self.records       = records or []

	def __len__(self) -> int:
		return len(self.records)

	def __iter__(self) -> Iterator[Record]:
		return iter(self.records)

	def __repr__(self) -> str:
		return 'Pak(version=%r, index_offset=%r, index_size=%r, footer_offset=%r, index_sha1=%r, mount_point=%r, records=%r)' % (
			self.version, self.index_offset, self.index_size, self.footer_offset, self.index_sha1, self.mount_point, self.records)

	def check_integrity(self, stream: io.BufferedReader, callback: Callable[[Optional[Record], str], None] = raise_check_error, ignore_null_checksums: bool = False) -> None:
		index_offset = self.index_offset
		buf = bytearray(DEFAULT_BUFFER_SIZE)

		read_record: Callable[[io.BufferedReader, str], Record]
		if self.version == 1:
			read_record = read_record_v1

		elif self.version == 2:
			read_record = read_record_v2

		elif self.version == 3:
			read_record = read_record_v3

		elif self.version == 4:
			read_record = read_record_v4

		elif self.version == 7:
			read_record = read_record_v7

		else:
			raise ValueError(f'unsupported version: {self.version}')

		def check_data(ctx, offset, size, sha1):
			if ignore_null_checksums and sha1 == b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00':
				return

			hasher = hashlib.sha1()
			stream.seek(offset, 0)

			while size > 0:
				if size >= DEFAULT_BUFFER_SIZE:
					size -= stream.readinto(buf)
					hasher.update(buf)
				else:
					rest = stream.read(size)
					assert rest is not None
					hasher.update(rest)
					size = 0

			if hasher.digest() != sha1:
				callback(ctx,
						 'checksum missmatch:\n'
						 '\tgot:      %s\n'
						 '\texpected: %s' % (
							 hasher.hexdigest(),
							 hexlify(sha1).decode('latin1')))

		# test index sha1 sum
		check_data("<archive index>", index_offset, self.index_size, self.index_sha1)

		for r1 in self:
			stream.seek(r1.offset, 0)
			r2 = read_record(stream, r1.filename)

			# test index metadata
			if r2.offset != 0:
				callback(r2, 'data record offset field is not 0 but %d' % r2.offset)

			if not same_metadata(r1, r2):
				callback(r1, 'metadata missmatch:\n%s' % metadata_diff(r1, r2))

			if r1.compression_method not in COMPR_METHODS:
				callback(r1, 'unknown compression method: 0x%02x' % r1.compression_method)

			if r1.compression_method == COMPR_NONE and r1.compressed_size != r1.uncompressed_size:
				callback(r1, 'file is not compressed but compressed size (%d) differes from uncompressed size (%d)' %
						 (r1.compressed_size, r1.uncompressed_size))

			if r1.data_offset + r1.compressed_size > index_offset:
				callback(None, 'data bleeds into index')

			# test file sha1 sum
			if ignore_null_checksums and r1.sha1 == b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00':
				pass
			elif r1.compression_blocks is None:
				check_data(r1, r1.data_offset, r1.compressed_size, r1.sha1)
			else:
				hasher = hashlib.sha1()
				base_offset = r1.base_offset
				for start_offset, end_offset in r1.compression_blocks:
					block_size = end_offset - start_offset
					stream.seek(base_offset + start_offset, 0)
					data = stream.read(block_size)
					hasher.update(data)
				
				if hasher.digest() != r1.sha1:
					callback(r1,
							'checksum missmatch:\n'
							'\tgot:      %s\n'
							'\texpected: %s' % (
								hasher.hexdigest(),
								hexlify(r1.sha1).decode('latin1')))

	def unpack(self, stream: io.BufferedReader, outdir: str=".", callback: Callable[[str], None] = lambda name: None) -> None:
		for record in self:
			record.unpack(stream, outdir, callback)

	def unpack_only(self, stream: io.BufferedReader, files: Iterable[str], outdir: str = ".", callback: Callable[[str], None] = lambda name: None) -> None:
		for record in self:
			if shall_unpack(files, record.filename):
				record.unpack(stream, outdir, callback)

	def frag_info(self) -> FragInfo:
		frags = FragInfo(self.footer_offset + 44)
		frags.add(self.index_offset, self.index_offset + self.index_size)
		frags.add(self.footer_offset, frags.size)

		for record in self.records:
			frags.add(record.offset, record.data_offset + record.compressed_size)

		return frags

	def print_list(self, details: bool = False, human: bool = False, delim: str = "\n", sort_key_func: Optional[Callable[[Record], Any]] = None, out: IO[str] = sys.stdout) -> None:
		records = self.records

		if sort_key_func:
			records = sorted(records, key=sort_key_func)

		if details:
			size_to_str: Callable[[int], str]
			if human:
				size_to_str = human_size
			else:
				size_to_str = str

			count = 0
			sum_size = 0
			out.write("    Offset        Size  Compr-Method  Compr-Size  SHA1                                      Name%s" % delim)
			for record in records:
				size  = size_to_str(record.uncompressed_size)
				sha1  = hexlify(record.sha1).decode('latin1')
				cmeth = record.compression_method

				if cmeth == COMPR_NONE:
					out.write("%10u  %10s             -           -  %s  %s%s" % (
						record.data_offset, size, sha1, record.filename, delim))
				else:
					out.write("%10u  %10s  %12s  %10s  %s  %s%s" % (
						record.data_offset, size, COMPR_METHOD_NAMES[cmeth],
						size_to_str(record.compressed_size), sha1,
						record.filename, delim))
				count += 1
				sum_size += record.uncompressed_size
			out.write("%d file(s) (%s) %s" % (count, size_to_str(sum_size), delim))
		else:
			for record in records:
				out.write("%s%s" % (record.filename, delim))

	def print_info(self, human: bool = False, out: IO[str] = sys.stdout) -> None:
		size_to_str: Callable[[int], str]
		if human:
			size_to_str = human_size
		else:
			size_to_str = str

		csize = 0
		size  = 0
		for record in self.records:
			csize += record.compressed_size
			size  += record.uncompressed_size

		frags = self.frag_info()

		out.write("Pak Version: %d\n" % self.version)
		out.write("Index SHA1:  %s\n" % hexlify(self.index_sha1).decode('latin1'))
		out.write("Mount Point: %s\n" % self.mount_point)
		out.write("File Count:  %d\n" % len(self.records))
		out.write("Archive Size:            %10s\n" % size_to_str(frags.size))
		out.write("Unallocated Bytes:       %10s\n" % size_to_str(frags.free()))
		out.write("Sum Compr. Files Size:   %10s\n" % size_to_str(csize))
		out.write("Sum Uncompr. Files Size: %10s\n" % size_to_str(size))
		out.write("\n")
		out.write("Fragments (%d):\n" % len(frags))

		for start, end in frags:
			out.write("\t%10s ... %10s (%10s)\n" % (start, end, size_to_str(end - start)))

	def mount(self, stream: io.BufferedReader, mountpt: str, foreground: bool = False, debug: bool = False) -> None:
		mountpt = os.path.abspath(mountpt)
		ops     = Operations(stream, self)
		args    = ['fsname=u4pak', 'subtype=u4pak', 'ro']

		if debug:
			foreground = True
			args.append('debug')

		if not foreground:
			deamonize()

		llfuse.init(ops, mountpt, args)
		try:
			llfuse.main()
		finally:
			llfuse.close()

# compare all metadata except for the filename
def same_metadata(r1: Record, r2: Record) -> bool:
	# data records always have offset == 0 it seems, so skip that
	return \
		r1.compressed_size        == r2.compressed_size    and \
		r1.uncompressed_size      == r2.uncompressed_size  and \
		r1.compression_method     == r2.compression_method and \
		r1.timestamp              == r2.timestamp          and \
		r1.sha1                   == r2.sha1               and \
		r1.compression_blocks     == r2.compression_blocks and \
		r1.encrypted              == r2.encrypted          and \
		r1.compression_block_size == r2.compression_block_size

def metadata_diff(r1: Record, r2: Record) -> str:
	diff = []

	for attr in ['compressed_size', 'uncompressed_size', 'timestamp', 'encrypted', 'compression_block_size']:
		v1 = getattr(r1,attr)
		v2 = getattr(r2,attr)
		if v1 != v2:
			diff.append('\t%s: %r != %r' % (attr, v1, v2))

	if r1.sha1 != r2.sha1:
		diff.append('\tsha1: %s != %s' % (hexlify(r1.sha1).decode('latin1'), hexlify(r2.sha1).decode('latin1')))

	if r1.compression_blocks != r2.compression_blocks:
		diff.append('\tcompression_blocks:\n\t\t%r\n\t\t\t!=\n\t\t%r' % (r1.compression_blocks, r2.compression_blocks))

	return '\n'.join(diff)

COMPR_NONE        = 0x00
COMPR_ZLIB        = 0x01
COMPR_BIAS_MEMORY = 0x10
COMPR_BIAS_SPEED  = 0x20

COMPR_METHODS: Set[int] = {COMPR_NONE, COMPR_ZLIB, COMPR_BIAS_MEMORY, COMPR_BIAS_SPEED}

COMPR_METHOD_NAMES: Dict[int, str] = {
	COMPR_NONE: 'none',
	COMPR_ZLIB: 'zlib',
	COMPR_BIAS_MEMORY: 'bias memory',
	COMPR_BIAS_SPEED:  'bias speed'
}

class Record(NamedTuple):
	filename:               str
	offset:                 int
	compressed_size:        int
	uncompressed_size:      int
	compression_method:     int
	timestamp:              Optional[int]
	sha1:                   bytes
	compression_blocks:     Optional[List[Tuple[int, int]]]
	encrypted:              bool
	compression_block_size: Optional[int]

	def sendfile(self, outfile: io.BufferedWriter, infile: io.BufferedReader) -> None:
		if self.compression_method == COMPR_NONE:
			sendfile(outfile, infile, self.data_offset, self.uncompressed_size)
		elif self.compression_method == COMPR_ZLIB:
			if self.encrypted:
				raise NotImplementedError('zlib decompression with encryption is not implemented yet')
			assert self.compression_blocks is not None
			base_offset = self.base_offset
			for start_offset, end_offset in self.compression_blocks:
				block_size = end_offset - start_offset
				infile.seek(base_offset + start_offset)
				block_content = infile.read(block_size)
				assert block_content is not None
				block_decompress = zlib.decompress(block_content)
				outfile.write(block_decompress)
		else:
			raise NotImplementedError('decompression is not implemented yet')

	@property
	def base_offset(self):
		return 0

	def read(self, data: Union[memoryview, bytes, mmap.mmap], offset: int, size: int) -> Union[bytes, bytearray]:
		if self.encrypted:
			raise NotImplementedError('decryption is not supported')

		if self.compression_method == COMPR_NONE:
			uncompressed_size = self.uncompressed_size

			if offset >= uncompressed_size:
				return b''

			i = self.data_offset + offset
			j = i + min(uncompressed_size - offset, size)
			return data[i:j]
		elif self.compression_method == COMPR_ZLIB:
			assert self.compression_blocks is not None
			base_offset = self.base_offset
			buffer = bytearray()
			end_offset = offset + size

			compression_block_size = self.compression_block_size
			assert compression_block_size
			start_block_index = offset // compression_block_size
			end_block_index   = end_offset // compression_block_size

			current_offset = compression_block_size * start_block_index
			for block_start_offset, block_end_offset in self.compression_blocks[start_block_index:end_block_index + 1]:
				block_size = block_end_offset - block_start_offset

				block_content = data[base_offset + block_start_offset:base_offset + block_end_offset]
				block_decompress = zlib.decompress(block_content)

				next_offset = current_offset + len(block_decompress)
				if current_offset >= offset:
					buffer.extend(block_decompress[:end_offset - current_offset])
				else:
					buffer.extend(block_decompress[offset - current_offset:end_offset - current_offset])

				current_offset = next_offset
			return buffer
		else:
			raise NotImplementedError(f'decompression method {self.compression_method} is not supported')

	def unpack(self, stream: io.BufferedReader, outdir: str = ".", callback: Callable[[str], None] = lambda name: None) -> None:
		prefix, name = os.path.split(self.filename)
		prefix = os.path.join(outdir,prefix)
		if not os.path.exists(prefix):
			os.makedirs(prefix)
		name = os.path.join(prefix,name)
		callback(name)
		fp: io.BufferedWriter
		with open(name, "wb") as fp: # type: ignore
			self.sendfile(fp, stream)

	@property
	def data_offset(self) -> int:
		return self.offset + self.header_size

	@property
	def alloc_size(self) -> int:
		return self.header_size + self.compressed_size

	@property
	def index_size(self) -> int:
		name_size = 4 + len(self.filename.replace(os.path.sep,'/').encode('utf-8')) + 1
		return name_size + self.header_size

	@property
	def header_size(self) -> int:
		raise NotImplementedError

class RecordV1(Record):
	__slots__ = ()

	def __new__(cls, filename: str, offset: int, compressed_size: int, uncompressed_size: int, compression_method: int, timestamp: Optional[int], sha1: bytes) -> RecordV1:
		return Record.__new__(cls, filename, offset, compressed_size, uncompressed_size,
							  compression_method, timestamp, sha1, None, False, None) # type: ignore

	@property
	def header_size(self) -> int:
		return 56

class RecordV2(Record):
	__slots__ = ()

	def __new__(cls, filename: str, offset: int, compressed_size: int, uncompressed_size: int, compression_method: int, sha1: bytes) -> RecordV2:
		return Record.__new__(cls, filename, offset, compressed_size, uncompressed_size,
							  compression_method, None, sha1, None, False, None) # type: ignore

	@property
	def header_size(self):
		return 48

class RecordV3(Record):
	__slots__ = ()

	def __new__(cls, filename: str, offset: int, compressed_size: int, uncompressed_size: int, compression_method: int, sha1: bytes,
				compression_blocks: Optional[List[Tuple[int, int]]], encrypted: bool, compression_block_size: Optional[int]) -> RecordV3:
		return Record.__new__(cls, filename, offset, compressed_size, uncompressed_size,
							  compression_method, None, sha1, compression_blocks, encrypted,
							  compression_block_size) # type: ignore

	@property
	def header_size(self) -> int:
		size = 53
		if self.compression_method != COMPR_NONE:
			assert self.compression_blocks is not None
			size += len(self.compression_blocks) * 16
		return size

# XXX: Don't know at which version exactly the change happens.
#      Only know 4 is relative, 7 is absolute.
class RecordV7(RecordV3):
	@property
	def base_offset(self):
		return self.offset

def read_path(stream: io.BufferedReader, encoding: str = 'utf-8') -> str:
	path_len, = st_unpack('<i',stream.read(4))
	if path_len < 0:
		# in at least some format versions, this indicates a UTF-16 path
		path_len = -2 * path_len
		encoding = 'utf-16le'
	return stream.read(path_len).decode(encoding).rstrip('\0').replace('/',os.path.sep)

def pack_path(path: str, encoding: str = 'utf-8') -> bytes:
	encoded_path = path.replace(os.path.sep, '/').encode('utf-8') + b'\0'
	return st_pack('<I', len(encoded_path)) + encoded_path

def write_path(stream: io.BufferedWriter, path: str, encoding: str = 'utf-8') -> bytes:
	data = pack_path(path,encoding)
	stream.write(data)
	return data

def read_record_v1(stream: io.BufferedReader, filename: str) -> RecordV1:
	return RecordV1(filename, *st_unpack('<QQQIQ20s',stream.read(56)))

def read_record_v2(stream: io.BufferedReader, filename: str) -> RecordV2:
	return RecordV2(filename, *st_unpack('<QQQI20s',stream.read(48)))

def read_record_v3(stream: io.BufferedReader, filename: str) -> RecordV3:
	offset, compressed_size, uncompressed_size, compression_method, sha1 = \
		st_unpack('<QQQI20s',stream.read(48))

	blocks: Optional[List[Tuple[int, int]]]
	if compression_method != COMPR_NONE:
		block_count, = st_unpack('<I',stream.read(4))
		blocks_bin = st_unpack('<%dQ' % (block_count * 2), stream.read(16 * block_count))
		blocks = [(blocks_bin[i], blocks_bin[i+1]) for i in range(0, block_count * 2, 2)]
	else:
		blocks = None

	encrypted, compression_block_size = st_unpack('<BI',stream.read(5))

	return RecordV3(filename, offset, compressed_size, uncompressed_size, compression_method,
					sha1, blocks, encrypted != 0, compression_block_size) # type: ignore

read_record_v4 = read_record_v3

def read_record_v7(stream: io.BufferedReader, filename: str) -> RecordV3:
	offset, compressed_size, uncompressed_size, compression_method, sha1 = \
		st_unpack('<QQQI20s',stream.read(48))

	blocks: Optional[List[Tuple[int, int]]]
	if compression_method != COMPR_NONE:
		block_count, = st_unpack('<I',stream.read(4))
		blocks_bin = st_unpack('<%dQ' % (block_count * 2), stream.read(16 * block_count))
		blocks = [(blocks_bin[i], blocks_bin[i+1]) for i in range(0, block_count * 2, 2)]
	else:
		blocks = None

	encrypted, compression_block_size = st_unpack('<BI',stream.read(5))

	return RecordV7(filename, offset, compressed_size, uncompressed_size, compression_method,
					sha1, blocks, encrypted != 0, compression_block_size) # type: ignore

def write_data(
		archive: io.BufferedWriter,
		fh: io.BufferedReader,
		size: int,
		compression_method: int = COMPR_NONE,
		encrypted: bool = False,
		compression_block_size: int = 0
) -> Tuple[int, bytes]:
	if compression_method != COMPR_NONE:
		raise NotImplementedError("compression is not implemented")

	if encrypted:
		raise NotImplementedError("encryption is not implemented")

	buf_size = DEFAULT_BUFFER_SIZE
	buf = bytearray(buf_size)
	bytes_left = size
	hasher = hashlib.sha1()
	while bytes_left > 0:
		data: Union[bytes, bytearray]
		if bytes_left >= buf_size:
			n = fh.readinto(buf)
			data = buf
			if n is None or n < buf_size:
				raise IOError('unexpected end of file')
		else:
			opt_data = fh.read(bytes_left)
			assert opt_data is not None
			n = len(opt_data)
			if n < bytes_left:
				raise IOError('unexpected end of file')
			data = opt_data
		bytes_left -= n
		hasher.update(data)
		archive.write(data)

	return size, hasher.digest()

def write_data_zlib(
		archive: io.BufferedWriter,
		fh: io.BufferedReader,
		size: int,
		compression_method: int = COMPR_NONE,
		encrypted: bool = False,
		compression_block_size: int = 65536
) -> Tuple[int, bytes, int, List[int]]:
	if encrypted:
		raise NotImplementedError("encryption is not implemented")

	buf_size = compression_block_size
	block_count = int(math.ceil(size / compression_block_size))
	base_offset = archive.tell()

	archive.write(st_pack('<I',block_count))

	# Seek Skip Offset
	archive.seek(block_count * 8 * 2, 1)

	record = st_pack('<BI', int(encrypted), compression_block_size)
	archive.write(record)

	cur_offset = base_offset + 4 + block_count * 8 * 2 + 5

	compress_blocks = [0] * block_count * 2
	compressed_size = 0
	compress_block_no = 0

	buf = bytearray(buf_size)
	bytes_left: int = size
	hasher = hashlib.sha1()
	while bytes_left > 0:
		n: int
		if bytes_left >= buf_size:
			n = fh.readinto(buf) or 0
			data = zlib.compress(memoryview(buf))

			compressed_size += len(data)
			compress_blocks[compress_block_no * 2] = cur_offset
			cur_offset += len(data)
			compress_blocks[compress_block_no * 2 + 1] = cur_offset
			compress_block_no += 1

			if n < buf_size:
				raise IOError('unexpected end of file')
		else:
			data = fh.read(bytes_left) or b''
			n = len(data)

			data = zlib.compress(data)
			compressed_size += len(data)
			compress_blocks[compress_block_no * 2] = cur_offset
			cur_offset += len(data)
			compress_blocks[compress_block_no * 2 + 1] = cur_offset
			compress_block_no += 1

			if n < bytes_left:
				raise IOError('unexpected end of file')
		bytes_left -= n
		hasher.update(data)
		archive.write(data)

	cur_offset = archive.tell()

	archive.seek(base_offset + 4, 0)
	archive.write(st_pack('<%dQ' % (block_count * 2), *compress_blocks))
	archive.seek(cur_offset, 0)

	return compressed_size, hasher.digest(), block_count, compress_blocks

def write_record_v1(
		archive: io.BufferedWriter,
		fh: io.BufferedReader,
		compression_method: int = COMPR_NONE,
		encrypted: bool = False,
		compression_block_size: int = 0) -> bytes:
	if encrypted:
		raise ValueError('version 1 does not support encryption')

	record_offset = archive.tell()

	st = os.fstat(fh.fileno())
	size = st.st_size
	# XXX: timestamp probably needs multiplication with some factor?
	record = st_pack('<16xQIQ20x',size,compression_method,int(st.st_mtime))
	archive.write(record)

	compressed_size, sha1 = write_data(archive,fh,size,compression_method,encrypted,compression_block_size)
	data_end = archive.tell()

	archive.seek(record_offset+8, 0)
	archive.write(st_pack('<Q',compressed_size))

	archive.seek(record_offset+36, 0)
	archive.write(sha1)

	archive.seek(data_end, 0)

	return st_pack('<QQQIQ20s',record_offset,compressed_size,size,compression_method,int(st.st_mtime),sha1)

def write_record_v2(
		archive: io.BufferedWriter,
		fh: io.BufferedReader,
		compression_method: int = COMPR_NONE,
		encrypted: bool = False,
		compression_block_size: int = 0) -> bytes:
	if encrypted:
		raise ValueError('version 2 does not support encryption')

	record_offset = archive.tell()

	st = os.fstat(fh.fileno())
	size = st.st_size
	record = st_pack('<16xQI20x',size,compression_method)
	archive.write(record)

	compressed_size, sha1 = write_data(archive,fh,size,compression_method,encrypted,compression_block_size)
	data_end = archive.tell()

	archive.seek(record_offset+8, 0)
	archive.write(st_pack('<Q',compressed_size))

	archive.seek(record_offset+28, 0)
	archive.write(sha1)

	archive.seek(data_end, 0)

	return st_pack('<QQQI20s',record_offset,compressed_size,size,compression_method,sha1)

def write_record_v3(
		archive: io.BufferedWriter,
		fh: io.BufferedReader,
		compression_method: int = COMPR_NONE,
		encrypted: bool = False,
		compression_block_size: int = 0) -> bytes:
	if compression_method != COMPR_NONE and compression_method != COMPR_ZLIB:
		raise NotImplementedError("compression is not implemented")

	record_offset = archive.tell()

	if compression_block_size == 0 and compression_method == COMPR_ZLIB:
		compression_block_size = 65536

	st = os.fstat(fh.fileno())
	size = st.st_size
	record = st_pack('<16xQI20x',size,compression_method)
	archive.write(record)

	if compression_method == COMPR_ZLIB:
		compressed_size, sha1, block_count, blocks = write_data_zlib(archive,fh,size,compression_method,encrypted,compression_block_size)
	else:
		record = st_pack('<BI',int(encrypted),compression_block_size)
		archive.write(record)
		compressed_size, sha1 = write_data(archive,fh,size,compression_method,encrypted,compression_block_size)
	data_end = archive.tell()

	archive.seek(record_offset+8, 0)
	archive.write(st_pack('<Q',compressed_size))

	archive.seek(record_offset+28, 0)
	archive.write(sha1)

	archive.seek(data_end, 0)

	if compression_method == COMPR_ZLIB:
		return st_pack('<QQQI20s',record_offset,compressed_size,size,compression_method,sha1) + st_pack('<I%dQ' % (block_count * 2), block_count, *blocks) + st_pack('<BI',int(encrypted),compression_block_size)
	else:
		return st_pack('<QQQI20sBI',record_offset,compressed_size,size,compression_method,sha1,int(encrypted),compression_block_size)

def read_index(
		stream: io.BufferedReader,
		check_integrity: bool = False,
		ignore_magic: bool = False,
		encoding: str = 'utf-8',
		force_version: Optional[int] = None,
		ignore_null_checksums: bool = False) -> Pak:
	stream.seek(-44, 2)
	footer_offset = stream.tell()
	footer = stream.read(44)
	magic, version, index_offset, index_size, index_sha1 = st_unpack('<IIQQ20s',footer)

	if not ignore_magic and magic != 0x5A6F12E1:
		raise ValueError('illegal file magic: 0x%08x' % magic)

	if force_version is not None:
		version = force_version

	read_record: Callable[[io.BufferedReader, str], Record]
	if version == 1:
		read_record = read_record_v1

	elif version == 2:
		read_record = read_record_v2

	elif version == 3:
		read_record = read_record_v3

	elif version == 4:
		read_record = read_record_v4

	elif version == 7:
		read_record = read_record_v7

	else:
		raise ValueError('unsupported version: %d' % version)

	if index_offset + index_size > footer_offset:
		raise ValueError('illegal index offset/size')

	stream.seek(index_offset, 0)

	mount_point = read_path(stream, encoding)
	entry_count = st_unpack('<I',stream.read(4))[0]

	pak = Pak(version, index_offset, index_size, footer_offset, index_sha1, mount_point)

	for i in range(entry_count):
		filename = read_path(stream, encoding)
		record   = read_record(stream, filename)
		pak.records.append(record)

	if stream.tell() > footer_offset:
		raise ValueError('index bleeds into footer')

	if check_integrity:
		pak.check_integrity(stream, ignore_null_checksums=ignore_null_checksums)

	return pak

def _pack_callback(name: str, files: List[str]) -> None:
	pass

def pack(stream: io.BufferedWriter, files_or_dirs: List[str], mount_point: str, version: int = 3, compression_method: int = COMPR_NONE,
		 encrypted: bool = False, compression_block_size: int = 0, callback: Callable[[str, List[str]], None] = _pack_callback,
		 encoding: str='utf-8') -> None:
	if version == 1:
		write_record = write_record_v1

	elif version == 2:
		write_record = write_record_v2

	elif version == 3:
		write_record = write_record_v3

	else:
		raise ValueError('version not supported: %d' % version)

	files: List[str] = []
	for name in files_or_dirs:
		if os.path.isdir(name):
			for dirpath, dirnames, filenames in os.walk(name):
				for filename in filenames:
					files.append(os.path.join(dirpath,filename))
		else:
			files.append(name)

	files.sort()

	records: List[Tuple[str, bytes]] = []
	for filename in files:
		callback(filename, files)
		fh: io.BufferedReader
		with open(filename, "rb") as fh: # type: ignore
			record = write_record(stream, fh, compression_method, encrypted, compression_block_size)
			records.append((filename, record))

	write_index(stream,version,mount_point,records,encoding)

def write_index(stream: IO[bytes], version: int, mount_point: str, records: List[Tuple[str, bytes]], encoding: str = 'utf-8') -> None:
	hasher = hashlib.sha1()
	index_offset = stream.tell()

	index_header = pack_path(mount_point, encoding) + st_pack('<I',len(records))
	index_size   = len(index_header)
	hasher.update(index_header)
	stream.write(index_header)

	for filename, record in records:
		encoded_filename = pack_path(filename, encoding)
		hasher.update(encoded_filename)
		stream.write(encoded_filename)
		index_size += len(encoded_filename)

		hasher.update(record)
		stream.write(record)
		index_size += len(record)

	index_sha1 = hasher.digest()
	stream.write(st_pack('<IIQQ20s', 0x5A6F12E1, version, index_offset, index_size, index_sha1))

def make_record_v1(filename: str) -> RecordV1:
	st   = os.stat(filename)
	size = st.st_size
	return RecordV1(filename, -1, size, size, COMPR_NONE, int(st.st_mtime), b'') # type: ignore

def make_record_v2(filename: str) -> RecordV2:
	size = os.path.getsize(filename)
	return RecordV2(filename, -1, size, size, COMPR_NONE, b'') # type: ignore

def make_record_v3(filename: str) -> RecordV3:
	size = os.path.getsize(filename)
	return RecordV3(filename, -1, size, size, COMPR_NONE, b'', None, False, 0) # type: ignore

# TODO: untested!
# removes, inserts and updates files, rewrites index, truncates archive if neccesarry
def update(stream: io.BufferedRandom, mount_point: str, insert: Optional[List[str]] = None, remove: Optional[List[str]] = None, compression_method: int = COMPR_NONE,
		   encrypted: bool = False, compression_block_size: int = 0, callback: Callable[[str], None] = lambda name: None,
		   ignore_magic: bool = False, encoding: str = 'utf-8', force_version: Optional[int] = None):
	if compression_method != COMPR_NONE:
		raise NotImplementedError("compression is not implemented")

	if encrypted:
		raise NotImplementedError("encryption is not implemented")

	pak = read_index(stream, False, ignore_magic, encoding, force_version)

	make_record: Callable[[str], Record]
	if pak.version == 1:
		write_record = write_record_v1
		make_record  = make_record_v1

	elif pak.version == 2:
		write_record = write_record_v2
		make_record  = make_record_v2

	elif pak.version == 3:
		write_record = write_record_v3
		make_record  = make_record_v3

	else:
		raise ValueError('version not supported: %d' % pak.version)

	# build directory tree of existing files
	root = Dir(-1)
	root.parent = root
	for record in pak:
		path = record.filename.split(os.path.sep)
		path, name = path[:-1], path[-1]

		parent = root
		for i, comp in enumerate(path):
			comp_encoded = comp.encode(encoding)
			try:
				entry = parent.children[comp_encoded]
			except KeyError:
				entry = parent.children[comp_encoded] = Dir(-1, parent=parent)

			if not isinstance(entry, Dir):
				raise ValueError("name conflict in archive: %r is not a directory" % os.path.join(*path[:i+1]))

			parent = entry

		if name in parent.children:
			raise ValueError("doubled name in archive: %s" % record.filename)

		parent.children[name.encode(encoding)] = File(-1, record, parent)

	# find files to remove
	if remove:
		for filename in remove:
			path = filename.split(os.path.sep)
			path, name = path[:-1], path[-1]

			parent = root
			for i, comp in enumerate(path):
				comp_encoded = comp.encode(encoding)
				try:
					entry = parent.children[comp_encoded]
				except KeyError:
					entry = parent.children[comp_encoded] = Dir(-1, parent=parent)

				if not isinstance(entry, Dir):
					# TODO: maybe option to ignore this?
					raise ValueError("file not in archive: %s" % filename)

				parent = entry

			if name not in parent.children:
				raise ValueError("file not in archive: %s" % filename)

			name_encoded = name.encode(encoding)
			entry = parent.children[name_encoded]
			del parent.children[name_encoded]

	# find files to insert
	if insert:
		files = []
		for name in insert:
			if os.path.isdir(name):
				for dirpath, dirnames, filenames in os.walk(name):
					for filename in filenames:
						files.append(os.path.join(dirpath,filename))
			else:
				files.append(name)

		for filename in files:
			path = filename.split(os.path.sep)
			path, name = path[:-1], path[-1]

			parent = root
			for i, comp in enumerate(path):
				comp_encoded = comp.encode(encoding)
				try:
					entry = parent.children[comp_encoded]
				except KeyError:
					entry = parent.children[comp_encoded] = Dir(-1, parent=parent)

				if not isinstance(entry, Dir):
					raise ValueError("name conflict in archive: %r is not a directory" % os.path.join(*path[:i+1]))

				parent = entry

			if name in parent.children:
				raise ValueError("doubled name in archive: %s" % filename)

			parent.children[name.encode(encoding)] = File(-1, make_record(filename), parent)

	# build new allocations
	existing_records: List[Record] = []
	new_records:      List[Record] = []

	for record in root.allrecords():
		if record.offset == -1:
			new_records.append(record)
		else:
			existing_records.append(record)

	# try to build new allocations in a way that needs a minimal amount of reads/writes
	allocations = []
	new_records.sort(key=lambda r: (r.compressed_size, r.filename),reverse=True)
	arch_size = 0
	for record in existing_records:
		size = record.alloc_size
		offset = record.offset
		if offset > arch_size:
			# find new records that fit the hole in order to reduce shifts
			# but never cause a shift torwards the end of the file
			# this is done so the rewriting/shifting code below is simpler
			i = 0
			while i < len(new_records) and arch_size < offset:
				new_record = new_records[i]
				new_size = new_record.alloc_size
				if arch_size + new_size <= offset:
					allocations.append((arch_size, new_record))
					del new_records[i]
					arch_size += new_size
				else:
					i += 1

		allocations.append((arch_size, record))
		arch_size += size

	# add remaining records at the end
	new_records.sort(key=lambda r: r.filename)
	for record in new_records:
		allocations.append((arch_size,record))
		arch_size += record.alloc_size

	index_offset = arch_size
	for offset, record in allocations:
		arch_size += record.index_size

	footer_offset = arch_size
	arch_size += 44

	current_size = os.fstat(stream.fileno()).st_size
	diff_size = arch_size - current_size
	# minimize chance of corrupting archive
	if diff_size > 0 and hasattr(os,'statvfs'):
		st = os.statvfs(stream.name)
		free = st.f_frsize * st.f_bfree
		if free - diff_size < DEFAULT_BUFFER_SIZE:
			raise ValueError("filesystem not big enough")

	index_records = []
	for offset, record in reversed(allocations):
		if record.offset == -1:
			# new record
			filename = record.filename
			callback("+" + filename)
			fh: io.BufferedReader
			with open(filename, "rb") as fh: # type: ignore
				record_bytes = write_record(stream, fh, record.compression_method, record.encrypted, record.compression_block_size or 0)
		elif offset != record.offset:
			assert offset > record.offset
			callback(" "+filename)
			fshift(stream, record.offset, offset, record.alloc_size)
			stream.seek(offset, 0)
			record_bytes = stream.read(record.header_size)
		index_records.append((filename, record_bytes))

	write_index(stream,pak.version,mount_point,index_records,encoding)

	if diff_size < 0:
		stream.truncate(arch_size)

def fshift(stream: io.BufferedRandom, src: int, dst: int, size: int) -> None:
	assert src < dst
	buf_size = DEFAULT_BUFFER_SIZE
	buf      = bytearray(buf_size)

	while size > 0:
		data: Union[bytes, bytearray]
		if size >= buf_size:
			stream.seek(src + size - buf_size, 0)
			stream.readinto(buf)
			data = buf
			size -= buf_size
		else:
			stream.seek(src, 0)
			data = stream.read(size) or b''
			size = 0

		stream.seek(dst + size, 0)
		stream.write(data)

def shall_unpack(paths: Iterable[str], name: str) -> bool:
	path = name.split(os.path.sep)
	for i in range(1, len(path) + 1):
		prefix = os.path.join(*path[0:i])
		if prefix in paths:
			return True
	return False

def human_size(size: int) -> str:
	if size < 2 ** 10:
		return str(size)

	elif size < 2 ** 20:
		str_size = "%.1f" % (size / 2 ** 10)
		unit = "K"

	elif size < 2 ** 30:
		str_size = "%.1f" % (size / 2 ** 20)
		unit = "M"

	elif size < 2 ** 40:
		str_size = "%.1f" % (size / 2 ** 30)
		unit = "G"

	elif size < 2 ** 50:
		str_size = "%.1f" % (size / 2 ** 40)
		unit = "T"

	elif size < 2 ** 60:
		str_size = "%.1f" % (size / 2 ** 50)
		unit = "P"

	elif size < 2 ** 70:
		str_size = "%.1f" % (size / 2 ** 60)
		unit = "E"

	elif size < 2 ** 80:
		str_size = "%.1f" % (size / 2 ** 70)
		unit = "Z"

	else:
		str_size = "%.1f" % (size / 2 ** 80)
		unit = "Y"

	if str_size.endswith(".0"):
		str_size = str_size[:-2]

	return str_size + unit

SORT_ALIASES: Dict[str, str] = {
	"s": "size",
	"S": "-size",
	"z": "zsize",
	"Z": "-zsize",
	"o": "offset",
	"O": "-offset",
	"n": "name"
}

KEY_FUNCS: Dict[str, Callable[[Record], Union[str, int]]] = {
	"size":  lambda rec: rec.uncompressed_size,
	"-size": lambda rec: -rec.uncompressed_size,

	"zsize":  lambda rec: rec.compressed_size,
	"-zsize": lambda rec: -rec.compressed_size,

	"offset":  lambda rec: rec.offset,
	"-offset": lambda rec: -rec.offset,

	"name": lambda rec: rec.filename.lower(),
}

def sort_key_func(sort: str) -> Callable[[Record], Tuple[Union[str, int], ...]]:
	key_funcs = []
	for key in sort.split(","):
		key = SORT_ALIASES.get(key,key)
		try:
			func = KEY_FUNCS[key]
		except KeyError:
			raise ValueError("unknown sort key: "+key)
		key_funcs.append(func)

	return lambda rec: tuple(key_func(rec) for key_func in key_funcs)

class Entry(object):
	__slots__ = 'inode', '_parent', 'stat', '__weakref__'

	inode: int
	_parent: Optional[weakref.ref[Dir]]
	stat: Optional[os.stat_result]

	def __init__(self, inode: int, parent: Optional[Dir] = None) -> None:
		self.inode  = inode
		self.parent = parent
		self.stat   = None

	@property
	def parent(self) -> Optional[Dir]:
		return self._parent() if self._parent is not None else None

	@parent.setter
	def parent(self, parent: Optional[Dir]) -> None:
		self._parent = weakref.ref(parent) if parent is not None else None

class Dir(Entry):
	__slots__ = 'children',

	children: OrderedDict[bytes, Union[Dir, File]]

	def __init__(self, inode: int, children: Optional[OrderedDict[bytes, Union[Dir, File]]] = None, parent: Optional[Dir] = None) -> None:
		Entry.__init__(self,inode,parent)
		if children is None:
			self.children = OrderedDict()
		else:
			self.children = children
			for child in children.values():
				child.parent = self

	def __repr__(self) -> str:
		return 'Dir(%r, %r)' % (self.inode, self.children)

	def allrecords(self) -> Iterable[Record]:
		for child in self.children.values():
			if isinstance(child, Dir):
				for record in child.allrecords():
					yield record
			else:
				yield child.record

class File(Entry):
	__slots__ = 'record',

	record: Record

	def __init__(self, inode: int, record: Record, parent: Optional[Dir] = None) -> None:
		Entry.__init__(self, inode, parent)
		self.record = record

	def __repr__(self) -> str:
		return 'File(%r, %r)' % (self.inode, self.record)

if HAS_LLFUSE:
	import errno
	# import weakref (already imported)
	# import stat (already imported)
	# import mmap (already imported)

	DIR_SELF   = '.'.encode(sys.getfilesystemencoding())
	DIR_PARENT = '..'.encode(sys.getfilesystemencoding())

	class Operations(llfuse.Operations):
		__slots__ = 'archive', 'root', 'inodes', 'arch_st', 'data'

		archive: io.BufferedReader
		inodes: Dict[int, Union[Dir, File]]
		root: Dir
		arch_st: os.stat_result
		data: mmap.mmap

		def __init__(self, archive: io.BufferedReader, pak: Pak) -> None:
			llfuse.Operations.__init__(self)
			self.archive = archive
			self.arch_st = os.fstat(archive.fileno())
			self.root    = Dir(llfuse.ROOT_INODE)
			self.inodes  = {self.root.inode: self.root}
			self.root.parent = self.root

			encoding = sys.getfilesystemencoding()
			inode = self.root.inode + 1
			for record in pak:
				path = record.filename.split(os.path.sep)
				path, name = path[:-1], path[-1]
				enc_name = name.encode(encoding)
				name, ext = os.path.splitext(name)

				parent = self.root
				for i, comp in enumerate(path):
					comp_encoded = comp.encode(encoding)
					try:
						entry = parent.children[comp_encoded]
					except KeyError:
						entry = parent.children[comp_encoded] = self.inodes[inode] = Dir(inode, parent=parent)
						inode += 1

					if not isinstance(entry, Dir):
						raise ValueError("name conflict in archive: %r is not a directory" % os.path.join(*path[:i+1]))

					parent = entry

				i = 0
				while enc_name in parent.children:
					sys.stderr.write("Warning: doubled name in archive: %s\n" % record.filename)
					i += 1
					enc_name = ("%s~%d%s" % (name, i, ext)).encode(encoding)

				parent.children[enc_name] = self.inodes[inode] = File(inode, record, parent)
				inode += 1

			archive.seek(0, 0)
			self.data = mmap.mmap(archive.fileno(), 0, access=mmap.ACCESS_READ)

			# cache entry attributes
			for inode in self.inodes:
				entry = self.inodes[inode]
				entry.stat = self._getattr(entry)

		def destroy(self) -> None:
			self.data.close()
			self.archive.close()

		def lookup(self, parent_inode: int, name: bytes, ctx) -> os.stat_result:
			try:
				entry = self.inodes[parent_inode]
				if name == DIR_SELF:
					pass

				elif name == DIR_PARENT:
					parent = entry.parent
					if parent is not None:
						entry = parent

				else:
					if not isinstance(entry, Dir):
						raise llfuse.FUSEError(errno.ENOTDIR)

					entry = entry.children[name]

			except KeyError:
				raise llfuse.FUSEError(errno.ENOENT)
			else:
				stat = entry.stat
				assert stat is not None
				return stat

		def _getattr(self, entry: Union[Dir, File]) -> llfuse.EntryAttributes:
			attrs = llfuse.EntryAttributes()

			attrs.st_ino        = entry.inode
			attrs.st_rdev       = 0
			attrs.generation    = 0
			attrs.entry_timeout = 300
			attrs.attr_timeout  = 300

			if isinstance(entry, Dir):
				nlink = 2 if entry is not self.root else 1
				size  = 5

				for name, child in entry.children.items():
					size += len(name) + 1
					if type(child) is Dir:
						nlink += 1

				attrs.st_mode  = stat.S_IFDIR | 0o555
				attrs.st_nlink = nlink
				attrs.st_size  = size
			else:
				attrs.st_nlink = 1
				attrs.st_mode  = stat.S_IFREG | 0o444
				attrs.st_size  = entry.record.uncompressed_size

			arch_st = self.arch_st
			attrs.st_uid     = arch_st.st_uid
			attrs.st_gid     = arch_st.st_gid
			attrs.st_blksize = arch_st.st_blksize
			attrs.st_blocks  = 1 + ((attrs.st_size - 1) // attrs.st_blksize) if attrs.st_size != 0 else 0
			if HAS_STAT_NS:
				attrs.st_atime_ns = arch_st.st_atime_ns
				attrs.st_mtime_ns = arch_st.st_mtime_ns
				attrs.st_ctime_ns = arch_st.st_ctime_ns
			else:
				attrs.st_atime_ns = int(arch_st.st_atime * 1000)
				attrs.st_mtime_ns = int(arch_st.st_mtime * 1000)
				attrs.st_ctime_ns = int(arch_st.st_ctime * 1000)

			return attrs

		def getattr(self, inode: int, ctx) -> os.stat_result:
			try:
				entry = self.inodes[inode]
			except KeyError:
				raise llfuse.FUSEError(errno.ENOENT)
			else:
				stat = entry.stat
				assert stat is not None
				return stat

		def getxattr(self, inode: int, name: bytes, ctx) -> bytes:
			try:
				entry = self.inodes[inode]
			except KeyError:
				raise llfuse.FUSEError(errno.ENOENT)
			else:
				if not isinstance(entry, File):
					raise llfuse.FUSEError(errno.ENODATA)

				if name == b'user.u4pak.sha1':
					return hexlify(entry.record.sha1)

				elif name == b'user.u4pak.compressed_size':
					return str(entry.record.compressed_size).encode('ascii')

				elif name == b'user.u4pak.compression_method':
					return COMPR_METHOD_NAMES[entry.record.compression_method].encode('ascii')

				elif name == b'user.u4pak.compression_block_size':
					return str(entry.record.compression_block_size).encode('ascii')

				elif name == b'user.u4pak.encrypted':
					return str(entry.record.encrypted).encode('ascii')

				else:
					raise llfuse.FUSEError(errno.ENODATA)

		def listxattr(self, inode: int, ctx) -> List[bytes]:
			try:
				entry = self.inodes[inode]
			except KeyError:
				raise llfuse.FUSEError(errno.ENOENT)
			else:
				if type(entry) is Dir:
					return []

				else:
					return [b'user.u4pak.sha1', b'user.u4pak.compressed_size',
							b'user.u4pak.compression_method', b'user.u4pak.compression_block_size',
							b'user.u4pak.encrypted']

		def access(self, inode: int, mode: int, ctx) -> bool:
			try:
				entry = self.inodes[inode]
			except KeyError:
				raise llfuse.FUSEError(errno.ENOENT)
			else:
				st_mode = 0o555 if type(entry) is Dir else 0o444
				return (st_mode & mode) == mode

		def opendir(self, inode: int, ctx):
			try:
				entry = self.inodes[inode]
			except KeyError:
				raise llfuse.FUSEError(errno.ENOENT)
			else:
				if type(entry) is not Dir:
					raise llfuse.FUSEError(errno.ENOTDIR)

				return inode

		def readdir(self, inode: int, offset: int) -> Iterable[Tuple[bytes, os.stat_result, int]]:
			try:
				entry = self.inodes[inode]
			except KeyError:
				raise llfuse.FUSEError(errno.ENOENT)
			else:
				if not isinstance(entry, Dir):
					raise llfuse.FUSEError(errno.ENOTDIR)

				names = list(entry.children)[offset:] if offset > 0 else entry.children
				for name in names:
					child = entry.children[name]
					stat = child.stat
					assert stat is not None
					yield name, stat, child.inode

		def releasedir(self, fh: int) -> None:
			pass

		def statfs(self, ctx) -> os.stat_result:
			attrs = llfuse.StatvfsData()

			arch_st = self.arch_st
			attrs.f_bsize  = arch_st.st_blksize
			attrs.f_frsize = arch_st.st_blksize
			attrs.f_blocks = arch_st.st_blocks
			attrs.f_bfree  = 0
			attrs.f_bavail = 0

			attrs.f_files  = len(self.inodes)
			attrs.f_ffree  = 0
			attrs.f_favail = 0

			return attrs

		def open(self, inode: int, flags: int, ctx) -> int:
			try:
				entry = self.inodes[inode]
			except KeyError:
				raise llfuse.FUSEError(errno.ENOENT)
			else:
				if type(entry) is Dir:
					raise llfuse.FUSEError(errno.EISDIR)

				if flags & 3 != os.O_RDONLY:
					raise llfuse.FUSEError(errno.EACCES)

				return inode

		def read(self, fh: int, offset: int, length: int) -> bytes:
			try:
				entry = self.inodes[fh]
			except KeyError:
				raise llfuse.FUSEError(errno.ENOENT)

			if not isinstance(entry, File):
				raise llfuse.FUSEError(errno.EISDIR)

			try:
				return entry.record.read(self.data, offset, length)
			except NotImplementedError:
				raise llfuse.FUSEError(errno.ENOSYS)

		def release(self, fh):
			pass

# based on http://code.activestate.com/recipes/66012/
def deamonize(stdout: str = '/dev/null', stderr: Optional[str] = None, stdin: str = '/dev/null') -> None:
	# Do first fork.
	try:
		pid = os.fork()
		if pid > 0:
			sys.exit(0) # Exit first parent.
	except OSError as e:
		sys.stderr.write("fork #1 failed: (%d) %s\n" % (e.errno, e.strerror))
		sys.exit(1)

	# Decouple from parent environment.
	os.chdir("/")
	os.umask(0)
	os.setsid()

	# Do second fork.
	try:
		pid = os.fork()
		if pid > 0:
			sys.exit(0) # Exit second parent.
	except OSError as e:
		sys.stderr.write("fork #2 failed: (%d) %s\n" % (e.errno, e.strerror))
		sys.exit(1)

	# Open file descriptors
	if not stderr:
		stderr = stdout

	si = open(stdin, 'r')
	so = open(stdout, 'a+')
	se = open(stderr, 'a+')

	# Redirect standard file descriptors.
	sys.stdout.flush()
	sys.stderr.flush()

	os.close(sys.stdin.fileno())
	os.close(sys.stdout.fileno())
	os.close(sys.stderr.fileno())

	os.dup2(si.fileno(), sys.stdin.fileno())
	os.dup2(so.fileno(), sys.stdout.fileno())
	os.dup2(se.fileno(), sys.stderr.fileno())


# ==============================================================================
# == KODE GUI (PySide6) ==
# ==============================================================================

class Worker(QObject):
    """
    Worker untuk menjalankan tugas berat di thread terpisah agar GUI tidak macet.
    """
    finished = Signal()
    progress = Signal(int, int) # saat ini, total
    log = Signal(str)
    error = Signal(str)
    pak_loaded = Signal(object)
    info_ready = Signal(str)
    test_results = Signal(list)

    def __init__(self):
        super().__init__()
        self.pak_file = None
        self.pak_obj = None

    def _log_and_progress(self, current_task, total_tasks, current_file):
        """Helper untuk memancarkan sinyal log dan progress."""
        self.progress.emit(current_task, total_tasks)
        self.log.emit(f"[{current_task}/{total_tasks}] Processing: {current_file}")

    @staticmethod
    def _collect_files(paths: List[str]) -> List[str]:
        """Mengumpulkan semua file dari daftar path (bisa file atau direktori)."""
        all_files = []
        for path in paths:
            if os.path.isdir(path):
                for dirpath, _, filenames in os.walk(path):
                    for filename in filenames:
                        full_path = os.path.join(dirpath, filename)
                        # Buat path relatif terhadap direktori input
                        rel_path = os.path.relpath(full_path, os.path.dirname(path))
                        all_files.append((full_path, rel_path))
            elif os.path.isfile(path):
                all_files.append((path, os.path.basename(path)))
        return all_files

    def load_pak(self, pak_file, check_integrity=False, ignore_nulls=False):
        """Memuat file .pak dan memancarkan objek pak."""
        self.pak_file = pak_file
        try:
            with open(self.pak_file, "rb") as stream:
                self.pak_obj = read_index(stream, check_integrity, ignore_null_checksums=ignore_nulls)
                self.log.emit(f"Successfully loaded '{os.path.basename(pak_file)}'. Found {len(self.pak_obj.records)} files.")
                self.pak_loaded.emit(self.pak_obj)
        except Exception as e:
            self.error.emit(f"Failed to load .pak file:\n{traceback.format_exc()}")
        finally:
            self.finished.emit()

    def get_info(self, human_readable=False):
        """Mendapatkan informasi arsip dan menyiapkannya untuk ditampilkan."""
        if not self.pak_obj:
            self.error.emit("No .pak file loaded.")
            self.finished.emit()
            return
        try:
            # Menggunakan StringIO untuk menangkap output dari print_info
            output_stream = io.StringIO()
            self.pak_obj.print_info(human=human_readable, out=output_stream)
            self.info_ready.emit(output_stream.getvalue())
        except Exception as e:
            self.error.emit(f"Failed to get archive info:\n{traceback.format_exc()}")
        finally:
            self.finished.emit()

    def unpack_files(self, out_dir, selected_files=None):
        """Mengekstrak file (semua atau yang dipilih)."""
        if not self.pak_obj or not self.pak_file:
            self.error.emit("No .pak file loaded.")
            self.finished.emit()
            return

        try:
            self.log.emit(f"Starting unpack to '{out_dir}'...")
            
            files_to_unpack = self.pak_obj.records
            if selected_files:
                files_to_unpack = [rec for rec in self.pak_obj.records if rec.filename in selected_files]
            
            total_files = len(files_to_unpack)
            
            with open(self.pak_file, "rb") as stream:
                for i, record in enumerate(files_to_unpack):
                    self.progress.emit(i + 1, total_files)
                    self.log.emit(f"Unpacking [{i+1}/{total_files}]: {record.filename}")
                    record.unpack(stream, out_dir)

            self.log.emit(f"Unpack finished successfully. {total_files} file(s) extracted.")
        except Exception as e:
            self.error.emit(f"Unpacking failed:\n{traceback.format_exc()}")
        finally:
            self.finished.emit()
            
    def pack_files(self, files_to_pack, output_pak, mount_point, version, use_zlib):
        """Membuat arsip .pak dari file/folder."""
        try:
            self.log.emit(f"Starting to pack files into '{output_pak}'...")

            all_files = []
            for item_path in files_to_pack:
                if os.path.isdir(item_path):
                    for dirpath, _, filenames in os.walk(item_path):
                        for filename in filenames:
                            all_files.append(os.path.join(dirpath, filename))
                else:
                    all_files.append(item_path)

            total_files = len(all_files)
            if total_files == 0:
                self.error.emit("No files found to pack.")
                self.finished.emit()
                return

            processed_count = 0

            def pack_progress_callback(name, files_list):
                nonlocal processed_count
                processed_count += 1
                self.progress.emit(processed_count, len(files_list))
                self.log.emit(f"Packing [{processed_count}/{len(files_list)}]: {name}")

            comp_method = COMPR_ZLIB if use_zlib else COMPR_NONE
            
            with open(output_pak, "wb") as wstream:
                pack(wstream, files_to_pack, mount_point, version, comp_method,
                     callback=pack_progress_callback)

            self.log.emit(f"Packing finished successfully. Created '{output_pak}'.")
        except Exception as e:
            self.error.emit(f"Packing failed:\n{traceback.format_exc()}")
        finally:
            self.finished.emit()

    def test_integrity(self, ignore_nulls=False):
        """Menjalankan pemeriksaan integritas pada file .pak."""
        if not self.pak_obj or not self.pak_file:
            self.error.emit("No .pak file loaded.")
            self.finished.emit()
            return
            
        try:
            self.log.emit("Starting integrity test...")
            errors = []

            def check_callback(ctx, message):
                if isinstance(ctx, Record):
                    errors.append(f"ERROR for {ctx.filename}: {message}")
                else:
                    errors.append(f"GENERAL ERROR: {message}")

            with open(self.pak_file, "rb") as stream:
                self.pak_obj.check_integrity(stream, check_callback, ignore_nulls)
            
            if not errors:
                self.log.emit("Integrity test finished. All ok.")
            else:
                self.log.emit(f"Integrity test finished. Found {len(errors)} error(s).")
            
            self.test_results.emit(errors)

        except Exception as e:
            self.error.emit(f"Integrity test failed:\n{traceback.format_exc()}")
        finally:
            self.finished.emit()

class U4PakGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Unreal Engine 4 .pak Tool")
        self.setGeometry(100, 100, 900, 700)

        self.pak_obj = None
        self.current_pak_file = ""

        # Setup worker thread
        self.thread = QThread()
        self.worker = Worker()
        self.worker.moveToThread(self.thread)
        self.thread.start()

        # --- UI Elements ---
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        # Membuat setiap tab
        self.create_unpack_tab()
        self.create_pack_tab()
        self.create_test_tab()
        
        # --- Koneksi Sinyal ---
        self.worker.log.connect(self.log_message)
        self.worker.error.connect(self.show_error_message)
        self.worker.progress.connect(self.update_progress)
        self.worker.pak_loaded.connect(self.on_pak_loaded)
        self.worker.info_ready.connect(self.display_info)
        self.worker.test_results.connect(self.display_test_results)
        self.worker.finished.connect(self.on_task_finished)

    def create_unpack_tab(self):
        """Membuat UI untuk tab 'Info & Unpack'."""
        tab_widget = QWidget()
        main_layout = QVBoxLayout(tab_widget)
        
        # --- Bagian Input File ---
        input_layout = QHBoxLayout()
        self.pak_path_edit = QLineEdit()
        self.pak_path_edit.setPlaceholderText("Path to .pak file")
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(lambda: self.browse_pak_file())
        input_layout.addWidget(QLabel("PAK File:"))
        input_layout.addWidget(self.pak_path_edit)
        input_layout.addWidget(browse_btn)
        main_layout.addLayout(input_layout)

        # --- Bagian Tombol Aksi ---
        action_layout = QHBoxLayout()
        self.load_btn = QPushButton("Load & List Contents")
        self.info_btn = QPushButton("Show Archive Info")
        self.load_btn.clicked.connect(self.load_pak_and_list)
        self.info_btn.clicked.connect(self.get_archive_info)
        action_layout.addWidget(self.load_btn)
        action_layout.addWidget(self.info_btn)
        action_layout.addStretch()
        main_layout.addLayout(action_layout)

        # --- Tampilan Tree untuk Konten ---
        self.file_tree = QTreeView()
        self.file_tree_model = QStandardItemModel()
        self.file_tree_model.setHorizontalHeaderLabels(['Filename', 'Size', 'Compressed Size', 'Compression', 'SHA1'])
        self.file_tree.setModel(self.file_tree_model)
        self.file_tree.setSortingEnabled(True)
        self.file_tree.header().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.file_tree.header().setStretchLastSection(False)
        self.file_tree.setSelectionMode(QTreeView.ExtendedSelection)
        main_layout.addWidget(self.file_tree)

        # --- Bagian Unpack ---
        unpack_layout = QHBoxLayout()
        self.unpack_dir_edit = QLineEdit()
        self.unpack_dir_edit.setPlaceholderText("Output directory for unpacking")
        browse_unpack_dir_btn = QPushButton("Browse...")
        browse_unpack_dir_btn.clicked.connect(self.browse_unpack_dir)
        unpack_layout.addWidget(QLabel("Unpack to:"))
        unpack_layout.addWidget(self.unpack_dir_edit)
        unpack_layout.addWidget(browse_unpack_dir_btn)
        
        self.unpack_selected_btn = QPushButton("Unpack Selected")
        self.unpack_all_btn = QPushButton("Unpack All")
        self.unpack_selected_btn.clicked.connect(lambda: self.unpack_files(selected=True))
        self.unpack_all_btn.clicked.connect(lambda: self.unpack_files(selected=False))
        
        unpack_btn_layout = QHBoxLayout()
        unpack_btn_layout.addStretch()
        unpack_btn_layout.addWidget(self.unpack_selected_btn)
        unpack_btn_layout.addWidget(self.unpack_all_btn)

        # --- Log dan Progress ---
        self.log_area_unpack = QTextEdit()
        self.log_area_unpack.setReadOnly(True)
        self.progress_bar_unpack = QProgressBar()
        
        main_layout.addLayout(unpack_layout)
        main_layout.addLayout(unpack_btn_layout)
        main_layout.addWidget(QLabel("Log:"))
        main_layout.addWidget(self.log_area_unpack)
        main_layout.addWidget(self.progress_bar_unpack)

        # Nonaktifkan tombol sampai file dimuat
        self.info_btn.setEnabled(False)
        self.unpack_selected_btn.setEnabled(False)
        self.unpack_all_btn.setEnabled(False)
        
        self.tabs.addTab(tab_widget, "Info & Unpack")

    def create_pack_tab(self):
        """Membuat UI untuk tab 'Pack'."""
        tab_widget = QWidget()
        main_layout = QVBoxLayout(tab_widget)

        # --- Daftar File untuk di-Pack ---
        main_layout.addWidget(QLabel("Files and Directories to Pack:"))
        self.pack_file_list = QListWidget()
        pack_list_buttons = QHBoxLayout()
        add_files_btn = QPushButton("Add Files...")
        add_dir_btn = QPushButton("Add Directory...")
        remove_item_btn = QPushButton("Remove Selected")
        clear_list_btn = QPushButton("Clear List")
        
        add_files_btn.clicked.connect(self.add_files_to_pack)
        add_dir_btn.clicked.connect(self.add_dir_to_pack)
        remove_item_btn.clicked.connect(lambda: [self.pack_file_list.takeItem(i.row()) for i in self.pack_file_list.selectedIndexes()])
        clear_list_btn.clicked.connect(self.pack_file_list.clear)

        pack_list_buttons.addWidget(add_files_btn)
        pack_list_buttons.addWidget(add_dir_btn)
        pack_list_buttons.addStretch()
        pack_list_buttons.addWidget(remove_item_btn)
        pack_list_buttons.addWidget(clear_list_btn)
        
        main_layout.addWidget(self.pack_file_list)
        main_layout.addLayout(pack_list_buttons)

        # --- Opsi Packing ---
        options_layout = QHBoxLayout()
        main_layout.addWidget(QLabel("Packing Options:"))
        self.pack_version_combo = QComboBox()
        self.pack_version_combo.addItems(["3", "2", "1"])
        self.use_zlib_check = QCheckBox("Use Zlib Compression")
        self.mount_point_edit = QLineEdit("../../../")
        options_layout.addWidget(QLabel("Version:"))
        options_layout.addWidget(self.pack_version_combo)
        options_layout.addWidget(self.use_zlib_check)
        options_layout.addWidget(QLabel("Mount Point:"))
        options_layout.addWidget(self.mount_point_edit)
        main_layout.addLayout(options_layout)

        # --- Output File ---
        output_layout = QHBoxLayout()
        self.pack_output_edit = QLineEdit()
        self.pack_output_edit.setPlaceholderText("Path for new .pak archive")
        browse_output_btn = QPushButton("Browse...")
        browse_output_btn.clicked.connect(self.browse_pack_output)
        output_layout.addWidget(QLabel("Output file:"))
        output_layout.addWidget(self.pack_output_edit)
        output_layout.addWidget(browse_output_btn)
        main_layout.addLayout(output_layout)

        self.pack_btn = QPushButton("Start Packing")
        self.pack_btn.clicked.connect(self.start_packing)
        main_layout.addWidget(self.pack_btn)
        
        # --- Log dan Progress ---
        self.log_area_pack = QTextEdit()
        self.log_area_pack.setReadOnly(True)
        self.progress_bar_pack = QProgressBar()

        main_layout.addStretch()
        main_layout.addWidget(QLabel("Log:"))
        main_layout.addWidget(self.log_area_pack)
        main_layout.addWidget(self.progress_bar_pack)

        self.tabs.addTab(tab_widget, "Pack")

    def create_test_tab(self):
        """Membuat UI untuk tab 'Test'."""
        tab_widget = QWidget()
        main_layout = QVBoxLayout(tab_widget)
        
        main_layout.addWidget(QLabel(
            "This tab tests the integrity of a .pak archive by verifying all checksums.\n"
            "Select a .pak file and click 'Run Test'."
        ))
        
        # --- Input File ---
        input_layout = QHBoxLayout()
        self.test_pak_path_edit = QLineEdit()
        self.test_pak_path_edit.setPlaceholderText("Path to .pak file")
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(lambda: self.browse_pak_file(target_edit=self.test_pak_path_edit))
        input_layout.addWidget(QLabel("PAK File:"))
        input_layout.addWidget(self.test_pak_path_edit)
        input_layout.addWidget(browse_btn)
        main_layout.addLayout(input_layout)

        # --- Opsi ---
        self.test_ignore_nulls_check = QCheckBox("Ignore checksums that are all nulls")
        main_layout.addWidget(self.test_ignore_nulls_check)

        self.test_btn = QPushButton("Run Integrity Test")
        self.test_btn.clicked.connect(self.run_integrity_test)
        main_layout.addWidget(self.test_btn)
        
        # --- Hasil ---
        self.test_results_area = QTextEdit()
        self.test_results_area.setReadOnly(True)
        
        main_layout.addStretch()
        main_layout.addWidget(QLabel("Log & Results:"))
        main_layout.addWidget(self.test_results_area)

        self.tabs.addTab(tab_widget, "Test")

    # --- Metode Logika dan Slot ---

    def browse_pak_file(self, target_edit=None):
        if target_edit is None:
            target_edit = self.pak_path_edit
        filepath, _ = QFileDialog.getOpenFileName(self, "Select .pak File", "", "PAK Files (*.pak)")
        if filepath:
            target_edit.setText(filepath)

    def browse_unpack_dir(self):
        dirpath = QFileDialog.getExistingDirectory(self, "Select Unpack Directory")
        if dirpath:
            self.unpack_dir_edit.setText(dirpath)
            
    def add_files_to_pack(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Select Files to Pack")
        if files:
            for f in files:
                self.pack_file_list.addItem(QListWidgetItem(f))

    def add_dir_to_pack(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Directory to Pack")
        if directory:
            self.pack_file_list.addItem(QListWidgetItem(directory))
            
    def browse_pack_output(self):
        filepath, _ = QFileDialog.getSaveFileName(self, "Save .pak File", "", "PAK Files (*.pak)")
        if filepath:
            self.pack_output_edit.setText(filepath)

    def set_ui_busy(self, busy):
        """Mengaktifkan/menonaktifkan UI selama operasi."""
        self.tabs.setEnabled(not busy)
        if busy:
            QApplication.setOverrideCursor(Qt.WaitCursor)
        else:
            QApplication.restoreOverrideCursor()

    def on_task_finished(self):
        """Dipanggil ketika worker selesai."""
        self.set_ui_busy(False)

    def log_message(self, msg):
        """Menampilkan pesan log di tab yang aktif."""
        current_tab_index = self.tabs.currentIndex()
        if current_tab_index == 0:
            self.log_area_unpack.append(msg)
        elif current_tab_index == 1:
            self.log_area_pack.append(msg)
        elif current_tab_index == 2:
            self.test_results_area.append(msg)

    def show_error_message(self, err_msg):
        """Menampilkan dialog pesan error."""
        self.log_message(f"ERROR: {err_msg}")
        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Critical)
        msg_box.setText("An Error Occurred")
        msg_box.setInformativeText(err_msg)
        msg_box.setWindowTitle("Error")
        msg_box.exec()

    def update_progress(self, value, total):
        """Memperbarui progress bar di tab yang aktif."""
        current_tab_index = self.tabs.currentIndex()
        progress_bar = None
        if current_tab_index == 0:
            progress_bar = self.progress_bar_unpack
        elif current_tab_index == 1:
            progress_bar = self.progress_bar_pack
        
        if progress_bar:
            progress_bar.setMaximum(total)
            progress_bar.setValue(value)

    def on_pak_loaded(self, pak_obj):
        """Mengisi tree view setelah .pak dimuat."""
        self.pak_obj = pak_obj
        self.file_tree_model.clear()
        self.file_tree_model.setHorizontalHeaderLabels(['Filename', 'Size', 'Compressed Size', 'Compression', 'SHA1'])

        path_map = {}
        root_item = self.file_tree_model.invisibleRootItem()

        for record in sorted(self.pak_obj.records, key=lambda r: r.filename):
            path_components = record.filename.split(os.path.sep)
            current_parent = root_item
            
            # Membuat path folder
            for i in range(len(path_components) - 1):
                dir_path = os.path.sep.join(path_components[:i+1])
                if dir_path not in path_map:
                    dir_item = QStandardItem(path_components[i])
                    dir_item.setEditable(False)
                    current_parent.appendRow(dir_item)
                    path_map[dir_path] = dir_item
                    current_parent = dir_item
                else:
                    current_parent = path_map[dir_path]

            # Menambahkan item file
            name_item = QStandardItem(path_components[-1])
            name_item.setEditable(False)
            name_item.setData(record.filename, Qt.UserRole) # Simpan path lengkap

            size_item = QStandardItem(human_size(record.uncompressed_size))
            csize_item = QStandardItem(human_size(record.compressed_size))
            comp_item = QStandardItem(COMPR_METHOD_NAMES.get(record.compression_method, 'unknown'))
            sha1_item = QStandardItem(hexlify(record.sha1).decode('latin1'))

            for item in [size_item, csize_item, comp_item, sha1_item]:
                item.setEditable(False)

            current_parent.appendRow([name_item, size_item, csize_item, comp_item, sha1_item])
        
        self.file_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        # Aktifkan tombol yang relevan
        self.info_btn.setEnabled(True)
        self.unpack_selected_btn.setEnabled(True)
        self.unpack_all_btn.setEnabled(True)
        
    def load_pak_and_list(self):
        pak_file = self.pak_path_edit.text()
        if not os.path.exists(pak_file):
            self.show_error_message(f"File not found: {pak_file}")
            return
        
        self.current_pak_file = pak_file
        self.log_area_unpack.clear()
        self.file_tree_model.clear()
        self.set_ui_busy(True)
        
        # Jalankan di thread
        self.worker.pak_file = pak_file
        self.worker.load_pak(pak_file)

    def get_archive_info(self):
        if not self.pak_obj:
            self.show_error_message("Load a .pak file first.")
            return
        
        self.set_ui_busy(True)
        self.worker.get_info(human_readable=True)
        
    def display_info(self, info_text):
        info_dialog = QMessageBox(self)
        info_dialog.setWindowTitle("Archive Information")
        info_dialog.setText(info_text)
        info_dialog.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info_dialog.exec()

    def unpack_files(self, selected=False):
        out_dir = self.unpack_dir_edit.text()
        if not out_dir:
            self.show_error_message("Please specify an output directory.")
            return
            
        if not self.pak_obj:
            self.show_error_message("Load a .pak file first.")
            return

        selected_files = None
        if selected:
            indexes = self.file_tree.selectionModel().selectedRows(0)
            if not indexes:
                self.show_error_message("No files selected to unpack.")
                return
            selected_files = [idx.data(Qt.UserRole) for idx in indexes if idx.data(Qt.UserRole)]
        
        self.set_ui_busy(True)
        self.worker.unpack_files(out_dir, selected_files)
        
    def start_packing(self):
        output_file = self.pack_output_edit.text()
        if not output_file:
            self.show_error_message("Please specify an output .pak file.")
            return
            
        items_to_pack = [self.pack_file_list.item(i).text() for i in range(self.pack_file_list.count())]
        if not items_to_pack:
            self.show_error_message("No files or directories added to the list.")
            return

        version = int(self.pack_version_combo.currentText())
        mount_point = self.mount_point_edit.text()
        use_zlib = self.use_zlib_check.isChecked()
        
        self.log_area_pack.clear()
        self.set_ui_busy(True)
        self.worker.pack_files(items_to_pack, output_file, mount_point, version, use_zlib)
        
    def run_integrity_test(self):
        pak_file = self.test_pak_path_edit.text()
        if not os.path.exists(pak_file):
            self.show_error_message(f"File not found: {pak_file}")
            return
            
        self.test_results_area.clear()
        self.set_ui_busy(True)
        
        # Memuat pak terlebih dahulu, lalu menjalankan tes
        # Ini bisa dioptimalkan dengan membuat fungsi worker tunggal
        try:
            with open(pak_file, "rb") as stream:
                pak_for_test = read_index(stream)
                self.worker.pak_obj = pak_for_test
                self.worker.pak_file = pak_file
                self.worker.test_integrity(self.test_ignore_nulls_check.isChecked())
        except Exception as e:
            self.show_error_message(f"Failed to load .pak for testing:\n{e}")
            self.set_ui_busy(False)
            
    def display_test_results(self, errors):
        if not errors:
            self.test_results_area.append("\n--- RESULT: All OK ---")
        else:
            self.test_results_area.append("\n--- RESULT: Found errors! ---")
            for error in errors:
                self.test_results_area.append(error)

    def closeEvent(self, event):
        """Memastikan thread worker berhenti saat aplikasi ditutup."""
        self.thread.quit()
        self.thread.wait()
        event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = U4PakGUI()
    window.show()
    sys.exit(app.exec())