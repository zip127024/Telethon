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

### Поддержка CDN (скачивание файлов через CDN дата-центры)

Telegram отвечает на `upload.getFile` редиректом `upload.fileCdnRedirect` для
официальных `api_id` (напр. Android `api_id` 4) **даже без флага `cdn_supported`**,
поэтому поддержка CDN обязательна для таких аккаунтов. Реализация (`telethon/crypto/cdndecrypter.py`,
CDN-ветка в `telethon/client/downloads.py`, CDN-отправители в `telegrambaseclient.py`):

- **Отдельный auth key на CDN DC** через DH (без login/authorization), транспорт
  `ConnectionTcpIntermediate` (CDN DC не отвечают на `ConnectionTcpFull` — зависает),
  первый запрос — `invokeWithLayer(initConnection(...))` с отпечатком аккаунта.
- RSA-ключи **всех** CDN DC регистрируются из `help.getCdnConfig` (`_load_cdn_keys`),
  конфиг перезапрашивается, если ключа для нужного DC нет.
- Расшифровка части — **AES-256-CTR**, key = `encryption_key`, IV = `encryption_iv`
  с последними 4 байтами = big-endian `offset / 16`. Используется `cryptography`,
  фолбэк на `pyaes`; чистый Python гоняется в `run_in_executor` (не блокирует loop).
- Каждая часть проверяется по SHA-256 (`file_hashes` редиректа + `upload.getCdnFileHashes`
  на DC файла). `CdnFileReuploadNeeded` → `upload.reuploadCdnFile` на DC файла в цикле.
  Протухший токен (`FILE_TOKEN_INVALID` / `CDN_*`) → повторный `upload.getFile` с текущего
  offset (`iter_download` следует за редиректом прозрачно, без приватного исключения).

### CDN: p_q_inner_data_dc — ТОЛЬКО для CDN DC, обычные DC его отвергают

Ключевой факт, проверенный вживую (анонимный handshake, 2026-10): обычные
(не-CDN) дата-центры **отвергают** `p_q_inner_data_dc` + RSA_PAD транспортной
ошибкой −404 на `req_DH_params`; они принимают только **легаси** `p_q_inner_data`
(`sha1(data)+data+padding`). CDN DC — наоборот: только `p_q_inner_data_dc` + RSA_PAD.

Поэтому схема выбирается **по типу DC**, а не глобально: `MTProtoSender.connect(...,
auth_dc_id=...)` → `authenticator.do_authentication(sender, dc_id)`. `dc_id=None`
(по умолчанию) = легаси для всех обычных DC; `dc_id` задаётся (`_cdn_auth_dc_id`,
на тестовых серверах +10000) только для CDN-отправителей. Утверждение «официальные
клиенты шлют `p_q_inner_data_dc` везде» на живых DC не подтвердилось — **не менять
схему обычных DC на `p_q_inner_data_dc`**, иначе каждый коннект к основному DC будет
падать и переподключаться.

### CDN: имя RPC-ошибки берётся из класса, а не из .message

Именованные классы ошибок Telethon хранят базовое `message` (`CdnMethodInvalidError().message
== 'BAD_REQUEST'`, а не `'CDN_METHOD_INVALID'`). В `downloads._rpc_error_name` имя
восстанавливается из класса через `rpcerrorlist`; проверять `error.message` на `CDN_*`
нельзя — пропустит именованные ошибки.

### MAX_CHUNK_SIZE = 1 MiB

В `downloads.py` поднят с 512 KiB до 1 MiB (как в 64Gram/tdesktop). `upload.getFile`
принимает до 1 MiB при offset, кратном размеру части (часть не должна пересекать границу
1 MiB — отсюда `offset % request_size == 0` для прямого итератора). Обновление
file-reference работает и для фото (`InputPhotoFileLocation`), и для документов.

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
