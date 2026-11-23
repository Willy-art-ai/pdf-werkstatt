# Prüfbericht Web-App

Automatisierte Testreihe: Die App wird in einem Browser (Chromium) bedient, jedes Ergebnis wird unabhängig geprüft – Seitenzahl und -reihenfolge (pdfinfo/pdftotext), Gültigkeit (qpdf --check), Größenlimits, Darstellung (Pixelanalyse), Formularfelder (pypdf) und Verschlüsselung (qpdf).

**Ergebnis: 37 von 37 Szenarien bestanden.** Fehler im Browser während des Laufs: keine.

| Nr. | Szenario | Ergebnis | Details |
|---|---|---|---|
| S01 | Große Foto-PDF (90 MB) öffnen | ✅ bestanden | 26 Seiten, 3.8 s bis zur Anzeige |
| S02 | Komprimieren „leicht“ | ✅ bestanden | 90,7 → 20.1 MB, Text durchsuchbar: true, qpdf ok |
| S03 | Komprimieren „mittel“ | ✅ bestanden | 90,7 → 5.2 MB, Text durchsuchbar: true, qpdf ok |
| S04 | Komprimieren „stark“ | ✅ bestanden | 90,7 → 1.5 MB, Text durchsuchbar: true, qpdf ok |
| S04b | Stufen werden kleiner | ✅ bestanden | leicht 20.1 > mittel 5.2 > stark 1.5 |
| S05 | Graustufen (Mittel + Graustufen) | ✅ bestanden | mittlere Farbsättigung 0 (0 = grau), Datei 4_gross_fotos_graustufen.pdf |
| S06 | Teilen nach Größe (15 MB, Mail-Kodierung) | ✅ bestanden | 9 Teile, max. 10.4 MB (Limit 10,9), 26 Seiten, Startseiten 1,4,7,10,13,16,19,22, |
| S07 | Teilen in 3 gleich große Teile | ✅ bestanden | 31.1 MB / 31.2 MB / 28.4 MB |
| S08 | Komprimieren + nach Seiten (alle 10) | ✅ bestanden | Vorschau „→ 3 Teile“, Teile mit 10/10/6 Seiten, Teil 3 beginnt mit Seite 21 |
| S09 | 250 Seiten nach Seiten teilen (alle 100) | ✅ bestanden | 100/100/50 Seiten, Anfang: Seite 1 / Seite 101 / Seite 201 |
| S10 | Nach Seiten bei kurzer PDF → kein Teilen nötig | ✅ bestanden | Das Dokument hat nicht mehr als 100 Seiten – kein Teilen nötig. |
| S11 | Seitenbereich „1-3, 7, 10-“ auswählen | ✅ bestanden | 245 Seiten ausgewählt (erwartet 3 + 1 + 241 = 245) |
| S12 | Bereich löschen („10-“) und drehen („1-2“) | ✅ bestanden | 9 Seiten übrig, Drehung [90, 90, 0, 0, 0, 0, 0, 0, 0] |
| S13 | Seiten extrahieren per Bereich („2-3“) | ✅ bestanden | 25_250_seiten_Seiten_2-3.pdf: 2 Seiten (Seite 2 und 3) |
| S14 | Handyfotos mit EXIF-Drehung (1, 3, 6, 8) | ✅ bestanden | quer-oben-rot quer-oben-rot quer-oben-rot quer-oben-rot (alle müssen quer mit rotem Balken oben sein) |
| S15 | 16-Bit-PNG wird korrekt dargestellt | ✅ bestanden | Helligkeit links 24, rechts 230 (Verlauf dunkel → hell) |
| S16 | Animiertes GIF → genau 1 Seite | ✅ bestanden | 1 Seite(n) |
| S17 | Transparentes PNG und WEBP | ✅ bestanden | 2 Seiten, Pixel Mitte/Ecke (79, 149, 227) (255, 255, 255) |
| S18 | HEIC und TIFF: verständliche Meldung statt Absturz | ✅ bestanden | „foto.heic“: Das Bildformat HEIC kann der Browser nicht lesen. Bitte vorher als JPG oder PNG speichern (z. B. mit der Windows-Fotos-App: „Speichern unter“). / „ |
| S19 | Leere und gefälschte PDF: klare Meldung | ✅ bestanden | „leer.pdf“ ist leer (0 Byte) und wurde übersprungen. / „text_als.pdf“ konnte nicht geöffnet werden: Die Datei ist keine gültige PDF oder ist beschädigt. |
| S20 | CMYK-Bild: Komprimieren bricht nicht | ✅ bestanden | gültige PDF, 78 KB, Hinweis: ja |
| S21 | Dateiname mit Umlauten und chinesischen Zeichen | ✅ bestanden | geöffnet: 3 Seiten, Speichername „Rechnung Müller – Größe 文件.pdf“ (Download-Name im Test-Browser nicht prüfbar) |
| S22 | Formular bleibt nach Teilen ausfüllbar | ✅ bestanden | Teil 1 (formular_1.pdf) hat Felder: Name,Zustimmung |
| S23 | Formular bleibt nach Zusammenführen ausfüllbar | ✅ bestanden | 253 Seiten, Felder: Name,Zustimmung, Ausfüllen mit pypdf: Test |
| S24 | Seitenzahlen „- n -“ ab Seite 2 (Deckblatt ohne Nummer) | ✅ bestanden | S.1: "Seite 1" · S.2: "Seite 2\n\n-2-" · S.250: "Seite 250\n\n- 250 -" · Hinweis: Seite 1 (z. B. Deckblatt) ohne Nummer. Seite 2 bek… |
| S25 | Wasserzeichen auf gedrehter Seite | ✅ bestanden | Seite 2 (gedreht): Text "Hallo Welt Seite 2 – Rechnung Müller H C LI U A TR R VE Vert", 42 rötliche Pixel in der Seitenmitte |
| S26 | Passwort setzen (AES-256) | ✅ bestanden | R6/AES-256, richtiges Passwort ok, falsches abgelehnt |
| S27 | Passwort entfernen (AES-256 von qpdf) | ✅ bestanden | 35_aes_256_qpdf_ohne_Passwort.pdf: unverschlüsselt, Text und Formular erhalten |
| S28 | Ältere Verschlüsselung (AES-128) mit Passwort | ✅ bestanden | angezeigt, gespeichert als ungeschützte Kopie (Seiten als Bild) · Hinweis: ja |
| S29 | PDF nur mit Rechte-Passwort (ohne Öffnen-Passwort) | ✅ bestanden | öffnet ohne Abfrage, Kopie ohne Schutz, Text erhalten |
| S30 | Stapelverarbeitung (5 Dateien, 2 davon defekt) | ✅ bestanden | 3 von 5 Datei(en) verarbeitet · zusammen 91,1 MB → 5,6 MB · 3 neue Datei(en) · ZIP: gross_fotos_komprimiert.pdf, 400_seiten_komprimiert.pdf, formular_komprimiert.pdf |
| S31 | Stapel: Teilen nach Größe (10 MB, ohne Komprimieren) | ✅ bestanden | 14 Teile, max. 6.9 MB (Limit 7,3) |
| S32 | Texterkennung (gescannte Seite) | ✅ bestanden | erkannter Text im Ergebnis: „180 Euro. Die Kaution von drei Monatsmieten ist vor Einzug zu“ |
| S33 | Kommentare: löschen und fest einbrennen | ✅ bestanden | verbleibende Anmerkungen: ['/Widget', '/Widget', '/Text'] (nur Notiz + Formularfelder) |
| S34 | Zusammenführen: Reihenfolge per Maus ziehen | ✅ bestanden | Seite 3, Seite 1, Seite 2 |
| S35 | Neue leere PDF + Text + Speichern | ✅ bestanden | 2 Seiten, Page size:       841.89 x 595.28 pts (A4), Text: "Neu erstellt äöü" |
| S36 | Doppelte und leere Seiten in einer Akte finden | ✅ bestanden | 13 doppelte Seiten in 3 Blöcken und 2 leere Seiten → 34 − 15 = 19 Seiten, Gutachten noch 5×1 Seite, alle 3 Rechnungen erhalten |

## Während der Prüfung gefundene und behobene Fehler

1. **PDFs, die von anderen Programmen verschlüsselt wurden** (z. B. qpdf, Acrobat), ließen sich öffnen, aber nicht speichern: Ihre Objekt-Container wurden beim Entschlüsseln nicht ausgepackt. Behoben; zusätzlich übernimmt die App bei einer unvollständigen Entschlüsselung die Seiten automatisch als Bild statt abzubrechen.
2. **Formularfelder** waren nach Teilen, Extrahieren oder Zusammenführen nicht mehr ausfüllbar. Behoben.
3. **Doppelte Seiten (Entwicklung):** Ein reiner Bildvergleich verwechselte gescannte Seiten mit gleichem Aufbau, aber anderen Zahlen. Deshalb gilt jetzt: Seiten sind nur doppelt, wenn ihr Text übereinstimmt – alle Zahlen exakt; gescannte Seiten werden dafür per Texterkennung gelesen. Nur optisch ähnliche Treffer werden nie automatisch vorausgewählt.

## Hinweise

- Ältere Verschlüsselungen (RC4, AES-128) werden angezeigt; beim Speichern werden die Seiten als Bild übernommen.
- HEIC- und TIFF-Bilder kann ein Browser in der Regel nicht lesen – die App zeigt dann einen Hinweis, wie man sie als JPG speichert.
- Download-Namen mit Umlauten konnte die Testumgebung nicht prüfen (deren Browser ersetzt sie durch „download“); geprüft wurde, dass die App den korrekten Namen übergibt.
