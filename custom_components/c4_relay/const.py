"""Constants for the Control4 Yandex Relay integration."""

DOMAIN = "c4_relay"

CONF_PAIRING_CODE = "pairing_code"
CONF_HA_URL = "ha_url"          # URL the Control4 controller uses to reach this HA
CONF_WEBHOOK_ID = "webhook_id"
CONF_BINDINGS = "bindings"      # {station media_player entity_id: C4 room id}

DEFAULT_PORT = 18765

# AlexxIT YandexStation
YANDEX_DOMAIN = "yandex_station"
SOURCE_STATION = "Станция"      # station source that means "no streaming"

NO_ROOM = "none"
