# Publishing archive contract

Offline dashboards consume candidate incident reports independently. A local
JSON archive (created with `fire-cluster archive-init`) stores the publishing
history; it never talks to a network service. Every published batch reuses the
existing `wildfire.core.cluster` rules, including maintenance-window
suppression, so report content and error behavior match the standalone
`fire-cluster --input ... --output ...` command, which remains unchanged.

## Commands

| Command | Purpose |
| --- | --- |
| `archive-init --archive PATH --consumer ID [--consumer ID ...]` | Create an archive registering one or more unique consumer ids. Refuses to overwrite an existing file. |
| `publish --archive PATH --input OBS.json --batch-id ID` | Cluster the full observation payload and reconcile it against the current state. Prints the batch receipt. |
| `pending --archive PATH --consumer ID` | Print that consumer's unacknowledged records in record order, each carrying the full immutable event content of its version. |
| `ack --archive PATH --consumer ID --record rN` | Acknowledge one exact record for one consumer. Re-acknowledging is a harmless duplicate. |

All failures print `fire-cluster: <reason>` to stderr and exit with code 2.

## Version and record semantics

* A new incident publishes a new **version** (`v1`, `v2`, ...) holding the
  full, immutable event content.
* An incident whose full content changed first gets a **withdraw** record for
  the old version and then a **publish** record for a freshly allocated
  version.
* A disappeared incident gets a withdraw record only; no new version is
  allocated.
* An incident whose content is unchanged produces no record.
* Version ids are never reused. If an incident disappears and later returns
  with its original content, it gets a brand new version id.
* Record ids (`r1`, `r2`, ...) are stable, monotonic, and never reused.
  Records are ordered first by `incident_id` (lexicographic), and for the same
  incident every withdraw precedes the replacement publish. Each withdraw
  record references the exact version it withdraws.
* Reopening the archive preserves every version, record id, order, receipt,
  and acknowledgement.

## Consumer acknowledgements

* Each consumer has an independent acknowledgement set. Consumers never see
  each other's acknowledgements in `pending`.
* An acknowledgement is bound to exactly one `(consumer_id, record_id)` pair.
  Acknowledging an old publish record does not acknowledge its later withdraw,
  its replacement publish, or any other consumer's copy.
* Duplicate acknowledgements return `"duplicate": true` and change nothing.
* Unknown consumer ids and unknown record ids are rejected; unacknowledged
  records can never be deleted from history.

## Batches and atomicity

* A batch's current state, new versions, publish/withdraw records, and the
  stored receipt commit together via a single atomic file replacement; on
  failure the previous archive and all acknowledgements are untouched.
* Replaying a `batch_id` with byte-equivalent JSON content (key order
  insensitive) returns the stored receipt and allocates no new version or
  record ids.
* The same `batch_id` with different content is rejected before any state
  change.
* Input that fails clustering validation is rejected before the archive is
  read for mutation, so failed batches do not consume their `batch_id`.
* Acknowledgement failures (unknown consumer or record) leave no partial
  state.
