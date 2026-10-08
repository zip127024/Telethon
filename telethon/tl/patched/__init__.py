from .. import types, alltlobjects
from ..custom.message import Message as _Message

class MessageEmpty(_Message, types.MessageEmpty):
    pass

types.MessageEmpty = MessageEmpty
alltlobjects.tlobjects[MessageEmpty.CONSTRUCTOR_ID] = MessageEmpty

class MessageService(_Message, types.MessageService):
    pass

types.MessageService = MessageService
alltlobjects.tlobjects[MessageService.CONSTRUCTOR_ID] = MessageService

class Message(_Message, types.Message):
    pass

types.Message = Message
alltlobjects.tlobjects[Message.CONSTRUCTOR_ID] = Message

class ChatInviteJoinResultOk(types.messages.ChatInviteJoinResultOk):
    """
    Fork: until layer 226 messages.importChatInvite and channels.joinChannel
    returned :tl:`Updates`, and code reads ``result.chats``. Keep that working
    by exposing the ``chats`` and ``users`` of the wrapped ``updates``.
    """
    @property
    def chats(self):
        return getattr(self.updates, 'chats', [])

    @property
    def users(self):
        return getattr(self.updates, 'users', [])

types.messages.ChatInviteJoinResultOk = ChatInviteJoinResultOk
alltlobjects.tlobjects[ChatInviteJoinResultOk.CONSTRUCTOR_ID] = ChatInviteJoinResultOk
