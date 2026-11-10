"""Constants of the eNet integration."""

DOMAIN = "enet_bridge"
MANUFACTURER = "Gira/Jung (eNet)"
SENSOR_TYPES = {"DVT_SF1S"}  # sun sensor: brightness in lux, battery/rechargeable battery state

CONF_PORT = "port"
CONF_INSTALLATION = "anlage"  # path to anlage.json (import during setup, or legacy data from before the store)
CONF_INSTALLATION_DATA = "anlage_daten"  # installation from setup (export/new), moved into the store on first start
DEFAULT_INSTALLATION = "/config/enet/anlage.json"
DEFAULT_DEFINITIONS = "/config/enet/definitionen.json"  # device definitions (tools/katalog.py)

PROTO_VERSION = 1

# channel kinds from anlage.json (tools/iex.py)
KIND_SWITCH = "schalten"
KIND_DIMMER = "dimmen"
KIND_BLIND = "jalousie"
KIND_KEY = "taste"
KIND_ROCKER = "wippe"
