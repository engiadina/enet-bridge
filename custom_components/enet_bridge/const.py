"""Konstanten der eNet-Integration."""

DOMAIN = "enet_bridge"
MANUFACTURER = "Gira/Jung (eNet)"
SENSOR_TYPEN = {"DVT_SF1S"}  # Sonnensensor: Helligkeit in Lux, Batterie- bzw. Akkuzustand

CONF_PORT = "port"
CONF_ANLAGE = "anlage"  # Pfad zu anlage.json (Import bei der Einrichtung bzw. Bestand vor dem Speicher)
CONF_ANLAGE_DATEN = "anlage_daten"  # Anlage aus der Einrichtung (Export/neu), wandert beim 1. Start in den Speicher
DEFAULT_ANLAGE = "/config/enet/anlage.json"
DEFAULT_DEFINITIONEN = "/config/enet/definitionen.json"  # Gerätedefinitionen (tools/katalog.py)

PROTO_VERSION = 1

# Kanalarten aus anlage.json (tools/iex.py)
ART_SCHALTEN = "schalten"
ART_DIMMEN = "dimmen"
ART_TASTE = "taste"
ART_WIPPE = "wippe"
