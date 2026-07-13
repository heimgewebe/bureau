# Bureau State-Root-Artefakte v1

Stand: 2026-07-13

## Zweck

Bureau unterscheidet aktiven Laufzeitstatus von auditierbaren Arbeitsartefakten. Der Befehl
`state-root-artifacts` inventarisiert die verwalteten Verzeichnisse `evidence` und `plans`
und bietet für andere Top-Level-Artefakte eine reviewgebundene, reversible Migration aus
dem aktiven State-Root an.

Der Vertrag löscht nichts. Unbekannte, gemischte, symlinkte, übergroße oder digestfalsche
Inhalte bleiben sichtbar und ungesund.

## Erkannte Klassen

### `completion-evidence-directory`

`evidence/` ist nur dann bekannt, wenn jedes direkte Kind ein echtes Verzeichnis mit exakt
folgenden Dateien ist:

- `pr.diff`
- `self-review.json`

Der Self-Review muss Schema 1 und den Typ `bureau_pr_self_review` tragen, auf `PASS` stehen,
Repository, PR, Base und Head binden, die fünf Achsen `correctness`, `integration`,
`regression_risk`, `security` und `tests` vollständig als `PASS` ausweisen und sowohl
Diff-Größe als auch Diff-SHA-256 korrekt binden. Der kanonische Review-SHA-256 wird über
den JSON-Inhalt ohne das Feld `review_sha256` berechnet.

Autorität: Review-Evidenz. Die Klasse belegt allein weder Merge, Deployment noch
Task-Abschluss.

Retention: bis zur belegten Ablösung; kein automatisches Löschen.

### `reviewed-plan-directory`

`plans/` ist nur dann bekannt, wenn jede Datei ein geprüftes `live-promote-plan` Schema 2
ist. Event, Initiative, Task-Projektion und Quellereignis müssen zusammenpassen. Der
Generatorhash darf den ursprünglichen Pending-Plan binden, während der spätere Review als
separates Overlay vorliegt.

Autorität: geprüfter Vorschlag. Der Plan erzeugt allein keine Registry-, Queue-, Claim-,
Dispatch- oder Merge-Autorität.

Retention: bis zur Anwendung oder belegten Ablösung; kein automatisches Löschen.

## Read-only Inventur

```bash
bureau --root /path/to/bureau --json state-root-artifacts
```

Die Antwort enthält für jedes verwaltete Kind:

- Typ und Größe;
- Datei- oder Baum-Digest;
- Quellzeit aus `mtime_ns` und Beobachtungszeit;
- Inhaltsklasse;
- Produzentenidentität;
- Retentionklasse;
- Autoritätsgrenze und Non-Claims;
- Validitätsstatus und genaue Ablehnungsgründe.

Die Inventur folgt keinen Symlinks und liest keine Pfade außerhalb der beiden verwalteten
Verzeichnisse.

## Reviewed create-only Migration

Eine Migration verschiebt ausschließlich explizit benannte Top-Level-Einträge. Der Plan
wird außerhalb des aktiven State-Roots create-only geschrieben:

```bash
bureau --root /path/to/bureau --state-root ~/.local/state/bureau \
  --json state-root-artifacts \
  --entry artifact-a --entry artifact-b \
  --destination-root ~/.local/share/bureau/quarantine/state-root/<run-id> \
  --write-plan /outside/state-root/migration-plan.json
```

Vor der Planerzeugung prüft Bureau:

- Quelle existiert und ist weder selbst noch intern ein Symlink;
- Datei-, Verzeichnis-, Anzahl- und Größenlimits;
- vollständige Datei- und Baum-Digests;
- keine textuelle Referenz aus `registry/` oder `docs/`;
- keine sichtbare Prozessreferenz über Arbeitsverzeichnis oder offene Dateideskriptoren;
- Ziel fehlt und überlappt den aktiven State-Root nicht.

Der Review setzt `review.status=reviewed`, `reviewer`, `reviewed_at` sowie Kopien von
`review_payload_sha256`, `entries_sha256` und `destination_root`. Der Payload-Digest
bindet alle operativen Felder des Plans einschließlich State-Root, Referenzwurzel,
Einträgen, Ziel, Ausführungsbedingungen und Non-Claims. Ohne diese Bindung erfolgt keine
Wirkung; jede nachträgliche Änderung am operativen Plan wird abgelehnt.

## Anwendung

```bash
bureau --root /path/to/bureau --state-root ~/.local/state/bureau \
  --json state-root-artifacts --apply-plan /outside/state-root/migration-plan.json
```

Plan und Receipt verwenden für Mutationen Schema 2. Auf Linux öffnet Bureau State-Root,
Referenzwurzel, deren direkte Eltern und den nächsten bestehenden Zielvorfahren
komponentenweise ab `/` mit `O_DIRECTORY|O_NOFOLLOW`. Der Plan bindet Pfad, Gerät, Inode
und Modus dieser Verzeichnisse. Zusätzlich wird geprüft, dass State-Root und
Referenzwurzel unter dem erwarteten Namen genau auf die gebundene Identität im gebundenen
Parent zeigen.

Vor dem ersten Effekt werden **alle** Einträge geprüft: Plan-Dateihash, Quellidentität,
Referenzen, Prozesse, Kollisionen und Dateisystemgrenze. Zielkomponenten werden relativ zu
einem gehaltenen Verzeichnisdeskriptor erzeugt und ebenfalls mit Gerät/Inode gebunden.
Die Verschiebung erfolgt ausschließlich descriptor-relativ per `renameat`-Semantik
(`os.rename` mit `src_dir_fd` und `dst_dir_fd`). Absolute Pfade werden am Effektübergang
nicht erneut als Mutationsziel aufgelöst.

Vor und nach jedem Effekt werden die gehaltenen Deskriptoren, ihre aktuellen Pfade und die
Parent-Kind-Beziehungen erneut geprüft. Bei einer Abweichung werden alle in diesem Lauf
bereits verschobenen Einträge descriptor-relativ in umgekehrter Reihenfolge kompensiert;
leere, in diesem Lauf erzeugte Zielverzeichnisse werden entfernt.

Ein create-only Receipt bindet Plan, Einträge, Ziel, Rollbackwege, alle Plan-Anker sowie
den tatsächlichen Ziel-Root und dessen direkten Parent. Es bindet außerdem seinen eigenen
kanonischen SHA-256. Eine identische Wiederholung öffnet dieselben Anker erneut, validiert
Parent-Kind-Beziehungen und Eintragsidentitäten und ist idempotent. Ein verändertes oder
älteres Receipt wird für Mutationen abgelehnt.

## Rollback

```bash
bureau --root /path/to/bureau --state-root ~/.local/state/bureau \
  --json state-root-artifacts \
  --rollback-receipt /outside/state-root/migration-plan.json.receipt.json
```

Rollback akzeptiert ausschließlich Receipt-Schema 2. Es öffnet dieselben gebundenen
Verzeichnisanker ohne Symlinkfolge, prüft den direkten Ziel-Parent, alle Zielkollisionen,
jeden Eintragsdigest und sämtliche Einträge vor der ersten Wirkung. Es verschiebt nur
unveränderte Quarantäneeinträge descriptor-relativ an ihre ursprünglichen Pfade zurück.
Bei einem Fehler wird auch der Rollbacklauf kompensiert. Receipt und
Quarantäneverzeichnis werden nicht gelöscht.

## Bedrohungsmodell und Plattformvertrag

Der abgesicherte Fall ist ein konkurrierender Prozess mit denselben Dateirechten, der
zwischen Review, Preflight und Effekt einen Vorfahren umbenennt, ersetzt oder durch einen
Symlink substituiert. Synthetische Tests belegen, dass Quelle, Ziel und Referenzwurzel dann
vor der Wirkung abgelehnt oder eine bereits begonnene Wirkung kompensiert wird. Der
Nachweis arbeitet ausschließlich in temporären Verzeichnissen; aktiver Bureau-State wird
nicht migriert.

Mutationen benötigen Linux, `dir_fd`-fähige `open`, `mkdir`, `rename`, `rmdir` und `stat`,
`O_DIRECTORY`, `O_NOFOLLOW` sowie `/proc/self/fd`. Fehlt eine Voraussetzung, entsteht ein
typisierter Fail-closed-Fehler. Es gibt keinen stillen Rückfall auf pfadbasierte
Mutationen. Inventur bleibt davon unberührt read-only nutzbar.

Nicht behauptet wird atomarer Schutz gegen den Austausch des finalen Eintragsnamens exakt
zwischen letzter Identitätsprüfung und `renameat`. Dieser enge Restspalt wird durch
descriptor-relative Eltern, unmittelbare Nachprüfung und Gesamtlaufkompensation begrenzt,
aber nicht als kernelatomar geschlossen dargestellt.

## Sicherheitsgrenzen

- `doctor` wird nur gesund, wenn jeder aktive Top-Level-Eintrag bekannt ist oder den
  aktiven State-Root über eine geprüfte Migration verlassen hat.
- Klassifikation ist keine Inhaltsautorität.
- Migration ist keine Löschfreigabe und kein Obsoleszenzbeleg.
- Nicht sichtbare Kernel-, Container- oder Fremdnutzer-Referenzen werden nicht behauptet.
- Neue Artefaktformen bleiben fail-closed unbekannt, bis ihr Vertrag explizit ergänzt ist.
