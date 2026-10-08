import asyncio
import inspect
import logging
import pickle

import pytest

from telethon import TelegramClient, errors, functions, types
from telethon.client import telegrambaseclient
from telethon.extensions import BinaryReader
from telethon.network import MTProtoSender
from telethon.sessions import MemorySession
from telethon.tl.alltlobjects import LAYER

FOREIGN_MESSAGE = 0x3ae56482  # message#3ae56482 of layer 224


class FakeSender:
    def __init__(self, client):
        self.client = client
        self.sent = []

    async def send(self, request, ordered=False):
        self.sent.append(request)
        if not self.client.flips_back:
            self.client.foreign = False
        return types.Config


def make_client(receive_updates=True, **kwargs):
    client = TelegramClient(
        MemorySession(), 4, 'hash', device_model='Realme8i', system_version='SDK 30',
        app_version='12.9.2', lang_code='ru', system_lang_code='ru-ru',
        init_params={'tz_offset': 10800, 'perf_cat': 2},
        receive_updates=receive_updates, **kwargs)
    client.foreign = False
    client.flips_back = False
    client.calls = 0
    client.fake_sender = FakeSender(client)

    async def call_unguarded(sender, request, ordered=False, flood_sleep_threshold=None):
        client.calls += 1
        await asyncio.sleep(0)
        if client.foreign:
            raise errors.TypeNotFoundError(FOREIGN_MESSAGE, b'\x82d\xe5:')
        return ('ok', request)

    client._call_unguarded = call_unguarded
    return client


def test_init_params_in_signature_and_serialized():
    assert 'init_params' in inspect.signature(TelegramClient.__init__).parameters
    client = make_client()
    init = client._init_request
    init.query = functions.help.GetConfigRequest()
    data = bytes(functions.InvokeWithLayerRequest(LAYER, init))
    with BinaryReader(data) as reader:
        back = reader.tgread_object()
    assert isinstance(back, functions.InvokeWithLayerRequest) and back.layer == LAYER
    assert isinstance(back.query, functions.InitConnectionRequest)
    assert (back.query.api_id, back.query.lang_pack) == (4, 'android')
    params = {v.key: v.value for v in back.query.params.value}
    assert params == {'tz_offset': types.JsonNumber(10800.0), 'perf_cat': types.JsonNumber(2.0)}
    assert isinstance(back.query.query, functions.help.GetConfigRequest)


@pytest.mark.asyncio
async def test_recovers_and_repeats_request():
    client = make_client(receive_updates=False)
    client.foreign = True
    assert await client._call(client.fake_sender, 'GetHistory') == ('ok', 'GetHistory')
    assert client.calls == 2

    (sent,) = client.fake_sender.sent
    # invokeWithLayer(LAYER, invokeWithoutUpdates(initConnection(help.getConfig)))
    assert isinstance(sent, functions.InvokeWithLayerRequest) and sent.layer == LAYER
    assert isinstance(sent.query, functions.InvokeWithoutUpdatesRequest)
    init = sent.query.query
    assert isinstance(init, functions.InitConnectionRequest)
    assert isinstance(init.query, functions.help.GetConfigRequest)
    # The account's own fingerprint and init_params are reused...
    assert (init.api_id, init.device_model, init.app_version) == (4, 'Realme8i', '12.9.2')
    assert init.params is client._init_request.params
    # ...and the shared template is untouched.
    assert init is not client._init_request and client._init_request.query is None


@pytest.mark.asyncio
async def test_without_no_updates_wrapper():
    client = make_client()
    client.foreign = True
    await client._call(client.fake_sender, 'X')
    assert isinstance(client.fake_sender.sent[0].query, functions.InitConnectionRequest)


@pytest.mark.asyncio
async def test_gives_up_when_layer_flips_back_immediately():
    client = make_client()
    client.foreign = client.flips_back = True
    with pytest.raises(errors.LayerConflictError) as info:
        await client._call(client.fake_sender, 'X')
    assert isinstance(info.value, errors.TypeNotFoundError)  # backwards compatible
    assert info.value.invalid_constructor_id == FOREIGN_MESSAGE
    assert '3ae56482' in str(info.value) and str(LAYER) in str(info.value)
    assert len(client.fake_sender.sent) == 1  # one recovery attempt per request


@pytest.mark.asyncio
async def test_cap_per_window_and_expiry(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(telegrambaseclient.time, 'monotonic', lambda: now[0])
    client = make_client(layer_recovery_limit=2, layer_recovery_window=600)
    for _ in range(2):
        client.foreign = True
        await client._call(client.fake_sender, 'X')
    client.foreign = True
    with pytest.raises(errors.LayerConflictError):
        await client._call(client.fake_sender, 'X')
    assert len(client.fake_sender.sent) == 2

    now[0] += 601  # old recoveries expire
    client.foreign = True
    assert await client._call(client.fake_sender, 'X') == ('ok', 'X')


@pytest.mark.asyncio
async def test_concurrent_failures_reinit_once():
    client = make_client()
    client.foreign = True
    results = await asyncio.gather(*(client._call(client.fake_sender, i) for i in range(5)))
    assert [r[1] for r in results] == list(range(5))
    assert len(client.fake_sender.sent) == 1


@pytest.mark.asyncio
async def test_disabled():
    client = make_client(layer_recovery_limit=0)
    client.foreign = True
    with pytest.raises(errors.TypeNotFoundError) as info:
        await client._call(client.fake_sender, 'X')
    assert not isinstance(info.value, errors.LayerConflictError)
    assert client.fake_sender.sent == []


@pytest.mark.asyncio
async def test_pushed_unknown_type_recovers_in_background():
    client = make_client()
    client.foreign = True
    error = errors.TypeNotFoundError(FOREIGN_MESSAGE, b'')
    client._handle_unknown_pushed_type(client.fake_sender, error)
    client._handle_unknown_pushed_type(client.fake_sender, error)  # task already running
    await client._layer_recovery_task
    assert len(client.fake_sender.sent) == 1 and not client.foreign

    # Updates that were already in flight when the layer was restored.
    client._handle_unknown_pushed_type(client.fake_sender, error)
    assert client._layer_recovery_task.done()
    assert len(client.fake_sender.sent) == 1


@pytest.mark.asyncio
async def test_pushed_unknown_type_cap_only_logs(caplog):
    client = make_client(layer_recovery_limit=1)
    client._layer_recoveries.append(telegrambaseclient.time.monotonic() - 60)
    client._handle_unknown_pushed_type(client.fake_sender, errors.TypeNotFoundError(FOREIGN_MESSAGE, b''))
    with caplog.at_level(logging.ERROR):
        await client._layer_recovery_task
    assert client.fake_sender.sent == []
    assert any('another API layer' in r.getMessage() for r in caplog.records)


class _Loggers(dict):
    def __missing__(self, key):
        return logging.getLogger(key)


@pytest.mark.asyncio
async def test_sender_reports_unknown_pushed_types():
    seen = []
    sender = MTProtoSender(None, loggers=_Loggers(), type_not_found_callback=lambda s, e: seen.append((s, e)))

    class Connection:
        async def recv(self):
            sender._user_connected = False  # stop after this message
            return b'body'

    class State:
        def decrypt_message_data(self, body):
            raise errors.TypeNotFoundError(FOREIGN_MESSAGE, b'')

    sender._connection, sender._state, sender._user_connected = Connection(), State(), True
    await sender._recv_loop()
    ((s, e),) = seen
    assert s is sender and e.invalid_constructor_id == FOREIGN_MESSAGE


def test_layer_conflict_error_pickles():
    error = errors.LayerConflictError(FOREIGN_MESSAGE, b'xy', LAYER, 3)
    back = pickle.loads(pickle.dumps(error))
    assert type(back) is errors.LayerConflictError
    assert (back.invalid_constructor_id, back.remaining, back.layer, back.recoveries) == \
        (FOREIGN_MESSAGE, b'xy', LAYER, 3)
    assert str(back) == str(error)
