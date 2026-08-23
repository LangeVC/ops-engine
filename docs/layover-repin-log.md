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
