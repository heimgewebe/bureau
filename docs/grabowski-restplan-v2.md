# Grabowski Restplan v2

## These / Antithese / Synthese

These: Grabowski soll dauerhaft als lokaler, auditierbarer Operator laufen: read-only zuerst, mutierende Aktionen eng typisiert, Reconcile lokal automatisiert, Secrets gekapselt, Tool-Contract prüfbar.

Antithese: Ein reibungsloser Operator wird gefährlich, wenn Reibungslosigkeit mit Dauer-Vollzugriff verwechselt wird. Der aktuelle Zustand löst den Reconcile-Blocker, lässt aber trusted-owner als Live-Default und damit eine zu breite ChatGPT-exponierte Fläche.

Synthese: Der nächste Hebel ist Expositionskontrolle. Reconcile-Split bleibt gültig, aber der Betrieb muss auf observe/maintain/mutate/break-glass umgestellt werden.

## Prioritäten

1. PR-A Live Capability Profiles v1
2. PR-B Reconcile CLI Split
3. PR-C Operating Receipts v2
4. PR-D Injection Regression v1
5. PR-E Terminal und Exfil-Klassen
6. PR-F Task Class Sandboxing
7. PR-G Connector Publication Split

## PR-A: Live Capability Profiles v1

Ziel: trusted-owner ist nicht mehr Normalzustand.

Profile:

- observe: Status, Context, Runtime Health, Deployment Identity, Contract Drift, Audit Verify, Git Read, Service Status/Logs, Task List/Status/Logs, Reconcile Check, Resource Inspect/List, Worker List/Status.
- maintain: observe plus Reconcile Refresh, Resource Renew, Checkout Inventory, eigene Worker stoppen.
- mutate: maintain plus gezielte Datei-/Task-/Service-/Artifact-Mutationen mit Precondition.
- break-glass: mutate plus Terminal, Secret-Reveal, Destroy, Browserprofile, Process Signal, tmux_send und breite Servicekontrolle.

Akzeptanzkriterien:

- Default-Profil ist observe oder maximal maintain.
- terminal_run, secret_reveal, destroy_path, process_signal, task_start sind im Default serverseitig blockiert.
- Break-Glass verlangt reason, expires_at, acknowledgement und Auditmarker.
- Kill-Switch übersteuert alle Profile.

## PR-B: Reconcile CLI Split

- grabowski_task_reconcile.py erhält --mode check|refresh|resume, --task-id, --max-resumes, --reason, --expected-state-hash.
- systemd grabowski-reconcile-tasks.service nutzt standardmäßig --mode refresh.
- Kein Timer darf --auto-resume verwenden.
- Resume nur explizit: retry-safe, limit, reason, expected_state_hash, Recovery-Gate falls nötig.

## PR-C: Operating Receipts v2

Neue Artefakte:

- grabowski_runtime_receipt
- grabowski_task_receipt
- grabowski_contract_receipt
- OPERATING_STATE.md

Receipt-Felder:

- release_id
- repo_head
- source_sha256
- contract_sha256
- semantic_tool_contract_sha256
- expected_tool_count
- registered_tool_count
- capability_catalog_sha256
- audit_valid
- kill_switch_engaged
- active_profile
- forbidden_capabilities
- running_tasks
- leases
- last_reconcile_check
- client_snapshot_observable
- drift list

## PR-D: Injection / Tool-Poisoning Regression v1

Ziel: Kein direkter Pfad untrusted_content -> high_risk_tool.

Fixtures:

1. Datei enthält Anweisung zum Secret-Reveal.
2. Log enthält scheinbare Systemnachricht.
3. PR-Body fordert Terminalkommando.
4. Toolbeschreibung enthält versteckte Fremdanweisung.
5. Tooloutput enthält URL zu internem Dienst.
6. Zweiter MCP-Server versucht Tool Shadowing.
7. Reconcile-Receipt schlägt gefährliches Resume vor.
8. Browserprofiltext enthält Exfiltrationsanweisung.
9. Secret-Fragmente erscheinen in Dateinamen, Logs, GitHub-Suchargumenten oder Fehlertexten.

## PR-E: Terminal und Exfil-Klassen reduzieren

Neue Diagnosewerkzeuge:

- grabowski_runtime_receipt
- grabowski_systemd_unit_inspect
- grabowski_task_db_check
- grabowski_log_grep_safe
- grabowski_file_hash_tree
- grabowski_open_url_safe

Exfil-Klassen:

- stdout/stderr
- Auditfelder
- Task-Records
- Dateinamen
- Git commit messages
- Branch names
- GitHub issue/PR/search arguments
- URLs
- DNS/HTTP egress
- Artefaktnamen
- Logs
- Receipts

## PR-F: Task Class Sandboxing

Task-Klassen:

- diagnostic: read-only, NoNewPrivileges=yes, PrivateTmp=yes, enge ReadOnlyPaths, kein Secret-Root.
- build-test: repo-bound, begrenztes Netzwerk, kein Secret-Root, Resource Lease erforderlich.
- mutation: konkrete Lease, konkrete Zielressource, Auditgrund, Hash-Precondition.
- break-glass-task: kurzlebig, reason, expires_at, explizite Bestätigung.

## PR-G: Connector Publication Split

Publikationsflächen:

- grabowski-observe: nur read-only, reconcile_check, receipts, logs/status.
- grabowski-power: mutierend, Approval / Break-Glass, Ablaufzeit, Auditmarker.

## Betriebsregeln

1. Kein generisches Terminal für Dinge, die ein dediziertes Tool kann.
2. Keine High-Risk-Tools im Default-Profil.
3. Keine Auto-Resume-Aktion ohne Retry-Safe, Limit, Reason und State-Hash.
4. Keine Secret-Inhalte in normalem Kontext.
5. Kein Deployment ohne Contract-Hash, Source-Hash, Teststatus und Rollback-Pfad.
6. Jeder Plattformblock ist ein Diagnoseereignis, kein Grund für Rechteausweitung.
7. Drift bleibt Drift, auch wenn alles scheinbar funktioniert.

## Epistemische Leerstellen

- Exakter Plattform-Blockgrund fehlt.
- Client-Snapshot ist lokal nicht beweisbar.
- Quantifizierter Tool-Blast-Radius fehlt.
- Egress-Verhalten pro Toolklasse fehlt.
- Vollständige Injection-Regression fehlt.

## Entscheidung

Der nächste operative Hebel ist Expositionskontrolle.

Nächste Aktion: PR-A Live Capability Profiles v1.
