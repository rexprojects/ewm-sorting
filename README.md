# SAP EWM Sorting Manager

Lokales Webtool zum Verwalten und Erstellen von SAP-EWM-Sorting-CSV-Dateien.

## Start

```powershell
python app.py
```

Danach im Browser öffnen:

```text
http://127.0.0.1:8000
```

## Was das Tool aus den aktuellen Sorting-Dateien ableitet

- CSV-Format: Semikolon als Trennzeichen, Windows-1252 (`cp1252`) als Encoding.
- Routing: Neue Bins werden automatisch in Dateien aufgenommen, deren vorhandene Zeilen denselben `Lagertyp` enthalten.
- Lagertyp-Erkennung: z. B. `KL12004  A01` -> `KL1`, `KD12004  A01` -> `KD1`, `AKF1-10` -> `ÜB1`, `AKF2-10` -> `ÜB2`, `WER1001  A01` -> `WER`, `KID91001  A01` -> `EK1`, `SPN1102  C01` -> `SP1`, `UBEK` -> `ÜB1`.
- Einordnung: Neue Bins werden bevorzugt innerhalb desselben Lagertyps anhand der natürlichen Bin-Struktur einsortiert.
- Export: `Fortlaufende Nummer` und `Sortierreihenfolge` werden nach dem Einfügen pro Datei neu berechnet.

Die Originaldateien im Ordner `sorting files original` bleiben unverändert und dienen nur als Startbestand.
Aktualisierte CSV-Dateien werden direkt im Arbeitspfad gespeichert. Beim nächsten Lauf liest das Tool zuerst diese aktuellen Dateien und greift nur für noch nicht exportierte Dateien auf den Originalordner zurück. Der ZIP-Download enthält nur die Dateien, die durch den aktuellen Export geändert wurden.
