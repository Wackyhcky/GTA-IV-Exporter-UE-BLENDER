"""GTA IV RPF (version 2) archive reader, e.g. pc/models/cdimages/playerped.rpf (Niko).

header (at 0): "RPF2", u32 TOC size, u32 entry count, u32 ?, u32 encrypted (non-zero)
TOC (at 0x800, AES like the IMG archives): 16-byte entries, then the name table
    directory: u32 name offset, u32 flags, u32 first entry | 0x80000000, u32 count
    file:      u32 name offset, u32 size in archive, u32 offset (low byte: resource type),
               u32 flags (bit 31: resource, whose data is a complete RSC05 file;
               otherwise bit 30: zlib-compressed, low 30 bits: uncompressed size)
"""

import struct
import zlib

from . import aes

RPF2_MAGIC = b"RPF2"


class RpfArchive:
    def __init__(self, path, key=None):
        self.path = path
        self.entries = {}   # lower-case path inside the archive -> (offset, size, flags)
        self._f = open(path, "rb")
        head = self._f.read(20)
        if head[:4] != RPF2_MAGIC:
            raise RuntimeError("%s is not a GTA IV RPF archive" % path)
        toc_size, count, _unk, encrypted = struct.unpack_from("<4I", head, 4)
        self._f.seek(0x800)
        toc = self._f.read(toc_size)
        if encrypted:
            if key is None:
                raise RuntimeError("%s is encrypted and no key was given" % path)
            toc = aes.decrypt(toc, key)
        names = toc[count * 16:]

        def name(off):
            off &= 0x7FFFFFFF
            end = names.find(b"\0", off)
            return names[off:end if end >= 0 else len(names)].decode("latin-1").lower()

        rows = [struct.unpack_from("<4I", toc, i * 16) for i in range(count)]

        def walk(index, prefix):
            n_off, _flags, first, n = rows[index]
            first &= 0x7FFFFFFF
            for i in range(first, min(first + n, count)):
                r = rows[i]
                nm = name(r[0])
                if r[2] & 0x80000000:
                    walk(i, prefix + nm + "/")
                else:
                    self.entries[prefix + nm] = (r[2] & 0xFFFFFF00, r[1], r[3])

        if rows and rows[0][2] & 0x80000000:
            walk(0, "")

    def names(self):
        return self.entries.keys()

    def read(self, name):
        e = self.entries.get(name.lower())
        if e is None:
            # also find a file by its name alone, wherever it is in the archive
            hits = [k for k in self.entries if k.rsplit("/", 1)[-1] == name.lower()]
            if not hits:
                return None
            e = self.entries[hits[0]]
        offset, size, flags = e
        self._f.seek(offset)
        data = self._f.read(size)
        if not flags & 0x80000000 and flags & 0x40000000:
            data = zlib.decompress(data)
        return data

    def close(self):
        self._f.close()
