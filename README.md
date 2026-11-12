# CCU-Historian Datenbank-Rettung (`historian_repair`)

Werkzeug (Python + Tkinter) zum **Analysieren, Retten und Zusammenführen** von
CCU-Historian-Datenbanken (H2, Dateien `*.mv.db` bzw. `*.h2.db`).

* Öffnet beliebig viele Quell-Datenbanken (Standard: zwei, A und B) – auch
  unterschiedlicher H2-Versionen gleichzeitig (1.4.x, 2.1.x, 2.2+).
* Zeigt je Datenbank und je Datenpunkt den enthaltenen Zeitraum (erster/letzter
  Wert, Anzahl) und auf einer Zeitachse die tageweise Abdeckung inklusive
  **unlesbarer (defekter) Tage**.
* Überträgt ausgewählte Datenpunkte und Zeiträume aus den Quellen in eine
  **neue** Datenbankdatei. Reihenfolge der Quellen = Priorität.
* **Nicht-invasiv:** Quelldateien werden nie geöffnet, sondern nur kopiert.
  Geöffnet wird ausschließlich die Kopie im Arbeitsverzeichnis. Größe und
  Änderungszeit der Originale werden vorher/nachher verglichen und im Bericht
  ausgewiesen. Die Ausgabedatei darf keine Quelldatei sein.
* Fehlertolerantes Lesen: Defekte Bereiche werden durch Bisektion
  eingegrenzt und übersprungen; alles Lesbare davor/danach wird gerettet. Die
  verlorenen Intervalle stehen im Bericht – und werden, falls eine andere Quelle
  sie enthält, automatisch aus dieser aufgefüllt.
* Datenbanken, die sich gar nicht mehr öffnen lassen: **Recover** über
  `org.h2.tools.Recover` (auf einer Kopie) und Re-Import in eine neue Datei.
* **Roh-Extraktion (Carving)** für H2-1.4-Dateien: liest alle noch
  vorhandenen Datenseiten direkt (ohne B-Baum/Verwaltungsstrukturen) und
  schreibt sie in eine neue Datenbank – auch wenn H2 die Datei nur noch in
  einem uralten/leeren Stand öffnet.
* **Rollback-Erkennung:** Enthält die Datei neuere Chunks als der
  Store-Header, warnt das Tool (H2 würde sonst stillschweigend einen alten
  Stand zeigen). Quellen werden zusätzlich schreibgeschützt geöffnet
  (`ACCESS_MODE_DATA=r`), damit H2 die Arbeitskopie nicht abschneidet.
* **ZIP-Backups als Quelle:** ZIP mit SQL-Skript (z. B. aus
  `SCRIPT TO '…zip' COMPRESSION ZIP`) oder mit `.mv.db`-Datei. Das Skript wird
  in eine Arbeitsdatenbank eingespielt (`RUNSCRIPT`); die H2-Version des
  Skripts wird aus der Kopfzeile `-- H2 x.y.z;` erkannt. 1.x-Skripte lassen
  sich mit H2 2.x importieren (`FROM_1X`, zusätzlich `DATETIME` → `TIMESTAMP`).
* Migration 1.4 → 2.x möglich (Ziel-H2-Version frei wählbar, bis 2.5.x).
* Abschlussverifikation: Zeilenzahl je Tabelle in der neuen DB = Summe der
  übernommenen Werte. Bericht als `<ausgabe>_repair_report.txt` + `.json`.

## Voraussetzungen

| Komponente | Version |
|---|---|
| Python | ≥ 3.9 mit Tkinter (unter Windows im Standard-Installer enthalten; Linux: Paket `python3-tk`) |
| Java (JRE/JDK) | ≥ 11 (für H2 2.x), z. B. Eclipse Temurin. `JAVA_HOME` setzen oder JVM-Pfad in der Oberfläche angeben |
| Python-Paket | `JPype1` (`pip install -r requirements.txt`) |
| H2-Treiber | passende `h2-<version>.jar` – Button **„H2 laden…“** lädt sie von Maven Central nach `jars/`; alternativ **„JAR hinzufügen…“** (z. B. das JAR aus der eigenen CCU-Historian-Installation) |

Welche H2-Version eine Datei braucht, erkennt das Tool am Dateikopf
(`format:1` → H2 1.4.x, `format:2` → 2.0/2.1, `format:3` → 2.2+) und wählt
automatisch ein passendes JAR, sofern vorhanden.
Wichtig: H2 2.2+ kann Dateien von 2.1 **nicht** öffnen (und umgekehrt) – daher
immer das passende JAR wählen.

## Start

Windows: `start_windows.bat` doppelklicken (installiert beim ersten Mal JPype).
Linux/macOS: `./start.sh` oder

```bash
python3 -m pip install -r requirements.txt
```

```bash
python3 -m historian_repair
```

## Ablauf (typischer Rettungsfall)

1. **CCU-Historian stoppen** (bzw. Datenbankdatei vorher wegkopieren). Liegt
   eine `*.lock.db` daneben, warnt das Tool.
2. Quelle **A** = Datenbank mit der höchsten Priorität (z. B. die aktuelle,
   teilweise defekte DB), Quelle **B** = Backup (oder umgekehrt – mit ▲/▼
   tauschbar). „Öffnen“ kopiert die Datei ins Arbeitsverzeichnis und analysiert sie.
3. Optional **„Prüfen“**: liest jeden Wert einmal komplett und markiert
   Datenpunkte mit unlesbaren Bereichen rot (MVStore-`COUNT(*)` liest keine
   Datenseiten, daher fallen Defekte erst beim Lesen auf).
4. Reiter **Datenpunkte**: je Quelle per ☑/☐ auswählen, was übernommen wird.
   Gelb = Datenpunkt nur in einer Quelle, Rot = Lesefehler.
   Doppelklick → Reiter **Zeitachse** zeigt Werte pro Tag je Quelle (rot =
   unlesbarer Tag).
5. Je Quelle optional **Zeitraum von/bis** setzen (Eingabe oder mit der Maus
   in der Zeitachse ziehen; Rechtsklick setzt zurück). Leer = alles.
   Zeiten sind die im Historian gespeicherten Ortszeiten (keine Umrechnung).
6. **Überlappung**:
   * *Zeitstempel-genau* (Standard): niedrigere Quellen liefern nur Werte,
     deren Zeitstempel in der neuen DB noch fehlt → füllt Lücken und defekte
     Bereiche.
   * *Zeitraum-Priorität*: im Zeitraum (erster…letzter übernommener Wert) einer
     höheren Quelle wird aus niedrigeren gar nichts übernommen.
7. **Ziel**: Ausgabedatei (z. B. `history.mv.db`), H2-Version der neuen DB
   (muss zur eingesetzten CCU-Historian-Version passen!), Schema-Vorlage
   (aus dieser Quelle werden Tabellenstruktur und Meta-Tabellen wie
   `CONFIG` übernommen; `DATA_POINTS` wird aus allen Quellen vereinigt).
8. **Vorschau** → **Neue Datenbank erstellen**. Danach Bericht prüfen, die neue
   Datei in das Datenverzeichnis des Historian kopieren (alte DB vorher
   sichern) und Historian starten.

## Kommandozeile

```bash
python3 -m historian_repair info  /pfad/history.mv.db --jar jars/h2-2.3.232.jar -v
```

```bash
python3 -m historian_repair recover /pfad/defekt.mv.db --jar jars/h2-1.4.200.jar
```

```bash
python3 -m historian_repair merge --src backup_2024.zip@jars/h2-2.5.252.jar --src aktuell.mv.db@jars/h2-2.5.252.jar --target-jar jars/h2-2.5.252.jar --out neu/history.mv.db
```

```bash
python3 -m historian_repair carve /pfad/defekt.mv.db --jar jars/h2-1.4.200.jar
```

```bash
python3 -m historian_repair merge --src aktuell.mv.db@jars/h2-2.3.232.jar --src backup.mv.db@jars/h2-1.4.200.jar --range B=2024-01-01..2024-06-30 --target-jar jars/h2-2.3.232.jar --out neu/history.mv.db
```

Optionen von `merge`: `--dedup timestamp|range`, `--tables REGEX`,
`--template N`, `--min-skip SEKUNDEN`, `--no-compact`, `--keep-workdir`,
`--user/--password` (Standard `sa`/leer). Exit-Code 2 = fertig, aber mit
Lesefehlern/Warnungen.

## Technische Details

* Zugriff über JDBC (JPype). Jedes H2-JAR wird in einem eigenen Classloader
  geladen → mehrere H2-Versionen parallel.
* JVM läuft mit `user.timezone=UTC`, dadurch werden `TIMESTAMP`-Werte 1:1
  (ohne Sommerzeit-Verschiebung) übertragen.
* Datentabellen = Tabellen mit Spalten `TS` und `VALUE`. Zuordnung zu
  `DATA_POINTS` über dessen Spalte `TABLE_NAME` (CCU-Historian), ersatzweise
  über `D_<INTERFACE>_<ADRESSE>_<PARAMETER>`. Nicht zuordenbare Tabellen
  werden trotzdem angezeigt und übertragen.
* Schema: `SCRIPT NODATA` aus der Vorlage → `RUNSCRIPT` im Ziel
  (bei 1.x → 2.x mit `FROM_1X`). Zusätzliche Tabellen anderer Quellen werden
  ebenso angelegt (Index-/Constraint-Namen eindeutig umbenannt).
* `DATA_POINTS`: Vereinigung über (`INTERFACE`, `ADDRESS`, `IDENTIFIER`);
  höhere Priorität gewinnt; bei `DP_ID`-Kollision neue ID (Warnung im
  Bericht); Identity wird auf max+1 gesetzt.
* Lesen in Zeitfenstern (~200 000 Zeilen); Fehler → Fortsetzung ab letztem
  gelesenen Zeitstempel bzw. Halbierung des Fensters bis zur Mindestgröße
  „Unlesbare Bereiche aufgeben ab [s]“ (Standard 600 s).
* Durchsatz (gemessen, synthetisch): ca. 60 000–90 000 Werte/s.

## Roh-Extraktion (Carving) – Funktionsweise

1. Alle Chunk-Header an Blockgrenzen werden gesammelt; jeder wird per
   Reflection als „letzter Chunk“ gesetzt, um seine Meta-Map (Map-ID → Name,
   weitere Chunk-Positionen) zu lesen.
2. Jeder Chunk-Bereich wird nach Seitenanfängen durchsucht (Prüfwert aus
   Chunk-ID, Offset und Seitenlänge).
3. Blattseiten der Tabellen werden mit H2s eigenen Klassen
   (`ValueDataType`, `VersionedValueType`) dekodiert, auch komprimierte.
4. Tabellendefinitionen stammen aus H2s interner Metatabelle (auch aus
   Rohbytes). Tabellen ohne auffindbare Definition heißen `CARVED_T<id>`
   bzw. `CARVED_MAP<id>` (Zuordnung zu Datenpunkten nicht sicher möglich →
   bewusst keine Namensraterei).
5. Mehrfach vorhandene Seitenversionen werden nach Zeilenschlüssel
   dedupliziert (neueste Chunk-Version gewinnt).

Benötigt `h2-1.4.200.jar`. Ergebnis: neue Datei im Arbeitsverzeichnis
(`carve_*/carved.mv.db`) + `carve_report.txt`; sie wird automatisch als
Quelle geöffnet und kann normal zusammengeführt werden.

## Einschränkungen / offene Punkte
* Recover (`org.h2.tools.Recover`) rettet nur, was H2 selbst noch lesen kann;
  völlig zerstörte Seiten sind verloren.
* Ziel mit H2 1.x aus einer H2-2.x-Quelle wird abgelehnt (keine Rück-Migration).
* Ein Skript einer H2-2.x-Version braucht ein JAR mindestens dieser Version.
* Carving nur für H2-1.4-Dateien (Format 1); es lädt die Datei komplett in den
  Speicher (RAM-Bedarf ≈ 3 × Dateigröße + gerettete Zeilen).

## Tests

```bash
python3 -m pytest -q tests
```

(benötigt `jars/h2-1.4.200.jar` und `jars/h2-2.3.232.jar`)
