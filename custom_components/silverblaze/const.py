"""Constants for the Silverblaze integration (no Home Assistant imports)."""

DOMAIN = "silverblaze"

CONF_UTILITY = "utility"
CONF_IMPORT_FROM = "import_from"
CONF_UPDATE_INTERVAL = "update_interval_hours"

DEFAULT_UPDATE_INTERVAL_HOURS = 12
MIN_UPDATE_INTERVAL_HOURS = 6
MAX_UPDATE_INTERVAL_HOURS = 24

CORRECTION_WINDOW_DAYS = 30
