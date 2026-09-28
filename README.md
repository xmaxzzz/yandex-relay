# Yandex Relay — Алиса → мультирум Control4

Говорите Яндекс Станции «Алиса, включи музыку» — музыка звучит из мультирума Control4 в комнате, где стоит станция. Станция остаётся хозяином очереди («Моя волна», плейлисты, «дальше», «пауза» голосом), Control4 только воспроизводит. Разговоры с Алисой (погода, таймеры) трансляцию не запускают.

*Yandex Station music played through Control4 rooms: the station keeps the queue and voice control, the Control4 controller plays the audio through its own outputs (matrix or network endpoints).*

Две части:

| Часть | Где | Что делает |
|---|---|---|
| **Yandex Relay** (`c4-driver/`) | контроллер Control4 | `media_service`-драйвер: играет присланный URL через цифровое аудио контроллера в нужной комнате, отдаёт кнопки панели в HA. Комнаты находит сам. |
| **Control4 Yandex Relay** (`custom_components/c4_relay`) | Home Assistant | Плеер `Control4 <комната>` как цель трансляции для [AlexxIT YandexStation](https://github.com/AlexxIT/YandexStation), привязка станций к комнатам, кнопки панели → команды станции. |

## Требования

- Control4 OS 3.x, Composer Pro; контроллер с цифровым аудио (CORE / EA).
- Home Assistant 2024.11+ с [AlexxIT YandexStation](https://github.com/AlexxIT/YandexStation), станции в локальном режиме, подписка Яндекс Музыки.
- HA доступен с контроллера по IP (без `.local`).

## Установка

1. **Драйвер:** скачать `yandex_relay.c4z` из [Releases](https://github.com/xmaxzzz/yandex-relay/releases), добавить в проект в Composer Pro. Connections к Digital Media привязываются сами. В свойствах: `Bridge Status = ONLINE`, `Pairing Code`.
2. **Интеграция через HACS:** HACS → ⋮ → Пользовательские репозитории → `https://github.com/xmaxzzz/yandex-relay`, тип **Интеграция** → установить **Control4 Yandex Relay** → перезапустить HA.
3. Настройки → Устройства и службы → Добавить → **Control4 Yandex Relay**: IP контроллера, порт, код сопряжения; затем выбрать комнату для каждой станции.

Подробно: [docs/HA-INSTALL.md](docs/HA-INSTALL.md). Проверка драйвера без HA: [docs/STAGE1-SITE-TEST.md](docs/STAGE1-SITE-TEST.md), `tools/relayctl.py`.

## Статус

Этап 1 (одна станция → одна комната). Драйвер проверен на живом контроллере; интеграция — на ядре HA 2026.2 в тестах, на живом HA идёт проверка. Дальше: громкость станции → громкость комнаты, приглушение комнаты, пока Алиса слушает, объединение комнат. Дизайн и протокол: [docs/DESIGN.md](docs/DESIGN.md).

## Разработка

```bash
python c4-driver/tests/test_driver.py          # драйвер на LuaJIT с заглушками C4 API (pip install lupa)
python c4-driver/build.py                      # сборка .c4z
docker build -t c4relay-hatest ha/tests        # интеграция на настоящем ядре HA
docker run --rm -v "$PWD:/work" -w /work/ha c4relay-hatest python -m pytest -q -p no:cacheprovider
```

Неофициальный проект, не связан с Яндексом и Snap One / Control4.
