# Layover Repin Log

Records each repin of the layover version pins against `ops-engine`
releases, with the drift-check outcome for the set.

## 2026-08-23 — repin to 2.2.0

Five layovers repinned from their prior pins to the current engine release
`2.2.0` (`pyproject.toml` `version`):

| layover         | prior pin | new pin |
| --------------- | --------- | ------- |
| lvc-ops         | 2.0.0     | 2.2.0   |
| capacium-ops    | 2.1.2     | 2.2.0   |
| elementeer-ops  | 2.0.0     | 2.2.0   |
| fusionaize-ops  | 2.0.0     | 2.2.0   |
| skillweave-ops  | 2.0.0     | 2.2.0   |

Drift check (`scripts/pin-drift-check.py`) is green for all five — the
`changed-consumed-names` column is `-` for every layover.

The `capacium-ops` `[postgres]` extra is preserved as intended: it is the
migration-runner target (`MigrationRunner`, `MigrationTargetConfig`,
`ApplyResult`, `runner_from_config`) and is not drift. Its `extra` field in
the consumption declaration retains the value `postgres`.

Verified by `tests/test_pins_current.sh`.

## 2026-08-23 — running-service verification (criterion 3)

The pins are claims in a file; the running bot is what actually serves webhooks.
`tests/test_pins_current.sh` now verifies against the running service, not the
file: it reaches each bot's public `https://ops.<org>/health` endpoint and
requires a `200` whose body reports `ok`. Measured live on 2026-08-23:

| layover         | running endpoint        | status |
| --------------- | ----------------------- | ------ |
| lvc-ops         | https://ops.langevc.com | 200 ok |
| capacium-ops    | https://ops.capacium.xyz| 200 ok |
| elementeer-ops  | https://ops.elementeer.xyz | 200 ok |
| fusionaize-ops  | https://ops.fusionaize.com | 200 ok |
| skillweave-ops  | https://ops.skillweave.xyz | 200 ok |

Two of the five hosts (`ops.elementeer.xyz`, `ops.skillweave.xyz`) answer on
Cloudflare origin certs (self-signed); the test probes them with certificate
verification relaxed for that origin only, without weakening the status/body
assertion.

The running service does **not** expose the installed `ops-exec` (ops-engine)
version over HTTP: `/health` reports `status` and `queue_size` only,
`/openapi.json` reports the FastAPI app's own `info.version` (`0.1.0`), and
`/version` is `404`. Verifying the installed-version — the second, stronger
half of "the deployed bot runs the pinned version" — therefore requires
reading `importlib.metadata.version("ops_engine")` inside the running
container, reachable only through the deploy host. That container-side probe is
out of reach from the runner that executes this test; the liveness of the
running service is what is provable here and what is recorded.

Skipping the live probe (air-gapped test runs) is explicit:
`PINS_CURRENT_SKIP_LIVE=1`.
