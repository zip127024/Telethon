"""
Tests for `telethon.crypto.cdndecrypter`.
"""
import hashlib
import os

import pyaes
import pytest

from telethon.crypto import cdndecrypter, CdnDecrypter
from telethon.errors import CdnFileTamperedError
from telethon.tl import types

SLOW = [pytest.param(True, id='pyaes')]
if cdndecrypter.Cipher is not None:
    SLOW.insert(0, pytest.param(False, id='cryptography'))


def _reference_ctr(key, counter_block, data):
    """Textbook CTR: keystream block i is AES(counter + i), with a 128-bit big-endian counter."""
    aes = pyaes.AES(key)
    counter = int.from_bytes(counter_block, 'big')
    out = bytearray()
    for i in range(0, len(data), 16):
        block = ((counter + i // 16) % (1 << 128)).to_bytes(16, 'big')
        stream = bytes(aes.encrypt(list(block)))
        out += bytes(a ^ b for a, b in zip(data[i:i + 16], stream))
    return bytes(out)


def _decrypter(slow, key=None, iv=None, file_hashes=()):
    decrypter = CdnDecrypter(key or os.urandom(32), iv or os.urandom(16), file_hashes)
    decrypter.slow = slow
    return decrypter


def _hashes(data, size):
    return [types.FileHash(offset=i, limit=size, hash=hashlib.sha256(data[i:i + size]).digest())
            for i in range(0, len(data), size)]


@pytest.mark.parametrize('slow', SLOW)
@pytest.mark.parametrize('offset,length', [(0, 100), (16, 33), (4096, 4096), (393232, 1000)])
def test_decrypt_matches_reference(slow, offset, length):
    key = hashlib.sha256(b'cdn key').digest()
    iv = bytes(range(16))
    data = os.urandom(length)
    # https://core.telegram.org/cdn: the last 4 bytes of the IV are replaced by offset / 16
    expected = _reference_ctr(key, iv[:12] + (offset // 16).to_bytes(4, 'big'), data)
    decrypter = _decrypter(slow, key, iv)
    assert decrypter.decrypt(offset, data) == expected
    assert decrypter.decrypt(offset, expected) == data


@pytest.mark.parametrize('slow', SLOW)
def test_decrypt_parts_are_slices_of_one_stream(slow):
    key = os.urandom(32)
    iv = os.urandom(12) + b'\xaa\xbb\xcc\xdd'  # the last 4 bytes are never used
    whole = os.urandom(64 * 1024)
    stream = _decrypter(slow, key, iv[:12] + bytes(4)).decrypt(0, whole)
    decrypter = _decrypter(slow, key, iv)
    for offset in (0, 16, 8192, 32768, 65536 - 48):
        assert decrypter.decrypt(offset, whole[offset:offset + 4096]) == stream[offset:offset + 4096]


def test_decrypt_backends_agree_and_need_aligned_offsets():
    key, iv, data = os.urandom(32), os.urandom(16), os.urandom(5000)
    results = {_decrypter(slow.values[0], key, iv).decrypt(4096, data) for slow in SLOW}
    assert len(results) == 1
    with pytest.raises(ValueError):
        _decrypter(False, key, iv).decrypt(8, data)


def test_get_hash():
    data = os.urandom(3 * 1000 + 10)
    decrypter = _decrypter(False, file_hashes=_hashes(data, 1000)[:2])
    assert decrypter.get_hash(0).offset == 0
    assert decrypter.get_hash(999).offset == 0
    assert decrypter.get_hash(1500).offset == 1000
    assert decrypter.get_hash(2000) is None
    decrypter.add_hashes(_hashes(data, 1000)[2:])
    assert decrypter.get_hash(3005).offset == 3000


def test_verify_whole_ranges_boundaries_and_eof():
    size = 1024
    data = os.urandom(3 * size + 100)
    decrypter = _decrypter(False, file_hashes=_hashes(data, size))

    # Whole ranges are verified
    assert decrypter.verify(0, data[:2 * size], eof=False) == 2 * size
    assert decrypter.verify(size, data[size:3 * size], eof=False) == 2 * size

    # A range cut by the end of the data is left for later, unless it's the end of the file
    assert decrypter.verify(0, data[:size + 100], eof=False) == size
    assert decrypter.verify(0, data[:size - 1], eof=False) == 0
    assert decrypter.verify(3 * size, data[3 * size:], eof=True) == 100
    assert decrypter.verify(0, data, eof=True) == len(data)

    # Any changed byte is noticed, in every position
    for position in (0, size - 1, size, 2 * size + 7, len(data) - 1):
        bad = bytearray(data)
        bad[position] ^= 1
        with pytest.raises(CdnFileTamperedError):
            decrypter.verify(0, bytes(bad), eof=True)

    # The cut part of a tampered range isn't verified yet, the whole range is
    bad = bytearray(data[:2 * size])
    bad[size + 5] ^= 1
    assert decrypter.verify(0, bytes(bad[:size + 10]), eof=False) == size
    with pytest.raises(CdnFileTamperedError):
        decrypter.verify(size, bytes(bad[size:]), eof=False)

    # Data must start where a hashed range does, with a known hash
    with pytest.raises(CdnFileTamperedError):
        decrypter.verify(16, data[16:size], eof=False)
    with pytest.raises(CdnFileTamperedError):
        _decrypter(False).verify(0, data, eof=True)
