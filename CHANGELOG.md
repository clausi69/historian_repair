# Änderungsprotokoll

Versionsnummer: `historian_repair/__init__.py` (`__version__`), angezeigt im
Fenstertitel, im Protokoll, im Bericht und mit `--version`.

## 0.7.0
- **Wiederverwendung von Arbeitsdaten mit Prüfsumme:** Quellkopien, ZIP-Importe,
  Prüfergebnisse („Prüfen“), Recover- und Carving-Ergebnisse erhalten ein
  `manifest.json` mit SHA-256 der Quelldatei (beim Kopieren im selben Durchgang
  berechnet), H2-Version, Einstellungen und Verarbeitungsformat. Wiederverwendung
  nur bei identischer Prüfsumme und unverändertem Ergebnis; abschaltbar
  („Arbeitsdaten wiederverwenden“ / `--no-reuse`). Arbeitsdaten älterer Versionen
  (ohne Manifest) werden nie verwendet.
- **Zielprüfung** („Ziel prüfen…“ / `check`): prüft eine fertige Datenbank auf
  Tauglichkeit für CCU-Historian 4.1.3 (Format, Anmeldung, CONFIG, DATA_POINTS,
  Identity, Datentabellen, Indizes, Funktionen) – nur lesend, auf einer Kopie.
- Fehler behoben: Datenpunkte ohne Datentabelle (z. B. aus geretteten
  DATA_POINTS) – der Historian legt Tabellen nur für neue Datenpunkte an.
  Fehlende Tabellen werden jetzt leer angelegt.
- Versionsnummer und Änderungsprotokoll.

## 0.6.0
- Abgleich mit CCU-Historian 4.1.3: Ziel-H2 2.1.214 als Standard mit Warnung,
  Zugangsdaten `sa`/`ccu-historian`, `OVERRIDING SYSTEM VALUE` für `DP_ID`,
  `TABLE_NAME` ergänzen, Indexnamen `<Tabelle>_IDX`, Kompatibilitätstest.

## 0.5.0
- Einstellungen und Quellen werden gespeichert (Passwörter unter Windows per DPAPI).
- Protokoll entschlackt, Fortschritt in der Statuszeile (MB, Werte, Laufzeit).
- `DATETIME` bei zusätzlichen Tabellen in H2-2.5-Zielen.

## 0.4.0
- Automatische Java-Suche (neueste Version ≥ 11), klare Meldung bei Java 8.

## 0.3.0
- ZIP-Backups (SQL-Skript oder `.mv.db`) als Quelle, H2 2.4/2.5.

## 0.2.0
- Roh-Extraktion (Carving), Rollback-Erkennung, schreibgeschütztes Öffnen der Kopien.

## 0.1.0
- Erste Version: Analyse, Zusammenführen, fehlertolerantes Lesen, Recover, GUI und CLI.
