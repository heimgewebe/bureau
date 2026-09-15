# Bureau operator-native intake v1

Stand: 2026-09-15

## Rolle und Zweck

ChatGPT über Grabowski ist der ausführende Operator. Der Nutzer bleibt Beobachter und Steuermann. Diese Oberfläche übernimmt daher Kandidatenaufnahme, Bewertung, Task-Vorschlag und kontrollierte Veröffentlichung maschinell. Sie verlangt im Erfolgsweg keine Shell-Befehle, Dateisuche, manuelle Registry-JSON-Bearbeitung oder Statusaggregation durch den Nutzer.

Der Domänenkern liegt ausschließlich in `bureau.operator_intake`. CLI und künftige typisierte Grabowski-Werkzeuge sind dünne Adapter. Es entsteht keine zweite Task-, Queue-, Claim-, PR- oder Approval-Wahrheit.

## Fünf Transportoperationen

### 1. `operator-candidate-record`

Ein versionierter JSON-Request wird append-only in den bestehenden Live Register geschrieben.

Pflichtfelder:

- `schema_version: 1`
- `idempotency_key`
- `title`
- `source_kind`
- `desired_outcome`

Optionale Bindungen sind Repository, Task, Kandidaten-ID, `supersedes_event_id`, Source-Locator, Source-SHA-256, Beobachtungszeit und Notiz. Unbekannte Felder werden abgelehnt. Derselbe Idempotenzschlüssel mit denselben Eingaben liefert die vorhandene Identität; abweichende Eingaben erzeugen `idempotency-conflict`.

Eine append-only Korrektur oder Zustandsfortschreibung verwendet einen neuen Idempotenzschlüssel und bindet `supersedes_event_id` an das aktuelle Event des bestehenden Kandidaten. Das Feld ist Bestandteil des Request-Hashs. Kandidaten-ID, Repository, Task, Status und `promotion_required` werden vom Vorgänger geerbt, sofern die optionale Kandidaten-ID nicht ausdrücklich mit derselben Identität angegeben wird. Ein zuvor fehlendes Repository darf durch eine Refinement-Fortschreibung ergänzt werden; ein bereits gebundenes Repository darf nicht auf eine andere Ressource umgebunden werden. Task-Bindungen dürfen weiterhin weder neu gebunden noch geändert werden. Bei strikter Katalogvalidierung werden die effektiv geerbten Bindungen vor dem Append gegen den aktuellen Registry-Snapshot geprüft. Typfalsche, boolesche oder nichtpositive Event-IDs scheitern vor jeder Mutation; bereits supersedierte oder fremde Events werden durch den Live-Register-Vertrag abgelehnt.

Die Aufnahme begründet keine Registry-, Queue-, Readiness-, Claim- oder Dispatch-Wahrheit. Sie ist wie `live-register` ein Always-on-State-Store-Append und darf den kanonischen read-only Registry-Snapshot zur strikten Katalogvalidierung lesen.

#### Evidenzgebundener Kandidatenabschluss

Derselbe Transport akzeptiert zusätzlich `operation: "close"`. Dieser Modus ist kein Registry-Task-Closeout und keine Promotion: Er terminalisiert ausschließlich den bestehenden Live-Register-Kandidaten append-only. Pflicht sind `candidate_id`, die **aktuelle** `expected_event_id`, ein eigener `idempotency_key` und mindestens ein Beleg. `outcome` ist optional; fehlt das Feld, setzt Bureau serverseitig `completed`. Wird `outcome` explizit angegeben, ist weiterhin ausschließlich `completed` zulässig. Jeder Beleg enthält exakt `source`, `reference` und einen lowercase SHA-256-Digest. Zugelassene Quellen sind `receipt`, `test`, `git`, `github`, `runtime`, `bureau`, `workspace`, `job` und `user`; höchstens 16 Belege sind erlaubt.

Vor dem Append liest Bureau die aktuelle Kandidatenidentität. Nur `active` oder `observed` darf zu `closed` wechseln. Eine veraltete `expected_event_id` scheitert ohne Effekt und verlangt einen Kandidaten-Readback. Der Abschluss setzt `promotion_required=false`, übernimmt Titel, Repository, Task-Bindung und die bisherige Operator-Provenienz und ergänzt darin ein strukturiertes `candidate_closeout` mit Request-Hash, Evidence-Hash, Einzelbelegen, Vorgänger-Event und eigenem `closeout_sha256`. Dadurch bleibt der Abschluss Teil derselben append-only Bureau-Historie statt eine zweite Closeout-Wahrheit zu erzeugen.

Ein identischer Close-Request gegen den bereits geschlossenen Kandidaten ist ein wirkungsfreier idempotenter Replay. Ein anderer Request darf einen terminalen Kandidaten nicht nachträglich umdeuten. `closed` bedeutet nur: Die **im Request gebundene Kandidatenarbeit** ist durch die angegebenen Belege als abgeschlossen klassifiziert. Daraus folgen weder Registry-Task-, Queue-, Deployment- noch weitergehende Systemkonvergenz-Wahrheit.

### 2. `operator-candidate-assess`

Die Bewertung ist read-only und liefert:

- Source-Freshness und Katalogvalidierung;
- exakte Duplikatbefunde nur über dieselbe Kandidaten-ID oder eine explizit identische Task-ID;
- höchstens 20 gemeinsame Source-Digests als separate, ausschließlich beratende `source_relationships` mit Gesamtzahl und Trunkierungsindikator;
- höchstens fünf deterministische Ähnlichkeitshinweise;
- Zielinitiative, vorgeschlagene Claims, Risiko- und Approval-Verträge;
- fehlende Felder;
- eine Entscheidung `promote`, `merge`, `refine`, `defer` oder `drop`.

Ein gemeinsamer Source-Digest begründet allein keine Identitätsgleichheit. Kandidaten aus demselben Review-Artefakt bleiben unabhängig vorschlagsfähig, wenn Repository, gewünschtes Ergebnis oder explizite Task-Bindung abweichen. Idempotente Wiederholungen werden bereits bei der Aufnahme über Kandidaten-ID, Idempotenzschlüssel und Request-Hash abgefangen.

Ähnlichkeit ist ausschließlich beratend. Sie darf nie automatisch mergen, schließen, unterdrücken oder Registry-Wahrheit verändern.

Die Bewertung verlangt genau einen Selektor: aktuelle Kandidaten-ID, aktuelle Event-ID oder den ursprünglichen `idempotency_key`. Der Idempotenzselektor ist insbesondere der eindeutige Readback-Pfad nach einem unklaren Aufnahmeergebnis. Er liefert den aktuellen, gegebenenfalls supersedierenden Stand derselben Kandidatenidentität und begründet keine neue Mutation.

### 3. `operator-task-propose`

Der Vorschlag bindet:

- die aktuelle Kandidaten-ID und das aktuelle Event;
- Source-Provenienz;
- den exakten Registry-Commit und Registry-Tree;
- den registrierten `publishing_task_id`;
- vollständiges Task-JSON und dessen Hash;
- den gerenderten Task-Dateihash;
- den kanonischen Ein-Datei-Änderungsdigest;
- Assessment und ungelöste Felder;
- den Publikationsvertrag `task_creation_from_external_evidence`.

Für den normalen Candidate→TaskSpec-Pfad ist kein separates Operator-Review erforderlich. Neue Vorschläge tragen deshalb `review.required=false` und `review.status=not_required`. Das ist ausdrücklich **keine** Publikationsautorität: Proposal und Preview verändern weder Registry noch Queue noch StateStore-Task-Wahrheit. Die eigentliche Operator-Autorität muss erst am Effektpfad durch eine serverseitig erzeugte Grabowski-Publication-Authority belegt werden.

Die TaskSpec-Zielidentität muss neu sein. Initiative, Abhängigkeiten, Claims, Capabilities und Acceptance werden gegen Registry und autoritativen StateStore geprüft. Generische Legacy-Acceptance wird ohne explizite Begründung abgelehnt. Die Plan-Datei wird create-only geschrieben.

### 4. `operator-task-review`

Für neue `task_creation_from_external_evidence`-Vorschläge ist dieser Schritt nicht mehr Teil des normalen Pfads. Ein kompatibler Aufruf liefert `status=not_required`, mutiert den Vorschlag nicht und erzeugt insbesondere **keine** Operator- oder Publikationsautorität.

Der bisherige Reviewvertrag bleibt für explizite Legacy-/Kompatibilitätsvorschläge mit `publication.action_class=registry_mutation` erhalten. Dort bindet das Review weiterhin den Operator an den exakten `proposal_sha256`, ersetzt die Plan-Datei atomar per Compare-and-Swap und erzeugt hashgebundene `reviewed_plan`-Evidenz. Ein identischer Wiederholungsaufruf ist idempotent; abweichender Reviewer, falscher Proposal-Hash, Symlinks oder unklare Readbacks scheitern fail-closed.

Damit unterhält der normale Effekt nicht zwei Autoritätsmodelle gleichzeitig: typisierte Candidate-Publikation verwendet serverseitige Operator-Autorität; allgemeine `registry_mutation` bleibt bei `reviewed_plan`.

### 5. `operator-task-publish`

Ohne `--apply` ist der Aufruf eine wirkungsfreie Vorschau. Sie prüft Planintegrität, Registry- und Kandidatendrift, Task-Schema, Dedupe-/Proposal-Bindung und ungelöste Felder. Beim typisierten Candidate-Pfad bleibt `approval.allowed=false`: Preview behauptet ausdrücklich keine Publikationsautorität. Sie liefert den benötigten StateStore-Ressourcenschlüssel, die registrierte Publishing-Task-ID und die **exakt erwarteten** Lease-Metadaten für den späteren Effekt.

Der Effektpfad akzeptiert keine caller-gelieferten Lease-Snapshots oder Metadaten als Autorität. Bureau liest Grabowskis private Resource-Datenbank selbst read-only und prüft vor der StateStore-Mutation:

- unterstütztes DB-Schema und private Datei;
- exakten Owner und vollständige Ressourcenschlüssel;
- gültige Lease-Zeit und Mindestrestlaufzeit;
- `kind=grabowski.bureau_task_publication_authority`;
- `authority_action_class=task_creation_from_external_evidence`;
- `authority_capability=bureau_mutation`;
- den registrierten `publishing_task_id`;
- `operation=state-task-publication`;
- den exakten `proposal_sha256`;
- `bureau_phase=work`.

Erst **nach** erfolgreicher Prüfung dieser server-owned Authority erzeugt Bureau die Operator-Approval-Evidence für `task_creation_from_external_evidence`. Vor dem StateStore-CAS wird dieselbe Authority unter einer `BEGIN IMMEDIATE`-Sperre der privaten Resource-Datenbank erneut validiert; diese Sperre bleibt bis nach dem StateStore-CAS bestehen. Release oder Austausch der Authority können den Effekt damit nicht zwischen Prüfung und Mutation überholen. Fehlende oder abweichende Authority-Felder scheitern vor dem StateStore-Effekt. Die öffentliche Grabowski-Resource-Acquire-Oberfläche lehnt den Authority-Kind als server-owned ab; ein Caller kann ihn daher nicht über die allgemeine Lease-Oberfläche prägen. Ein Caller-Feld wie `operator=true`, `read_only=true` oder eine selbst gebaute Metadatenstruktur besitzt keine Autoritätswirkung.

Die TaskSpec-Publikation erfolgt im autoritativen StateStore mit Revision/CAS- und Readback-Prüfung. Ein erfolgreicher Receipt bindet Proposal, TaskSpec-Revision, Spec-Digest, Lease-Evidenz und die verwendete Approval-Entscheidung. Idempotenter Replay prüft denselben typisierten oder Legacy-Vertrag erneut und startet keinen zweiten Effekt. Queue und Registry-Dateien bleiben unverändert.

**Vertrauensgrenze:** Die private Grabowski-Resource-Datenbank ist serverseitiger Operatorzustand. Wer diese Datenbank außerhalb des Vertrages beliebig direkt verändern kann, liegt außerhalb des Caller-Spoofing-Modells und hätte bereits eine stärkere lokale Systemkompromittierung.

Der Publisher merged nicht, queued nicht, claimt nicht, dispatcht nicht und deployt nicht. Allgemeine `registry_mutation` bleibt weiterhin an `reviewed_plan` gebunden.

## Fehlervertrag

`OperatorIntakeError` liefert stabil:

- `code`
- `retryable`
- `effect_started`
- `ambiguity`
- `required_readback`
- `details`

Ein unbekannter Fehler nach möglichem Push oder PR-Effekt wird nicht blind wiederholt. Er wird als `publication-unclear` mit erforderlichem Remote-Branch-, PR- und Task-Datei-Readback ausgegeben.

## Idempotenz und Receipts

Ein erfolgreicher Effekt schreibt ein create-only Receipt mit Proposal-, Plan-, Registry-, Task-, Lease-, Branch-, PR- und Readback-Bindung. Ein identischer Wiederholungsaufruf liefert dieses Receipt auch nach späterem Registry-Fortschritt erneut, ohne Leases oder Publisher erneut zu benutzen. Manipulierte oder fremde Receipts werden abgelehnt.

## CLI als Transport

Beispiel für die Aufnahme:

```bash
bureau --json operator-candidate-record --request candidate-request.json
```

Beispiel für eine Korrektur im selben Kandidaten-Lebenszyklus:

```json
{
  "schema_version": 1,
  "idempotency_key": "conversation:refinement:2",
  "title": "Korrigierte Kandidatenbeschreibung",
  "source_kind": "conversation",
  "desired_outcome": "Den vorhandenen Kandidaten präzisieren",
  "supersedes_event_id": 31
}
```

Bewertung:

```bash
bureau --json operator-candidate-assess --candidate-id candidate-...
```

Mehrdeutiger Aufnahme-Readback über den exakten Idempotenzschlüssel:

```bash
bureau --json operator-candidate-assess --idempotency-key conversation:...
```

Vorschlag in einem expliziten sauberen Registry-Checkout:

```bash
bureau --root /path/to/clean/bureau --json operator-task-propose \
  --candidate-id candidate-... \
  --task-json task.json \
  --publishing-task-id OPERATOR-MACHINE-READABILITY-V1-T017 \
  --write-plan proposal.json
```

Wirkungsfreie Veröffentlichungsvorschau:

```bash
bureau --root /path/to/clean/bureau --json operator-task-publish \
  --plan proposal.json --preview
```

Effekt nach serverseitiger Grabowski-Authority-/Lease-Akquise:

```bash
bureau --root /path/to/clean/bureau --json operator-task-publish \
  --plan proposal.json --apply \
  --lease-binding lease-binding.json \
  --workspace-root /path/to/operator-publications \
  --receipt /path/to/receipt.json
```

`lease-binding.json` enthält nur die Transportbindung an Owner und registrierte Publishing-Task-ID. Die tatsächliche Authority wird daraus nicht geglaubt, sondern live aus Grabowskis privater Resource-Datenbank gelesen und gegen Proposal, Task, Operation, Aktionsklasse, Capability und Phase geprüft. Für explizite Legacy-`registry_mutation`-Proposals bleibt `operator-task-review` vor dem Publish erforderlich.

### 6. `operator-task-ready`

Schließt ausschließlich die Readiness-Lücke für **standalone** TaskSpecs nach ihrer bereits belegten Publication. `operator-task-publish` bleibt absichtlich pre-merge und schreibt den neuen Task nur als `planned`; erst dieser zweite, mergegebundene Schritt darf genau diesen Task im autoritativen StateStore auf `ready` setzen.

Der Vertrag ist eng und fail-closed. Er verlangt:

- ein intaktes `bureau_task_publication_receipt` mit exakter Task-ID, Proposal-Digest, TaskSpec-Revision und Taskdatei-Digest;
- aktuelle Registry-Bytes und den kanonischen TaskSpec-Digest exakt wie im Publication-Receipt;
- einen frischen GitHub-Readback desselben PR mit `MERGED`, exakt demselben Head, Branch und Base `main`;
- einen standalone Task ohne `depends_on`, Parent oder Children;
- im StateStore exakt die im Receipt gebundene Revision, denselben Spec-Digest, dieselbe Spec und `state=planned`.

Die Repository-Identität stammt primär aus dem GitHub-`origin` des Registry-Checkouts. Ein immutable canonical Runtime-Snapshot besitzt absichtlich kein Git-Remote; ausschließlich wenn `bureau runtime-identity` genau diesen Snapshot als manifestgebunden, integer und `canonical-read-only` bestätigt, darf dessen gebundene `expected_repository`-Identität verwendet werden. Beliebige Nicht-Git-Verzeichnisse bleiben fail-closed.

Vor jeder Wirkung kann der vollständige Vertrag read-only geprüft werden:

```bash
bureau --json operator-task-ready \
  --publication-receipt /path/to/publication-receipt.json \
  --preview
```

Der Effekt besteht aus genau einem revisionsgebundenen CAS `planned -> ready`. Queue, Claims, Dispatch, Registry-Dateien und Runtime bleiben unverändert. Der erfolgreiche CAS wird sofort revisions- und digestgebunden zurückgelesen und anschließend in einem create-only Promotion-Receipt festgehalten:

```bash
bureau --json operator-task-ready \
  --publication-receipt /path/to/publication-receipt.json \
  --apply \
  --promotion-receipt /path/to/promotion-receipt.json
```

Ein späterer Readback oder exakter Replay akzeptiert nur dieses Receipt, wenn Publication-/Merge-Bindung und die aktuelle promovierte StateStore-Revision weiterhin exakt übereinstimmen:

```bash
bureau --json operator-task-ready \
  --publication-receipt /path/to/publication-receipt.json \
  --readback \
  --promotion-receipt /path/to/promotion-receipt.json
```

Drift bei PR, Head, Branch, Taskdatei, Registry-Spec, StateStore-Revision oder Receipt stoppt fail-closed. Eine fehlgeschlagene oder unklare CAS-/Receipt-Phase begründet keinen Blind-Retry, sondern verlangt exakten StateStore-/Receipt-Readback.

## Nichtbehauptungen

Diese Oberfläche begründet nicht:

- automatische semantische Duplikaterkennung als Wahrheit;
- Queue-, Readiness-, Claim- oder Dispatch-Autorität;
- Merge-, Deployment- oder Verifikationsautorität;
- ein Recht, Kosten-, Sicherheits-, Datenschutz-, Irreversibilitäts- oder Steuergates zu umgehen;
- die Abwesenheit gleichberechtigter Eingriffe außerhalb der belegten Git-, Lease- und Readback-Grenzen.
