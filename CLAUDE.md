# CLAUDE.md

Приватный форк Telethon (`zip127024/Telethon`, ветка `v1`), основан на upstream Telethon v1. Upstream переехал с GitHub ([LonamiWebs/Telethon](https://github.com/LonamiWebs/Telethon), заморожен на слое 222) на **[codeberg.org/Lonami/Telethon](https://codeberg.org/Lonami/Telethon)** (ветка `v1`).

Текущая база: upstream **1.45.0** (коммит `bad095bd`), TL **layer 229** (`telethon_generator/data/api.tl` == `api.tl` из tdesktop v7.2.9).

## Обзор

Форк используется проектом `new_commenter` — системой Telegram-ботов для автоматического комментирования каналов. Каждый бот подписан на ~250 каналов и работает как отдельный процесс (34+ процессов одновременно на одном сервере).

## Команды

```bash
# Установка из форка
pip install "git+ssh://git@github.com/zip127024/Telethon.git@v1"

# Принудительная переустановка
pip install --force-reinstall "git+ssh://git@github.com/zip127024/Telethon.git@v1"

# Сгенерировать TL-код локально (telethon/tl/{functions,types} в .gitignore) и прогнать тесты
python setup.py gen tl errors
python -m pytest -q tests
```

Сборка — setuptools (`setup.py` генерирует TL-код при установке из архива/git). Upstream перешёл на Hatch (`hatch_build.py`, `gentl.py`, новый `pyproject.toml`) — в форк это **не** взято, чтобы установка `archive/refs/heads/v1.zip` и `git+...@v1` у ~30 проектов не менялась.

## Кастомные изменения относительно upstream

### init_params (perf_cat, tz_offset)

`telegrambaseclient.py` — добавлен параметр `init_params: dict` в конструктор клиента. Преобразуется в TL `JsonObject` и передаётся в `InitConnectionRequest.params`. Используется для передачи `perf_cat`, `tz_offset`, `signature`, `certificate` и других параметров, имитирующих официальный Android-клиент. Это критически важно для предотвращения массовых банов аккаунтов.

### lang_pack

`telegrambaseclient.py` — для `api_id` 4 и 21724 автоматически выставляется `lang_pack='android'`, для 2040 — `lang_pack='tdesktop'`.

### Восстановление слоя API (layer recovery)

Telegram хранит слой API **на auth key**: соединение, последним отправившее `invokeWithLayer(initConnection)`, определяет слой объектов для **всех** соединений этого ключа (даже после своего отключения). Если тот же `.session` использует программа с другим слоем (другой Telethon/Pyrogram), нам приходят неизвестные конструкторы -> `TypeNotFoundError`.

* `UserMethods._call` (users.py) — на `TypeNotFoundError` переотправляет `invokeWithLayer(LAYER, [invokeWithoutUpdates](initConnection(help.getConfig)))` на том же sender с **копией** `_init_request` (fingerprint и `init_params` сохраняются) и повторяет запрос один раз. Тело upstream `_call` не менялось, только переименовано в `_call_unguarded`.
* Лимит: `layer_recovery_limit` (по умолчанию 3, `0` — выключить) за `layer_recovery_window` секунд (600). Дальше — `errors.LayerConflictError` (подкласс `TypeNotFoundError`, старые `except` продолжают работать; `_update_loop` на нём, как и раньше, отключает клиент при getDifference).
* Push-апдейты: `MTProtoSender._recv_loop` раньше молча выбрасывал нечитаемые апдейты (бот «слеп» до getDifference, до 30 мин). Теперь sender вызывает `type_not_found_callback`, клиент переинициализирует слой в фоне (debounce 10 с, тот же лимит).
* Тесты: `tests/telethon/client/test_layer_recovery.py`.

## Критические знания (не удалять)

### tcpfull.py — НЕ ловить IncompleteReadError в read_packet

В `telethon/network/connection/tcpfull.py` метод `read_packet()` **не должен** перехватывать `asyncio.IncompleteReadError`. Upstream коммит `295c7363` добавил:

```python
except asyncio.IncompleteReadError as exc:
    return exc.partial
```

Это создаёт busy-loop при обрыве соединения: reader на EOF -> readexactly мгновенно бросает IncompleteReadError -> partial data возвращается как "валидный пакет" -> recv_loop сразу читает снова -> повтор без пауз -> **100% CPU на процесс**. При 34+ процессах это полностью забивает сервер.

Правильное поведение — пробросить исключение вверх, чтобы `_recv_loop` (connection.py) обработал его как обрыв соединения и запустил reconnect с backoff.

**При мерже upstream всегда проверять, что этот catch не вернулся.**

### updates.py — sleep при deadline_delay <= 0

В `_update_loop` (telethon/client/updates.py), когда `deadline_delay <= 0`, добавлен `await asyncio.sleep(0.1)` перед `continue`. Без этого цикл крутится без await при истёкших дедлайнах каналов.

### Throttle в recv_loop

В `connection.py` и `mtprotosender.py` добавлен `await asyncio.sleep(0)` после обработки каждого пакета, чтобы event loop не блокировался при высоком потоке обновлений от ~250 каналов.

### NO_UPDATES_TIMEOUT

В `telethon/_updates/messagebox.py` увеличен с 15 до 30 минут для снижения частоты вызовов `get_difference` на аккаунтах с большим количеством каналов.

### Мерж upstream / обновление слоя

* Upstream: `git fetch --no-tags https://codeberg.org/Lonami/Telethon.git +v1:refs/remotes/codeberg/v1`. Мержить **релизный** коммит (`Bump to vX.Y`), а не голову ветки: пост-релизные коммиты бывают сломаны (напр. `528e0d05` добавил в `forward_messages` keyword-only `reply_to` без дефолта -> `TypeError` на каждом вызове; в форк не взят).
* Конфликты разрешать в пользу форка: tcpfull (см. выше; upstream `cb06a95a` по-прежнему возвращает непустой `exc.partial`), `pyproject.toml`/Hatch (не брать), init_params/lang_pack, layer recovery.
* После мержа: `python setup.py gen tl errors`, `pytest tests`, проверить `grep LAYER telethon_generator/data/api.tl`, что сгенерированный код совпадает с PyPI-релизом того же номера, и что `'init_params' in inspect.signature(TelegramClient.__init__).parameters`.
* Слой 229 — свои правки поверх upstream 1.45.0: `dialogCommunity` в `iter_dialogs`/`custom.Dialog` (нет `peer`; community_id -> `PeerChannel`), `inputMediaPoll.correct_answers` — индексы ответов (`Vector<int>`), а не `option`.
* Ломающие изменения 223-229 для кода проектов: все `keyboardButton*` удалены (теперь `KeyboardButton(text, type=ButtonType*)` / `KeyboardInlineButton(text, type=InlineButtonType*)`, строки инлайн-клавиатуры — `KeyboardInlineButtonRow`); `channels.joinChannel` и `messages.importChatInvite` возвращают `messages.ChatInviteJoinResult` (`ChatInviteJoinResultOk.updates` / `ChatInviteJoinResultWebView`), а не `Updates`; `types.Poll` требует `hash`; `messages.getPollResults` — `poll_hash`; `channels.editCreator` -> `messages.editChatCreator`.
