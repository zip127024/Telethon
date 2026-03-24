# CLAUDE.md

Приватный форк Telethon (`zip127024/Telethon`, ветка `v1`), основан на [LonamiWebs/Telethon](https://github.com/LonamiWebs/Telethon).

## Обзор

Форк используется проектом `new_commenter` — системой Telegram-ботов для автоматического комментирования каналов. Каждый бот подписан на ~250 каналов и работает как отдельный процесс (34+ процессов одновременно на одном сервере).

## Команды

```bash
# Установка из форка
pip install "git+ssh://git@github.com/zip127024/Telethon.git@v1"

# Принудительная переустановка
pip install --force-reinstall "git+ssh://git@github.com/zip127024/Telethon.git@v1"
```

## Кастомные изменения относительно upstream

### init_params (perf_cat, tz_offset)

`telegrambaseclient.py` — добавлен параметр `init_params: dict` в конструктор клиента. Преобразуется в TL `JsonObject` и передаётся в `InitConnectionRequest.params`. Используется для передачи `perf_cat`, `tz_offset`, `signature`, `certificate` и других параметров, имитирующих официальный Android-клиент. Это критически важно для предотвращения массовых банов аккаунтов.

### lang_pack

`telegrambaseclient.py` — для `api_id` 4 и 21724 автоматически выставляется `lang_pack='android'`, для 2040 — `lang_pack='tdesktop'`.

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
