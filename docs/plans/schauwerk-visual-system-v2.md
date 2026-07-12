# Schauwerk Visual System v2

## Anlass

Der bisherige Miro-Praxistest bewies Transport, Autorisierung, Layout-Erstellung und Readback. Der technische Qualitätswert von 92/100 bewertete jedoch vor allem Objektanzahl, Frames, Connectoren und grobe Geometrie. Er beweist keine gute Informationsarchitektur oder Gestaltung. Das neue System ersetzt die Zählmetrik durch eine semantische und narrative Qualitätsprüfung.

## Ziel

Schauwerk erzeugt übersichtliche, klare, inhaltlich dichte, prägnante und optisch kohärente Miro-Flächen. Die Darstellung folgt dem Zweck des Inhalts statt einer dekorativen Standardvorlage.

## Forschungsbasis

Die Entscheidung stützt sich auf die offiziellen Miro-Ressourcen zu intelligentem Canvas, Diagrammen, Tabellen, Zeitachsen, Dokumenten, Präsentationen, Talktrack, Vorlagen, Focus Mode und Layers. Daraus folgt: Ein gutes Board ist ein modularer, begehbarer Informationsraum mit passenden Objekttypen und einem klaren Lesepfad, nicht eine Sammlung gleichförmiger Kästen und Haftnotizen.

Referenzen:

- https://miro.com/de/
- https://miro.com/de/templates/
- https://miro.com/de/capabilities/slides/
- https://miro.com/de/capabilities/diagrams/
- https://help.miro.com/hc/de

## Gestaltungsvertrag

1. **Informationsarchitektur vor Dekoration.** Jedes Board besitzt Einstieg, Übersicht, Vertiefung, Synthese und Evidenz.
2. **Objekttyp folgt Funktion.** Haftnotizen dienen veränderlichen Ideen; Shapes bilden Entitäten; Connectoren kodieren Beziehungen; Tabellen verdichten Vergleichsdaten; Dokumente tragen längere Erläuterungen; Frames tragen die Narration.
3. **Eine Hauptaussage pro Frame.** Frames haben eine eindeutige Rolle und einen sichtbaren Titel.
4. **Hierarchie ist messbar.** Titel, Kernaussage, Hauptdarstellung und Evidenz sind typografisch und räumlich unterscheidbar.
5. **Farbe ist semantisch.** Farbe kodiert Rolle oder Zustand und ist nie bloße Dekoration. Bedeutung bleibt zusätzlich durch Form oder Text erkennbar.
6. **Dichte bleibt kontrolliert.** Lange Texte, zu viele gleichgewichtete Objekte, unnötige Connectoren und überfüllte Frames blockieren die Freigabe.
7. **Rohmaterial bleibt getrennt.** Evidenz und Anhang sind erreichbar, dominieren aber nicht den Hauptlesepfad.
8. **Technischer Readback ergänzt, ersetzt aber nicht die Planprüfung.** Miro-Geometrie kann unvollständig sein. Daher wird der deterministische Boardplan vollständig geprüft und anschließend gegen den Remote-Readback gebunden.

## Kanonischer Boardaufbau

1. Orientierung / Cover
2. Systemkarte / Überblick
3. Kernmodule nach Inhaltstyp
4. Synthese / Entscheidung
5. Evidenz / Anhang

Der Referenzpilot verwendet sieben Frames: Cover, Lesekarte, Objektwahl, Informationsarchitektur, Qualitätsgate, Referenzbeispiel und Evidenz.

## Umsetzung

### Phase A — Vertrag und Schema

- Visual-System-v2-Vertrag und semantische Objekttypen
- Board-Spezifikation mit Frames, Modulen, Rollen, Farbcodes und Lesepfad
- deterministische Validierung und JSON-Schemas

### Phase B — Qualitätsgate

- Prüfung von Frame-Rollen, Lesepfad, Hierarchie, Objekttypen, Textdichte, semantischer Farbe, Connector-Disziplin und Anhangstrennung
- Blocker statt kosmetischer Warnungen bei falscher Objektwahl oder fehlender Narration
- technische Remote-Metriken bleiben als Konformitätsbeleg erhalten

### Phase C — Referenzrenderer

- kompakter Miro-DSL-Renderer für ein hochwertiges Referenzboard
- weniger Objekte, größere Weißräume, konsistente Raster, wenige semantische Farben
- keine Haftnotizwand und keine dekorativen Symbole ohne Funktion

### Phase D — Live-Abnahme

- neues allowlist-gebundenes Education-Board
- Remote-Erstellung und Readback
- visueller Screenshot/Browser-Review, soweit der Provider dies zulässt
- Qualitätsbeleg v2 und dokumentierter Vergleich zum alten Gate

### Phase E — Veröffentlichung

- vollständige Tests, CI und diffgebundener Selbstreview
- Merge in Schauwerk
- Bureau-T001 mit PR-, CI-, Live- und Qualitätsbelegen verifizieren
- Initiative abschließen

## Abbruchkriterien

- Miro-Autorisierung nicht gesund
- Boardalias nicht eindeutig oder nicht allowlist-gebunden
- Visual-Quality-v2 enthält einen Blocker
- Remote-Readback widerspricht dem kompilierten Plan
- öffentliche Evidenz enthält Provider-IDs, Board-URLs oder Geheimnisse
- fremder Repo-, Worktree- oder Lease-Zustand würde überschrieben

## Nicht-Ansprüche

- kein allgemeines Grafikdesignsystem für beliebige Medien
- kein Ersatz für menschliches Urteil bei jedem Spezialthema
- keine automatische Änderung bestehender fremder Boards
- keine Behauptung, dass reine Objektzählung visuelle Qualität beweist
