# Org Identifier Sweep — moved

The identifier sweep that once lived here — a machine-readable declaration of
one organisation's repos and the canonical org key each reference resolves
through — has moved out of this template. It carried one organisation's
repository register inside an Apache-2.0 template an adopter has no use for.

The sweep now lives in the calling organisation's own repository. The engine's
canonical-org-key behaviour it exercised is unchanged and remains covered by
`tests/test_canonical_org_key.py` and `tests/test_canonical_key_real_payload.py`,
which derive the key from the webhook payload rather than from any
organisation's register. See `CONTRACT.md`, "Layover pin drift (ADP-015)" for
the register that left alongside this sweep.

This file is retained only as a pointer, so a reader who follows an old
reference lands on the explanation rather than on a missing page.
