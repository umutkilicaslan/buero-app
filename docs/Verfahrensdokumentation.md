# Verfahrensdokumentation (GoBD)

für die Buchführung und Rechnungsstellung mit der Software „Büro“

Stand: [Datum eintragen] · Version 1.0

Felder in `[eckigen Klammern]` bitte ausfüllen. Bei jeder wesentlichen Änderung (neue Software-Version,
anderer Speicherort, neue Bank) eine neue Version mit Datum anlegen; alte Versionen aufbewahren.

---

## 1. Allgemeine Beschreibung

| | |
|---|---|
| Steuerpflichtige/r | [Name, Anschrift] |
| Steuernummer | [Steuernummer], Finanzamt [Name] |
| Tätigkeit | [z. B. freiberufliche Zirkuspädagogik und Trainingstätigkeit, § 18 EStG] |
| Gewinnermittlung | Einnahmenüberschussrechnung nach § 4 Abs. 3 EStG |
| Umsatzsteuer | [Kleinunternehmer nach § 19 UStG / Regelbesteuerung], [Ist-/Soll-Versteuerung] |
| Zuständig | Die steuerpflichtige Person selbst (keine Mitarbeitenden, keine Vertretung) |
| Steuerberatung | [keine / Name] |

Belege entstehen überwiegend aus Leistungen an Träger der Kinder- und Jugendhilfe, Schulen, Vereine,
Unternehmen und Privatpersonen sowie aus betrieblichen Ausgaben.

## 2. Eingesetzte Software und Hardware

- „Büro“: lokale Webanwendung (Python, FastAPI, SQLite), läuft ausschließlich auf dem eigenen Rechner
  [Gerät, z. B. MacBook Air, macOS-Version]; erreichbar nur unter 127.0.0.1 (kein Netzwerkzugriff von außen).
- Keine Cloud-Dienste, keine Datenübermittlung an Dritte. Steueranmeldungen und -erklärungen werden
  manuell in „Mein ELSTER“ eingegeben; die Software übermittelt nichts an die Finanzverwaltung.
- Geschäftskonto: [Bank, z. B. Kontist], Kontoauszüge als CSV-Export.
- Vorher eingesetzte Software: [z. B. Accountable bis TT.MM.JJJJ]. Rechnungen bis Nr. [z. B. 2026-18]
  wurden dort erstellt und sind dort bzw. als PDF-Export archiviert: [Speicherort].
- Quellcode und Versionsstand der Software: [Ordner / Version / Datum].

## 3. Datenablage

Alle Daten liegen in einem Ordner (Standard: `~/Freelance/buero-daten/`):

| Ablage | Inhalt |
|---|---|
| `buero.sqlite3` | Datenbank: Stammdaten, Rechnungen, Ausgaben, Einnahmen, Banktransaktionen, Protokoll |
| `invoices/JJJJ/` | ausgestellte Rechnungen und Stornorechnungen als PDF (ggf. PDF/A-3 mit ZUGFeRD/Factur-X-XML) |
| `receipts/JJJJ/` | Eingangsbelege, unverändert im Originalformat (PDF, Bild, XML) |
| `reminders/JJJJ/` | Zahlungserinnerungen/Mahnungen als PDF |
| `bank-imports/` | jede importierte Kontoauszugs-CSV, unverändert |
| `backups/` | automatische Datenbankkopie bei jedem Programmstart |

Elektronisch eingegangene Belege werden in ihrer Ursprungsform gespeichert. Papierbelege werden
[aufbewahrt im Ordner … / gescannt; das Papieroriginal wird aufbewahrt bzw. nach Prüfung vernichtet,
siehe Abschnitt 6].

## 4. Ablauf der Rechnungsstellung (Ausgangsrechnungen)

1. Anlage eines **Entwurfs** (Kunde, Leistungszeitraum, Positionen). Entwürfe haben keine Nummer und
   können geändert oder gelöscht werden.
2. Die umsatzsteuerliche Behandlung und der Pflichthinweis werden aus den Kundenstammdaten abgeleitet
   (§ 4 Nr. 25, § 4 Nr. 21, Reverse Charge, § 3a Abs. 2, § 19, Regelsteuersatz). Manuelle Abweichungen
   werden protokolliert.
3. **Ausstellen**: Prüfung der Pflichtangaben nach § 14 Abs. 4 UStG, Vergabe der nächsten fortlaufenden
   Nummer im Format `JJJJ-NN` (je Jahr lückenlos, eindeutig), Einfrieren der Anschriften und Steuerdaten
   (Snapshot), Erzeugung der PDF-Datei, Speicherung mit SHA-256-Prüfsumme.
4. Ab dem Ausstellen ist die Rechnung **unveränderbar**. Dies wird sowohl in der Anwendung als auch durch
   Trigger in der Datenbank erzwungen (kein UPDATE der Rechnungsdaten, kein DELETE, PDF-Prüfsumme nur
   einmal setzbar). Beim Öffnen wird die Prüfsumme der PDF-Datei kontrolliert.
5. **Korrektur** ausschließlich durch Stornorechnung mit eigener Nummer und Bezug auf die Originalrechnung;
   anschließend ggf. neue Rechnung.
6. **Zahlungseingang**: Zuordnung über den Bankimport oder manuell; Teilzahlungen möglich.
   **Mahnungen** werden als unveränderliche PDF-Dokumente archiviert.
7. Nummernfortführung beim Umstieg: Die erste Nummer in „Büro“ wurde auf [z. B. 2026-19] gesetzt;
   die Änderung ist im Protokoll vermerkt. [Bekannte Besonderheiten früherer Nummernkreise erläutern,
   z. B. Nummer 2025-29 nicht vergeben / Vertauschung 2025-35/36: …]

## 5. Ausgaben, Einnahmen ohne Rechnung, Bank

- **Ausgaben** werden mit Zahlungsdatum, Lieferant, Betrag, Steuerbetrag, Kategorie (Zuordnung zur Anlage EÜR)
  und betrieblichem Anteil erfasst; der Beleg wird angehängt und mit SHA-256 gespeichert.
  Belege können weder geändert noch gelöscht werden. Fehlerhafte Ausgaben werden mit Begründung
  **storniert (voided)**, nicht gelöscht.
- Wirtschaftsgüter über 800 € netto werden über die Nutzungsdauer linear und monatsgenau abgeschrieben;
  bis 800 € netto als GWG sofort.
- Fremdwährungsbelege: Euro-Betrag laut Kontoauszug.
- **Sonstige Einnahmen** (z. B. Honorare ohne eigene Rechnung) werden mit Datum, Zahler und Betrag erfasst.
- **Bankimport**: Die CSV-Datei wird unverändert abgelegt. Jede Buchung erhält einen Dublettenschlüssel;
  Datum, Betrag und Verwendungszweck sind nach dem Import nicht mehr änderbar. Jede Buchung wird einer
  Rechnung, Ausgabe, Einnahme, Steuerzahlung, Privatbuchung oder Umbuchung zugeordnet oder mit Begründung
  ignoriert (z. B. „bereits in Accountable gebucht“). Alle Zuordnungen werden protokolliert und sind
  rückgängig zu machen, wobei der Vorgang im Protokoll erhalten bleibt.
- Zeitnähe: Belege und Kontoumsätze werden [wöchentlich / monatlich, spätestens bis zum 10. des Folgemonats]
  erfasst und abgestimmt.

## 6. Ersetzendes Scannen (falls genutzt)

[Papierbelege werden mit … gescannt (Farbe, mind. 300 dpi), auf Lesbarkeit und Vollständigkeit geprüft
und als PDF hochgeladen. Das Papieroriginal wird danach [aufbewahrt / vernichtet]. Nicht vernichtet werden
Belege, deren Original gesetzlich erforderlich ist.] — Falls nicht genutzt: „Papierbelege werden im
Original aufbewahrt.“

## 7. Internes Kontrollsystem

- Pflichtfeldprüfung vor dem Ausstellen; Warnung bei Steuerbefreiung ohne hinterlegten Nachweis
  (z. B. Anerkennung als Träger der freien Jugendhilfe nach § 75 SGB VIII).
- Prüfung der lückenlosen Nummernfolge.
- Kleinunternehmer-Grenzen (§ 19 Abs. 1 UStG) werden laufend angezeigt.
- Hinweise auf Ausgaben ohne Beleg und nicht zugeordnete Kontoumsätze.
- **Protokoll (Audit-Log)**: Jede Anlage, Änderung, Ausstellung, Stornierung, Zuordnung und Einstellungsänderung
  wird mit Zeitstempel und Vorher/Nachher-Werten gespeichert. Das Protokoll kann nur ergänzt werden.

## 8. Datensicherung und Aufbewahrung

- Automatische Kopie der Datenbank bei jedem Start (`backups/`), keine automatische Löschung.
- Vollsicherung (Datenbank und alle Dokumente als geprüfte ZIP-Datei) [wöchentlich / monatlich] auf
  [externes Laufwerk / verschlüsselten Cloud-Ordner], zusätzlich [Time Machine].
- Aufbewahrung: Rechnungen und Buchungsbelege 8 Jahre, Bücher, Aufzeichnungen, Jahresabschlüsse/EÜR und
  diese Dokumentation 10 Jahre (§ 147 AO, § 14b UStG), jeweils ab Ende des Kalenderjahres.
  Die Software löscht keine Daten.
- Bei einem Wechsel des Rechners oder der Software bleiben Datenbank und Dokumente in maschinell
  auswertbarer Form verfügbar (SQLite, PDF, CSV).

## 9. Datenzugriff der Finanzverwaltung (§ 147 Abs. 6 AO)

Über „Reports → Export“ werden für ein Jahr ausgegeben: Rechnungen, Zahlungen, Ausgaben, Einnahmen,
Kontoumsätze, EÜR und Protokoll als CSV (Semikolon, UTF-8) sowie ein ZIP-Archiv mit allen PDFs, Belegen,
Bankdateien und dieser Verfahrensdokumentation. Die Datenbank selbst (SQLite) kann zusätzlich übergeben werden.

---

Ort, Datum, Unterschrift: ______________________
