# Grabowski Safety & Smooth Ops

Status: active
Owner: Grabowski/Bureau
Created: 2026-06-29

## Zweck

Dieser Arbeitsbereich operationalisiert den Plan für eine dauerhaft reibungslose und sichere Verwendung von Grabowski.

## These / Antithese / Synthese

These: Grabowski soll als lokaler, auditierbarer Operator dauerhaft stabil laufen: read-only zuerst, mutierende Aktionen eng typisiert, Reconcile automatisiert, Secrets gekapselt, Tool-Contract prüfbar.

Antithese: Reibungslosigkeit wird gefährlich, wenn sie mit dauerhafter Allmacht verwechselt wird. Terminal, Secrets, Services, Browserprofile und Auto-Resume erzeugen echten Blast Radius.

Synthese: Reibung wird verlagert: Alltagsdiagnose wird glatt und langweilig; destruktive oder geheime Aktionen bleiben bewusst sperrig, auditiert und zeitlich begrenzt.

## Priorisierte Ausführung

1. GBW-001: Operating Baseline und Receipts einfrieren. Status: IN_PROGRESS.
2. GBW-002: Reconcile in check, refresh und resume splitten. Status: READY.
3. GBW-003: Capability-Profile observe, maintain, mutate, break-glass einführen.
4. GBW-004: Tool-Schemas und Contract-Receipts härten.
5. GBW-005: Terminalfläche in dedizierte Diagnosewerkzeuge überführen.
6. GBW-006: Secrets und Browserprofile kapseln.
7. GBW-007: systemd- und Worker-Sandboxing phasenweise aktivieren.
8. GBW-008: Prompt-Injection- und Tool-Poisoning-Regression bauen.

## Entscheidungsprinzip

Standard wird lokaler Autopilot plus read-only Receipts. ChatGPT erhält mutierende Macht nur temporär, zweckgebunden und auditiert.
