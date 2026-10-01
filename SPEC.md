# Clipboard-Manager für GNOME/Wayland – Spezifikation

Zielsystem: Fedora 44, GNOME Shell 50, Wayland, NVIDIA (proprietärer Treiber).
Oberstes Ziel: **Stabilität über GNOME-/Wayland-Updates hinweg.** Features sind zweitrangig.

## Ausgangsbasis: Fork von Clipman

Wir bauen nicht bei null an, sondern forken **Clipman** (https://github.com/MohammedEl-sayedAhmed/clipman, Apache-2.0).
Clipman hat bereits genau die Zielarchitektur:

- GNOME-Shell-Extension erkennt Clipboard-Änderungen über `Meta.Selection` `owner-changed`, liest per MIME-Fallback-Kette, sendet per D-Bus an den Daemon
- Daemon in Python + GTK 4 + libadwaita, Speicherung in SQLite (WAL) unter `~/.local/share/clipman/`
- Text und Bilder, SHA256-Deduplizierung, Suche, Pins (vom Pruning ausgenommen), Limit 50–5000
- Super+V öffnet Popup nahe dem Cursor, Einfügen per `wl-copy` + virtuellem Clutter-Keyboard (terminal-aware Ctrl+Shift+V)
- systemd-User-Service mit Auto-Restart, Testsuite, install.sh/uninstall.sh

Lizenzpflichten beachten: LICENSE und NOTICE behalten, Änderungen im CHANGELOG bzw. NOTICE vermerken.

### Phase 0 – Bestandsaufnahme (noch keine Änderungen)

1. README.md, ARCHITECTURE.md, `extension/extension.js`, `clipman/window.py`, `clipman/database.py` lesen.
2. Kurzbericht: Wie wird das Fenster "nahe dem Cursor" positioniert? Wo liegt das Pruning? Wie werden Bilder gespeichert und aufgeräumt? Was davon ist fragil gegenüber GNOME-Updates?
3. `install.sh` auf Fedora-44-Tauglichkeit prüfen (vermutlich apt-basiert). Fedora-Pakete: `python3-gobject gtk4 libadwaita python3-dbus wl-clipboard`.
4. Extension auf GNOME-50-Kompatibilität prüfen (`metadata.json` `shell-version`, verwendete APIs).
5. Plan für die Anpassungen unten vorlegen, erst nach Freigabe umsetzen.

### Phase 1 – Lauffähig auf Fedora 44 / GNOME 50

- `install.sh` um Fedora-Zweig (dnf) erweitern oder separates `install-fedora.sh`
- `shell-version` um "50" ergänzen, API-Brüche beheben
- Extension zuerst in verschachtelter Shell testen: `dbus-run-session gnome-shell --devkit --wayland`
- Vorhandene Super+V-Belegung von `win11-clipboard-history` auf diesem System entfernen (Entscheidung Phase 0)
- Vor der Installation prüfen, ob Super+V anderweitig belegt ist (`org.gnome.shell.keybindings toggle-message-tray`, eigene Tastenkürzel in `media-keys/custom-keybindings`, andere Clipboard-Extensions) und darauf hinweisen

## Anpassungen gegenüber Clipman (Phase 2)

Bestehende Clipman-Struktur und -Konventionen beibehalten, Änderungen möglichst klein und gekapselt halten, damit Upstream-Updates weiterhin gemergt werden können.

### UI

Schlicht, Dark Mode als Standard. Oberste Ebene als Tabs:

1. **Verlauf** – die letzten 50 Einträge (Standard-Limit auf 50 setzen), neuester oben; Pins sind chronologisch einsortiert (nicht oben fixiert)
   - Textkacheln: Vorschau auf 1, 2 oder 3 Zeilen begrenzt (einstellbar), mit Ellipsis
   - Bilder als Kachel mit **Thumbnail direkt in der Liste** (nicht nur Hover-Tooltip)
   - Thumbnail-Größe per Schieberegler einstellbar (z. B. 80–400 px Höhe), sofort wirksam
2. **Angepinnt** – eigener Tab mit allen Pins, gleiche Kacheldarstellung (bisherige Filter Text/Bilder können als Unterfilter bleiben oder entfallen)
3. **Emojis** – Raster mit Suchfeld, Kategorien, zuletzt verwendete oben
4. **Sonderzeichen** – Raster gängiger Symbole (Pfeile, Währungen, Mathe, Typografie, Griechisch, Box-Zeichen) mit Suchfeld
5. Snippets aus Clipman dürfen bleiben, aber nicht im Weg sein

Bedienung: komplett per Tastatur (Pfeiltasten, Enter, Strg+Tab für Tabs, Tippen startet Suche), Klick fügt ein.

### Fenstergröße und -position

- Fenster in Höhe (und Breite) frei veränderbar, Größe wird gespeichert und wiederhergestellt
- Öffnet in der Nähe des Mauszeigers mit dem **zuletzt gemerkten Versatz** zum Cursor (Standard 0/0), am Monitorrand so korrigiert, dass es vollständig sichtbar ist
- Nach dem Öffnen frei verschiebbar; beim Schließen wird der neue Versatz = Fensterposition minus Mausposition zum Öffnungszeitpunkt gespeichert
- Da eine GTK-App unter Wayland ihre eigene Position nicht kennt, übernimmt die **Extension** das Lesen der Position (`Meta.Window`, `position-changed`) und das Platzieren (`move_frame()`)
- Der Versatz wird in der **SQLite-Einstellungstabelle des Daemons** gespeichert (kein GSettings-Schema der Extension). Die Extension bleibt zustandslos:
  - Beim Ausblenden meldet sie nur die finale Fensterposition und die Mausposition zum Öffnungszeitpunkt per D-Bus an den Daemon; der Daemon berechnet und speichert den Versatz.
  - Beim Öffnen übergibt der Daemon den Versatz an die Extension; die Randkorrektur (Fenster vollständig sichtbar) bleibt in der Extension, weil nur sie die Monitorgeometrie kennt.
  - Alle D-Bus-Aufrufe asynchron mit Timeout. Läuft der Daemon nicht oder antwortet nicht, blockiert die Extension nichts und nimmt Versatz 0/0.
- Optionaler Modus: "relativ zum Mauszeiger" (Standard) oder "feste Position"; Aktion "Versatz zurücksetzen"

### Cache und Stabilität

- Bild- und Thumbnail-Dateien beim Pruning zuverlässig mitlöschen; beim Start verwaiste Dateien aufräumen
- Thumbnails pro Größe cachen, lazy und in einem Hintergrund-Thread erzeugen, bei Größenwechsel alte verwerfen
- Obergrenzen für Aufnahme: Bilder z. B. 20 MB, Text z. B. 1 MB, Überschreitungen loggen
- Start darf nie blockieren: Historie asynchron laden; beschädigte Datenbank sichern und leer starten statt abzustürzen
- Extension: `enable()`/`disable()` sofort zurückkehren und vollständig aufräumen, alle Handler in try/catch, D-Bus-Aufrufe asynchron mit Timeout, nur öffentliche APIs, keine Monkey-Patches
- Für jede neue Funktion Tests ergänzen, bestehende Testsuite muss grün bleiben

## Vorgehen

1. Phase 0: Bestandsaufnahme und Plan, keine Änderungen
2. Phase 1: Fedora-44-/GNOME-50-Lauffähigkeit, in verschachtelter Shell testen, dann installieren
3. Phase 2 in dieser Reihenfolge: Limit 50 + Pins-Tab → Thumbnails in der Liste + Größenregler → Zeilenzahl → Fenstergröße/-versatz → Cache-Härtung → Emoji- und Sonderzeichen-Tabs
4. Nach jedem Schritt committen, damit sich Änderungen einzeln zurückrollen lassen
