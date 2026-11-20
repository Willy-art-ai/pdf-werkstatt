#!/bin/bash
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 wurde nicht gefunden. Bitte von https://www.python.org/downloads/ installieren."
  read -p "Enter zum Schließen ..."
  exit 1
fi
echo "Prüfe benötigte Bibliotheken ..."
PIP="python3 -m pip install --quiet --disable-pip-version-check --user"
$PIP pikepdf pillow reportlab tkinterdnd2 2>/dev/null || $PIP --break-system-packages pikepdf pillow reportlab tkinterdnd2
# optional: iPhone-Fotos (.heic)
$PIP pillow-heif >/dev/null 2>&1 || $PIP --break-system-packages pillow-heif >/dev/null 2>&1
python3 pdf_werkstatt.py "$@"
