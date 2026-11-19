# data-qd-172 | Wildfire sensor alert aggregation

Turns geolocated sensor observations into time-bounded incident clusters, with contributing sensors and confidence counts. Python 3.11+; standard library only.

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
.venv/bin/fire-cluster --input fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

Input is synthetic. Clustering uses a configured distance and time window; it is a triage aid, not a verified fire perimeter. MIT licensed.

Fixture summary: 3 observations form 2 candidate incidents.
