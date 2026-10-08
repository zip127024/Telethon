"""
Tests for `telethon.client.downloads`, in particular files that Telegram
redirects to a CDN DC (https://core.telegram.org/cdn).

Offline: ``client._call`` is answered by a fake file DC and a fake CDN DC,
and MTProto senders never really connect.
"""
import asyncio
import copy
import datetime
import hashlib
import os

import pytest

from telethon import TelegramClient, errors
from telethon.client import downloads, telegrambaseclient
from telethon.crypto import rsa
from telethon.network import (
    MTProtoSender, ConnectionTcpFull, ConnectionTcpIntermediate, ConnectionTcpObfuscated
)
from telethon.sessions import MemorySession
from telethon.tl import functions, types

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    Cipher = None

CDN_DC = 203
HASH_SIZE = 16384
LOCATION = types.InputDocumentFileLocation(id=777, access_hash=1, file_reference=b'ref', thumb_size='')


def _ctr(key, iv, offset, data):
    """AES-256-CTR as described in https://core.telegram.org/cdn."""
    iv = iv[:12] + (offset // 16).to_bytes(4, 'big')
    if Cipher:
        encryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
        return encryptor.update(data) + encryptor.finalize()
    import pyaes
    return pyaes.AESModeOfOperationCTR(key, pyaes.Counter(int.from_bytes(iv, 'big'))).encrypt(data)


class FakeTelegram:
    """The DC of a file, which redirects it to a CDN DC, and that CDN DC."""
    def __init__(self, data, *, cdn=True, initial_hashes=2, hash_size=HASH_SIZE):
        self.data = data
        self.hash_size = hash_size
        self.cdn = cdn
        self.initial_hashes = initial_hashes
        self.key, self.iv = os.urandom(32), os.urandom(16)
        self.token = None
        self.tokens = 0
        self.file_requests = []  # sent to the file's DC
        self.cdn_requests = []  # GetCdnFileRequest sent to the CDN DC
        self.inits = []  # InitConnectionRequest seen by the CDN DC
        self.parts_until_expiry = None  # CDN parts served before the file token expires
        self.reuploads = 0  # how many times the CDN asks to reupload the file
        self.corrupt = 0  # how many CDN parts have a changed byte

    def hashes(self, offset, count=4):
        size = self.hash_size
        start = offset - offset % size
        end = min(len(self.data), start + count * size)
        return [types.FileHash(o, size, hashlib.sha256(self.data[o:o + size]).digest())
                for o in range(start, end, size)]

    def _check_token(self, request):
        if request.file_token != self.token:
            raise errors.BadRequestError(request, 'FILE_TOKEN_INVALID')

    def file_dc(self, request):
        self.file_requests.append(copy.copy(request))  # the iterator reuses its request
        if isinstance(request, functions.upload.GetFileRequest):
            assert request.limit <= 1024 * 1024 and request.offset % request.limit == 0
            if not self.cdn:
                return types.upload.File(types.storage.FileUnknown(), 0,
                                         self.data[request.offset:request.offset + request.limit])
            self.tokens += 1
            self.token = b'token-%d' % self.tokens
            return types.upload.FileCdnRedirect(
                CDN_DC, self.token, self.key, self.iv, self.hashes(0, self.initial_hashes))

        self._check_token(request)
        if isinstance(request, functions.upload.ReuploadCdnFileRequest):
            assert request.request_token == b'reupload-%d' % self.reuploads
            self.reuploads -= 1
            return self.hashes(0)
        if isinstance(request, functions.upload.GetCdnFileHashesRequest):
            return self.hashes(request.offset)
        raise AssertionError(request)

    def cdn_dc(self, request):
        if isinstance(request, functions.InvokeWithLayerRequest):
            assert isinstance(request.query, functions.InitConnectionRequest)
            self.inits.append(request.query)
            request = request.query.query

        assert isinstance(request, functions.upload.GetCdnFileRequest)
        self.cdn_requests.append(copy.copy(request))
        offset, limit = request.offset, request.limit
        # https://core.telegram.org/api/files#downloading-files
        assert offset % 4096 == 0 and limit % 4096 == 0 and (1024 * 1024) % limit == 0
        assert offset // (1024 * 1024) == (offset + limit - 1) // (1024 * 1024)
        self._check_token(request)

        if self.reuploads:
            return types.upload.CdnFileReuploadNeeded(b'reupload-%d' % self.reuploads)

        if self.parts_until_expiry is not None:
            if not self.parts_until_expiry:
                self.parts_until_expiry = None
                self.token = None
                self._check_token(request)
            self.parts_until_expiry -= 1

        encrypted = bytearray(_ctr(self.key, self.iv, offset, self.data[offset:offset + limit]))
        if self.corrupt and encrypted:
            self.corrupt -= 1
            encrypted[len(encrypted) // 2] ^= 1
        return types.upload.CdnFile(bytes(encrypted))


@pytest.fixture
def fake_senders(monkeypatch):
    """MTProtoSender.connect only records how it was called."""
    connects = []

    async def connect(self, connection, *, auth_dc_id=None):
        connects.append((type(connection), auth_dc_id))
        self._user_connected = True
        return True

    async def disconnect(self):
        self._user_connected = False

    monkeypatch.setattr(MTProtoSender, 'connect', connect)
    monkeypatch.setattr(MTProtoSender, 'disconnect', disconnect)
    return connects


def _client(server, connection=ConnectionTcpFull):
    class Client(TelegramClient):
        pass  # its own class-level _config and _cdn_config

    client = Client(MemorySession(), 4, 'hash', connection=connection,
                    device_model='Pixel 7', app_version='11.0.0')

    async def get_dc(dc_id, cdn=False):
        assert cdn and dc_id == CDN_DC
        return types.DcOption(id=dc_id, ip_address='127.0.0.1', port=443, cdn=True)

    async def call(sender, request, ordered=False, flood_sleep_threshold=None):
        await asyncio.sleep(0)
        if sender is client._sender:
            return server.file_dc(request)
        assert sender.dc_id == CDN_DC and sender.is_connected()
        return server.cdn_dc(request)

    client._get_dc = get_dc
    client._call = call
    return client


def _borrows(client):
    state, _ = client._borrowed_senders[CDN_DC]
    return state._n


def test_cdn_limit():
    assert downloads._cdn_limit(0, 100) == 4096
    assert downloads._cdn_limit(0, 16384) == 16384
    assert downloads._cdn_limit(16384, 65536) == 16384  # offset must be a multiple
    assert downloads._cdn_limit(0, 5 * 1024 * 1024) == 1024 * 1024
    assert downloads._cdn_limit(3 * 1024 * 1024, 1024 * 1024) == 1024 * 1024
    assert downloads._cdn_limit(3 * 128 * 1024, 512 * 1024) == 128 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize('part_size_kb', [4, 16, 64, 1024])
@pytest.mark.parametrize('known_size', [False, True])
async def test_download_file_through_cdn(fake_senders, part_size_kb, known_size):
    data = os.urandom(10 * HASH_SIZE + 1234)
    server = FakeTelegram(data)
    client = _client(server)

    result = await client.download_file(
        LOCATION, bytes, part_size_kb=part_size_kb, file_size=len(data) if known_size else None)
    assert result == data

    # Asked the file's DC once; the CDN DC got whole hashed ranges
    get_file = [r for r in server.file_requests if isinstance(r, functions.upload.GetFileRequest)]
    assert len(get_file) == 1
    assert all(r.offset % HASH_SIZE == 0 for r in server.cdn_requests)
    # The hashes that the redirect lacked came from the file's DC, not the CDN
    assert any(isinstance(r, functions.upload.GetCdnFileHashesRequest) for r in server.file_requests)

    # Own connection (intermediate transport, own auth key for DC 203) ...
    assert fake_senders == [(ConnectionTcpIntermediate, CDN_DC)]
    # ... whose first request carried the device information
    assert len(server.inits) == 1
    init = server.inits[0]
    assert (init.api_id, init.device_model, init.app_version) == (4, 'Pixel 7', '11.0.0')
    assert client._init_request.query is None
    # and the CDN sender was returned
    assert _borrows(client) == 0


@pytest.mark.asyncio
async def test_iter_download_follows_cdn_redirect(fake_senders):
    data = os.urandom(6 * HASH_SIZE + 100)
    server = FakeTelegram(data)
    client = _client(server)

    # Not aligned to the hashed ranges (generic iterator)
    chunks = [bytes(c) async for c in client.iter_download(
        LOCATION, offset=20000, chunk_size=5000, request_size=16384)]
    assert b''.join(chunks) == data[20000:]
    assert all(len(c) == 5000 for c in chunks[:-1])

    # Just the header
    stream = client.iter_download(LOCATION, request_size=4096)
    assert await stream.__anext__() == data[:4096]
    await stream.close()
    assert _borrows(client) == 0
    # One CDN sender for everything
    assert len(fake_senders) == 1


@pytest.mark.asyncio
async def test_cdn_file_size_multiple_of_part_size(fake_senders):
    data = os.urandom(4 * HASH_SIZE)
    server = FakeTelegram(data, initial_hashes=4)
    client = _client(server)
    chunks = await asyncio.wait_for(
        client.iter_download(LOCATION, request_size=HASH_SIZE).collect(), timeout=10)
    assert b''.join(chunks) == data
    assert chunks[-1] == b''  # like upload.getFile past the end of a file


@pytest.mark.asyncio
async def test_cdn_reupload(fake_senders):
    data = os.urandom(3 * HASH_SIZE)
    server = FakeTelegram(data)
    server.reuploads = 2
    client = _client(server)
    assert await client.download_file(LOCATION, bytes, part_size_kb=16) == data
    reuploads = [r for r in server.file_requests if isinstance(r, functions.upload.ReuploadCdnFileRequest)]
    assert [r.request_token for r in reuploads] == [b'reupload-2', b'reupload-1']
    assert all(r.file_token == b'token-1' for r in reuploads)


@pytest.mark.asyncio
async def test_cdn_token_expiry_resumes_at_offset(fake_senders):
    data = os.urandom(8 * HASH_SIZE + 10)
    server = FakeTelegram(data)
    server.parts_until_expiry = 3
    client = _client(server)
    assert await client.download_file(LOCATION, bytes, part_size_kb=16) == data
    get_file = [r for r in server.file_requests if isinstance(r, functions.upload.GetFileRequest)]
    assert [r.offset for r in get_file] == [0, 3 * HASH_SIZE]
    assert server.tokens == 2


@pytest.mark.asyncio
async def test_cdn_tampered_part_is_never_returned(fake_senders):
    data = os.urandom(4 * HASH_SIZE)
    server = FakeTelegram(data)
    server.corrupt = 1
    client = _client(server)
    # Small parts: the bad range is detected before any of its bytes are returned
    chunks = [bytes(c) async for c in client.iter_download(LOCATION, request_size=4096)]
    assert b''.join(chunks) == data
    assert server.tokens == 2

    server = FakeTelegram(data)
    server.corrupt = 1000
    client = _client(server)
    with pytest.raises(errors.CdnFileTamperedError):
        await client.download_file(LOCATION, bytes, part_size_kb=16)
    get_file = [r for r in server.file_requests if isinstance(r, functions.upload.GetFileRequest)]
    assert len(get_file) == 1 + downloads.MAX_CDN_RESTARTS
    assert _borrows(client) == 0


@pytest.mark.asyncio
async def test_cdn_range_that_cannot_be_requested_whole(fake_senders):
    # 20 KB ranges: the one at 20 KB can only be requested 4 KB at a time
    data = os.urandom(3 * 20480)
    server = FakeTelegram(data, hash_size=20480)
    client = _client(server)
    with pytest.raises(errors.CdnFileTamperedError):
        await asyncio.wait_for(client.download_file(LOCATION, bytes, part_size_kb=16), timeout=10)


def test_rpc_error_name_recovers_tl_string():
    # Named classes keep the base 'BAD_REQUEST' message; the TL name is recovered
    assert downloads._rpc_error_name(errors.CdnMethodInvalidError(request=None)) == 'CDN_METHOD_INVALID'
    # Unmapped errors keep the TL string in .message
    assert downloads._rpc_error_name(
        errors.BadRequestError(request=None, message='FILE_TOKEN_INVALID')) == 'FILE_TOKEN_INVALID'
    assert downloads._is_cdn_failure(errors.CdnMethodInvalidError(request=None))
    assert not downloads._is_cdn_failure(errors.BadRequestError(request=None, message='LOCATION_INVALID'))


@pytest.mark.asyncio
async def test_cdn_named_error_restarts(fake_senders):
    data = os.urandom(3 * HASH_SIZE)
    server = FakeTelegram(data)
    client = _client(server)
    base_cdn = server.cdn_dc
    hit = [0]

    def cdn_dc(request):
        if isinstance(request, functions.InvokeWithLayerRequest) or isinstance(
                request, functions.upload.GetCdnFileRequest):
            # A named CDN error (its .message is 'BAD_REQUEST') on the very first part
            if hit[0] == 0:
                hit[0] = 1
                raise errors.CdnMethodInvalidError(request=request)
        return base_cdn(request)

    server.cdn_dc = cdn_dc
    assert await client.download_file(LOCATION, bytes, part_size_kb=16) == data
    # It fell back to the file DC and got a fresh redirect
    get_file = [r for r in server.file_requests if isinstance(r, functions.upload.GetFileRequest)]
    assert len(get_file) == 2 and server.tokens == 2


@pytest.mark.asyncio
async def test_cdn_endless_reupload_gives_up(fake_senders):
    server = FakeTelegram(os.urandom(HASH_SIZE))
    server.reuploads = 10 ** 6
    client = _client(server)
    with pytest.raises(ConnectionError):
        await client.download_file(LOCATION, bytes)
    assert _borrows(client) == 0


@pytest.mark.asyncio
async def test_download_without_cdn_uses_1mb_parts(fake_senders):
    data = os.urandom(2 * 1024 * 1024 + 5)
    server = FakeTelegram(data, cdn=False)
    client = _client(server)
    chunks = [c async for c in client.iter_download(LOCATION)]
    assert b''.join(chunks) == data
    assert [r.limit for r in server.file_requests] == [1024 * 1024] * 3
    # An offset that isn't a multiple of the part size must not cross a 1 MB boundary
    chunks = [bytes(c) async for c in client.iter_download(LOCATION, offset=512 * 1024)]
    assert b''.join(chunks) == data[512 * 1024:]
    assert not fake_senders


@pytest.mark.asyncio
async def test_bots_cannot_use_cdn(fake_senders):
    server = FakeTelegram(os.urandom(HASH_SIZE))
    client = _client(server)
    client._mb_entity_cache.self_bot = True
    with pytest.raises(ValueError):
        await client.download_file(LOCATION, bytes)


@pytest.mark.asyncio
async def test_call_cdn_initializes_connection_once(fake_senders):
    server = FakeTelegram(os.urandom(4 * HASH_SIZE), initial_hashes=4)
    client = _client(server)
    server.file_dc(functions.upload.GetFileRequest(LOCATION, 0, 4096))  # issues a token
    sender = await client._borrow_exported_sender(CDN_DC, cdn=True)
    requests = [functions.upload.GetCdnFileRequest(server.token, i * 4096, 4096) for i in range(4)]
    await asyncio.gather(*(client._call_cdn(sender, r) for r in requests))
    assert len(server.inits) == 1 and server.inits[0].query is requests[0]

    # Reused while connected; reconnected (and initialized again) once it isn't
    await client._return_exported_sender(sender)
    assert await client._borrow_exported_sender(CDN_DC, cdn=True) is sender
    sender._user_connected = False
    assert await client._borrow_exported_sender(CDN_DC, cdn=True) is sender
    assert sender.needs_init and len(fake_senders) == 2


@pytest.mark.asyncio
async def test_cdn_sender_keeps_custom_transport(fake_senders):
    client = _client(FakeTelegram(b''), connection=ConnectionTcpObfuscated)
    await client._borrow_exported_sender(CDN_DC, cdn=True)
    assert fake_senders == [(ConnectionTcpObfuscated, CDN_DC)]


@pytest.mark.asyncio
async def test_cdn_sender_on_test_server_offsets_dc_id(fake_senders):
    client = _client(FakeTelegram(b''))

    async def get_dc(dc_id, cdn=False):
        return types.DcOption(id=dc_id, ip_address='149.154.167.40', port=443, cdn=True)

    client._get_dc = get_dc
    await client._borrow_exported_sender(CDN_DC, cdn=True)
    # p_q_inner_data_dc on the test servers carries the DC ID plus 10000
    assert fake_senders == [(ConnectionTcpIntermediate, CDN_DC + 10000)]


@pytest.mark.asyncio
async def test_cdn_connect_timeout(fake_senders, monkeypatch):
    async def hang(self, connection, *, auth_dc_id=None):
        await asyncio.sleep(3600)

    monkeypatch.setattr(MTProtoSender, 'connect', hang)
    monkeypatch.setattr(telegrambaseclient, '_CDN_CONNECT_TIMEOUT', 0.01)
    client = _client(FakeTelegram(b''))
    type(client)._cdn_config = object()
    with pytest.raises(ConnectionError):
        await client._borrow_exported_sender(CDN_DC, cdn=True)
    assert type(client)._cdn_config is None  # fetched again next time
    assert CDN_DC not in client._borrowed_senders


@pytest.mark.asyncio
async def test_load_cdn_keys(test_rsa_key, monkeypatch):
    public, _ = test_rsa_key
    other = rsa.rsa.PublicKey(public.n, 3)
    monkeypatch.setattr(rsa, '_server_keys', {})
    configs = []

    async def call(sender, request, ordered=False, flood_sleep_threshold=None):
        assert isinstance(request, functions.help.GetCdnConfigRequest)
        configs.append(request)
        return types.CdnConfig([
            types.CdnPublicKey(203, public.save_pkcs1().decode()),
            types.CdnPublicKey(205, other.save_pkcs1().decode()),
        ])

    client = _client(None)
    client._call = call
    await client._load_cdn_keys(205)
    # Every CDN DC's key, not just the one of the DC asked for
    assert set(rsa._server_keys) == {rsa._compute_fingerprint(public), rsa._compute_fingerprint(other)}
    await client._load_cdn_keys(203)
    assert len(configs) == 1
    await client._load_cdn_keys(207)  # unknown: fetched again
    assert len(configs) == 2


@pytest.mark.asyncio
async def test_exported_sender_does_not_modify_init_request(fake_senders, monkeypatch):
    sent = []

    async def send(self, request, ordered=False):
        sent.append(request)

    monkeypatch.setattr(MTProtoSender, 'send', send)
    client = _client(None)

    async def get_dc(dc_id, cdn=False):
        return types.DcOption(id=dc_id, ip_address='149.154.167.40', port=443, media_only=True)

    async def call(sender, request, ordered=False, flood_sleep_threshold=None):
        assert isinstance(request, functions.auth.ExportAuthorizationRequest)
        return types.auth.ExportedAuthorization(id=1, bytes=b'auth')

    client._get_dc = get_dc
    client._call = call
    await client._create_exported_sender(2)

    (request,) = sent
    assert isinstance(request.query, functions.InitConnectionRequest)
    assert isinstance(request.query.query, functions.auth.ImportAuthorizationRequest)
    assert request.query is not client._init_request and client._init_request.query is None
    # Regular (non-CDN) senders use the legacy auth scheme, even on a test DC
    assert fake_senders == [(ConnectionTcpFull, None)]


def _photo(file_reference):
    return types.Photo(
        id=55, access_hash=1, file_reference=file_reference, date=datetime.datetime.now(),
        sizes=[types.PhotoSize('x', 10, 10, 6)], dc_id=2)


@pytest.mark.asyncio
async def test_photo_file_reference_is_refreshed(fake_senders):
    client = _client(None)

    async def call(sender, request, ordered=False, flood_sleep_threshold=None):
        assert request.location.thumb_size == 'x'
        if request.location.file_reference != b'new':
            raise errors.FileReferenceExpiredError(request)
        return types.upload.File(types.storage.FileUnknown(), 0, b'photo!')

    async def get_messages(chat, ids):
        assert (chat, ids) == ('chat', 7)
        return types.Message(7, types.PeerUser(1), None, '', media=types.MessageMediaPhoto(photo=_photo(b'new')))

    client._call = call
    client.get_messages = get_messages
    message = types.Message(7, types.PeerUser(1), None, '', media=types.MessageMediaPhoto(photo=_photo(b'old')))
    message._input_chat = 'chat'
    assert await client.download_media(message, bytes) == b'photo!'

    # The same reference again means it can't be refreshed: no endless loop
    async def same(chat, ids):
        return message

    client.get_messages = same
    with pytest.raises(errors.FileReferenceExpiredError):
        await client.download_media(message, bytes)
