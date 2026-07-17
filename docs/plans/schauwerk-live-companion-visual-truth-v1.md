# Schauwerk Live Companion & Visual Truth v1

Stand: 17. Juli 2026

## Ziel

Schauwerk soll nicht nur deterministisch Miro-Inhalte erzeugen, sondern Anbieterdrift erkennen, ein deploybares Kontrollpanel bereitstellen und authentifizierte visuelle Evidenz an den exakten Boardzustand binden. Drei kuratierte Referenzkompositionen bilden den ersten Golden-Board-Korpus.

## Ausgangslage

- Der Miro-MCP-Zugang ist live autorisiert und erlaubt Board-Lese- und Schreiboperationen.
- Der beobachtete Livekatalog umfasst 33 Werkzeuge.
- Die offizielle Miro-MCP-Referenz dokumentiert zusätzliche Werkzeuge, die im Livekatalog noch nicht erscheinen.
- Der Web-SDK-Companion ist deterministisch baubar, aber nicht als veröffentlichte und installierte Miro-App belegt.
- Offline-Vorschau, geometrische Regression und menschliche Reviewverträge existieren; eine authentifizierte Provideraufnahme ist bisher nicht receipt-gebunden verfügbar.
- Der getrennte REST-Bildpfad besitzt noch kein Credential. Miro dokumentiert inzwischen zusätzlich einen REST-PATCH für bestehende Bildobjekte.

## Vertikalschnitt

1. **Providerdrift**
   - Der Capability-Audit trennt beobachteten Livekatalog, offizielle Referenz und Schauwerk-Adapterabdeckung.
   - Fehlende, zusätzliche und noch nicht integrierte Werkzeuge werden maschinenlesbar ausgewiesen.
   - Referenzdaten sind versions- und quellengebunden; Tests verwenden deterministische Fixtures.

2. **Companion-Releasevertrag**
   - Ein Release-Manifest bindet Buildreceipt, öffentliche HTTPS-App-URL, erwartete Scopes und Developer-App-Identität ohne Geheimnisse.
   - Ein Doctor prüft HTTPS-Erreichbarkeit, erwartete Dateien, Security-Header und exakte Digests.
   - App-Registrierung, Teaminstallation und OAuth bleiben explizite externe Gates und werden nicht aus MCP-Credentials abgeleitet.

3. **Visual Truth**
   - Eine authentifizierte Miro-Aufnahme wird als Eingabeartefakt geprüft und an Boardalias, wiederholbaren Snapshotdigest, Bilddigest, Abmessungen, Erfassungszeit und Reviewstatus gebunden.
   - Unauthentifizierte Login-, Freigabe- oder Fehlerseiten dürfen keinen PASS erzeugen.
   - Der Vertrag behauptet keine automatische ästhetische Wahrheit; er schließt nur die bisher fehlende Provider-Sichtstufe.

4. **Golden Boards**
   - Drei bewusst verschiedene, deterministische Referenzkompositionen werden bereitgestellt: Systemlandschaft, Entscheidungsfluss und narrative Reise.
   - Jede Komposition besitzt eigene Dichte-, Hierarchie-, Objekt- und Bildregeln und passiert Preview- und Qualitätsgates.

5. **Live-Gates**
   - Companion-Veröffentlichung unter öffentlichem HTTPS, Developer-App-Konfiguration und Teaminstallation werden nur nach realem Readback als erfüllt markiert.
   - Ein optionaler Bild-Update-Versuch vergleicht REST-PATCH und bestehende Replace-Saga in einem isolierten Testobjekt; er ist kein Bestandteil des ersten Merge-Gates.

## Sicherheitsgrenzen

- Keine Wiederverwendung des MCP-OAuth-Credentials für REST oder Web SDK.
- Keine Mutation bestehender oder fremder Boards ohne exakte Allowlist und neuen, isolierten Testbereich.
- Keine Tokens, Board-IDs, Team-IDs oder Upload-URLs in Receipts.
- Kein PASS aus unauthentifizierten Screenshots oder bloßer lokaler Companion-Ausführung.
- Kein automatisches Merge-, Deployment- oder Miro-Installationsrecht aus grünen Tests.

## Abschlusskriterien

Der Schnitt ist abgeschlossen, wenn Implementierung, Tests, vollständiger Diff, diffgebundene Review, grüne CI und Post-Merge-Prüfung vorliegen. Externe Miro-App-Registrierung und Teaminstallation dürfen als offenes Live-Gate verbleiben, müssen dann aber mit genauer Ursache und nächster Aktion belegt sein.