"""
This module holds the CdnDecrypter utility class.
"""
from hashlib import sha256

import pyaes

from ..errors import CdnFileTamperedError

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    Cipher = None


def _ctr_cryptography(key, iv, data):
    decryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def _ctr_pyaes(key, iv, data):
    counter = pyaes.Counter(int.from_bytes(iv, 'big'))
    return pyaes.AESModeOfOperationCTR(key, counter=counter).decrypt(data)


class CdnDecrypter:
    """
    Decrypts and verifies the parts of a file that Telegram redirected to
    a CDN data center (``upload.fileCdnRedirect``). https://core.telegram.org/cdn

    The parts are encrypted with AES-256-CTR and, since CDNs are not trusted,
    every part must match the SHA-256 hashes the file's own DC gives out.
    """
    # Pure-Python AES takes seconds per MB, too long for the event loop
    slow = Cipher is None

    def __init__(self, encryption_key, encryption_iv, file_hashes=()):
        """
        :param encryption_key: the ``encryption_key`` of the redirect.
        :param encryption_iv: the ``encryption_iv`` of the redirect.
        :param file_hashes: the :tl:`FileHash` known so far.
        """
        self.key = bytes(encryption_key)
        self.iv = bytes(encryption_iv)
        self.hashes = {}
        self.add_hashes(file_hashes)

    def add_hashes(self, file_hashes):
        """Remembers more :tl:`FileHash` (e.g. ``upload.getCdnFileHashes``)."""
        for file_hash in file_hashes or ():
            self.hashes[file_hash.offset] = file_hash

    def get_hash(self, offset):
        """Returns the :tl:`FileHash` covering ``offset``, or `None`."""
        file_hash = self.hashes.get(offset)
        if file_hash is not None:
            return file_hash
        return next((h for h in self.hashes.values()
                     if h.offset <= offset < h.offset + h.limit), None)

    def decrypt(self, offset, data):
        """
        Decrypts the part of the file starting at ``offset`` (a multiple of
        16) with AES-256-CTR: the IV is ``encryption_iv`` with its last
        4 bytes replaced by the big-endian ``offset / 16``.
        """
        if offset % 16:
            raise ValueError('CDN offset must be a multiple of 16, got {}'.format(offset))
        iv = self.iv[:12] + (offset // 16).to_bytes(4, 'big')
        ctr = _ctr_pyaes if self.slow else _ctr_cryptography
        return ctr(self.key, iv, bytes(data))

    def verify(self, offset, data, eof):
        """
        Checks the hashes of the decrypted ``data`` of the file, which starts
        at ``offset``, the start of a hashed range. All the hashes must be
        known (see `get_hash`).

        Raises `CdnFileTamperedError` on mismatch. Returns how many leading
        bytes were verified: a range ``data`` ends in the middle of can only
        be verified if it is the end of the file (``eof``).
        """
        position = offset
        end = offset + len(data)
        while position < end:
            file_hash = self.hashes.get(position)
            if file_hash is None:
                raise CdnFileTamperedError()  # no hash starts here
            piece = data[position - offset:position - offset + file_hash.limit]
            if len(piece) < file_hash.limit and not eof:
                break
            if sha256(piece).digest() != file_hash.hash:
                raise CdnFileTamperedError()
            position += len(piece)
        return position - offset
