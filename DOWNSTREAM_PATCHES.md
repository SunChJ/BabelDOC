# Downstream patch ledger

Each patch must identify why it belongs downstream, whether it can be proposed
upstream, and the condition under which it can be removed.

| ID | Status | Purpose | Upstream tracking | Removal condition |
| --- | --- | --- | --- | --- |
| `gloss-0001` | Active | Establish downstream identity, provenance, safe automation, and maintenance policy. | Not applicable: repository governance. | The Gloss downstream is retired. |
| `gloss-0002` | Active | Add a lightweight, versioned runtime capability handshake for Gloss. | Downstream integration boundary. | Upstream exposes an equivalent stable runtime protocol. |
| `gloss-0003` | Active | Add reproducible dependency locking and Linux/macOS package validation. | Candidate upstream CI improvement. | Upstream adopts equivalent locked cross-platform release checks. |

Runtime, performance, and compatibility patches will be added in separate,
focused commits and PRs.
