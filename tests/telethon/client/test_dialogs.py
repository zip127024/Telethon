from unittest.mock import MagicMock

import pytest

from telethon import utils
from telethon.client.dialogs import _DialogsIter
from telethon.tl import types


def _channel(channel_id):
    return types.Channel(
        id=channel_id, title='channel', photo=types.ChatPhotoEmpty(), date=None,
        broadcast=True, access_hash=111)


def _community(community_id):
    return types.Community(
        id=community_id, title='community', photo=types.ChatPhotoEmpty(),
        date=None, access_hash=222)


def _iter(result):
    return _DialogsIter(
        _Client(result), None, offset_date=None, offset_id=0,
        offset_peer=types.InputPeerEmpty(), ignore_pinned=False,
        ignore_migrated=False, folder=None)


class _Client:
    def __init__(self, result):
        self._result = result
        self._mb_entity_cache = MagicMock()
        self._message_box = MagicMock()

    async def __call__(self, request):
        return self._result


@pytest.mark.asyncio
async def test_iter_dialogs_with_dialog_community():
    # Layer 228+ returns dialogCommunity rows in messages.getDialogs. They have
    # no peer, top_message or counters, and communities are addressed as
    # channels (community_id is a channel ID).
    channel, community = _channel(10), _community(20)
    result = types.messages.Dialogs(
        dialogs=[
            types.Dialog(
                peer=types.PeerChannel(10), top_message=5, read_inbox_max_id=5,
                read_outbox_max_id=5, unread_count=3, unread_mentions_count=0,
                unread_reactions_count=0, unread_poll_votes_count=1,
                notify_settings=types.PeerNotifySettings(), pts=7),
            types.DialogCommunity(
                community_id=20, notify_settings=types.PeerNotifySettings(),
                pinned=True),
        ],
        messages=[],
        chats=[channel, community],
        users=[],
    )
    dialogs = [d async for d in _iter(result)]

    assert [d.id for d in dialogs] == [utils.get_peer_id(channel), utils.get_peer_id(community)]
    regular, comm = dialogs
    assert regular.is_channel and not regular.is_community
    assert (regular.unread_count, regular.unread_poll_votes_count) == (3, 1)

    assert comm.is_community and not comm.is_channel and not comm.is_group
    assert comm.entity is community
    assert comm.input_entity == types.InputPeerChannel(20, 222)
    assert comm.pinned and not comm.archived and comm.folder_id is None
    assert comm.message is None and comm.date is None
    assert (comm.unread_count, comm.unread_mentions_count,
            comm.unread_reactions_count, comm.unread_poll_votes_count) == (0, 0, 0, 0)
    assert comm.draft is not None


@pytest.mark.asyncio
async def test_iter_dialogs_community_entity_missing_is_skipped():
    result = types.messages.Dialogs(
        dialogs=[types.DialogCommunity(
            community_id=20, notify_settings=types.PeerNotifySettings())],
        messages=[], chats=[], users=[])
    assert [d async for d in _iter(result)] == []
