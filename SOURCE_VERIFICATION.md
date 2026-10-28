# SOURCE_VERIFICATION — IRIS-SIG-23

| | |
|---|---|
| Probe executed (UTC) | 2026-10-27 09:28:25Z |
| Briefed source | `https://ratsdokumente.de/api/search` |
| Method | Read-only DNS / WHOIS / HTTP probe. No authentication attempted. No access restriction bypassed. |
| Operator user-agent | An operator User-Agent carrying a real contact address. The address is redacted in the committed artefacts (see [`docs/evidence/README.md`](docs/evidence/README.md#user-agent)). |

## 1. Control — resolver and egress are healthy

Establishes that any failure below is a property of the target, not of this machine.

```
oparl.org              A -> 157.90.134.52
politik-bei-uns.de     A -> 94.130.15.200
```

## 2. Briefed host — DNS

```
$ dig ratsdokumente.de A
  ;; ->>HEADER<<- opcode: QUERY, status: NXDOMAIN, id: 57404
  ;; flags: qr rd ra; QUERY: 1, ANSWER: 0, AUTHORITY: 1, ADDITIONAL: 1
  ; EDNS: version: 0, flags:; udp: 1232

$ dig ratsdokumente.de NS +short   ->  
$ dig ratsdokumente.de A  +short   ->  
```

## 3. Briefed host — registry (DENIC, authoritative for .de)

```
$ whois ratsdokumente.de
  domain:       DE
  status:       ACTIVE
  Domain: ratsdokumente.de
  Status: free
```

## 4. Briefed host — HTTP

```
https://ratsdokumente.de/                      HTTP:000  final:https://ratsdokumente.de/  ct:  bytes:0
https://ratsdokumente.de/api/search            HTTP:000  final:https://ratsdokumente.de/api/search  ct:  bytes:0
https://ratsdokumente.de/robots.txt            HTTP:000  final:https://ratsdokumente.de/robots.txt  ct:  bytes:0

$ curl https://ratsdokumente.de/api/search
  curl: (6) Could not resolve host: ratsdokumente.de
```

## 5. Candidate alternatives

```
https://oparl.org/                       HTTP:200  final:https://oparl.org/  ct:text/html; charset=UTF-8  bytes:44258
https://oparl.org/spezifikation          HTTP:200  final:https://oparl.org/spezifikation/  ct:text/html; charset=UTF-8  bytes:38629
https://politik-bei-uns.de/              HTTP:200  final:https://politik-bei-uns.de/  ct:text/html; charset=utf-8  bytes:10340

$ curl https://oparl.org/robots.txt
  User-agent: *
  Disallow:
  
  Sitemap: https://oparl.org/sitemap_index.xml
```

## 6. Findings

1. **`ratsdokumente.de` does not exist.** The authoritative resolver returns `NXDOMAIN`, there are no
   NS, A or SOA records, and the DENIC registry reports the domain as `Status: free` — i.e. it is not
   registered to anyone. The failure is therefore not an outage, a block, or a rate limit: there is no
   service to reach, and no access restriction exists to be respected or bypassed.
2. **The control probes succeed from the same machine at the same moment**, so local DNS and egress are
   healthy and the result is a property of the target.
3. **The nearest real analogue to the briefed source is "Politik bei uns"** (`politik-bei-uns.de`), an
   aggregator of municipal council documents with full-text and map search. It responds HTTP 200, but its
   own homepage states it is no longer actively maintained and that no data updates take place. It is
   therefore unsuitable as a live production source, though it remains useful as a historical corpus for
   building representative test fixtures.
4. **OParl (`oparl.org`) is live, specified and open.** It publishes a formal specification and a
   developer platform, and its `robots.txt` places no restriction on automated access.

## 7. Decision

**Branch B** applies. The rule, fixed before the probe was run: if the briefed endpoint is live and serves a parseable, paginated collection, it becomes the primary source; if it is obsolete, inaccessible or restricted, the finding is recorded verbatim, the briefed adapter is retained and fixture-tested, and a justified alternative becomes the primary live source. No access restriction is bypassed either way.

- The briefed `RatsdokumenteAdapter` (`src/iris_signal_harvester/adapters/ratsdokumente.py`) is implemented against the contract described in the brief and
  is fully covered by fixture-based tests, so it can be enabled unchanged should such a source exist or
  be stood up. It is **not** presented as verified against a live endpoint.
- The **OParl federation adapter is the primary live source**, because it is the open standard through
  which German council information systems actually publish, and it is what makes the brief’s objective
  — coverage across hundreds of municipal councils — achievable at all.
- No access restriction was encountered and none was bypassed.

## 8. Live OParl endpoint — capture of `muenchen-transparent.de`

The README and `config/bodies.yaml` assert facts about `https://www.muenchen-transparent.de/oparl/v1.0`, and
sections 1-7 above contain no capture of that host. A capture of **shape and counts only** (no publisher
records) lives in [`docs/evidence/`](docs/evidence/README.md), probed read-only on **2026-10-28**, one request
at a time, with an operator User-Agent whose contact address is redacted there (see
[`docs/evidence/README.md`](docs/evidence/README.md#user-agent)). Each file's `_capture` block records its own
time and spacing.

`robots.txt` was read (`muenchen-transparent_access_rules.json`): HTTP 200, directives `User-agent: *`,
`Disallow: /*/dav/` and a `Sitemap:` line. **That rule does not cover any path this project reads**: the
OParl paths requested (`/oparl/v1.0`, `/oparl/v1.0/list/body`, and each body's `.../list/paper`) contain no
`/dav/` segment. Nothing the project fetches is disallowed.

| Claim in README / `bodies.yaml` | What the capture shows on 2026-10-28 |
|---|---|
| OParl 1.0 | `System.oparlVersion = https://schema.oparl.org/1.0/`, `otherOparlVersions = []`. Confirmed. |
| 26 bodies | `list/body` returned 26 bodies on one page (`totalPages: 1`). Confirmed. |
| `Body.license` null | Corrected: the `license` key is **absent** on all 26 bodies (not served as `null`). The adapter reads both as "no licence". |
| Pagination is `links.next` | `links.next` is an absolute URL (`...?id=<n>`), `pagination` carries `elementsPerPage` and `totalPages`. Confirmed. |
| `modified_since` honoured | Observed on `body/1/list/paper`, first page (items on page 1 / `totalPages`): no filter 100 / 15; `2016-01-01` 100 / 9; `2020-01-01` 100 / 8; `2025-01-01` 100 / 3; `2026-01-01` 80 / 1; `2026-06-01` 45 / 1. The filter is honoured and narrows the result set. All six rows are in `muenchen-transparent_modified_since_probe.json`, which holds counts and pagination metadata, not the raw responses. A figure from the original 2026-10-27 probe was withdrawn: its raw response was not saved and it could not be reproduced. |
| `-0001-11-30T00:00:00+00:53` sentinel | Present in `Paper.modified` **and** `Paper.created` on 96 of the first 100 papers of `body/1`. Confirmed, and it is the norm there rather than a rare edge case, which makes the date-provenance ladder more load-bearing than first claimed. |
| `Paper.date` frequently null | Corrected: the `date` key is **absent** (not `null`) from all 100 papers on the captured page. The adapter treats absent and null alike. |
| `Paper.location` absent | No `location` key on any of the 100 papers. Confirmed. |

Two further observations: only 1 of 26 bodies declares an `ags` (the capture records the count, not which body),
so the other 25 can only ever reach geo tier 5; and body/0's advertised paper URL
(`https://www.muenchen-transparent.de/oparl/v1.0/list/paper`) returned **HTTP 404** when requested once
(`muenchen-transparent_access_rules.json`).

How this file is produced: sections 1-7 record the 2026-10-27 probe of the briefed host and were not
regenerated by the current CLI. `make verify` / `iris-harvest verify-source` does **not** regenerate this
committed file; by default it writes `reports/SOURCE_VERIFICATION.md`, which is git-ignored. This section 8 is
maintained by hand from `docs/evidence/` and neither tool generates it.
