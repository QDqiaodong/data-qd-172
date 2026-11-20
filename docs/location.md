# Location-uncertainty audit contract

Clustering links observations by their *reported* coordinates. When a
sensor's coordinate accuracy is poor, a duty officer cannot tell whether
an incident would still hold together once every observation's
measurement error is accounted for, or whether it only exists because two
error margins happen to overlap. The `location-audit` command is a
one-shot offline audit that separates those cases. It reuses the existing
clustering and maintenance-window rules unchanged: incident membership,
ids, `confidence`, `mean_severity`, suppression results, the legacy
command behavior and the existing report file format are never modified.

## Inputs

`fire-cluster location-audit` reads one JSON document:

* `--input`: the same full observation JSON accepted by the legacy
  `fire-cluster --input` command (`radius_km`, `window_minutes`,
  `observations`, optional `maintenance_windows`), where each observation
  may additionally carry `location_uncertainty_m`.

`location_uncertainty_m` is a hard error radius in meters: the
observation's true position is guaranteed to lie within that distance of
its reported coordinates. The field is optional and defaults to 0. When
present it must be a finite, non-negative number; strings, `null`,
booleans, NaN, infinities and negative values are rejected and abort the
run with exit status 2 before any output is written. The field is
validated on every observation, including maintenance-suppressed ones.

Time-window eligibility still uses the original timestamps, and
maintenance windows still suppress observations by the existing
clustering rule. Suppressed observations take no part in the audit: they
appear only in the embedded `cluster_report.suppressed_observation_ids`
and cannot bridge any location link.

## Pairwise link classification

For every pair of unsuppressed observations whose timestamps differ by at
most `window_minutes` (the existing rule, boundary inclusive), let:

* `d` — the great-circle distance between the reported coordinates, using
  the same haversine as clustering (Earth radius 6371.0088 km); longitude
  wraps correctly across the date line, and planar latitude/longitude
  differences are never used;
* `u` — the sum of the two `location_uncertainty_m` values, in km;
* `radius_km` — the existing clustering radius.

Then:

* `d + u <= radius_km` — the link is **certain**: it holds under *every*
  combination of true positions allowed by the two error radii;
* `d <= radius_km + u` — the link is **possible**: at least one allowed
  combination of true positions brings the pair within the radius;
* otherwise — the link cannot exist under any allowed positions.

Both boundaries are inclusive: a pair sitting exactly on `d + u ==
radius_km` is certain, and a pair exactly on `d == radius_km + u` is
possible. Pairs outside the time window are never evaluated spatially.

## Components

The audit builds two graphs over the unsuppressed observations and
reports their connected components:

* **Certain components** (certain links only). Every certain link holds
  for all allowed true positions at once, so the observations of a
  certain component stay linked by certain paths no matter where the true
  positions fall inside their error radii. These are the stable cores of
  an event.
* **Possible components** (certain plus possible links). A possible
  component is the conservative outer envelope of every individually
  feasible link. Membership only means a connecting chain cannot be ruled
  out — it does **not** guarantee that all links of such a chain are
  simultaneously realizable by one choice of true positions, so a
  possible component is not a precise, realizable cluster and never
  replaces the incident report. What it does certify: two observations in
  *different* possible components have no allowed direct or indirect
  connection path at all.

Because certain links are a subset of the reported-coordinate incident
links, which are a subset of the possible links, every certain component
lies inside one incident and every incident lies inside one possible
component. An incident whose members split into several certain
components, or that shares a possible component with another incident, is
one whose connectivity depends on the error margins — exactly the
situation the duty officer needs to see. The original incidents remain
the event result; the components only annotate their robustness.

## Output fields

The output JSON (written with exclusive create, like the other commands,
with keys in stable sorted order) contains:

* `radius_km`, `window_minutes`: the effective clustering parameters the
  audit ran with;
* `cluster_report`: the complete unmodified `cluster()` output, including
  `suppressed_observation_ids`;
* `certain_components` / `possible_components`: lists of
  `{component_id, observation_ids}`; `component_id` is the smallest
  member observation id (the same convention as `incident_id`), member
  lists are sorted, and components are sorted by `component_id`;
* `observations`: one entry per unsuppressed observation (sorted by
  observation id) with `observation_id`, the effective
  `location_uncertainty_m`, its `incident_id` from the reported-coordinate
  cluster report, and its `certain_component_id` /
  `possible_component_id`.

Reordering the input observations or maintenance windows cannot change
any output: component ids, memberships and list orderings are all
derived from sorted observation ids.

## CLI

```sh
fire-cluster location-audit \
  --input observations.json \
  --output run/location-audit.json
```

The stdout summary prints `observations`, `incidents`,
`certain_components` and `possible_components`. Any validation failure
(invalid `location_uncertainty_m`, invalid coordinates, bad maintenance
window, ...) exits with status 2 and writes no output file. Running the
audit never changes what the legacy
`fire-cluster --input ... --output ...` command produces, with or without
uncertainty fields present.
