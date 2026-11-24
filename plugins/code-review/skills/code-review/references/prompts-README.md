# Worker templates

`review-roles.py` renders the templates against the pinned review plan. Worker instructions point to generated reading packs, the reviewed commit and base, and any applicable repository rules. The orchestrator records the host worker ID separately from the worker's response.

| Template | Inputs | Result |
| --- | --- | --- |
| `reviewer.md` | One pack and its exact file list | Candidates, `COVERED`, `CHECKED`, and `DEEP` |
| `second-reader.md` | A high-risk pack and a complementary lens | Candidates and unresolved questions |
| `workflow.md` | Index of changed cross-file paths | Candidates and coverage evidence by check |
| `history.md` | Pinned checkout and selected paths | Candidate regressions supported by history |
| `prior-discussion.md` | Current pull request metadata | Candidate issues supported by earlier discussion |
| `verifier.md` | Up to two candidates and their source locations | Independent verdict, confidence, severity and entry evidence |

Readers return one block per candidate with `file`, `line`, `category`, `summary`, and `evidence`. Behavior candidates cite the actual `entry`; rule findings also cite `rule`. A verifier returns one block per candidate with `candidate`, `score`, `verdict`, `severity`, `trigger`, `entry`, and `evidence`. Its entry is one repository-relative `path:line` reference.

Use the run ledger to record results and distinguish confirmed issues from unresolved evidence. A worker who proposed a finding cannot independently verify it. Keep individual shell or agent results as review artifacts within the current run directory.
