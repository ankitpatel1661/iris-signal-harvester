# `docs/` — evidence and sample output

| Path | What it is |
|---|---|
| `samples/` | The deliverable the brief asks for: a staging extract, the quality report from a clean run, the same run repeated (0 inserted, 0 updated), and a deliberately degraded run that **fails** with a ten-code rejection histogram. The degraded one is the evidence that errors are visible and invalid coordinates are not silently accepted. |
| `evidence/` | Dated, redacted capture of the live OParl endpoint, kept because the documentation states facts about it and a stated fact should be checkable. |

The sample corpus is **generated**, not real municipal data, and is labelled as such wherever it
appears. The probed endpoint declares no licence, so its records are not redistributed here:
`evidence/` holds aggregate counts and record shapes only.
