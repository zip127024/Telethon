"""
Tests for `telethon.network.authenticator` (against a fake server, with a test
RSA key) and the choice of its scheme by `telethon.network.MTProtoSender`.
"""
import asyncio
import collections
import logging
import os
import struct
import time
from hashlib import sha1, sha256

import pyaes
import pytest

from telethon import helpers
from telethon.crypto import AES, AuthKey, rsa
from telethon.errors import InvalidBufferError
from telethon.extensions import BinaryReader
from telethon.network import MTProtoSender, authenticator
from telethon.tl.functions import ReqPqMultiRequest, ReqDHParamsRequest, SetClientDHParamsRequest
from telethon.tl.types import (
    ResPQ, PQInnerData, PQInnerDataDc, ServerDHParamsOk, ServerDHInnerData,
    ClientDHInnerData, DhGenOk
)

# https://core.telegram.org/mtproto/samples-auth_key
P, Q = 0x494C553B, 0x53911073
# Any 2048-bit modulus works for the exchange itself (the client doesn't check primality)
DH_PRIME = int.from_bytes(sha256(b'dh prime').digest() * 8, 'big') | (1 << 2047) | 1


def _ige_decrypt(data, key, iv):
    """AES-256-IGE decryption from raw AES blocks (independent of telethon.crypto)."""
    aes = pyaes.AES(key)
    previous_cipher, previous_plain = iv[:16], iv[16:]
    out = bytearray()
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        xored = bytes(a ^ b for a, b in zip(block, previous_plain))
        plain = bytes(a ^ b for a, b in zip(bytes(aes.decrypt(list(xored))), previous_cipher))
        out += plain
        previous_cipher, previous_plain = block, plain
    return bytes(out)


def rsa_pad_decrypt(private, encrypted):
    """Inverse of RSA_PAD (https://core.telegram.org/mtproto/auth_key); returns the 192 padded bytes."""
    value = pow(int.from_bytes(encrypted, 'big'), private.d, private.n)
    assert value < private.n
    key_aes_encrypted = value.to_bytes(256, 'big')
    temp_key_xor, aes_encrypted = key_aes_encrypted[:32], key_aes_encrypted[32:]
    temp_key = bytes(a ^ b for a, b in zip(temp_key_xor, sha256(aes_encrypted).digest()))
    data_with_hash = _ige_decrypt(aes_encrypted, temp_key, bytes(32))
    data_pad_reversed, digest = data_with_hash[:192], data_with_hash[192:]
    data_with_padding = data_pad_reversed[::-1]
    assert digest == sha256(temp_key + data_with_padding).digest()
    return data_with_padding


def legacy_decrypt(private, encrypted):
    """Inverse of the legacy scheme: sha1(data) + data + padding."""
    value = pow(int.from_bytes(encrypted, 'big'), private.d, private.n)
    plain = value.to_bytes(255, 'big')
    with BinaryReader(plain[20:]) as reader:
        inner = reader.tgread_object()
    assert plain[:20] == sha1(bytes(inner)).digest()
    return inner


class FakeServer:
    """
    The server side of the auth key generation, standing in for the
    `MTProtoPlainSender` that `do_authentication` sends requests with.
    """
    def __init__(self, fingerprint, private, *, legacy_refused=False):
        self.fingerprint = fingerprint
        self.private = private
        self.legacy_refused = legacy_refused  # like CDN DCs
        self.inner = None
        self.auth_key = None

    async def send(self, request):
        if isinstance(request, ReqPqMultiRequest):
            self.nonce = request.nonce
            self.server_nonce = int.from_bytes(os.urandom(16), 'little', signed=True)
            return ResPQ(self.nonce, self.server_nonce, rsa.get_byte_array(P * Q),
                         [self.fingerprint ^ 1, self.fingerprint])

        assert request.nonce == self.nonce and request.server_nonce == self.server_nonce
        if isinstance(request, ReqDHParamsRequest):
            assert request.public_key_fingerprint == self.fingerprint
            assert (request.p, request.q) == (rsa.get_byte_array(P), rsa.get_byte_array(Q))
            try:
                with BinaryReader(rsa_pad_decrypt(self.private, request.encrypted_data)) as reader:
                    self.inner = reader.tgread_object()
            except AssertionError:
                if self.legacy_refused:
                    raise InvalidBufferError(struct.pack('<i', -404))
                self.inner = legacy_decrypt(self.private, request.encrypted_data)

            assert (self.inner.nonce, self.inner.server_nonce) == (self.nonce, self.server_nonce)
            self.key, self.iv = helpers.generate_key_data_from_nonce(
                self.server_nonce, self.inner.new_nonce)
            self.a = int.from_bytes(os.urandom(256), 'big')
            answer = bytes(ServerDHInnerData(
                self.nonce, self.server_nonce, 3, rsa.get_byte_array(DH_PRIME),
                rsa.get_byte_array(pow(3, self.a, DH_PRIME)), int(time.time())))
            answer = sha1(answer).digest() + answer
            answer += os.urandom(-len(answer) % 16)
            return ServerDHParamsOk(self.nonce, self.server_nonce,
                                    AES.encrypt_ige(answer, self.key, self.iv))

        assert isinstance(request, SetClientDHParamsRequest)
        with BinaryReader(AES.decrypt_ige(request.encrypted_data, self.key, self.iv)[20:]) as reader:
            client_inner = reader.tgread_object()
        assert isinstance(client_inner, ClientDHInnerData)
        g_b = int.from_bytes(client_inner.g_b, 'big')
        self.auth_key = AuthKey(rsa.get_byte_array(pow(g_b, self.a, DH_PRIME)))
        return DhGenOk(self.nonce, self.server_nonce,
                       self.auth_key.calc_new_nonce_hash(self.inner.new_nonce, 1))


def test_rsa_pad_layout(server_rsa_key):
    fingerprint, private = server_rsa_key
    data = os.urandom(96)  # about the size of p_q_inner_data_dc
    encrypted = rsa.encrypt_pad(fingerprint, data)
    assert len(encrypted) == 256
    data_with_padding = rsa_pad_decrypt(private, encrypted)
    assert len(data_with_padding) == 192 and data_with_padding[:96] == data
    # The padding and temp_key are random
    assert rsa.encrypt_pad(fingerprint, data) != encrypted


@pytest.mark.asyncio
@pytest.mark.parametrize('dc_id', [2, -4, 10002, 203])
async def test_authentication_sends_dc(server_rsa_key, dc_id):
    server = FakeServer(*server_rsa_key, legacy_refused=True)
    auth_key, time_offset = await authenticator.do_authentication(server, dc_id=dc_id)
    assert isinstance(server.inner, PQInnerDataDc) and server.inner.dc == dc_id
    assert server.inner.pq == rsa.get_byte_array(P * Q)
    assert auth_key.key == server.auth_key.key
    assert abs(time_offset) <= 1


@pytest.mark.asyncio
async def test_authentication_legacy(server_rsa_key):
    server = FakeServer(*server_rsa_key)
    auth_key, _ = await authenticator.do_authentication(server)
    assert type(server.inner) is PQInnerData
    assert auth_key.key == server.auth_key.key

    with pytest.raises(InvalidBufferError):
        await authenticator.do_authentication(FakeServer(*server_rsa_key, legacy_refused=True))


class FakeConnection:
    def __init__(self, dc_id):
        self._dc_id = dc_id
        self._connected = False
        self.connects = 0
        self._closed = asyncio.Event()

    async def connect(self, timeout=None):
        self._connected = True
        self.connects += 1

    async def disconnect(self):
        self._connected = False

    async def send(self, data):
        pass

    async def recv(self):
        await self._closed.wait()  # nothing ever arrives
        raise ConnectionError()


def _sender():
    loggers = collections.defaultdict(lambda: logging.getLogger('telethon.test'))
    return MTProtoSender(None, loggers=loggers, retries=3, delay=0)


@pytest.mark.asyncio
async def test_sender_passes_auth_dc_id(monkeypatch):
    # The sender sends whatever scheme the client asked for: the legacy one
    # by default, p_q_inner_data_dc when an auth_dc_id is given (CDN DCs).
    calls = []

    async def do_authentication(plain, dc_id=None):
        calls.append(dc_id)
        return AuthKey(os.urandom(256)), 0

    monkeypatch.setattr(authenticator, 'do_authentication', do_authentication)

    sender = _sender()
    await sender.connect(FakeConnection(2))
    await sender.disconnect()

    sender = _sender()
    await sender.connect(FakeConnection(203), auth_dc_id=203)
    await sender.disconnect()
    assert calls == [None, 203]


@pytest.mark.asyncio
async def test_sender_retries_transport_refusal(monkeypatch):
    # A -404 transport error during auth is retried like a dropped connection
    attempts = []

    async def do_authentication(plain, dc_id=None):
        attempts.append(dc_id)
        if len(attempts) == 1:
            raise InvalidBufferError(struct.pack('<i', -404))
        return AuthKey(os.urandom(256)), 0

    monkeypatch.setattr(authenticator, 'do_authentication', do_authentication)
    sender = _sender()
    connection = FakeConnection(203)
    await sender.connect(connection, auth_dc_id=203)
    await sender.disconnect()
    assert sender.auth_key and attempts == [203, 203] and connection.connects == 2
