#!/usr/bin/env python3
"""
PDF-Werkstatt – Komprimieren, Teilen, Zusammenfügen, Seiten bearbeiten,
Beschriften (Seitenzahlen / Wasserzeichen) und Passwortschutz.

Start mit Fenster:        python pdf_werkstatt.py
Start ohne Fenster (CLI): python pdf_werkstatt.py datei.pdf --komprimieren mittel --teilen 15
                          python pdf_werkstatt.py datei.pdf --seiten 100

Benötigt: pikepdf, pillow, reportlab
Optional: tkinterdnd2 (Drag & Drop), pillow-heif (iPhone-Fotos .heic)
"""
import argparse
import io
import math
import queue
import re
import shutil
import sys
import tempfile
import threading
import warnings
import zlib
from pathlib import Path

try:
    import pikepdf
    from pikepdf import Name, PdfImage
    from PIL import Image, ImageOps, ImageSequence
except ImportError:
    print("Fehlende Bibliotheken. Bitte installieren mit:\n  python -m pip install pikepdf pillow reportlab")
    sys.exit(1)

try:                                          # für 16-Bit-Bilder – kommt mit pikepdf/Pillow meist mit
    import numpy as np
except ImportError:
    np = None

try:                                          # iPhone-Fotos (.heic) – optional
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass

# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------
STUFEN = {
    "leicht": {"name": "Leicht – beste Qualität", "max_px": 2400, "quality": 85},
    "mittel": {"name": "Mittel – empfohlen", "max_px": 1600, "quality": 70},
    "stark":  {"name": "Stark – kleinste Datei", "max_px": 1100, "quality": 50},
}
MB = 1_000_000            # Mail-Anbieter rechnen meist so (sicherer als 1024²)
MAIL_FAKTOR = 1.37        # Anhänge werden per Base64 kodiert: ca. +37 % in der Mail
BILD_ENDUNGEN = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".heic", ".heif"}
A4 = (595.28, 841.89)

SEITENZAHL_FORMATE = {
    "Seite 1 von 10": "Seite {n} von {gesamt}",
    "Seite 1": "Seite {n}",
    "1 / 10": "{n} / {gesamt}",
    "1": "{n}",
    "– 1 –": "– {n} –",
}
SEITENZAHL_POSITIONEN = ["unten mittig", "unten rechts", "unten links", "oben rechts", "oben mittig"]
DECKKRAFT = {"leicht (15 %)": 0.15, "mittel (25 %)": 0.25, "kräftig (40 %)": 0.40}


class Hinweis(Exception):
    """Fehler mit einer verständlichen Meldung für den Nutzer."""


def fmt(n_bytes: float) -> str:
    if n_bytes < 100_000:                     # kleine Dateien in KB statt „0.0 MB“
        return f"{max(1, round(n_bytes / 1000))} KB"
    return f"{n_bytes / MB:.1f} MB"


def freier_name(ordner: Path, stamm: str, endung=".pdf") -> Path:
    """Liefert einen Dateinamen, der noch nicht existiert (nie etwas überschreiben)."""
    p = ordner / f"{stamm}{endung}"
    i = 2
    while p.exists():
        p = ordner / f"{stamm} ({i}){endung}"
        i += 1
    return p


def oeffnen(pfad, passwort=""):
    try:
        return pikepdf.open(pfad, password=passwort)
    except pikepdf.PasswordError:
        raise Hinweis(f"„{Path(pfad).name}“ ist passwortgeschützt.\n\n"
                      "Im Reiter „Passwort“ kannst du den Schutz entfernen (wenn du das Passwort kennst).")
    except FileNotFoundError:
        raise Hinweis(f"„{Path(pfad).name}“ wurde nicht gefunden (verschoben oder gelöscht?).")
    except (pikepdf.PdfError, ValueError, OSError) as e:
        if isinstance(e, PermissionError):
            raise
        raise Hinweis(f"„{Path(pfad).name}“ ist beschädigt oder keine echte PDF-Datei "
                      f"und kann nicht gelesen werden.\n\nDetails: {str(e).split(': ')[-1][:200]}")


def seiten_kopieren(ziel, quelle, indizes):
    """Hängt Seiten aus quelle an ziel an – möglichst mit funktionierenden Formularfeldern."""
    indizes = list(indizes)
    vorher = len(ziel.pages)
    if hasattr(ziel, "add_pages_from"):           # neuere pikepdf-Versionen: Formulare bleiben ausfüllbar
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                ziel.add_pages_from(quelle, indizes)
            return
        except Exception:
            while len(ziel.pages) > vorher:        # halb kopierte Seiten wieder entfernen
                del ziel.pages[-1]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ziel.pages.extend([quelle.pages[i] for i in indizes])


def seitenzahl(pfad) -> int:
    with oeffnen(pfad) as pdf:
        return len(pdf.pages)


def seiten_parsen(text: str, n: int) -> list:
    """'1-3, 7, 10-' → [0,1,2,6,9,…]  (0-basiert, sortiert). Leer oder 'alle' = alle Seiten."""
    text = text.strip().lower().replace(" ", "").replace("–", "-").replace("—", "-")
    if text in ("", "alle"):
        return list(range(n))
    erg = set()
    for teil in re.split(r"[,;]+", text):
        if not teil:
            continue
        m = re.fullmatch(r"(\d+)?-(\d+)?", teil)
        if m:
            a = int(m.group(1)) if m.group(1) else 1
            b = int(m.group(2)) if m.group(2) else n
        elif teil.isdigit():
            a = b = int(teil)
        else:
            raise Hinweis(f"„{teil}“ verstehe ich nicht.\n\nBeispiele: 5   oder   1-3, 7, 10-   (10- = ab Seite 10 bis Ende)")
        if a > b:
            a, b = b, a
        if a < 1 or b > n:
            raise Hinweis(f"Die Angabe „{teil}“ passt nicht – die PDF hat {n} Seiten.")
        erg.update(range(a - 1, b))
    if not erg:
        raise Hinweis("Bitte Seiten angeben, z. B. 1-3, 7")
    return sorted(erg)


def bereiche_text(idx: list) -> str:
    """[0,1,2,6] → '1-3,7'"""
    teile, start, prev = [], None, None
    for i in idx:
        if start is None:
            start = prev = i
        elif i == prev + 1:
            prev = i
        else:
            teile.append((start, prev))
            start = prev = i
    if start is not None:
        teile.append((start, prev))
    return ",".join(f"{a + 1}" if a == b else f"{a + 1}-{b + 1}" for a, b in teile)


def _speichern(pdf, ziel):
    pdf.save(ziel, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)


# ---------------------------------------------------------------------------
# Komprimieren
# ---------------------------------------------------------------------------
def _bilder_finden(resources, besucht):
    """Liefert alle Bild-XObjects einer Seite, auch in verschachtelten Formularen."""
    if resources is None or "/XObject" not in resources:
        return
    for _, xobj in resources.XObject.items():
        try:
            key = xobj.objgen
        except Exception:
            continue
        if key in besucht:
            continue
        besucht.add(key)
        subtype = xobj.get("/Subtype")
        if subtype == Name.Image:
            yield xobj
        elif subtype == Name.Form:
            yield from _bilder_finden(xobj.get("/Resources"), besucht)


def _bild_neu_kodieren(obj, max_px: int, quality: int, grau=False) -> int:
    """Kodiert ein Bild als JPEG neu (ggf. verkleinert / grau). Gibt die gesparten Bytes zurück."""
    if obj.get("/ImageMask", False):
        return 0
    if "/Decode" in obj or isinstance(obj.get("/Mask"), pikepdf.Array):
        return 0
    bpc = int(obj.get("/BitsPerComponent", 8))
    if bpc not in (2, 4, 8):
        return 0
    w, h = int(obj.Width), int(obj.Height)
    if w * h < 40_000:                       # winzige Bilder lohnen nicht
        return 0
    filt = obj.get("/Filter")
    filter_liste = [str(f) for f in filt] if isinstance(filt, pikepdf.Array) else ([str(filt)] if filt else [])
    if any(f in ("/JBIG2Decode", "/CCITTFaxDecode") for f in filter_liste):
        return 0                              # Schwarz-Weiß-Scans sind schon optimal

    alt = len(obj.read_raw_bytes())
    pil = PdfImage(obj).as_pil_image()
    if pil.mode not in ("RGB", "L"):
        pil = pil.convert("L" if pil.mode in ("LA", "1") else "RGB")
    if grau and pil.mode != "L":
        pil = pil.convert("L")

    faktor = min(1.0, max_px / max(w, h))
    if faktor < 1.0:
        pil = pil.resize((max(1, round(w * faktor)), max(1, round(h * faktor))), Image.LANCZOS)

    buf = io.BytesIO()
    pil.save(buf, "JPEG", quality=quality, optimize=True)
    neu = buf.getvalue()
    if not grau and len(neu) >= alt * 0.95:   # keine echte Ersparnis → Original behalten
        return 0

    obj.write(neu, filter=Name.DCTDecode)
    if "/DecodeParms" in obj:
        del obj["/DecodeParms"]
    obj.Width, obj.Height = pil.width, pil.height
    obj.ColorSpace = Name.DeviceRGB if pil.mode == "RGB" else Name.DeviceGray
    obj.BitsPerComponent = 8
    return alt - len(neu)


def komprimieren(quelle: Path, ziel: Path, stufe: str, fortschritt=None, grau=False) -> int:
    """Komprimiert quelle → ziel. Gibt die neue Dateigröße zurück."""
    p = STUFEN[stufe]
    with oeffnen(quelle) as pdf:
        besucht = set()
        n = len(pdf.pages)
        for i, seite in enumerate(pdf.pages):
            for bild in list(_bilder_finden(seite.obj.get("/Resources"), besucht)):
                try:
                    _bild_neu_kodieren(bild, p["max_px"], p["quality"], grau)
                except Exception:
                    pass                      # exotische Bildformate unverändert lassen
            if fortschritt:
                fortschritt((i + 1) / n * 0.9, f"Komprimiere Seite {i + 1} von {n} …")
        try:
            pdf.remove_unreferenced_resources()
        except Exception:
            pass
        if fortschritt:
            fortschritt(0.95, "Speichere komprimierte Datei …")
        pdf.save(ziel, compress_streams=True, recompress_flate=True,
                 object_stream_mode=pikepdf.ObjectStreamMode.generate)
    return ziel.stat().st_size


# ---------------------------------------------------------------------------
# Teilen
# ---------------------------------------------------------------------------
def teilen(quelle: Path, ausgabe_ordner: Path, max_bytes: int, basisname: str, fortschritt=None):
    """Teilt eine PDF in möglichst wenige Teile ≤ max_bytes. Gibt (Dateien, Warnungen) zurück."""
    warnungen = []
    with oeffnen(quelle) as pdf:
        n = len(pdf.pages)
        durchschnitt = max(1, quelle.stat().st_size / n)

        def baue(a, b):
            neu = pikepdf.new()
            seiten_kopieren(neu, pdf, range(a, b))
            buf = io.BytesIO()
            _speichern(neu, buf)
            return buf.getvalue()

        teile, start = [], 0
        while start < n:
            rest = n - start
            # größte Seitenzahl k finden, bei der der Teil noch unter das Limit passt
            lo, lo_daten, hi = 0, None, rest + 1
            k = max(1, min(rest, int(max_bytes / durchschnitt)))
            while hi - lo > 1:
                daten = baue(start, start + k)
                if len(daten) <= max_bytes:
                    lo, lo_daten = k, daten
                else:
                    hi = k
                if hi - lo <= 1:
                    break
                k = (lo + hi) // 2 if hi <= rest else min(rest, max(lo + 1, int(lo * 1.5)))
            if lo == 0:                       # eine einzelne Seite ist schon zu groß
                lo, lo_daten = 1, baue(start, start + 1)
                warnungen.append(f"Seite {start + 1} allein ist {fmt(len(lo_daten))} groß "
                                 f"und überschreitet das Limit – tipp: vorher stärker komprimieren.")
            teile.append((start + 1, start + lo, lo_daten))
            start += lo
            if fortschritt:
                fortschritt(start / n, f"Teil {len(teile)} fertig (Seiten bis {start} von {n}) …")

    gesamt = len(teile)
    dateien = []
    for i, (von, bis, daten) in enumerate(teile, 1):
        pfad = ausgabe_ordner / (f"{basisname}_Teil_{i}_von_{gesamt}.pdf" if gesamt > 1
                                 else f"{basisname}_optimiert.pdf")
        pfad.write_bytes(daten)
        dateien.append((pfad, von, bis, len(daten)))
    return dateien, warnungen


def teilen_nach_seiten(quelle: Path, ausgabe_ordner: Path, seiten_pro_teil: int, basisname: str, fortschritt=None):
    """Teilt eine PDF in Teile mit je seiten_pro_teil Seiten (letzter Teil ggf. kürzer)."""
    dateien = []
    with oeffnen(quelle) as pdf:
        n = len(pdf.pages)
        gesamt = -(-n // seiten_pro_teil)            # aufrunden
        for i in range(gesamt):
            a, b = i * seiten_pro_teil, min(n, (i + 1) * seiten_pro_teil)
            neu = pikepdf.new()
            seiten_kopieren(neu, pdf, range(a, b))
            pfad = ausgabe_ordner / f"{basisname}_Teil_{i + 1}_von_{gesamt}.pdf"
            _speichern(neu, pfad)
            neu.close()
            dateien.append((pfad, a + 1, b, pfad.stat().st_size))
            if fortschritt:
                fortschritt(b / n, f"Teil {i + 1} von {gesamt} fertig …")
    return dateien


# ---------------------------------------------------------------------------
# Komprimieren & Teilen – Gesamtablauf für eine Datei
# ---------------------------------------------------------------------------
def verarbeiten(quelle, ausgabe_ordner, stufe=None, teil_mb=None, mail_kodierung=True, log=print,
                fortschritt=None, teil_seiten=None, grau=False):
    """teil_mb: nach Größe teilen  |  teil_seiten: alle X Seiten teilen (nur eins von beiden angeben)."""
    quelle = Path(quelle)
    ausgabe_ordner = Path(ausgabe_ordner or quelle.parent)
    ausgabe_ordner.mkdir(parents=True, exist_ok=True)
    stamm = quelle.stem
    orig_groesse = quelle.stat().st_size
    log(f"Datei: {quelle.name}  ({fmt(orig_groesse)}, {seitenzahl(quelle)} Seiten)")

    tmp_ordner = Path(tempfile.mkdtemp(prefix="pdf_werkstatt_"))
    try:
        _ablauf(quelle, ausgabe_ordner, stamm, orig_groesse, tmp_ordner,
                stufe, teil_mb, mail_kodierung, log, fortschritt, teil_seiten, grau)
    finally:
        shutil.rmtree(tmp_ordner, ignore_errors=True)   # Zwischendateien immer aufräumen
    if fortschritt:
        fortschritt(1.0, "Fertig.")
    log("Fertig ✔")


def _ablauf(quelle, ausgabe_ordner, stamm, orig_groesse, tmp_ordner,
            stufe, teil_mb, mail_kodierung, log, fortschritt, teil_seiten=None, grau=False):
    teilen_aktiv = bool(teil_mb or teil_seiten)
    arbeitsdatei = quelle
    tmp = None
    zusatz = f"{stufe}_grau" if grau else stufe
    if stufe:
        log(f"Komprimiere – Stufe: {STUFEN[stufe]['name']}" + (" + Graustufen" if grau else ""))
        prog = (lambda f, m: fortschritt(f * (0.5 if teilen_aktiv else 1), m)) if fortschritt else None
        if teilen_aktiv:
            tmp = tmp_ordner / "zwischenstand.pdf"
            ziel = tmp
        else:
            ziel = ausgabe_ordner / f"{stamm}_komprimiert_{zusatz}.pdf"
        neu = komprimieren(quelle, ziel, stufe, prog, grau)
        ersparnis = (1 - neu / orig_groesse) * 100
        log(f"Komprimiert: {fmt(orig_groesse)} → {fmt(neu)}  ({ersparnis:+.0f} %)".replace("+", "−", 1)
            if ersparnis > 0 else f"Komprimiert: {fmt(neu)} – kaum Ersparnis möglich (PDF ist schon gut optimiert).")
        if neu >= orig_groesse and not grau:
            log("Hinweis: Die komprimierte Version ist nicht kleiner – es wird das Original verwendet.")
            if not teilen_aktiv:
                ziel.unlink(missing_ok=True)
        else:
            arbeitsdatei = ziel
            if not teilen_aktiv:
                log(f"Gespeichert: {ziel}")
        stamm_teile = f"{stamm}_{zusatz}"
    else:
        stamm_teile = stamm

    def komprimierte_behalten(grund):
        """Kein Teilen nötig: ggf. die komprimierte Zwischendatei als Ergebnis speichern."""
        if arbeitsdatei is tmp:
            ziel = ausgabe_ordner / f"{stamm}_komprimiert_{zusatz}.pdf"
            if ziel.exists():
                ziel.unlink()
            shutil.move(str(tmp), str(ziel))   # funktioniert auch über Laufwerke hinweg
            log(f"{grund} – kein Teilen nötig.\nGespeichert: {ziel}")
        else:
            log(f"{grund} – kein Teilen nötig.")

    if teil_seiten:
        n = seitenzahl(arbeitsdatei)
        if n <= teil_seiten:
            komprimierte_behalten(f"Die Datei hat nur {n} Seiten")
        else:
            log(f"Teile alle {teil_seiten} Seiten …")
            prog = (lambda f, m: fortschritt((0.5 + f * 0.5) if stufe else f, m)) if fortschritt else None
            dateien = teilen_nach_seiten(arbeitsdatei, ausgabe_ordner, teil_seiten, stamm_teile, prog)
            for pfad, von, bis, gr in dateien:
                log(f"  {pfad.name}:  Seiten {von}–{bis},  {fmt(gr)}")
            log(f"{len(dateien)} Teile gespeichert in: {ausgabe_ordner}")

    elif teil_mb:
        limit = teil_mb * MB
        if mail_kodierung:
            limit = int(limit / MAIL_FAKTOR)
            log(f"Mail-Limit {teil_mb:g} MB → Anhang darf max. {fmt(limit)} groß sein (Base64-Aufschlag eingerechnet).")
        groesse = arbeitsdatei.stat().st_size
        if groesse <= limit:
            komprimierte_behalten(f"Die Datei ({fmt(groesse)}) passt bereits in eine Mail")
        else:
            log(f"Teile in Stücke à max. {fmt(limit)} …")
            prog = (lambda f, m: fortschritt((0.5 + f * 0.5) if stufe else f, m)) if fortschritt else None
            dateien, warnungen = teilen(arbeitsdatei, ausgabe_ordner, limit, stamm_teile, prog)
            if len(dateien) == 1 and not warnungen:
                log(f"Nach optimiertem Speichern ({fmt(dateien[0][3])}) passt die Datei in eine Mail – "
                    f"kein Teilen nötig.\nGespeichert: {dateien[0][0]}")
                return
            for pfad, von, bis, gr in dateien:
                log(f"  {pfad.name}:  Seiten {von}–{bis},  {fmt(gr)}")
            for w in warnungen:
                log("⚠ " + w)
            log(f"{len(dateien)} Teile gespeichert in: {ausgabe_ordner}")


# ---------------------------------------------------------------------------
# Zusammenfügen (PDFs und Bilder)
# ---------------------------------------------------------------------------
def _bild_seiten_anhaengen(pdf, pfad: Path, seitenformat="A4") -> int:
    """Hängt ein Bild (bei mehrseitigen TIFFs alle Seiten) als PDF-Seite(n) an. Gibt die Seitenzahl zurück."""
    DREHUNG = {2: [Image.FLIP_LEFT_RIGHT], 3: [Image.ROTATE_180], 4: [Image.FLIP_TOP_BOTTOM],
               5: [Image.TRANSPOSE], 6: [Image.ROTATE_270], 7: [Image.TRANSVERSE], 8: [Image.ROTATE_90]}
    with Image.open(pfad) as img:
        ist_jpeg = img.format == "JPEG"
        dpi = img.info.get("dpi")
        try:
            drehung = int(img.getexif().get(0x0112, 1) or 1)
        except Exception:
            drehung = 1
        if img.format == "TIFF":              # mehrseitige TIFFs (Faxe, Scans) → mehrere Seiten
            frames = [f.copy() for f in ImageSequence.Iterator(img)]
        else:                                 # GIF/WEBP-Animationen: nur das erste Bild
            img.seek(0)
            frames = [img.copy()]
    for fr in frames:
        roh = None
        if ist_jpeg and drehung == 1 and fr.mode in ("RGB", "L"):
            roh = pfad.read_bytes()           # JPEG unverändert übernehmen (kein Qualitätsverlust)
        else:
            for t in DREHUNG.get(drehung, []):     # Handyfoto-Ausrichtung (EXIF) anwenden
                fr = fr.transpose(t)
            if fr.mode in ("I;16", "I;16B", "I;16L", "I", "F"):   # 16-Bit-Bilder sauber auf 8 Bit
                a = np.asarray(fr, dtype=np.float64) if np else None
                if a is not None:
                    lo, hi = float(a.min()), float(a.max())
                    a = (a - lo) * (255.0 / (hi - lo)) if hi > lo else a * 0
                    fr = Image.fromarray(a.clip(0, 255).astype("uint8"), "L")
                else:
                    fr = fr.point(lambda v: v / 256).convert("L")
            if fr.mode in ("RGBA", "LA") or (fr.mode == "P" and "transparency" in fr.info):
                fr = fr.convert("RGBA")
                weiss = Image.new("RGB", fr.size, "white")
                weiss.paste(fr, mask=fr.split()[-1])
                fr = weiss
            elif fr.mode not in ("RGB", "L"):
                fr = fr.convert("L" if fr.mode in ("1", "I;16", "I", "F") else "RGB")

        w, h = fr.size
        bild = pikepdf.Stream(pdf, b"")
        bild.Type, bild.Subtype = Name.XObject, Name.Image
        bild.Width, bild.Height, bild.BitsPerComponent = w, h, 8
        bild.ColorSpace = Name.DeviceRGB if fr.mode == "RGB" else Name.DeviceGray
        if roh is not None:
            bild.write(roh, filter=Name.DCTDecode)
        elif ist_jpeg:                        # gedrehtes Foto → wieder als JPEG
            buf = io.BytesIO()
            fr.save(buf, "JPEG", quality=92)
            bild.write(buf.getvalue(), filter=Name.DCTDecode)
        else:                                 # PNG, Screenshots, Scans → verlustfrei
            bild.write(zlib.compress(fr.tobytes(), 6), filter=Name.FlateDecode)

        if seitenformat == "A4":
            pw, ph = (A4[1], A4[0]) if w > h else A4
        else:                                 # Seitengröße wie das Bild
            d = dpi[0] if dpi and 50 <= float(dpi[0]) <= 1200 else None
            if d:
                pw, ph = w * 72 / float(d), h * 72 / float(d)
            else:
                f = A4[1] / max(w, h)
                pw, ph = w * f, h * f
        f = min(pw / w, ph / h)
        dw, dh = w * f, h * f
        x, y = (pw - dw) / 2, (ph - dh) / 2
        inhalt = pikepdf.Stream(pdf, f"q {dw:.3f} 0 0 {dh:.3f} {x:.3f} {y:.3f} cm /Im0 Do Q".encode())
        seite = pikepdf.Dictionary(
            Type=Name.Page, MediaBox=[0, 0, round(pw, 3), round(ph, 3)],
            Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=bild)),
            Contents=inhalt)
        pdf.pages.append(pikepdf.Page(seite))
    return len(frames)


def zusammenfuegen(dateien, ziel: Path, seitenformat="A4", stufe=None, log=print, fortschritt=None):
    dateien = [Path(d) for d in dateien]
    neu = pikepdf.new()
    offen = []
    tmp_ordner = Path(tempfile.mkdtemp(prefix="pdf_werkstatt_"))
    try:
        for i, p in enumerate(dateien):
            if p.suffix.lower() == ".pdf":
                src = oeffnen(p)
                offen.append(src)             # muss bis zum Speichern offen bleiben
                seiten_kopieren(neu, src, range(len(src.pages)))
                log(f"  + {p.name}  ({len(src.pages)} Seiten)")
            else:
                try:
                    k = _bild_seiten_anhaengen(neu, p, seitenformat)
                except Exception as e:
                    raise Hinweis(f"Das Bild „{p.name}“ konnte nicht gelesen werden.\n\n{e}")
                log(f"  + {p.name}  (Bild{f', {k} Seiten' if k > 1 else ''})")
            if fortschritt:
                fortschritt((i + 1) / len(dateien) * (0.5 if stufe else 0.9), f"Füge {p.name} hinzu …")
        if stufe:
            tmp = tmp_ordner / "zusammen.pdf"
            _speichern(neu, tmp)
            for s in offen:
                s.close()
            offen = []
            log(f"Komprimiere – Stufe: {STUFEN[stufe]['name']}")
            prog = (lambda f, m: fortschritt(0.5 + f * 0.5, m)) if fortschritt else None
            komprimieren(tmp, ziel, stufe, prog)
        else:
            _speichern(neu, ziel)
        n = len(neu.pages)
    finally:
        neu.close()
        for s in offen:
            s.close()
        shutil.rmtree(tmp_ordner, ignore_errors=True)
    log(f"Gespeichert: {ziel}  ({n} Seiten, {fmt(ziel.stat().st_size)})")
    return ziel


# ---------------------------------------------------------------------------
# Seiten herausziehen / löschen / drehen
# ---------------------------------------------------------------------------
def seiten_bearbeiten(quelle: Path, ausgabe_ordner: Path, aktion: str, auswahl: str, winkel=90, log=print) -> Path:
    quelle = Path(quelle)
    with oeffnen(quelle) as pdf:
        n = len(pdf.pages)
        idx = seiten_parsen(auswahl, n)
        bereich = bereiche_text(idx)
        if aktion == "herausziehen":
            ziel = freier_name(ausgabe_ordner, f"{quelle.stem}_Seiten_{bereich.replace(',', '_')}"[:120])
            neu = pikepdf.new()
            seiten_kopieren(neu, pdf, idx)
            _speichern(neu, ziel)
            neu.close()
            log(f"{len(idx)} Seite(n) herausgezogen ({bereich}).")
        elif aktion == "loeschen":
            if len(idx) == n:
                raise Hinweis("Es können nicht alle Seiten gelöscht werden.")
            ziel = freier_name(ausgabe_ordner, f"{quelle.stem}_ohne_Seiten_{bereich.replace(',', '_')}"[:120])
            for i in reversed(idx):
                del pdf.pages[i]
            _speichern(pdf, ziel)
            log(f"{len(idx)} Seite(n) gelöscht ({bereich}) – es bleiben {n - len(idx)} Seiten.")
        elif aktion == "drehen":
            ziel = freier_name(ausgabe_ordner, f"{quelle.stem}_gedreht")
            for i in idx:
                pdf.pages[i].rotate(winkel, relative=True)
            _speichern(pdf, ziel)
            richtung = {90: "90° nach rechts", 180: "180°", 270: "90° nach links"}[winkel % 360]
            log(f"{len(idx)} Seite(n) um {richtung} gedreht ({bereich}).")
        else:
            raise ValueError(aktion)
    log(f"Gespeichert: {ziel}")
    return ziel


# ---------------------------------------------------------------------------
# Beschriften: Seitenzahlen und Wasserzeichen
# ---------------------------------------------------------------------------
def _rotation(page) -> int:
    obj = page.obj
    while obj is not None:
        if "/Rotate" in obj:
            return int(obj.Rotate) % 360
        obj = obj.get("/Parent")
    return 0


def beschriften(quelle: Path, ausgabe_ordner: Path, seitenzahlen=None, wasserzeichen=None, log=print, fortschritt=None):
    """seitenzahlen = {"format": "Seite {n} von {gesamt}", "position": "unten mittig", "ab_seite": 1, "start": 1}
       wasserzeichen = {"text": "VERTRAULICH", "deckkraft": 0.25}"""
    try:
        from reportlab.pdfgen import canvas
        from reportlab.pdfbase.pdfmetrics import stringWidth
    except ImportError:
        raise Hinweis("Für diese Funktion fehlt die Bibliothek „reportlab“.\n"
                      "Bitte das Programm über die Start-Datei starten – sie installiert sie automatisch.")
    quelle = Path(quelle)
    if wasserzeichen:
        try:                                  # Standardschrift kann nur westeuropäische Zeichen
            wasserzeichen["text"].strip().encode("cp1252")
        except UnicodeEncodeError:
            raise Hinweis("Das Wasserzeichen enthält Zeichen, die die eingebaute Schrift nicht darstellen kann "
                          "(z. B. Chinesisch, Emojis).\n\nBitte nur Buchstaben, Ziffern und gängige Satzzeichen "
                          "verwenden – Umlaute und ß gehen.")
    zusatz = "_".join(x for x, an in (("nummeriert", seitenzahlen), ("Wasserzeichen", wasserzeichen)) if an)

    with oeffnen(quelle) as pdf:
        n = len(pdf.pages)
        if seitenzahlen and int(seitenzahlen.get("ab_seite", 1)) > n:
            raise Hinweis(f"„Ab Seite {seitenzahlen['ab_seite']}“ geht nicht – die PDF hat nur {n} Seiten.")
        ziel = freier_name(ausgabe_ordner, f"{quelle.stem}_{zusatz}")
        buf = io.BytesIO()
        c = canvas.Canvas(buf)
        boxen = []
        if seitenzahlen:
            ab = max(1, int(seitenzahlen.get("ab_seite", 1)))
            start = int(seitenzahlen.get("start", 1))
            letzte_nr = start + (n - ab)
        for i, page in enumerate(pdf.pages):
            x0, y0, x1, y1 = [float(v) for v in page.cropbox]
            x0, x1, y0, y1 = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
            w, h = x1 - x0, y1 - y0
            boxen.append(pikepdf.Rectangle(x0, y0, x1, y1))
            # Beschriftung in der *sichtbaren* Ausrichtung anlegen – add_overlay gleicht
            # eine Seitendrehung (/Rotate) selbst aus.
            rot = _rotation(page)
            if rot and "/Rotate" not in page.obj:
                page.obj.Rotate = rot         # geerbte Drehung an die Seite schreiben (für add_overlay)
            vw, vh = (h, w) if rot in (90, 270) else (w, h)
            c.setPageSize((vw, vh))
            c.saveState()

            if wasserzeichen and wasserzeichen.get("text", "").strip():
                text = wasserzeichen["text"].strip()
                diag = math.hypot(vw, vh)
                k = max(0.5, stringWidth(text, "Helvetica-Bold", 1))     # Textbreite bei Schriftgröße 1
                cos_, sin_ = vw / diag, vh / diag                         # Winkel der Diagonale
                # schräger Text (Breite k·gr, Höhe ~0,75·gr) muss auch auf schmale Seiten ganz passen
                gr = min(160, 0.75 * diag / k,
                         0.85 * vw / (k * cos_ + 0.75 * sin_),
                         0.85 * vh / (k * sin_ + 0.75 * cos_))
                c.saveState()
                c.setFillColorRGB(0.45, 0.45, 0.45)
                c.setFillAlpha(float(wasserzeichen.get("deckkraft", 0.25)))
                c.setFont("Helvetica-Bold", gr)
                c.translate(vw / 2, vh / 2)
                c.rotate(math.degrees(math.atan2(vh, vw)))
                c.drawCentredString(0, -gr * 0.35, text)
                c.restoreState()

            if seitenzahlen and i + 1 >= ab:
                nr = start + (i + 1 - ab)
                text = seitenzahlen["format"].format(n=nr, gesamt=letzte_nr)
                pos = seitenzahlen.get("position", "unten mittig")
                rand = max(18.0, min(vw, vh) * 0.035)
                y = rand if pos.startswith("unten") else vh - rand - 7
                c.setFont("Helvetica", 10)
                c.setFillColorRGB(0.15, 0.15, 0.15)
                if pos.endswith("mittig"):
                    c.drawCentredString(vw / 2, y, text)
                elif pos.endswith("rechts"):
                    c.drawRightString(vw - rand * 1.6, y, text)
                else:
                    c.drawString(rand * 1.6, y, text)
            c.restoreState()
            c.showPage()
            if fortschritt:
                fortschritt((i + 1) / n * 0.5, f"Bereite Seite {i + 1} von {n} vor …")
        c.save()

        with pikepdf.open(io.BytesIO(buf.getvalue())) as overlay:
            for i, page in enumerate(pdf.pages):
                page.add_overlay(overlay.pages[i], boxen[i])
                if fortschritt and i % 20 == 0:
                    fortschritt(0.5 + (i + 1) / n * 0.45, f"Beschrifte Seite {i + 1} von {n} …")
            _speichern(pdf, ziel)
    teile = []
    if seitenzahlen:
        teile.append(f"Seitenzahlen ({seitenzahlen['format'].format(n=start, gesamt=letzte_nr)} … "
                     f"{seitenzahlen['format'].format(n=letzte_nr, gesamt=letzte_nr)})")
    if wasserzeichen:
        teile.append(f"Wasserzeichen „{wasserzeichen['text']}“")
    log(" + ".join(teile) + f" auf {n} Seiten eingefügt.")
    log(f"Gespeichert: {ziel}")
    return ziel


# ---------------------------------------------------------------------------
# Passwort
# ---------------------------------------------------------------------------
def passwort_setzen(quelle: Path, ausgabe_ordner: Path, passwort: str, log=print) -> Path:
    quelle = Path(quelle)
    ziel = freier_name(ausgabe_ordner, f"{quelle.stem}_geschuetzt")
    with oeffnen(quelle) as pdf:
        pdf.save(ziel, encryption=pikepdf.Encryption(owner=passwort, user=passwort, R=6),
                 object_stream_mode=pikepdf.ObjectStreamMode.generate)
    log("Passwortschutz gesetzt (AES-256-Verschlüsselung).")
    log(f"Gespeichert: {ziel}")
    return ziel


def passwort_entfernen(quelle: Path, ausgabe_ordner: Path, passwort: str, log=print) -> Path:
    quelle = Path(quelle)
    try:
        pdf = pikepdf.open(quelle, password=passwort)
    except pikepdf.PasswordError:
        raise Hinweis("Das Passwort ist falsch." if passwort else "Bitte das aktuelle Passwort eingeben.")
    with pdf:
        if not pdf.is_encrypted:
            raise Hinweis("Diese PDF hat keinen Passwortschutz.")
        ziel = freier_name(ausgabe_ordner, f"{quelle.stem}_ohne_Passwort")
        pdf.save(ziel, encryption=False)
    log("Passwortschutz entfernt.")
    log(f"Gespeichert: {ziel}")
    return ziel


# ---------------------------------------------------------------------------
# Fenster (Tkinter)
# ---------------------------------------------------------------------------
def starte_fenster(start_datei=None):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    # Drag & Drop (optional – benötigt das Paket tkinterdnd2)
    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        root = TkinterDnD.Tk()
        dnd_ok = True
    except Exception:
        root = tk.Tk()
        dnd_ok = False

    root.title("PDF-Werkstatt")
    root.minsize(660, 700)
    pad = {"padx": 10, "pady": 4}
    ZONE_BG, ZONE_AKTIV = "#eef3fb", "#d4e4fa"
    start_buttons = []
    drop_handler = {}                         # Reiter → Funktion(pfade)

    def datei_info(p):
        try:
            g = fmt(Path(p).stat().st_size)
        except OSError:
            g = "?"
        try:
            return f"{g}, {seitenzahl(p)} Seiten"
        except Hinweis:
            return f"{g}, 🔒 passwortgeschützt"
        except Exception:
            return g

    def zone_bauen(parent, leertext, beim_klick):
        hinweis = "hierher ziehen oder klicken" if dnd_ok else "klicken (Drag & Drop: Paket „tkinterdnd2“ fehlt)"
        z = tk.Label(parent, text=leertext.format(hinweis=hinweis), bg=ZONE_BG, fg="#2b4a75",
                     font=("TkDefaultFont", 11), relief="ridge", bd=2, height=3, cursor="hand2",
                     justify="center")
        z.pack(fill="x", padx=6, pady=(6, 4))
        z.bind("<Button-1>", lambda e: beim_klick())
        z.hinweis = hinweis
        return z

    def nur_pdfs(pfade):
        erg = []
        for p in pfade:
            p = Path(p)
            if p.is_dir():
                erg += sorted(x for x in p.iterdir() if x.suffix.lower() == ".pdf")
            elif p.suffix.lower() == ".pdf":
                erg.append(p)
        return erg

    tabs = ttk.Notebook(root)
    tabs.pack(fill="x", padx=10, pady=(10, 4))

    # ------------------------------------------------------------------ Reiter 1
    t1 = ttk.Frame(tabs)
    tabs.add(t1, text="  Komprimieren & Teilen  ")
    t1_dateien = []
    komp_var = tk.BooleanVar(value=True)
    stufe_var = tk.StringVar(value="mittel")
    grau_var = tk.BooleanVar(value=False)
    teil_var = tk.BooleanVar(value=False)
    mb_var = tk.StringVar(value="15")
    mail_var = tk.BooleanVar(value=True)
    art_var = tk.StringVar(value="mb")
    seiten_var = tk.StringVar(value="100")
    seiten_anzahl = {"n": 0}

    def t1_waehlen():
        p = filedialog.askopenfilenames(filetypes=[("PDF-Dateien", "*.pdf")])
        if p:
            t1_setzen(list(p))

    def t1_ordner():
        o = filedialog.askdirectory(title="Ordner mit PDFs wählen")
        if o:
            t1_setzen([o])

    zone1 = zone_bauen(t1, "PDF(s) {hinweis}\nMehrere PDFs oder ein ganzer Ordner = Stapelverarbeitung", t1_waehlen)

    def t1_setzen(pfade):
        pdfs = nur_pdfs(pfade)
        if not pdfs:
            messagebox.showwarning("Hinweis", "Keine PDF-Datei gefunden.")
            return
        t1_dateien[:] = pdfs
        ordner_vorschlagen(pdfs[0])
        if len(pdfs) == 1:
            seiten_anzahl["n"] = 0
            info = datei_info(pdfs[0])
            m = re.search(r"(\d+) Seiten", info)
            seiten_anzahl["n"] = int(m.group(1)) if m else 0
            zone1.config(text=f"📄 {pdfs[0].name}\n{info}  –  andere PDF {zone1.hinweis}")
        else:
            seiten_anzahl["n"] = 0
            gesamt = sum(p.stat().st_size for p in pdfs)
            namen = ", ".join(p.name for p in pdfs[:3]) + (" …" if len(pdfs) > 3 else "")
            zone1.config(text=f"📚 {len(pdfs)} PDFs ausgewählt ({fmt(gesamt)})\n{namen}")
        seiten_vorschau()
    drop_handler[str(t1)] = t1_setzen

    zb = ttk.Frame(t1)
    zb.pack(fill="x", padx=6)
    ttk.Button(zb, text="Dateien …", command=t1_waehlen).pack(side="left")
    ttk.Button(zb, text="Ganzer Ordner …", command=t1_ordner).pack(side="left", padx=6)

    f2 = ttk.LabelFrame(t1, text="Komprimieren")
    f2.pack(fill="x", **pad)
    ttk.Checkbutton(f2, text="PDF komprimieren", variable=komp_var).pack(anchor="w", padx=6, pady=(4, 0))
    zs = ttk.Frame(f2)
    zs.pack(anchor="w", padx=28)
    for key, s in STUFEN.items():
        ttk.Radiobutton(zs, text=s["name"], value=key, variable=stufe_var).pack(side="left", padx=(0, 12))
    ttk.Checkbutton(f2, text="Zusätzlich in Graustufen umwandeln (Bilder/Scans werden schwarz-weiß, spart Platz)",
                    variable=grau_var).pack(anchor="w", padx=28, pady=(2, 6))

    f3 = ttk.LabelFrame(t1, text="Teilen")
    f3.pack(fill="x", **pad)
    ttk.Checkbutton(f3, text="In Teile aufteilen", variable=teil_var).pack(anchor="w", padx=6, pady=(4, 0))
    zeile = ttk.Frame(f3)
    zeile.pack(anchor="w", padx=28)
    ttk.Radiobutton(zeile, text="Nach Größe – max.", value="mb", variable=art_var).pack(side="left")
    mb_box = ttk.Spinbox(zeile, from_=1, to=500, increment=1, width=6, textvariable=mb_var)
    mb_box.pack(side="left", padx=4)
    ttk.Label(zeile, text="MB pro Mail").pack(side="left")
    mail_chk = ttk.Checkbutton(f3, text="Mail-Kodierung einrechnen (empfohlen – Anhänge werden in der Mail ca. 37 % größer)",
                               variable=mail_var)
    mail_chk.pack(anchor="w", padx=50)
    zeile2 = ttk.Frame(f3)
    zeile2.pack(anchor="w", padx=28, pady=(0, 6))
    ttk.Radiobutton(zeile2, text="Nach Seiten – alle", value="seiten", variable=art_var).pack(side="left")
    seiten_box = ttk.Spinbox(zeile2, from_=1, to=100000, increment=10, width=7, textvariable=seiten_var)
    seiten_box.pack(side="left", padx=4)
    ttk.Label(zeile2, text="Seiten ein neuer Teil").pack(side="left")
    seiten_info = ttk.Label(zeile2, text="", foreground="gray")
    seiten_info.pack(side="left", padx=8)

    def seiten_vorschau(*_):
        try:
            n, k = seiten_anzahl["n"], int(seiten_var.get())
            seiten_info.config(text=f"→ {-(-n // k)} Teil(e)" if n and k > 0 else "")
        except Exception:
            seiten_info.config(text="")

    def art_umschalten(*_):
        mb = art_var.get() == "mb"
        mb_box.config(state="normal" if mb else "disabled")
        mail_chk.config(state="normal" if mb else "disabled")
        seiten_box.config(state="disabled" if mb else "normal")
        seiten_vorschau()
    art_var.trace_add("write", art_umschalten)
    seiten_var.trace_add("write", seiten_vorschau)
    art_umschalten()

    def t1_start():
        if not t1_dateien:
            return messagebox.showwarning("Hinweis", "Bitte zuerst eine PDF auswählen.")
        if not komp_var.get() and not teil_var.get():
            return messagebox.showwarning("Hinweis", "Bitte „Komprimieren“ und/oder „Teilen“ anhaken.")
        teil_mb = teil_seiten = None
        if teil_var.get() and art_var.get() == "mb":
            try:
                teil_mb = float(mb_var.get().replace(",", "."))
                assert teil_mb > 0
            except Exception:
                return messagebox.showwarning("Hinweis", "Bitte eine gültige MB-Zahl eingeben.")
        elif teil_var.get():
            try:
                teil_seiten = int(seiten_var.get().strip())
                assert teil_seiten > 0
            except Exception:
                return messagebox.showwarning("Hinweis", "Bitte eine gültige Seitenzahl (ganze Zahl) eingeben.")
        dateien = list(t1_dateien)
        stufe = stufe_var.get() if komp_var.get() else None
        grau = grau_var.get() and komp_var.get()
        mail = mail_var.get()

        def arbeit(log, prog, ordner):
            fehlgeschlagen = []
            for i, d in enumerate(dateien):
                if len(dateien) > 1:
                    log(f"\n——— Datei {i + 1} von {len(dateien)} ———")
                sub = (lambda f, m, i=i: prog((i + f) / len(dateien), m))
                try:
                    verarbeiten(d, ordner, stufe, teil_mb, mail, log, sub, teil_seiten, grau)
                except PermissionError:
                    raise                     # betrifft alle Dateien → abbrechen und melden
                except Exception as e:
                    if len(dateien) == 1:
                        raise
                    grund = str(e).split("\n")[0] if isinstance(e, Hinweis) else f"Fehler: {e}"
                    log("⚠ Übersprungen: " + grund)
                    fehlgeschlagen.append(d.name)
            if len(dateien) > 1:
                log(f"\n{len(dateien) - len(fehlgeschlagen)} von {len(dateien)} Dateien verarbeitet."
                    + (f"  Übersprungen: {', '.join(fehlgeschlagen)}" if fehlgeschlagen else ""))
        ausfuehren(arbeit)

    b = ttk.Button(t1, text="▶  Start", command=t1_start)
    b.pack(pady=(4, 8))
    start_buttons.append(b)

    # ------------------------------------------------------------------ Reiter 2
    t2 = ttk.Frame(tabs)
    tabs.add(t2, text="  Zusammenfügen  ")
    t2_dateien = []
    t2_info = {}
    format_var = tk.StringVar(value="A4")
    t2_komp_var = tk.StringVar(value="nein")
    name_var = tk.StringVar(value="Zusammengefügt")

    ttk.Label(t2, text="PDFs und Bilder (JPG, PNG, TIFF, HEIC …) " +
              ("hierher ziehen" if dnd_ok else "hinzufügen") +
              " – Reihenfolge mit der Maus ziehen oder mit ↑ ↓ ändern.",
              foreground="#2b4a75").pack(anchor="w", padx=8, pady=(8, 2))
    lf = ttk.Frame(t2)
    lf.pack(fill="x", padx=6)
    liste = tk.Listbox(lf, height=9, activestyle="none", selectmode="browse", bg=ZONE_BG,
                       highlightthickness=1, relief="ridge")
    sb = ttk.Scrollbar(lf, orient="vertical", command=liste.yview)
    liste.config(yscrollcommand=sb.set)
    liste.pack(side="left", fill="both", expand=True)
    sb.pack(side="left", fill="y")
    knoepfe = ttk.Frame(lf)
    knoepfe.pack(side="left", fill="y", padx=(6, 0))

    def t2_anzeigen(auswahl=None):
        liste.delete(0, "end")
        for i, p in enumerate(t2_dateien, 1):
            liste.insert("end", f" {i}.  {p.name}    ({t2_info.get(p, '')})")
        if not t2_dateien:
            liste.insert("end", "   (noch leer – Dateien hierher ziehen oder „Hinzufügen …“)")
        if auswahl is not None and 0 <= auswahl < len(t2_dateien):
            liste.selection_set(auswahl)
            liste.see(auswahl)

    def t2_hinzu(pfade):
        neu = []
        for p in pfade:
            p = Path(p)
            if p.is_dir():
                neu += sorted(x for x in p.iterdir() if x.suffix.lower() in BILD_ENDUNGEN | {".pdf"})
            elif p.suffix.lower() in BILD_ENDUNGEN | {".pdf"}:
                neu.append(p)
        if not neu:
            return messagebox.showwarning("Hinweis", "Keine PDF- oder Bilddatei gefunden.")
        for p in neu:
            t2_info[p] = "Bild" if p.suffix.lower() in BILD_ENDUNGEN else datei_info(p).split(", ", 1)[-1]
        t2_dateien.extend(neu)
        ordner_vorschlagen(neu[0])
        t2_anzeigen(len(t2_dateien) - 1)
    drop_handler[str(t2)] = t2_hinzu

    def t2_auswahl():
        s = liste.curselection()
        return s[0] if s and s[0] < len(t2_dateien) else None

    def t2_schieben(d):
        i = t2_auswahl()
        if i is None or not (0 <= i + d < len(t2_dateien)):
            return
        t2_dateien[i], t2_dateien[i + d] = t2_dateien[i + d], t2_dateien[i]
        t2_anzeigen(i + d)

    def t2_entfernen():
        i = t2_auswahl()
        if i is not None:
            del t2_dateien[i]
            t2_anzeigen(min(i, len(t2_dateien) - 1))

    def t2_sortieren():
        t2_dateien.sort(key=lambda p: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)])
        t2_anzeigen()

    for text, cmd in (("Hinzufügen …", lambda: t2_hinzu(filedialog.askopenfilenames(
                          filetypes=[("PDFs und Bilder", "*.pdf *.jpg *.jpeg *.png *.tif *.tiff *.bmp *.gif *.webp *.heic"),
                                     ("Alle Dateien", "*.*")]))),
                      ("↑  nach oben", lambda: t2_schieben(-1)),
                      ("↓  nach unten", lambda: t2_schieben(1)),
                      ("Entfernen", t2_entfernen),
                      ("Nach Name sortieren", t2_sortieren),
                      ("Liste leeren", lambda: (t2_dateien.clear(), t2_anzeigen()))):
        ttk.Button(knoepfe, text=text, command=cmd).pack(fill="x", pady=1)

    ziehen = {"i": None}
    liste.bind("<Button-1>", lambda e: ziehen.update(i=liste.nearest(e.y)))

    def t2_ziehen(e):
        i, j = ziehen["i"], liste.nearest(e.y)
        if i is None or i >= len(t2_dateien) or j >= len(t2_dateien) or i == j:
            return
        t2_dateien.insert(j, t2_dateien.pop(i))
        ziehen["i"] = j
        t2_anzeigen(j)
    liste.bind("<B1-Motion>", t2_ziehen)
    t2_anzeigen()

    opt = ttk.Frame(t2)
    opt.pack(fill="x", padx=8, pady=(8, 2))
    ttk.Label(opt, text="Seitengröße für Bilder:").grid(row=0, column=0, sticky="w")
    ttk.Combobox(opt, textvariable=format_var, values=["A4", "wie Bild"], state="readonly",
                 width=10).grid(row=0, column=1, sticky="w", padx=4, pady=2)
    ttk.Label(opt, text="Danach komprimieren:").grid(row=1, column=0, sticky="w")
    ttk.Combobox(opt, textvariable=t2_komp_var, values=["nein", "leicht", "mittel", "stark"], state="readonly",
                 width=10).grid(row=1, column=1, sticky="w", padx=4, pady=2)
    ttk.Label(opt, text="(empfohlen bei Handyfotos)", foreground="gray").grid(row=1, column=2, sticky="w")
    ttk.Label(opt, text="Dateiname:").grid(row=2, column=0, sticky="w")
    ttk.Entry(opt, textvariable=name_var, width=30).grid(row=2, column=1, columnspan=2, sticky="w", padx=4, pady=2)
    ttk.Label(opt, text=".pdf").grid(row=2, column=3, sticky="w")

    def t2_start():
        if not t2_dateien:
            return messagebox.showwarning("Hinweis", "Bitte zuerst Dateien hinzufügen.")
        name = re.sub(r'[\\/:*?"<>|]', "_", name_var.get().strip()) or "Zusammengefügt"
        if name.lower().endswith(".pdf"):
            name = name[:-4]
        dateien = list(t2_dateien)
        fmt_wahl = "A4" if format_var.get() == "A4" else "bild"
        stufe = None if t2_komp_var.get() == "nein" else t2_komp_var.get()

        def arbeit(log, prog, ordner):
            log(f"Füge {len(dateien)} Datei(en) zusammen …")
            zusammenfuegen(dateien, freier_name(ordner, name), fmt_wahl, stufe, log, prog)
            log("Fertig ✔")
        ausfuehren(arbeit)

    b = ttk.Button(t2, text="▶  Zusammenfügen", command=t2_start)
    b.pack(pady=(4, 8))
    start_buttons.append(b)

    # ------------------------------------------------- Hilfsbaustein: eine einzelne PDF
    def einzel_pdf(tab, text):
        zustand = {"pfad": None}

        def waehlen():
            p = filedialog.askopenfilename(filetypes=[("PDF-Dateien", "*.pdf")])
            if p:
                setzen([p])

        z = zone_bauen(tab, "PDF " + "{hinweis}" + f"\n{text}", waehlen)

        def setzen(pfade):
            pdfs = nur_pdfs(pfade)
            if not pdfs:
                return messagebox.showwarning("Hinweis", "Bitte eine PDF-Datei verwenden.")
            zustand["pfad"] = pdfs[0]
            ordner_vorschlagen(pdfs[0])
            z.config(text=f"📄 {pdfs[0].name}\n{datei_info(pdfs[0])}  –  andere PDF {z.hinweis}")
        drop_handler[str(tab)] = setzen
        return zustand

    # ------------------------------------------------------------------ Reiter 3
    t3 = ttk.Frame(tabs)
    tabs.add(t3, text="  Seiten  ")
    t3_pdf = einzel_pdf(t3, "Seiten herausziehen, löschen oder drehen")
    aktion_var = tk.StringVar(value="herausziehen")
    auswahl_var = tk.StringVar()
    winkel_var = tk.StringVar(value="90° nach rechts")

    fa = ttk.LabelFrame(t3, text="Was soll passieren?")
    fa.pack(fill="x", **pad)
    ttk.Radiobutton(fa, text="Seiten herausziehen – nur diese Seiten als neue PDF speichern",
                    value="herausziehen", variable=aktion_var).pack(anchor="w", padx=6, pady=(6, 0))
    ttk.Radiobutton(fa, text="Seiten löschen – neue PDF ohne diese Seiten speichern",
                    value="loeschen", variable=aktion_var).pack(anchor="w", padx=6)
    zd = ttk.Frame(fa)
    zd.pack(anchor="w", padx=6, pady=(0, 6))
    ttk.Radiobutton(zd, text="Seiten drehen um", value="drehen", variable=aktion_var).pack(side="left")
    ttk.Combobox(zd, textvariable=winkel_var, values=["90° nach rechts", "180°", "90° nach links"],
                 state="readonly", width=15).pack(side="left", padx=4)

    fs = ttk.LabelFrame(t3, text="Welche Seiten?")
    fs.pack(fill="x", **pad)
    ze = ttk.Frame(fs)
    ze.pack(anchor="w", padx=6, pady=6)
    ttk.Entry(ze, textvariable=auswahl_var, width=28).pack(side="left")
    ttk.Label(ze, text="  z. B.  5   oder   1-3, 7, 10-   (10- = ab 10 bis Ende)", foreground="gray").pack(side="left")
    ttk.Label(fs, text="Beim Drehen bedeutet ein leeres Feld: alle Seiten.", foreground="gray").pack(anchor="w", padx=6, pady=(0, 6))

    def t3_start():
        quelle = t3_pdf["pfad"]
        if not quelle:
            return messagebox.showwarning("Hinweis", "Bitte zuerst eine PDF auswählen.")
        aktion, auswahl = aktion_var.get(), auswahl_var.get()
        if aktion != "drehen" and not auswahl.strip():
            return messagebox.showwarning("Hinweis", "Bitte angeben, welche Seiten (z. B. 1-3, 7).")
        winkel = {"90° nach rechts": 90, "180°": 180, "90° nach links": 270}[winkel_var.get()]
        ausfuehren(lambda log, prog, ordner: (seiten_bearbeiten(quelle, ordner, aktion, auswahl, winkel, log),
                                               log("Fertig ✔")))

    b = ttk.Button(t3, text="▶  Anwenden", command=t3_start)
    b.pack(pady=(4, 8))
    start_buttons.append(b)

    # ------------------------------------------------------------------ Reiter 4
    t4 = ttk.Frame(tabs)
    tabs.add(t4, text="  Beschriften  ")
    t4_pdf = einzel_pdf(t4, "Seitenzahlen und/oder Wasserzeichen einfügen")
    sz_var = tk.BooleanVar(value=True)
    sz_format = tk.StringVar(value="Seite 1 von 10")
    sz_pos = tk.StringVar(value="unten mittig")
    sz_ab = tk.StringVar(value="1")
    sz_start = tk.StringVar(value="1")
    wz_var = tk.BooleanVar(value=False)
    wz_text = tk.StringVar(value="VERTRAULICH")
    wz_deck = tk.StringVar(value="mittel (25 %)")

    fz = ttk.LabelFrame(t4, text="Seitenzahlen")
    fz.pack(fill="x", **pad)
    ttk.Checkbutton(fz, text="Seitenzahlen einfügen", variable=sz_var).grid(row=0, column=0, columnspan=4, sticky="w", padx=6, pady=(6, 2))
    ttk.Label(fz, text="Format:").grid(row=1, column=0, sticky="w", padx=(28, 4))
    ttk.Combobox(fz, textvariable=sz_format, values=list(SEITENZAHL_FORMATE), state="readonly", width=16).grid(row=1, column=1, sticky="w", pady=2)
    ttk.Label(fz, text="Position:").grid(row=1, column=2, sticky="w", padx=(16, 4))
    ttk.Combobox(fz, textvariable=sz_pos, values=SEITENZAHL_POSITIONEN, state="readonly", width=14).grid(row=1, column=3, sticky="w", pady=2)
    ttk.Label(fz, text="Ab Seite:").grid(row=2, column=0, sticky="w", padx=(28, 4))
    ttk.Spinbox(fz, from_=1, to=100000, width=6, textvariable=sz_ab).grid(row=2, column=1, sticky="w", pady=(2, 6))
    ttk.Label(fz, text="beginnt mit Nr.:").grid(row=2, column=2, sticky="w", padx=(16, 4))
    ttk.Spinbox(fz, from_=0, to=100000, width=6, textvariable=sz_start).grid(row=2, column=3, sticky="w", pady=(2, 6))
    ttk.Label(fz, text="Tipp: „Ab Seite 2“ lässt das Deckblatt ohne Nummer.", foreground="gray").grid(
        row=3, column=0, columnspan=4, sticky="w", padx=28, pady=(0, 6))

    fw = ttk.LabelFrame(t4, text="Wasserzeichen")
    fw.pack(fill="x", **pad)
    ttk.Checkbutton(fw, text="Wasserzeichen quer über jede Seite", variable=wz_var).grid(row=0, column=0, columnspan=4, sticky="w", padx=6, pady=(6, 2))
    ttk.Label(fw, text="Text:").grid(row=1, column=0, sticky="w", padx=(28, 4))
    ttk.Combobox(fw, textvariable=wz_text, width=22,
                 values=["VERTRAULICH", "ENTWURF", "KOPIE", "MUSTER", "NUR ZUR ANSICHT"]).grid(row=1, column=1, sticky="w", pady=(2, 6))
    ttk.Label(fw, text="Stärke:").grid(row=1, column=2, sticky="w", padx=(16, 4))
    ttk.Combobox(fw, textvariable=wz_deck, values=list(DECKKRAFT), state="readonly", width=14).grid(row=1, column=3, sticky="w", pady=(2, 6))

    def t4_start():
        quelle = t4_pdf["pfad"]
        if not quelle:
            return messagebox.showwarning("Hinweis", "Bitte zuerst eine PDF auswählen.")
        if not sz_var.get() and not wz_var.get():
            return messagebox.showwarning("Hinweis", "Bitte „Seitenzahlen“ und/oder „Wasserzeichen“ anhaken.")
        sz = wz = None
        if sz_var.get():
            try:
                sz = {"format": SEITENZAHL_FORMATE[sz_format.get()], "position": sz_pos.get(),
                      "ab_seite": int(sz_ab.get()), "start": int(sz_start.get())}
                assert sz["ab_seite"] >= 1
            except Exception:
                return messagebox.showwarning("Hinweis", "Bitte bei „Ab Seite“ und „Nr.“ ganze Zahlen eingeben.")
        if wz_var.get():
            if not wz_text.get().strip():
                return messagebox.showwarning("Hinweis", "Bitte einen Text für das Wasserzeichen eingeben.")
            wz = {"text": wz_text.get().strip(), "deckkraft": DECKKRAFT[wz_deck.get()]}
        ausfuehren(lambda log, prog, ordner: (beschriften(quelle, ordner, sz, wz, log, prog), log("Fertig ✔")))

    b = ttk.Button(t4, text="▶  Beschriften", command=t4_start)
    b.pack(pady=(4, 8))
    start_buttons.append(b)

    # ------------------------------------------------------------------ Reiter 5
    t5 = ttk.Frame(tabs)
    tabs.add(t5, text="  Passwort  ")
    t5_pdf = einzel_pdf(t5, "Passwort setzen oder entfernen")
    pw_modus = tk.StringVar(value="setzen")
    pw1, pw2 = tk.StringVar(), tk.StringVar()
    pw_zeigen = tk.BooleanVar(value=False)

    fp = ttk.LabelFrame(t5, text="Passwort")
    fp.pack(fill="x", **pad)
    ttk.Radiobutton(fp, text="Passwort setzen – PDF lässt sich nur noch mit Passwort öffnen",
                    value="setzen", variable=pw_modus).grid(row=0, column=0, columnspan=3, sticky="w", padx=6, pady=(6, 0))
    ttk.Radiobutton(fp, text="Passwort entfernen – du musst das aktuelle Passwort kennen",
                    value="entfernen", variable=pw_modus).grid(row=1, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 6))
    pw1_lbl = ttk.Label(fp, text="Neues Passwort:")
    pw1_lbl.grid(row=2, column=0, sticky="w", padx=(28, 4))
    e1 = ttk.Entry(fp, textvariable=pw1, show="•", width=28)
    e1.grid(row=2, column=1, sticky="w", pady=2)
    pw2_lbl = ttk.Label(fp, text="Wiederholen:")
    pw2_lbl.grid(row=3, column=0, sticky="w", padx=(28, 4))
    e2 = ttk.Entry(fp, textvariable=pw2, show="•", width=28)
    e2.grid(row=3, column=1, sticky="w", pady=2)
    ttk.Checkbutton(fp, text="Passwort anzeigen", variable=pw_zeigen,
                    command=lambda: [e.config(show="" if pw_zeigen.get() else "•") for e in (e1, e2)]
                    ).grid(row=2, column=2, sticky="w", padx=10)
    pw_hinweis = ttk.Label(fp, text="Wichtig: Passwort gut merken – ohne es lässt sich die Datei nicht mehr öffnen.",
                           foreground="#a05a00")
    pw_hinweis.grid(row=4, column=0, columnspan=3, sticky="w", padx=28, pady=(4, 8))

    def pw_umschalten(*_):
        setzen = pw_modus.get() == "setzen"
        pw1_lbl.config(text="Neues Passwort:" if setzen else "Aktuelles Passwort:")
        e2.config(state="normal" if setzen else "disabled")
        pw2_lbl.config(foreground="" if setzen else "gray")
        pw_hinweis.config(text="Wichtig: Passwort gut merken – ohne es lässt sich die Datei nicht mehr öffnen."
                          if setzen else "Es wird eine Kopie ohne Passwort gespeichert – das Original bleibt geschützt.")
    pw_modus.trace_add("write", pw_umschalten)

    def t5_start():
        quelle = t5_pdf["pfad"]
        if not quelle:
            return messagebox.showwarning("Hinweis", "Bitte zuerst eine PDF auswählen.")
        p = pw1.get()
        if pw_modus.get() == "setzen":
            if not p:
                return messagebox.showwarning("Hinweis", "Bitte ein Passwort eingeben.")
            if p != pw2.get():
                return messagebox.showwarning("Hinweis", "Die beiden Passwörter stimmen nicht überein.")
            ausfuehren(lambda log, prog, ordner: (passwort_setzen(quelle, ordner, p, log), log("Fertig ✔")))
        else:
            ausfuehren(lambda log, prog, ordner: (passwort_entfernen(quelle, ordner, p, log), log("Fertig ✔")))

    b = ttk.Button(t5, text="▶  Speichern", command=t5_start)
    b.pack(pady=(4, 8))
    start_buttons.append(b)

    # ------------------------------------------------------------ gemeinsamer Teil
    ordner_var = tk.StringVar()
    ordner_auto = {"wert": ""}

    def ordner_vorschlagen(pfad):
        neu = str(Path(pfad).parent)
        if not ordner_var.get() or ordner_var.get() == ordner_auto["wert"]:
            ordner_var.set(neu)
            ordner_auto["wert"] = neu

    f4 = ttk.LabelFrame(root, text="Speichern in")
    f4.pack(fill="x", **pad)
    ttk.Entry(f4, textvariable=ordner_var).pack(side="left", fill="x", expand=True, padx=6, pady=6)
    ttk.Button(f4, text="Ordner …",
               command=lambda: ordner_var.set(filedialog.askdirectory() or ordner_var.get())).pack(side="right", padx=6)

    bar = ttk.Progressbar(root, maximum=1.0)
    bar.pack(fill="x", padx=10)
    status = ttk.Label(root, text="")
    status.pack(anchor="w", padx=10)
    logbox = tk.Text(root, height=7, wrap="word", state="disabled")
    logbox.pack(fill="both", expand=True, padx=10, pady=(2, 10))

    q = queue.Queue()

    def poll():
        try:
            while True:
                art, a, b_ = q.get_nowait()
                if art == "log":
                    logbox.config(state="normal")
                    logbox.insert("end", a + "\n")
                    logbox.see("end")
                    logbox.config(state="disabled")
                elif art == "prog":
                    bar["value"] = a
                    status.config(text=b_)
                elif art == "ende":
                    for btn in start_buttons:
                        btn.config(state="normal")
                    typ, text = a
                    if text and typ == "hinweis":
                        status.config(text="")
                        messagebox.showwarning("Hinweis", text)
                    elif text:
                        status.config(text="")
                        messagebox.showerror("Fehler", text)
        except queue.Empty:
            pass
        root.after(100, poll)

    def ausfuehren(arbeit):
        """Führt arbeit(log, fortschritt, ordner) im Hintergrund aus und zeigt Fehler verständlich an."""
        o = ordner_var.get().strip()
        if not o:
            return messagebox.showwarning("Hinweis", "Bitte einen Speicherordner wählen.")
        ordner = Path(o)
        logbox.config(state="normal"); logbox.delete("1.0", "end"); logbox.config(state="disabled")
        bar["value"] = 0
        for btn in start_buttons:
            btn.config(state="disabled")

        def lauf():
            ergebnis = ("", None)
            try:
                ordner.mkdir(parents=True, exist_ok=True)
                arbeit(lambda m: q.put(("log", m, None)), lambda f, m: q.put(("prog", f, m)), ordner)
                q.put(("prog", 1.0, "Fertig."))
            except Hinweis as e:
                ergebnis = ("hinweis", str(e))
            except PermissionError as e:
                ergebnis = ("fehler", "Eine Datei konnte nicht geschrieben werden – sie ist vermutlich noch "
                            "in einem PDF-Programm geöffnet, oder der Zielordner ist schreibgeschützt.\n\n"
                            "Bitte die PDF schließen bzw. einen anderen Speicherordner wählen.\n\n"
                            f"Details: {e}")
            except Exception as e:
                ergebnis = ("fehler", f"Unerwarteter Fehler:\n{e}")
            q.put(("ende", ergebnis, None))
        threading.Thread(target=lauf, daemon=True).start()

    # Drag & Drop: überall im Fenster ablegbar – geht an den gerade offenen Reiter
    if dnd_ok:
        def beim_drop(event):
            pfade = root.tk.splitlist(event.data)
            handler = drop_handler.get(tabs.select())
            if handler:
                handler(pfade)
            return event.action

        def registrieren(widget):
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", beim_drop)
            except Exception:
                pass
            for kind in widget.winfo_children():
                registrieren(kind)
        registrieren(root)
        root._pdf_drop = beim_drop           # für Tests

    root._tabs = tabs                         # für Tests
    if start_datei:
        t1_setzen([start_datei])
    poll()
    root.mainloop()


# ---------------------------------------------------------------------------
def main():
    if len(sys.argv) == 1:
        starte_fenster()
        return
    # PDF auf die Start-Datei gezogen → Fenster mit dieser Datei öffnen
    if len(sys.argv) == 2 and sys.argv[1].lower().endswith(".pdf"):
        starte_fenster(sys.argv[1])
        return
    ap = argparse.ArgumentParser(description="PDF komprimieren und/oder teilen (alle weiteren Funktionen im Fenster)")
    ap.add_argument("datei")
    ap.add_argument("--komprimieren", choices=list(STUFEN), help="Stufe: leicht, mittel, stark")
    ap.add_argument("--graustufen", action="store_true", help="beim Komprimieren in Graustufen umwandeln")
    teil = ap.add_mutually_exclusive_group()
    teil.add_argument("--teilen", type=float, metavar="MB", help="nach Größe teilen: max. Mail-Größe in MB")
    teil.add_argument("--seiten", type=int, metavar="N", help="nach Seiten teilen: alle N Seiten ein neuer Teil")
    ap.add_argument("--ohne-mail-kodierung", action="store_true", help="Base64-Aufschlag nicht einrechnen")
    ap.add_argument("--ausgabe", help="Ausgabeordner (Standard: Ordner der Datei)")
    a = ap.parse_args()
    if not a.komprimieren and not a.teilen and not a.seiten:
        ap.error("bitte --komprimieren und/oder --teilen / --seiten angeben")
    if a.seiten is not None and a.seiten < 1:
        ap.error("--seiten muss mindestens 1 sein")
    if a.graustufen and not a.komprimieren:
        ap.error("--graustufen funktioniert nur zusammen mit --komprimieren")
    try:
        verarbeiten(a.datei, a.ausgabe, a.komprimieren, a.teilen, not a.ohne_mail_kodierung,
                    teil_seiten=a.seiten, grau=a.graustufen)
    except Hinweis as e:
        print("Hinweis:", e)
        sys.exit(1)
    except PermissionError as e:
        print(f"Fehler: Keine Schreibrechte bzw. Datei ist in einem anderen Programm geöffnet.\n({e})")
        sys.exit(1)
    except Exception as e:
        print(f"Unerwarteter Fehler: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
