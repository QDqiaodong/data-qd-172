# Human review workbench contract

Offline analysts confirm or dismiss candidate incidents in a local review
workbench: a single JSON file created with `fire-cluster review-init`,
completely independent from the publishing archive (`docs/publishing.md`).
Dashboard acknowledgements in the archive never count as review conclusions.
The workbench reads the same full observation JSON the standalone report and
`publish` accept — no archive batch has to be written first — and re-derives
the current incidents with the existing `wildfire.core.cluster` rules,
including maintenance-window suppression. No network service is involved.

## Commands

| Command | Purpose |
| --- | --- |
| `review-init --workbench PATH` | Create an empty workbench. Refuses to overwrite an existing file. |
| `review-import --workbench PATH --input OBS.json` | Replace the current observation set with the payload's full content and reconcile review cases. Prints the import receipt. |
| `review-current --workbench PATH` | List every active case with its incident, owner, revision, current conclusion, and review basis. |
| `review-todo --workbench PATH [--owner ID \| --unassigned]` | List exactly the active cases that still lack a current effective conclusion, optionally filtered to one owner or to unassigned cases. |
| `review-assign --workbench PATH --case cN --expected-revision K --actor WHO --owner WHO --reason TEXT` | Assign or reassign the owner of an active, still-pending case. |
| `review-conclude --workbench PATH --case cN --expected-revision K --actor WHO --verdict confirmed\|dismissed --note TEXT` | Record the human conclusion of an owned, active, still-pending case. |
| `review-history --workbench PATH --case cN` | Print the full audit trail of one case, active or retired. |

All output is JSON. All failures print `fire-cluster: <reason>` to stderr
and exit with code 2, leaving the workbench untouched.

## Imports and case identity

* Every import is the **complete** current observation set, never a delta.
  Each observation's `id` and `sensor` must be non-empty strings; everything
  else follows the existing clustering input contract, and a valid
  observation belongs to exactly one current incident (suppressed
  observations belong to none).
* Each distinct review object gets a `case_id` (`c1`, `c2`, ...) from a
  monotonic counter. Case ids are never reused and are entirely separate
  from the clustering `incident_id`.
* Between two imports, a previously **active** incident and a new incident
  are **directly linked** when their valid observation id sets intersect.
* The old `case_id` continues only when old and new incident are each
  other's **unique** link. Member changes and `incident_id` renames do not
  break a one-to-one continuation.
* Merges, splits, and cross-reorganizations retire **every** involved old
  case and create fresh cases. All direct predecessor/successor links are
  kept with their shared observation ids — never just the largest overlap
  or smallest id.
* Retired cases never reactivate. If an incident disappears and its
  observation ids later return verbatim, a brand new case is created.
* Re-importing the same effective state is a no-op: no new case, revision,
  link, or todo entry. Reordering the observations array is also a no-op.

## Review basis, revisions, and conclusions

* A case's **review basis** is the full incident report, each valid member's
  `id`, `sensor`, `time`, `lat`, `lon`, `severity`, and the import's
  `radius_km` and `window_minutes`. Basis comparison ignores JSON object key
  order and observation array order, compares numbers numerically and
  strings as-is (timestamps are never normalized), and ignores unlisted
  extra observation fields. Maintenance windows, suppressed observations,
  and other incidents cannot invalidate a case whose basis is unchanged.
* A one-to-one continuation with an unchanged basis keeps conclusion, owner,
  and revision. A changed basis — including a coordinate or time correction
  that leaves the cluster report identical — keeps `case_id` and owner,
  clears the current conclusion, bumps the revision, and re-opens the case.
* Cases born from merges, splits, or cross-reorganizations never inherit a
  conclusion. They inherit an owner only when every direct predecessor has
  the same non-empty owner; otherwise they start unassigned. Brand new cases
  without predecessors start unassigned. An owner may hold any number of
  cases.
* New and reopened cases appear on the todo list until concluded. Concluded
  active cases stay visible in `review-current`; retired cases can no longer
  be claimed.
* `review-assign` and `review-conclude` both require the `case_id`, the
  `expected_revision` read with it, and a non-empty `actor`. The expected
  revision is checked **before** anything else, so stale credentials are
  rejected even when the request would otherwise be a no-op. Re-setting the
  current owner with the matching revision returns `"changed": false`.
* Conclusions accept only `confirmed` or `dismissed` with a non-empty note,
  and the actor must be the current owner. One revision admits exactly one
  conclusion: the first success bumps the revision, so same-conclusion
  replays and opposite verdicts carrying the old revision are rejected
  without overwriting the original and without appending audit records.
  Unknown, retired, or already-concluded cases, stale revisions, empty
  notes, and owner mismatches are all explicit errors.
* A case's revision changes only when that case itself changes (assignment,
  conclusion, basis change, retirement); other cases never invalidate its
  credentials.

## History and atomicity

* `review-history` returns every owner change (actor, reason, revision),
  every conclusion (verdict, note, actor, revision, and the immutable basis
  it was recorded against — full member fields, clustering parameters, and
  the incident report), and the predecessor/successor links. A conclusion
  whose basis no longer matches is still returned, marked
  `"currently_effective": false`. Later corrections, reassignments, or the
  incident's disappearance never rewrite saved history.
* One import's case changes, conclusion invalidations, owner inheritances,
  and link records commit together through a single atomic file replacement;
  so do a human operation's state change and its audit record. Invalid
  input, stale operations, and failed writes leave no partial state and
  consume no identities, and reopening the workbench after any of them shows
  exactly the last committed state.

## Example session

```sh
# Create the workbench and import the current observation set.
fire-cluster review-init --workbench run/workbench.json
fire-cluster review-import --workbench run/workbench.json --input fixtures/input.json
# => {"changed": true, "created": [{"case_id": "c1", ...}, {"case_id": "c2", ...}],
#     "todo": ["c1", "c2"], ...}

# See what still needs review (optionally per owner or unassigned only).
fire-cluster review-todo --workbench run/workbench.json --unassigned

# Claim case c1 (read at revision 1) for analyst-a.
fire-cluster review-assign --workbench run/workbench.json --case c1 \
    --expected-revision 1 --actor lead --owner analyst-a --reason "triage rotation"
# => {"case_id": "c1", "changed": true, "owner": "analyst-a", "revision": 2}

# The owner records the human conclusion.
fire-cluster review-conclude --workbench run/workbench.json --case c1 \
    --expected-revision 2 --actor analyst-a --verdict confirmed \
    --note "two north sensors concur"
# => {"case_id": "c1", "conclusion": {...}, "revision": 3}

# Concluded cases stay visible; pending ones stay on the todo list.
fire-cluster review-current --workbench run/workbench.json

# A later import whose corrected coordinates reopen c1, a merge or split
# that retires it — every step remains queryable.
fire-cluster review-history --workbench run/workbench.json --case c1
```
