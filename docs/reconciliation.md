# Primary/backup gateway reconciliation contract

Two offline gateways receive the same physical sensor samples. Either export
may miss rows and each gateway numbers its own rows, so a backup export can
never be concatenated into the primary one: a duplicated sample would change
the observation counts and confidence of the candidate incidents. The
`reconcile` command compares the two exports, explains which backup rows are
safe to recover, and writes a payload that is itself a valid clustering input
for the unchanged `fire-cluster --input ... --output ...` command.

```sh
fire-cluster reconcile --input gateways.json --output run/replay.json
# The product is consumed by the existing clustering entry point unchanged:
fire-cluster --input run/replay.json --output run/report.json
# It is also a valid input for `publish`, `review-import`, and every other
# command that accepts the full observation payload.
```

## Input

One JSON object with:

| Field | Meaning |
| --- | --- |
| `primary` | Observation rows from the primary gateway; `id` is unique inside the export, `sensor` is shared terminology across gateways, `utc_ms` is an exact integer Unix millisecond timestamp. |
| `backup` | Observation rows from the backup gateway; same shape except the integer clock reading is named `clock_ms`. |
| `anchors` | Non-empty list of timing records. Each gives `clock_ms`, `utc_min_ms`, `utc_max_ms`: the true UTC of that clock reading lies in the closed interval. The corrected time is `clock_ms + offset` for one integer offset shared by every sensor. |
| `tolerance_ms` | Nonnegative integer. A primary and a backup row can explain the same sample only when their corrected times differ by at most this amount. |
| `radius_km`, `window_minutes` | Passed through to clustering; `radius_km` must be positive and `window_minutes` nonnegative. |
| `maintenance_windows` | Optional; preserved verbatim and still applied by the downstream clustering. |

Each observation row carries `id`, `sensor`, `lat`, `lon`, `severity` with
the same ranges as clustering input (latitude in `[-90, 90]`, longitude in
`[-180, 180]`, severity in `[0, 1]`). Rows within one export always describe
distinct physical samples, so no within-export deduplication is performed;
reusing a raw row number on the two gateways is legal and never drives
pairing. Both arrays may be empty or in any order.

Every anchor must be satisfiable by the same integer offset. Inverted anchor
intervals, an empty anchor intersection, missing fields, non-integer or
non-finite numbers, out-of-range measurements, and duplicate ids inside one
gateway fail the whole run with `fire-cluster: <reason>` and exit code 2; no
half-valid output file is written.

## What counts as a duplicate

For one anchor-allowed offset, a primary row and a backup row form a
candidate duplicate pair when the sensor string, latitude, longitude, and
severity are identical and
`abs(primary.utc_ms - (backup.clock_ms + offset)) <= tolerance_ms`. Each row
is used at most once, so the pairs at a fixed offset are a one-to-one
matching. Reconciliation compares **every** allowed offset and **every**
matching at each offset and maximizes the number of duplicate pairs. Every
(offset, matching) combination reaching that global maximum is an equally
valid optimal solution; the command never prefers a sensor, a smaller time
gap, a smaller offset, or lexicographically smaller rows.

The pairing graph only changes at finite breakpoints (each candidate pair is
active on one closed offset interval), so the anchor range is split into
constant-graph segments rather than enumerated millisecond by millisecond.
An hours- or years-wide anchor range whose pairing conditions never change
costs the same as a single-millisecond range; processing grows with the
number of candidate pairs and segments, never with the millisecond width.

## Output

The top level keeps `radius_km`, `window_minutes`, and any
`maintenance_windows`, plus the merged `observations` and a
`reconciliation` block.

Every primary row is retained unconditionally with its original values and
UTC. Exported observation ids are namespaced as `primary:<raw id>` and
`backup:<raw id>`; `sensor` is kept verbatim and `time` is an offset-bearing
UTC ISO string with millisecond precision. A backup row is added to the
replay only when it is unpaired in **every** optimal solution **and** every
optimal solution gives it the same corrected UTC (the optimal offset set is
a single integer). All other backup rows stay out of `observations`.

`reconciliation` contains:

* `tolerance_ms` and `anchor_offset_range` (`min`/`max` integer bounds of
  the offset allowed by the anchors);
* `max_duplicate_pairs`, the global maximum;
* `optimal_offset_intervals`: sorted, disjoint closed intervals covering
  exactly the offsets used by at least one optimal solution. Adjacent
  integers are merged into one interval, so gaps (for example offsets `0`
  and `10` with nothing optimal between) stay separate;
* `backup_rows`, one report per backup row (sorted by raw id), regardless of
  the input order:
  * `possible_primary_ids` — every primary raw row it can be paired with in
    *some optimal solution* (never a pairing that only occurs below the
    global maximum), sorted;
  * `can_be_unpaired_in_optimal_solution` — whether an optimal solution
    exists in which this row is not matched;
  * `decision` — `included` or `shelved`;
  * `reason_code` and a human-readable `reason`;
  * `corrected_utc_ms` when the optimal offset is unique, otherwise
    `possible_corrected_utc_ms` with `min`/`max` bounds over the optimal
    offsets.

### Backup row reason codes

| `reason_code` | Situation |
| --- | --- |
| `duplicate_always_same_primary` | A duplicate in every optimal solution and always of the same primary row; the primary export already carries the sample. |
| `duplicate_primary_ambiguous` | A duplicate in every optimal solution, but the primary row it matches differs between optimal solutions. |
| `duplicate_in_some_optimal_solutions` | Judged a duplicate only in part of the optimal solutions (some optimal solution leaves the row unpaired); replaying it could double-count, so it is shelved. |
| `no_duplicate_utc_undetermined` | Never a duplicate, but the optimal offsets span more than one integer, so its corrected UTC is not fixed. |
| `included_no_duplicate_utc_fixed` | Unpaired in every optimal solution and the offset is fixed; the row is added with its corrected UTC and original measurements. |

## Reviewing what was shelved

The reports are plain JSON, so standard tooling answers the common
questions:

```sh
# Rows that entered the replay and why:
jq '.reconciliation.backup_rows[] | select(.decision == "included")' run/replay.json

# Shelved rows with the exact reason:
jq '.reconciliation.backup_rows[] | select(.decision == "shelved")
    | {id, reason_code, reason, possible_primary_ids,
       possible_corrected_utc_ms}' run/replay.json

# Offsets (merged closed intervals) that still explain the most duplicates:
jq '.reconciliation.optimal_offset_intervals' run/replay.json
```

The CLI's stdout summary prints the merged observation count, the global
maximum duplicate-pair count, and the included backup ids. Reordering rows
or anchors in the input never changes the result.
