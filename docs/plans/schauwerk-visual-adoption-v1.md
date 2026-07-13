# Schauwerk Visual System v2 adoption v1

## Zweck

Visual System v2 soll nicht nur ein fest codiertes Referenzboard erzeugen, sondern reale, quellgebundene Inhalte in eine klare und prüfbare Boarddramaturgie übersetzen. Der erste Vertikalschnitt ist der bestehende Software-Pilot.

## Ausgangslage

- Visual System v2 besitzt einen deterministischen Boardvertrag, ein Qualitätsgate und einen Miro-Renderer.
- Der Referenzgenerator ist inhaltlich fest auf das Visual-System-Erklärboard zugeschnitten.
- Der Software-Pilot nutzt weiterhin ein starres Vier-Spalten-Layout ohne v2-Qualitätsbeleg.
- Der bisherige Renderer bleibt zunächst erhalten, damit bestehende Aufrufe nicht brechen.

## Umsetzung

1. Den v2-Boardvertrag für reale Softwareinhalte erweitern, ohne das Referenzboard zu verändern.
2. Einen deterministischen Compiler von `software-pilot-snapshot.v1` zu `schauwerk-visual-board.v2` bauen.
3. Eine endliche Lesefolge mit Einstieg, Architektur, Entscheidungen, Lieferung, Risiken/Synthese und Evidenz erzeugen.
4. Quellen, Revisionen, Testzustand, Risiken und Grenzen sichtbar und digestgebunden abbilden.
5. Optionale CLI-Ausgaben für Board-Spezifikation, Qualitätsbeleg und v2-Miro-DSL ergänzen; bestehende Ausgaben unverändert lassen.
6. Adversarielle und deterministische Tests sowie vollständige Repository-Validierung ausführen.
7. Nur auf einem neuen isolierten Board einen Live-Miro-Nachweis durchführen. Bestehende Boards werden nicht verändert.

## Abnahme

- gleiche Eingabe erzeugt bytegleiche v2-Ausgaben;
- mindestens 90/100, keine Qualitätsblocker;
- genau ein Titel und eine These pro Frame;
- Evidenz ist letzter Frame und Quellenautorität bleibt sichtbar;
- keine Haftnotizen für fertige Fakten;
- Legacy-Renderer bleibt kompatibel;
- vollständiger GitHub-Diff, gebundenes Self-Review, grüne CI und Post-Merge-Prüfung;
- isolierter Live-Nachweis oder ausdrücklich offenes, nicht kaschiertes Provider-Gate.

## Nicht-Ziele

- automatische Umgestaltung vorhandener Boards;
- gleichzeitige Migration aller Piloten;
- objektive oder universelle Schönheitsbehauptung;
- Entfernung des bisherigen Software-Renderers;
- Änderung fremder Miro-Boards oder Worktrees.
