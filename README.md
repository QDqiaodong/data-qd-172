# data-qd-172 | Wildfire sensor alert aggregation

Turns geolocated sensor observations into time-bounded incident clusters, with contributing sensors and confidence counts. Python 3.11+; standard library only.

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
.venv/bin/fire-cluster --input fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

## Publishing candidate events for offline dashboards

Beyond one-shot reports, `fire-cluster` keeps a local publishing archive so
offline dashboards can independently catch up on event changes. The archive is
a single local JSON file; no network service is involved. Clustering and
maintenance-window rules are reused unchanged.

```sh
# Register one or more dashboard consumer ids.
fire-cluster archive-init --archive run/archive.json --consumer X --consumer Y

# Publish a batch; the receipt lists the publish/withdraw records it created.
fire-cluster publish --archive run/archive.json --input batch.json --batch-id b1

# Each consumer reads its own unacknowledged records, in record order.
fire-cluster pending --archive run/archive.json --consumer X

# Acknowledgement binds one consumer to one exact record id.
fire-cluster ack --archive run/archive.json --consumer X --record r1
```

Each successful batch replaces the current incident state: new incidents
publish a new immutable version, changed incidents withdraw the old version
before publishing a new one, disappeared incidents are withdrawn only, and
unchanged incidents produce no records. Version ids (`v1`, `v2`, ...) and
record ids (`r1`, `r2`, ...) come from monotonic counters and are never
reused. Replaying the same `batch_id` with identical input returns the
original receipt without allocating new identities; the same `batch_id` with
different input is rejected, and invalid clustering input leaves the archive
untouched. See `docs/publishing.md` for the full archive contract.

Input is synthetic. Clustering uses a configured distance and time window; it is a triage aid, not a verified fire perimeter. MIT licensed.

Fixture summary: 3 observations form 2 candidate incidents.
