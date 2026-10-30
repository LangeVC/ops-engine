# Mirror cadence

The mirror cadence is stated **once**, here. It answers two questions that
have so far been answered only by habit:

- **what** is mirrored, and **when** a sync is considered late;
- **who** decides a departure, and where that decision is recorded.

## The rule

A mirrored repository follows the default cadence unless it carries a named
exception:

> A mirrored repository is re-synced on every push to its default branch, and
> the mirror must not lag the primary by more than the declared drift window.
> The default drift window is **300 seconds** (`max_drift_seconds`).

The cadence is not a schedule in the cron sense — mirroring is push-driven,
not timer-driven. The cadence is the *contract the mirror must keep after a
push*: same branch, same HEAD within the drift window.

## The split

The same split the FFR run established for release naming and org identity
applies here:

| Concern | Owner |
|---|---|
| **Mechanics** — how sync is verified and how a deviation is detected | `ops-engine` (`MirrorHandler`, `MirrorConfig.max_drift_seconds`, `scripts/mirror-cadence-check.py`) |
| **Policy** — what each layover mirrors, its drift window, and any departure | the layover (per-org `config.yml` and the declaration below) |

The mechanics do not decide *whether* a deviation is acceptable. They only
report it. The layover decides, and a decision that is not written down is not
a decision — it is a habit, and a habit breaks silently.

## Named exceptions

A mirrored repository either follows the default cadence or carries a named
exception. An exception has three mandatory parts:

1. **`reason`** — *why* the cadence is different (not just that it is);
2. **`decided_by`** — who or what recorded the decision (an operator, a ticket,
   a PR);
3. **`value`** — the actual cadence or drift window it runs under.

`scripts/mirror-cadence-check.py` reports every mirrored repository whose
drift window deviates from the default and is **not** covered by a named
exception. It is a check, not a gate: it states the deviation so a human can
either correct the value or record the exception.

## Machine-readable declaration

The single source of truth for the policy half is the JSON block below. Each
entry is a mirrored repository; `drift_seconds` is its declared drift window,
and `exception` names any departure from the default (absent = follows the
default). The check validates this block against the rule.

```json
{
  "schema": 1,
  "default_drift_seconds": 300,
  "mirrors": [
    {
      "org": "langevc",
      "repo": "ops-engine",
      "drift_seconds": 300
    }
  ]
}
```

This repository (`langevc/ops-engine`) is itself mirrored to GitHub and
follows the default cadence with no exception; it is the single declared entry
so the check has a canonical, verifiable instance in-tree. A layover that
mirrors further repositories extends its own `mirrors` list; a departure
without an `exception` entry is reported by the check.
