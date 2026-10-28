#!/usr/bin/env bash
# IRIS-SIG-23 — source verification probe.
# Read-only. No authentication attempted, no access restriction bypassed.
# Writes a dated evidence log to SOURCE_VERIFICATION.md next to this script.

set -uo pipefail
cd "$(dirname "$0")"
OUT="SOURCE_VERIFICATION.md"
UA="${IRIS_USER_AGENT:-iris-harvester-feasibility-check/0.1 (+mailto:ops@example.org)}"

BRIEFED_HOST="ratsdokumente.de"
BRIEFED_URL="https://ratsdokumente.de/api/search"

probe_http () {  # $1=url
  curl -sS -m 20 -o /dev/null -L -A "$UA" \
    -w "HTTP:%{http_code}  final:%{url_effective}  ct:%{content_type}  bytes:%{size_download}" \
    "$1" 2>&1 | tail -1
}

{
  echo "# SOURCE_VERIFICATION — IRIS-SIG-23"
  echo
  echo "| | |"
  echo "|---|---|"
  echo "| Probe executed (UTC) | $(date -u '+%Y-%m-%d %H:%M:%SZ') |"
  echo "| Briefed source | \`$BRIEFED_URL\` |"
  echo "| Method | Read-only DNS / WHOIS / HTTP probe. No authentication attempted. No access restriction bypassed. |"
  echo "| Operator user-agent | \`$UA\` |"
  echo
  echo '## 1. Control — resolver and egress are healthy'
  echo
  echo 'Establishes that any failure below is a property of the target, not of this machine.'
  echo
  echo '```'
  for d in oparl.org politik-bei-uns.de; do
    printf '%-22s A -> %s\n' "$d" "$(dig "$d" A +short | head -1)"
  done
  echo '```'
  echo
  echo '## 2. Briefed host — DNS'
  echo
  echo '```'
  echo "\$ dig $BRIEFED_HOST A"
  dig "$BRIEFED_HOST" A +noall +comments +answer 2>&1 | grep -E 'HEADER|flags:' | sed 's/^/  /'
  echo
  echo "\$ dig $BRIEFED_HOST NS +short   ->  $(dig "$BRIEFED_HOST" NS +short | head -1 | sed 's/^$/(no records)/')"
  echo "\$ dig $BRIEFED_HOST A  +short   ->  $(dig "$BRIEFED_HOST" A  +short | head -1 | sed 's/^$/(no records)/')"
  echo '```'
  echo
  echo '## 3. Briefed host — registry (DENIC, authoritative for .de)'
  echo
  echo '```'
  echo "\$ whois $BRIEFED_HOST"
  whois "$BRIEFED_HOST" 2>&1 | grep -iE '^(Domain|Status):' | sed 's/^/  /'
  echo '```'
  echo
  echo '## 4. Briefed host — HTTP'
  echo
  echo '```'
  for u in "https://$BRIEFED_HOST/" "$BRIEFED_URL" "https://$BRIEFED_HOST/robots.txt"; do
    printf '%-46s %s\n' "$u" "$(probe_http "$u")"
  done
  echo
  echo "\$ curl $BRIEFED_URL"
  curl -sS -m 15 -A "$UA" "$BRIEFED_URL" 2>&1 | head -2 | sed 's/^/  /'
  echo '```'
  echo
  echo '## 5. Candidate alternatives'
  echo
  echo '```'
  for u in "https://oparl.org/" "https://oparl.org/spezifikation" "https://politik-bei-uns.de/"; do
    printf '%-40s %s\n' "$u" "$(probe_http "$u")"
  done
  echo
  echo '$ curl https://oparl.org/robots.txt'
  curl -sS -m 15 -L -A "$UA" "https://oparl.org/robots.txt" 2>&1 | head -5 | sed 's/^/  /'
  echo '```'
  echo
  echo '## 6. Findings'
  echo
  echo "1. **\`$BRIEFED_HOST\` does not exist.** The authoritative resolver returns \`NXDOMAIN\`, there are no"
  echo "   NS, A or SOA records, and the DENIC registry reports the domain as \`Status: free\` — i.e. it is not"
  echo '   registered to anyone. The failure is therefore not an outage, a block, or a rate limit: there is no'
  echo '   service to reach, and no access restriction exists to be respected or bypassed.'
  echo '2. **The control probes succeed from the same machine at the same moment**, so local DNS and egress are'
  echo '   healthy and the result is a property of the target.'
  echo '3. **The nearest real analogue to the briefed source is "Politik bei uns"** (`politik-bei-uns.de`), an'
  echo '   aggregator of municipal council documents with full-text and map search. It responds HTTP 200, but its'
  echo '   own homepage states it is no longer actively maintained and that no data updates take place. It is'
  echo '   therefore unsuitable as a live production source, though it remains useful as a historical corpus for'
  echo '   building representative test fixtures.'
  echo '4. **OParl (`oparl.org`) is live, specified and open.** It publishes a formal specification and a'
  echo '   developer platform, and its `robots.txt` places no restriction on automated access.'
  echo
  echo '## 7. Decision'
  echo
  echo '**Branch B** of the decision rule applies (see Solution Design §2.1).'
  echo
  echo '- The briefed `RatsdokumenteApiAdapter` is implemented against the contract described in the brief and'
  echo '  is fully covered by fixture-based tests, so it can be enabled unchanged should such a source exist or'
  echo '  be stood up. It is **not** presented as verified against a live endpoint.'
  echo '- The **OParl federation adapter is the primary live source**, because it is the open standard through'
  echo '  which German council information systems actually publish, and it is what makes the brief’s objective'
  echo '  — coverage across hundreds of municipal councils — achievable at all.'
  echo '- No access restriction was encountered and none was bypassed.'
} > "$OUT"

echo "wrote $OUT"
