# Live-probe evidence: `muenchen-transparent.de` (OParl 1.0)

This directory holds a small capture of the one live OParl endpoint the project relies on. It holds
**shape and counts only, no publisher records**: key names with value types, aggregate counts, pagination
metadata, status codes, and the System object. It exists so that the facts asserted in the README,
`SOURCE_VERIFICATION.md` and `config/bodies.yaml` can be checked from the repository. The conclusions are
tabulated in [`SOURCE_VERIFICATION.md`](../../SOURCE_VERIFICATION.md), section 8.

## What was captured, and when

Probed on **2026-10-28**, read-only `GET` requests, strictly one at a time. No authentication was attempted
and no access restriction was met. Each file's `_capture` block records its own time, status code and
request spacing; the files were not all captured in one session, so no single window or request total is
stated here.

### User-Agent

Every probe was made with an operator User-Agent that carried a real contact address, as robots etiquette
expects. That address has since been redacted in the committed artefacts. Where a file's `probed_by` field
shows `iris-harvester-feasibility-check/0.1 (+mailto:[redacted])`, the `[redacted]` stands for the address
that was sent. `ops@example.org` is only the placeholder default of `IRIS_USER_AGENT` in this project's
configuration; it is not a record of what was sent.

| File | Request | Contents |
|---|---|---|
| `muenchen-transparent_system.json` | `GET /oparl/v1.0` | The System object, complete except `contactEmail`. |
| `muenchen-transparent_body_list_page1.json` | `GET /oparl/v1.0/list/body` | Counts over all 26 bodies (the whole list is one page), pagination, and the key names and value types of a body. No body names, ids or AGS values. |
| `muenchen-transparent_paper_page_body1.json` | `GET /oparl/v1.0/body/1/list/paper` | Statistics computed over the 100 papers on the first page (key presence, sentinel counts, `paperType` counts), pagination, and the key names and value types of a paper. No paper titles, ids or references. |
| `muenchen-transparent_modified_since_probe.json` | the same URL, unfiltered and with `?modified_since=` set to `2016-01-01`, `2020-01-01`, `2025-01-01`, `2026-01-01` and `2026-06-01` | Items on the first page, `elementsPerPage`, `totalPages` and whether `links.next` was present, plus the unfiltered page's sentinel and field-presence counts. No paper content. |
| `muenchen-transparent_access_rules.json` | `GET /robots.txt`, and body/0's advertised paper URL | The `robots.txt` status and directives, and the HTTP status of body/0's advertised paper URL. |

## What it substantiates

- OParl **1.0**, **26 bodies**; `links.next` pagination with `pagination.{elementsPerPage,totalPages}`.
- **`Body.license` is not declared**: the key is absent on all 26 bodies, not `null`.
- Only **1 of 26** bodies carries an `ags`; the other 25 carry none. (The capture does not record which body it is.)
- **`modified_since` is honoured and narrows the result set**: first-page `totalPages` falls as the cut-off
  moves forward (table below).
- The **`-0001-11-30T00:00:00+00:53` sentinel** is in `created` and `modified` on 96 of 100 papers.
- **`Paper.location` is an absent key** (0 of 100) and so is **`Paper.date`** (0 of 100). They are not `null`.
- **robots.txt** (`muenchen-transparent_access_rules.json`): HTTP 200, with exactly three directives:
  `User-agent: *`, `Disallow: /*/dav/` and a `Sitemap:` line. **`Disallow: /*/dav/` does not cover any path
  this project reads**: the OParl adapter requests the System URL (`/oparl/v1.0`), the body list
  (`/oparl/v1.0/list/body`) and each body's advertised paper list (for example
  `/oparl/v1.0/body/1/list/paper`, with `modified_since` or `id` query parameters), and none of those paths
  contains a `/dav/` segment. Nothing the project fetches is disallowed.
- **body/0's advertised paper URL** (`https://www.muenchen-transparent.de/oparl/v1.0/list/paper`) answered
  **HTTP 404** on the one request made (same file). A body can advertise a collection URL that does not
  resolve, and the harvester treats that as a per-body failure, not a run failure.

### The `modified_since` observation, dated 2026-10-28

`GET https://www.muenchen-transparent.de/oparl/v1.0/body/1/list/paper` with and without `modified_since`,
first page of each:

| `modified_since` | items on page 1 | `totalPages` |
|---|---|---|
| (none) | 100 | 15 |
| 2016-01-01 | 100 | 9 |
| 2020-01-01 | 100 | 8 |
| 2025-01-01 | 100 | 3 |
| 2026-01-01 | 80 | 1 |
| 2026-06-01 | 45 | 1 |

All six rows are in `muenchen-transparent_modified_since_probe.json`, together with the unfiltered page's
sentinel and field-presence counts. The counts are point-in-time (the endpoint's data changes between
probes), so the figure to rely on is not any single number but the invariant they evidence: `modified_since`
is honoured, and the result set narrows monotonically as the cut-off moves forward. A figure from an earlier,
unrecorded 2026-10-27 probe was withdrawn because it could not be reproduced, and is cited nowhere.

## Licence and redistribution

The endpoint declares **no licence** (`Body.license` absent on every body, and the System object has no licence
field). Absence of a licence is not permission. This material is therefore committed as **evidence of a protocol
observation, not as a dataset**: it holds shape and counts only, and should not be copied out, extended or
used as a corpus. The synthetic corpus in `tests/fixtures/cassettes/corpus/` is modelled on this *structure*
and contains none of this content.

Redactions: `contactEmail` replaced with `[redacted]`; the operator contact address in the User-Agent replaced
with `[redacted]`; no publisher record content (names, titles, ids, references, AGS values) is kept.

## Regenerating

```
UA='iris-harvester-feasibility-check/0.1 (+mailto:you@example.org)'   # use your own contact
curl -A "$UA" https://www.muenchen-transparent.de/robots.txt
curl -A "$UA" https://www.muenchen-transparent.de/oparl/v1.0
curl -A "$UA" https://www.muenchen-transparent.de/oparl/v1.0/list/body
curl -A "$UA" https://www.muenchen-transparent.de/oparl/v1.0/list/paper      # body/0's advertised paper URL: HTTP 404 when probed
curl -A "$UA" https://www.muenchen-transparent.de/oparl/v1.0/body/1/list/paper
curl -A "$UA" 'https://www.muenchen-transparent.de/oparl/v1.0/body/1/list/paper?modified_since=2016-01-01T00:00:00%2B00:00'
# the further cut-offs of the dated table (2020-01-01, 2025-01-01, 2026-01-01, 2026-06-01) follow the same pattern:
curl -A "$UA" 'https://www.muenchen-transparent.de/oparl/v1.0/body/1/list/paper?modified_since=2026-06-01T00:00:00%2B00:00'
```

Wait at least two seconds between requests. The live data will have moved on: counts and `totalPages` will differ.
