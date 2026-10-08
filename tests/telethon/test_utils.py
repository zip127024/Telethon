import io
import pathlib

import pytest

from telethon import utils
from telethon.tl.types import (
    MessageMediaGame, Game, PhotoEmpty
)


def test_game_input_media_memory_error():
    large_long = 2**62
    media = MessageMediaGame(Game(
        id=large_long,  # <- key to trigger `MemoryError`
        access_hash=large_long,
        short_name='short_name',
        title='title',
        description='description',
        photo=PhotoEmpty(large_long),
    ))
    input_media = utils.get_input_media(media)
    bytes(input_media)  # <- shouldn't raise `MemoryError`


def test_private_get_extension():
    # Positive cases
    png_header = bytes.fromhex('89 50 4e 47 0d 0a 1a 0a  00 00 00 0d 49 48 44 52')
    png_buffer = io.BytesIO(png_header)

    class CustomFd:
        def __init__(self, name):
            self.name = name

    assert utils._get_extension('foo.bar.baz') == '.baz'
    assert utils._get_extension(pathlib.Path('foo.bar.baz')) == '.baz'
    assert utils._get_extension(CustomFd('foo.bar.baz')) == '.baz'

    # Negative cases
    null_header = bytes.fromhex('00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00')
    null_buffer = io.BytesIO(null_header)

    empty_header = bytes()
    empty_buffer = io.BytesIO(empty_header)

    assert utils._get_extension('foo') == ''
    assert utils._get_extension(pathlib.Path('foo')) == ''
    assert utils._get_extension(null_header) == ''
    assert utils._get_extension(null_buffer) == ''
    assert utils._get_extension(null_buffer) == ''  # make sure it did seek back
    assert utils._get_extension(empty_header) == ''
    assert utils._get_extension(empty_buffer) == ''
    assert utils._get_extension(empty_buffer) == ''  # make sure it did seek back
    assert utils._get_extension(CustomFd('foo')) == ''


def test_rle_encode_trailing_zeros():
    assert utils._rle_encode(b'\x12\x00\x00\x00\x00') == b'\x12\x00\x04'


def test_quiz_poll_input_media_correct_answers_are_indices():
    # Since layer 223 inputMediaPoll.correct_answers is Vector<int>: the
    # indices of the correct answers in poll.answers, no longer their options.
    from telethon.tl.types import (
        MessageMediaPoll, Poll, PollAnswer, PollResults, PollAnswerVoters,
        TextWithEntities, InputMediaPoll
    )

    def answer(text, option):
        return PollAnswer(TextWithEntities(text, []), option)

    poll = Poll(id=1, question=TextWithEntities('q', []), hash=0, quiz=True, answers=[
        answer('a', b'0'), answer('b', b'1'), answer('c', b'2')])
    media = MessageMediaPoll(poll, PollResults(results=[
        PollAnswerVoters(b'0', voters=1),
        PollAnswerVoters(b'2', correct=True, voters=4),
    ], solution='because', solution_entities=[]))

    input_media = utils.get_input_media(media)
    assert isinstance(input_media, InputMediaPoll)
    assert input_media.correct_answers == [2]
    assert input_media.solution == 'because'
    bytes(input_media)  # must serialize
