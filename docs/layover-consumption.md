# Layover Consumption Declaration — moved

The layover register that once lived here — a JSON block naming one
organisation's layovers, their `ops-engine` pins, and the contract names each
consumes — has moved out of this template. It was an organisation's data
wearing a document's clothes, asserted by the engine's own tests and therefore
load-bearing inside an Apache-2.0 template an adopter has no use for.

The register now lives in the calling organisation's own repository, and
`scripts/pin-drift-check.py` reads it from a path supplied on the command line
(`--layovers PATH`). With no register supplied, the check reports it has nothing
to check and exits zero; a register whose declared pins disagree with the
engine version fails and names both. See `CONTRACT.md`, "Layover pin drift
(ADP-015)".

This file is retained only as a pointer, so a reader who follows an old
reference lands on the explanation rather than on a missing page.
