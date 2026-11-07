#!/usr/bin/env python3
"""
Liest einen eNet-Server-Projektexport (.iex) und schreibt die Anlage als JSON.

  python3 tools/iex.py "Anlage.iex" -o anlage.json

Die Auswertung selbst steht in custom_components/enet_bridge/iex.py (gemeinsam mit der Einrichtung in HA).
"""
import json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "custom_components", "enet_bridge"))
from iex import read_iex  # noqa: E402


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    anlage = read_iex(sys.argv[1])
    out = json.dumps(anlage, indent=2, ensure_ascii=False)
    if "-o" in sys.argv:
        with open(sys.argv[sys.argv.index("-o") + 1], "w", encoding="utf-8") as f:
            f.write(out + "\n")
    else:
        print(out)


if __name__ == "__main__":
    main()
