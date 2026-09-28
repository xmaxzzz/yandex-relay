# Этап 1 — проверка драйвера на объекте

Цель: убедиться, что драйвер Yandex Relay работает на живом контроллере **до** установки c4_relay в HA. Проверяем только цепочку «HTTP-команда → звук в комнате → события обратно». Станция и AlexxIT на этом шаге не участвуют.

Нужно: Composer Pro, ПК в той же сети, что контроллер, Python 3 (для `tools/relayctl.py`, только стандартная библиотека), любая прямая ссылка на mp3 для теста.

## 1. Установка

1. Собрать пакет: `python c4-driver/build.py` → `c4-driver/yandex_relay.c4z`.
2. Composer Pro → добавить драйвер (Driver → Add Driver / перетащить `.c4z`) в любую комнату.
3. **Connections.** С v0.1.6 `Digital Audio` и `Digital Audio Client` привязываются к `Digital Media → Digital Audio` сами при добавлении драйвера (`autobind`, как у TuneIn). Подтверждено на объекте 2026-09-28. Проверить в Connections; если не привязались — привязать вручную. Без них звук может пойти, но панель не выберет Relay источником: не будет `DEVICE_SELECTED` / `GetQueue` / `GetDashboard`, а на экране останется только громкость (объект, 2026-09-28).
4. Свойства драйвера: **Debug Mode = On**. Открыть вкладку Lua (Lua Output).
4. Проверить свойства:
   - `Bridge Status` = `ONLINE :18765` (если другое — порт занят, поменять `Bridge Port`);
   - `Pairing Code` — 8 символов;
   - `Rooms Found` — число и список комнат проекта.

В Lua Output должно быть: `v0.1.0 ready, N rooms, paired=false` и строка `project location types …`.

**Если `Rooms Found` показывает одну комнату «Room …» или список неполный:** разбор проекта не угадал тип комнаты (ожидается `<type>8</type>`). Прислать строку `project location types …` из Lua Output — там гистограмма типов.

**Обновление драйвера.** На этом объекте «Update Driver» в Composer не заменяет код на контроллере, даже если номер сборки и дата `<modified>` новее: работает только удаление драйвера и повторное добавление (2026-09-28). После этого — новый Pairing Code, connections привязать заново, сопряжение повторить. Проверять версию по `relayctl info`, а не по свойству в Composer. Какой код сейчас работает, видно по полю `driver_version` в каждом событии webhook.

## 2. Команды с ПК

```bash
set RELAY_HOST=<IP контроллера>
set RELAY_CODE=<Pairing Code>
python tools/relayctl.py info
python tools/relayctl.py rooms
```

`rooms` должен вернуть комнаты с правильными ID и именами. ID нужной комнаты понадобится дальше.

## 3. Обратный канал

Во втором окне:

```bash
python tools/relayctl.py listen
```

Драйвер сопрягается с этим ПК (`Paired With` = IP ПК), в окне появляется `{"event": "hello", …}`. Окно не закрывать — сюда будут приходить события.

Если `hello` не пришёл: брандмауэр Windows блокирует входящие на порт 8099.

## 4. Проверки

| # | Действие | Ожидается |
|---|---|---|
| 1 | `relayctl play --room <ID> --url <mp3> --title Тест --artist Проверка` | Звук в комнате через матрицу. На панели/в приложении C4 — «Тест / Проверка». В listen — `state: playing`. |
| 2 | На панели C4 — Next, Prev | В listen — `transport next`, `transport prev`. Звук не меняется (треки переключает станция, её здесь нет). |
| 3 | На панели C4 — Pause | Звук останавливается, в listen — `transport pause`, затем `state: paused`. |
| 4 | `relayctl resume --room <ID>` | Трек играет с начала (особенность интернет-радио C4, см. DESIGN §4). |
| 5 | `relayctl pause --room <ID>` | Звук останавливается. В listen — `state: paused`, **без** `transport pause` (своя команда не должна вернуться эхом). |
| 6 | `relayctl play --url https://нет.такого/x.mp3 --fallback <mp3> …` | Через несколько секунд играет fallback. `relayctl state` → `source: fallback`. |
| 7 | Во время игры выбрать в комнате другой источник | В listen — `deselected`. |
| 8 | Дать треку доиграть до конца | `state: ended`, драйвер сам ничего не запускает. |
| 9 | Перезагрузить контроллер | `Pairing Code` тот же, `Paired With` сохранился, `Bridge Status` снова ONLINE. |
| 10 | Выбрать Yandex Relay в списке Listen | Подсказка «Скажите станции…», в listen — `selected`. |

## 5. Что прислать, если что-то не так

Lua Output с Debug Mode = On. Особенно строки:
- `proxy INTERNET_RADIO_SELECTED {…}` — есть ли там `ROOM_ID` и `QUEUE_INFO`;
- `proxy PAUSE {…}` после `relayctl pause` — вернулась ли команда комнаты с `ROOM_ID`;
- `room map …` — формат карты очередей;
- любые `[ERROR]`.

## 6. После проверки

`relayctl listen` пересопрягает драйвер с ПК. Когда будет готов c4_relay, сопряжение с HA выполнится заново из мастера интеграции.
