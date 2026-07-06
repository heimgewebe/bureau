# Plan — WGX Vibe Lane Lifecycle v1

Datum: 2026-07-06

## These

Feature-Branches sind nicht das eigentliche Problem. Das Problem ist, dass Branches und Worktrees ohne registrierten Lebenszyklus entstehen und später als Altlasten liegenbleiben.

## Antithese

Ein vollautomatisches `wgx vibe start` wäre zu früh. Es würde neue Motorik erzeugen, bevor die bestehenden Branches, Worktrees und offenen Lanes sichtbar und adoptierbar sind.

## Synthese

VIBE-LIFECYCLE-V1 führt einen kleinen, fail-closed Lebenszyklus ein:

- `wgx vibe status`: bestehende Vibe-Receipts anzeigen
- `wgx vibe doctor`: Branch-/Worktree-/Receipt-Lücken sichtbar machen
- `wgx vibe adopt`: bestehenden Branch/Worktree nachträglich als Lane registrieren

## Reihenfolge

1. Receipt-Format stabilisieren.
2. `status` und `doctor` read-only einführen.
3. `adopt` receipt-only einführen.
4. Erst danach `start`, `abort`, `close` mit echter Git-Motorik erweitern.
5. Bureau, Chronik, rLens und heimlern erst nach stabiler lokaler Lane-Schicht anbinden.

## Nicht-Ziele

- kein Auto-Merge
- kein Auto-Cleanup
- keine automatische Bureau-Lane
- keine Chronik-Runtime-Abhängigkeit
- keine rLens-Erzeugung per Default
- keine Branch-Löschung ohne explizite Exit-Regel

## Risiko

Falscher Cleanup kann Arbeit verlieren. Deshalb ist dieser Slice zunächst read-only beziehungsweise receipt-only.

## Nutzen

Bestehende Feature-Branches werden adoptierbar. Neue Arbeit bekommt später einen klaren Exit. Der Branch wird zur befristeten Lane statt zum Wohnort.
