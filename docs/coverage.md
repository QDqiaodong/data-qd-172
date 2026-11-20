# Sampling coverage audit contract

Clustering only sees observations that actually arrive, so a silent period
can mean either a quiet forest or a sensor that stopped reporting. The
`coverage-audit` command is a one-shot audit that compares the received
observations against what every sensor was *scheduled* to sample, then
attaches an explainable evidence-completeness verdict to each candidate
incident. It reuses the existing clustering and maintenance-window rules
unchanged: incident membership, ids, `confidence`, `mean_severity`, the
legacy command behavior and the existing report file format are never
modified.

## Inputs

`fire-cluster coverage-audit` reads two JSON documents:

* `--input`: the same full observation JSON accepted by the legacy
  `fire-cluster --input` command (including optional
  `maintenance_windows`);
* `--schedule`: a sampling plan of the form
  `{"sensors": [{"sensor_id", "active_start", "active_end",
  "cadence_seconds", "phase"}, ...]}`.

For each plan, scheduled slots are `phase + k * cadence_seconds` for every
integer `k`. A slot is counted only when it falls in **both** the audit
window `[--start, --end)` and the deployment interval
`[active_start, active_end)` — both lower bounds are inclusive and both
upper bounds exclusive. `active_start` landing exactly on a slot therefore
counts; a slot landing exactly on `active_end` or the audit end does not.

Every timestamp (observations, windows, plan fields, audit bounds) must
carry a UTC offset. Instants are compared as the moments they denote, so
`2026-07-01T00:00:00Z` and `2026-07-01T00:00:00+00:00` produce and hit the
same slot. Validation rejects offset-free or unparseable timestamps,
non-positive or non-integer `cadence_seconds`, inverted deployment
intervals, duplicate `sensor_id` plans, and any observation whose sensor
has no plan. Nothing invalid is silently ignored.

`--tolerance-seconds` must be an integer strictly below half of **every**
plan's `cadence_seconds`; the audit refuses to run otherwise.
`--minimum-coverage` must be a number within `[0, 1]`.

## Slot coverage rules

* An observation can cover a slot only when its sensor matches exactly and
  the absolute time difference is at most `tolerance_seconds`.
* One observation covers at most one slot; one slot is covered at most
  once even when several readings match it. Duplicate readings for a slot
  never increase coverage.
* Because the tolerance is below half a cadence, a reading is eligible for
  at most one slot. If a reading is equidistant from candidates, the
  smallest total deviation wins, then the earliest slot UTC instant, then
  the lexicographically smallest observation id, so the result is
  independent of input ordering.
* A slot whose UTC instant falls in a maintenance window `[start, end)`
  for its sensor is a **planned outage**: excluded from the coverage
  denominator and never listed as missing. Overlapping or duplicate
  windows merge and only affect each slot once. A maintenance-period
  observation stays suppressed by the existing clustering rule, so it can
  neither cover a slot nor appear as `off_schedule`.
* An unplanned-outage slot with no matching observation is **missing**.
* A valid, non-suppressed observation that matches no scheduled slot is
  reported as `off_schedule`; it never inflates any coverage figure.

## Incident verdicts

Each incident keeps its full original clustering fields and gains:

* `event_window`: `[first_member_time, last_member_time + 1 microsecond)`,
  half-open in UTC. A single-moment incident therefore spans that moment
  through one microsecond later, so the slot at its exact moment still
  participates.
* `sampling_coverage`: the network-level slot counts recomputed with the
  exact same plan and matching rules over the intersection of the audit
  window and the event window. Planned-outage slots stay outside the
  denominator.

Verdict `status` is one of:

* `no_expected_samples` — no eligible slots exist in the intersection;
  `coverage_ratio` is `null`;
* `incomplete` — eligible slots exist and
  `covered / eligible < minimum_coverage`;
* `sufficient` — eligible slots exist and the ratio meets
  `minimum_coverage`.

An incomplete verdict never deletes or alters an incident; it only
records why the supporting evidence may be partial.

## Output fields

The output JSON (written with exclusive create, like the other commands)
contains:

* `audit_window`: normalized UTC `start`/`end` of the audit;
* `tolerance_seconds`, `minimum_coverage`: the effective parameters;
* `coverage`: network totals — `expected_slots` (all scheduled slots
  including planned outages), `planned_outage_slots`, `eligible_slots`
  (expected minus outages, the ratio denominator), `covered_slots`,
  `missing_slots`, `coverage_ratio` (`null` when nothing was eligible),
  and `off_schedule_observation_count`;
* `sensors`: one entry per planned sensor (sorted by `sensor_id`) with the
  same per-sensor counts, its `coverage_ratio`, sorted
  `off_schedule_observation_ids`, and a stable, time-sorted `slots` list.
  Each slot entry has `slot_time`, `status`
  (`covered` / `missing` / `planned_outage`) and the `observation_id`
  that covers it (`null` otherwise), making every covered slot
  traceable;
* `off_schedule_observations`: stable, id-sorted list of
  `{observation_id, sensor_id, time}` for readings that matched no slot;
* `cluster_report`: the complete unmodified `cluster()` output;
* `incidents`: the original incident fields plus `event_window` and
  `sampling_coverage`. The incident block adds `eligible_slots`,
  `covered_slots`, `missing_slots`, `planned_outage_slots`,
  `expected_slots`, `coverage_ratio`, `minimum_coverage`, and
  `missing_slot_details` (sorted by slot time then sensor id) identifying
  every uncovered eligible slot inside the event range.

Reordering any input list cannot change the conclusion; all slot and
observation detail lists are deterministically sorted.

## CLI

```sh
fire-cluster coverage-audit \
  --input observations.json \
  --schedule sampling-plan.json \
  --start 2026-07-01T00:00:00Z \
  --end 2026-07-02T00:00:00Z \
  --tolerance-seconds 60 \
  --minimum-coverage 0.9 \
  --output run/coverage.json
```

The stdout summary prints `observations`, `incidents`, the network
`coverage` ratio, `eligible_slots` and `missing_slots`. Any validation or
precondition failure exits with status 2 and writes no output file.
Running the audit never changes what the legacy
`fire-cluster --input ... --output ...` command produces, with or without
maintenance windows.
