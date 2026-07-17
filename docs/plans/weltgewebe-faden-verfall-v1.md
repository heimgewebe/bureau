# Weltgewebe Fadenverfall v1

## Ziel

Unverzwirnte Fäden lösen sich ab ihrer kanonischen Entstehung über 168 Stunden sukzessive auf. Nach Ablauf erscheinen sie nicht mehr in aktiven Projektionen. Historische Ereignisse bleiben erhalten. Verzwirntes, dauerhaftes Garn ist von diesem Verfall ausgenommen und wird in einer getrennten Domänenaufgabe modelliert.

## Ausführungsreihenfolge

1. Den bestehenden PR #1461 gegen den aktuellen Weltgewebe-Main-Stand und seine roten Gates prüfen.
2. Entstehungszeit, Ablaufgrenze und Projektionssemantik für JSONL und PostgreSQL vereinheitlichen.
3. Migration, Replay, API und Webdarstellung mit festen Uhren und exakten Grenzfällen testen.
4. Den exakten PR-Head vollständig validieren, den Diff extern bereitstellen und erst danach mergen.
5. Nach Merge Main-CI und, falls autorisiert, die betroffene Laufzeitprojektion verifizieren.

## Beweisgrenzen

- Verfall entfernt keine historischen Webungsereignisse.
- Garn wird nicht aus UI-Merkmalen oder bloßer Zeitbeständigkeit abgeleitet.
- Konfigurationsbereinigung, Cache-Kompaktion und Garnmodell sind separate Kandidaten 640 bis 642.
- Registrierung ist weder Merge- noch Deployfreigabe.
