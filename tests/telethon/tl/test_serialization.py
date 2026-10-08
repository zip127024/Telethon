import pytest

from telethon.tl import types, functions


def test_nested_invalid_serialization():
    large_long = 2**62
    request = functions.account.SetPrivacyRequest(
        key=types.InputPrivacyKeyChatInvite(),
        rules=[types.InputPrivacyValueDisallowUsers(users=[large_long])]
    )
    with pytest.raises(TypeError):
        bytes(request)


def test_chat_invite_join_result_ok_exposes_chats_and_users():
    # Fork: importChatInvite / joinChannel returned Updates until layer 226;
    # code reading result.chats must keep working with ChatInviteJoinResultOk.
    from telethon.extensions import BinaryReader
    from telethon.sessions import MemorySession

    channel = types.Channel(id=10, title='t', photo=types.ChatPhotoEmpty(), date=None, access_hash=77)
    user = types.User(id=5, access_hash=55)
    data = bytes(types.messages.ChatInviteJoinResultOk(
        types.Updates(updates=[], users=[user], chats=[channel], date=None, seq=0)))

    result = BinaryReader(data).tgread_object()
    assert isinstance(result, types.messages.ChatInviteJoinResultOk)
    assert result.chats[0].id == 10 and result.users[0].id == 5
    assert bytes(result) == data
    assert set(result.to_dict()) == {'_', 'updates'}

    session = MemorySession()
    session.process_entities(result)
    assert session.get_input_entity(-1000000000010) == types.InputPeerChannel(10, 77)

    short = types.messages.ChatInviteJoinResultOk(types.UpdatesTooLong())
    assert short.chats == [] and short.users == []
    assert not hasattr(types.messages.ChatInviteJoinResultWebView(1, 2, []), 'chats')
