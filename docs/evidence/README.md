# Live-probe evidence: `muenchen-transparent.de` (OParl 1.0)

This directory holds a small, redacted capture of the one live OParl endpoint the project relies on.
It exists so that the facts asserted in the README, `docs/IMPLEMENTATION_PLAN.md` and `config/bodies.yaml`
can be checked from the repository (review finding I-12). The conclusions are tabulated in
[`SOURCE_VERIFICATION.md`](../../SOURCE_VERIFICATION.md), section 8.

## What was captured, and when

Probed **2026-10-28, 08:56-08:58 UTC**, read-only. Seven `GET` requests, strictly one at a time and at least
two seconds apart, with the user-agent `iris-harvester-feasibility-check/0.1 (+mailto:ops@example.org)`.
`robots.txt` was fetched first (`Disallow: /*/dav/`; nothing under `/oparl/` is disallowed). No authentication
was attempted and no access restriction was met.

| File | Request | Contents |
|---|---|---|
| `muenchen-transparent_system.json` | `GET /oparl/v1.0` | The System object, complete except `contactEmail`. |
| `muenchen-transparent_body_list_page1.json` | `GET /oparl/v1.0/list/body` | All 26 bodies (the whole list is one page), each cut down to identity, name, `ags`/`rgs`, `license`, `paper` link and timestamps. |
| `muenchen-transparent_paper_page_body1.json` | `GET /oparl/v1.0/body/1/list/paper` | 20 of the 100 papers on the first page, cut down and with titles truncated, plus statistics computed over all 100. |
| `muenchen-transparent_modified_since_probe.json` | the same URL with `?modified_since=` set to `2016-01-01` and `2025-01-01`, and one request to body 0's advertised paper link | Counts and pagination only; no paper content. |

Each file carries a `_capture` block (URL, status, server date, user-agent, redactions) and, where
relevant, a `_statistics_over_full_page` block so that the trimmed `data` array does not misrepresent
the whole page.

## What it substantiates

- OParl **1.0**, **26 bodies**; `links.next` pagination with `pagination.{elementsPerPage,totalPages}`.
- **`Body.license` is not declared**: the key is absent on all 26 bodies.
- Only **1 of 26** bodies carries an `ags`; the other 25 carry none.
- **`modified_since` is honoured and narrows the result set**, and the filter is carried forward in `links.next`.
  In the committed JSON: no filter 15 pages, `2016-01-01` 9 pages, `2025-01-01` 3 pages (100 items on each
  first page). See the dated table below for the fuller observation.
- The **`-0001-11-30T00:00:00+00:53` sentinel** is in `created` and `modified` on 96 of 100 papers.
- **`Paper.location` is an absent key** (0 of 100) and so is **`Paper.date`** (0 of 100). They are not `null`.
- `Body.license` is an absent key on all 26 bodies, not `null`.
- Only 1 of 26 bodies declares an `ags`, and that body's advertised paper URL (`.../oparl/v1.0/list/paper`)
  answered HTTP 404 on the one request made.

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
sentinel and field-presence counts. The counts are point-in-time — the endpoint's data changes between
probes — so the figure to rely on is not any single number but the invariant they evidence: `modified_since`
is honoured, and the result set narrows monotonically as the cut-off moves forward. A figure from an earlier,
unrecorded 2026-10-27 probe was withdrawn because it could not be reproduced, and is cited nowhere.

## Licence and redistribution

The endpoint declares **no licence** (`Body.license` absent on every body, and the System object has no licence
field). Absence of a licence is not permission. This material is therefore committed as **evidence of a protocol
observation, not as a dataset**: it is a small excerpt, truncated and redacted, kept to support the claims
above, and should not be copied out, extended or used as a corpus. The synthetic corpus in
`tests/fixtures/cassettes/corpus/` is modelled on this *structure* and contains none of this content.

Redactions: `contactEmail` replaced with `[redacted]`; contact, website and organisational link fields and
legislative-term arrays dropped from bodies; paper titles truncated to 48 characters; originator, directing
organisation, auxiliary-file and related-paper fields dropped.

## Regenerating

```
UA='iris-harvester-feasibility-check/0.1 (+mailto:you@example.org)'   # use your own contact
curl -A "$UA" https://www.muenchen-transparent.de/robots.txt
curl -A "$UA" https://www.muenchen-transparent.de/oparl/v1.0
curl -A "$UA" https://www.muenchen-transparent.de/oparl/v1.0/list/body
curl -A "$UA" https://www.muenchen-transparent.de/oparl/v1.0/body/1/list/paper
curl -A "$UA" 'https://www.muenchen-transparent.de/oparl/v1.0/body/1/list/paper?modified_since=2016-01-01T00:00:00%2B00:00'
# the further cut-offs of the dated table (2020-01-01, 2025-01-01, 2026-01-01, 2026-06-01) follow the same pattern:
curl -A "$UA" 'https://www.muenchen-transparent.de/oparl/v1.0/body/1/list/paper?modified_since=2026-06-01T00:00:00%2B00:00'
```

Wait at least two seconds between requests. The live data will have moved on: counts and `totalPages` will differ.
