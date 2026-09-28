# yandex-relay — контекст проекта

## Назначение проекта
Голосом включаешь музыку на Яндекс Станции — она играет в мультируме Control4 в комнате станции. Станция остаётся мастером очереди (через AlexxIT YandexStation в Home Assistant), Control4 только воспроизводит. Разговоры с Алисой трансляцию не запускают. Позже: разная музыка в разных комнатах, объединение комнат в сессию C4.

Полный дизайн, протокол и этапы: `docs/DESIGN.md`. Читать перед любой работой.

## Технологии
- Control4 DriverWorks (Lua), `media_service` proxy, HTTP-сервер на `C4:CreateServer`. Контроллер CA-5 / CORE 5.
- Home Assistant custom integration (Python, config flow).
- Зависимость на стороне HA: AlexxIT YandexStation, станции в локальном режиме (уже установлено).

## Важные файлы и папки
- `c4-driver/` — драйвер Yandex Relay: `driver.lua`, `driver.xml`, `www/icons/`, `build.py`, `tests/` (LuaJIT + заглушки C4).
- `tools/relayctl.py` — управление драйвером напрямую, без HA (проверка на объекте).
- `custom_components/c4_relay/` — интеграция HA (в корне, как требует HACS); `hacs.json`; `ha/tests/` — её тесты (чистые функции + ядро HA с моком драйвера).
- `docs/DESIGN.md` — дизайн и решения; `docs/TODO.md` — задачи следующих версий.
- Референсы (только читать, не менять): `D:\CLAUDE\yandex_music_driver` (SELECT_INTERNET_RADIO, now playing), `D:\CLAUDE\tunein_extracted` (очереди по `QUEUE_ID`).

## Правила работы с проектом
- Работать только внутри папки этого проекта, если не указано иное.
- Перед изменением кода читать релевантные файлы проекта.
- Не удалять существующую логику без явного подтверждения.
- При изменении скриптов показывать полный итоговый файл.
- Сохранять стиль и структуру существующего кода.
- **Универсальность:** никаких комнат, ID, имён станций, адресов под конкретный объект в коде и дефолтах. Комнаты — из `C4:GetProjectItems`, станции и привязка — из HA.
- **Репозиторий публичный** (github.com/xmaxzzz/yandex-relay, нужно для HACS). Секреты: Pairing Code и webhook URL не хардкодить и не коммитить; реальные IP объектов в тестах и доках не использовать (только 192.0.2.x). Long-lived token HA в драйвер не класть: связь в обратную сторону идёт через webhook.
- **Версии драйвера:** при каждом функциональном изменении `DRIVER_VERSION` + блок changelog сверху `driver.lua` (новые сверху, старые не удалять) + `<version>` и свойство `Driver Version` в `driver.xml`.
- `.c4z` = zip с `driver.lua`, `driver.xml`, `www/icons/*.png` в корне, пути через `/`. В git только текущий `c4-driver/yandex_relay.c4z`.

## Команды проверки
```bash
# Lua: синтаксис через AST-парсер
python -c "from luaparser import ast; ast.parse(open('c4-driver/driver.lua', encoding='utf-8').read()); print('lua ok')"
# Поведение драйвера на LuaJIT 2.1 (как DriverWorks jit=1) с заглушками C4 API: pip install lupa
python c4-driver/tests/test_driver.py
# Сборка .c4z (заодно сверяет версии driver.lua / driver.xml)
python c4-driver/build.py
# HA-интеграция: настоящее ядро HA (2026.x) в Docker, образ собирается один раз
docker build -t c4relay-hatest ha/tests
docker run --rm -v "D:/CLAUDE/yandex-relay:/work" -w /work/ha c4relay-hatest python -m pytest -q -p no:cacheprovider
```
(в Git Bash перед `docker run` нужен `MSYS_NO_PATHCONV=1`, иначе пути `/work` портятся). Установка интеграции: `docs/HA-INSTALL.md`.
Тесты не заменяют объект: заглушки повторяют то, что C4 *должен* присылать. Проверка на объекте — `docs/STAGE1-SITE-TEST.md` (Debug Mode = On, Lua Output, `tools/relayctl.py`). Каждое новое поведение C4, увиденное на объекте, фиксировать тестом в `c4-driver/tests/`.

## AutoMemory
Запоминать только устойчивые факты по этому проекту:
- архитектурные решения;
- используемые устройства и адреса;
- принятые соглашения;
- особенности интеграций;
- пользовательские правила именно для этого проекта.

Не запоминать временные задачи, одноразовые ошибки и черновые гипотезы.
