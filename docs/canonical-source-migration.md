# Canonical Source Migration

Every layover resolves `ops-engine` from its Forgejo canonical, not from the
GitHub mirror. This document names the canonical, records why the mirror must
not be the install source, states how reachability is proven per consuming
environment, and names the exception per environment where one genuinely
cannot reach the canonical.

## The rule

The canonical home of `ops-engine` is Forgejo at

```
git.langevc.com/langevc/ops-engine
```

GitHub (`github.com/LangeVC/ops-engine`) is a read-only distribution mirror.
A layover's dependency line consumes the canonical, not the mirror:

```
ops-engine @ git+https://git.langevc.com/langevc/ops-engine.git@v3.0.0
```

## Why the mirror must not be the install source

Consuming from the mirror inverts the Forgejo-first rule that the consuming
repositories' own `AGENTS.md` mandates. Two concrete failure modes make it a
correctness issue, not a preference:

1. **A mirror outage becomes a build outage.** The langevc mirror was dead for
   eleven days (LVC-225) with no operator signal. A layover pinned to the
   mirror cannot build during that window even though canonical is healthy.
2. **A canonical fix does not reach a consumer until the mirror runs.** The
   mirror is pushed by a workflow on the Forgejo side (see
   `.forgejo/workflows/mirror.yml`); a layover that resolves from GitHub is
   one mirror lag behind every canonical change, with no way to distinguish
   "fixed upstream" from "mirror has not run".

## The pin is the same commit on both forges

The pinned tag `v3.0.0` resolves to the same commit on canonical and mirror:

| forge | ref | resolved commit |
|---|---|---|
| Forgejo canonical | `refs/tags/v3.0.0` (annotated `eda1b1f`) | `6c73e7711a92eb824aedfd71c07377888f33fce3` |
| GitHub mirror | `refs/tags/v3.0.0` | `6c73e7711a92eb824aedfd71c07377888f33fce3` |

So moving the pin to the canonical URL changes the *source of truth*, not the
content. The migration is a repoint, not a re-resolution.

## Reachability, proven not assumed

Reachability is proven per consuming environment, never carried from one
environment to another. Two environments consume `ops-engine` per org layover:

1. **The runner** — where CI checks out and tests the layover. It reaches
   canonical over HTTPS (`git ls-remote`) and SSH
   (`git@git.langevc.com:langevc/ops-engine.git`).
2. **The deploy host** — where the layover's `Dockerfile` runs
   `pip install .` and resolves `ops-engine` from its `pyproject.toml`
   dependency line. It reaches canonical over anonymous HTTPS (a full
   `git clone --branch v3.0.0 https://git.langevc.com/langevc/ops-engine.git`
   succeeds without a credential; the repository is public).

The five consuming environments are `lvc-ops`, `capacium-ops`,
`elementeer-ops`, `fusionaize-ops`, and `skillweave-ops`. Each is a separate
repository under its own org key on the canonical host
(`langevc/lvc-ops`, `capacium/capacium-ops`, `elementeer/elementeer-ops`,
`fusionaize/fusionaize-ops`, `skillweave/skillweave-ops`), each with its own
`pyproject.toml` pin and its own runner and deploy host.

## Exceptions — environments that cannot reach canonical

None. Every consuming environment reaches the canonical host, so the rule
"every layover consumes `ops-engine` from canonical" has no exception today.
Each environment is named here with its reachability, so an exception cannot
hide as a silent omission:

| environment | reaches canonical? | reason |
|---|---|---|
| runner — each of the five layovers | yes | `git.langevc.com` is a public host (Cloudflare-proxied, no credential); `git ls-remote` over HTTPS and over SSH both resolve the pinned tag (test check B) |
| deploy host — each of the five layovers | yes | the same public host; an anonymous `git clone --branch v3.0.0` completes, which is the exact path the layover `Dockerfile` uses to resolve the dependency (test check B) |

There is no exception because the canonical repository is public, not a
private boundary. The anonymous clone in `tests/test_canonical_source.sh`
proves no credential is required, so no consumer is barred from the canonical
for lack of access. If a consuming environment is ever moved behind a boundary
that cannot reach `git.langevc.com`, its exception is named here per
environment with its reason — and that environment, not the others, stays on
the mirror.

## Machine-readable declaration

```json
{
  "schema": 1,
  "package": "ops_engine",
  "canonical": {
    "host": "git.langevc.com",
    "path": "langevc/ops-engine",
    "url": "https://git.langevc.com/langevc/ops-engine.git",
    "ssh_url": "git@git.langevc.com:langevc/ops-engine.git"
  },
  "mirror": {
    "url": "https://github.com/LangeVC/ops-engine.git",
    "role": "read-only distribution mirror"
  },
  "pinned_tag": "v3.0.0",
  "pinned_commit": "6c73e7711a92eb824aedfd71c07377888f33fce3",
  "consuming_environments": [
    "langevc/lvc-ops",
    "capacium/capacium-ops",
    "elementeer/elementeer-ops",
    "fusionaize/fusionaize-ops",
    "skillweave/skillweave-ops"
  ]
}
```

## Where the layover pins live

This lane works in `langevc/ops-engine`; the pins themselves live in each
layover's own `pyproject.toml`, which is outside this repository. The
canonical-source requirement is stated here and enforced by
`tests/test_canonical_source.sh`, which proves the canonical host is reachable
from the runner environment against the pinned tag and that the pinned commit
agrees with the mirror. The layover-side repoint is the consuming repository's
change; this documentation is the contract that change must satisfy.
