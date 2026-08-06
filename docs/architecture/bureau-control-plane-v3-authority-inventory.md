# Bureau Control Plane v3: Autoritäts- und Consumerinventar

## Zweck

`bureau authority-inventory` erzeugt eine begrenzte, maschinenlesbare und strikt read-only Sicht auf die heutigen Bureau-Autoritäten und Consumer.

Der Inventarlauf verbindet vier Belegklassen:

1. statische Python-Analyse für Registry-, StateStore-, GitHub- und Grabowski-Zugriffe;
2. Workflowanalyse für GitHub-Transport- und Registry-Schreibkandidaten;
3. systemd-Unit-Inventar einschließlich optionalem read-only Live-Readback;
4. SQLite-Readback mit `mode=ro`, `query_only`, Integritätsprüfung, Schema und begrenzten Tabellenzählungen.

## Aufruf

```bash
bureau \
  --root /pfad/zum/bureau-release \
  --state-root ~/.local/state/bureau \
  --json \
  authority-inventory
```

Der direkte Modulaufruf `python -m bureau.authority_inventory` bleibt für hermetische Tests und eingebettete Nutzung verfügbar; die kanonische Operatoroberfläche ist die Haupt-CLI.

Für hermetische Tests oder Hosts ohne systemd-Userbus kann der Live-Readback mit `--skip-systemd` abgeschaltet werden. Das wird im Ergebnis sichtbar; fehlende Live-Evidence wird nicht als Gesundheit interpretiert.

## Ausgabe

Jeder Consumer nennt:

- Quellpfad oder externe Vertragsidentität,
- gelesene Autoritätsflächen,
- mögliche Schreibflächen,
- daraus abgeleitete angenommene Autoritäten,
- den konkreten Aktualitätsvertrag der jeweiligen Belegklasse,
- statische Evidenzmarker,
- Zielautorität und Zielinterface unter Control Plane v3,
- Migrationsdisposition.

`assumed_authorities` beschreibt ausschließlich, welche Autoritätsflächen der Consumer nach statischer oder explizit deklarierter Evidence derzeit voraussetzt. Das Feld erteilt keine Berechtigung.

`freshness_contract` trennt repository-revisionsgebundene Erkennung von echter Live-Evidence. Bei Pythonmodulen und Workflows bleibt die Aktualität externer Quellen ausdrücklich unbeobachtet. systemd-Livezustand wird separat über den read-only Probeabschnitt gebunden. Externe Autoritäten besitzen jeweils einen eigenen benannten Readbackvertrag.

Besonders sichtbar bleiben:

- heutige Doppelwriter für Git Registry und StateStore,
- operative Git-Schreiber, die nach dem Cutover entfernt werden müssen,
- GitHub-Transporte, die nur Code, CI oder redigierte Snapshots transportieren dürfen,
- StateStore-Schreiber, die in den Single-Writer-Vertrag konvergieren müssen.

## Grenzen

Die Oberfläche erteilt keine Mutation, klassifiziert keine fremde Arbeit als übernehmbar und beweist bei fehlendem Live-Systemd-Readback keine Runtimegesundheit. Ein statischer Schreibkandidat ist ein zu prüfender Consumer, nicht automatisch ein Fehler oder eine Löschfreigabe.

Der Inventarhash bindet den vollständigen beobachteten Inhalt. Ein späterer Apply- oder Cutover-Vertrag muss den Hash frisch lesen und separat autorisieren.
