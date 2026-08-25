# The Monday refresh

Every Monday an agent re-checks the primary sources, updates `data/data.json`, and opens a
pull request. **Nothing goes live until a human merges it.** A wrong date is the one failure
mode this tracker cannot absorb, so the loop is deliberately not fully autonomous.

## What it does

1. Reads the current `data/data.json` from `main`.
2. Checks every source in the table below for anything published since `meta.last_updated`.
3. Applies the edits described under "What to change".
4. Re-verifies anything whose `last_verified` is more than 45 days old.
5. Validates the JSON, opens a PR with a plain-English summary of what moved and why.

## Sources to check, primary only

| Framework | Where |
|---|---|
| GHG Protocol | ghgprotocol.org standard-development pages, blog, the consolidated Standard Development Plan |
| ISO | iso.org project pages for the 14060 family, ISO/TC 207/SC 7 activity |
| SBTi | sciencebasedtargets.org news, blog, standards pages; files.sciencebasedtargets.org for the criteria PDFs |
| ISSB | ifrs.org news, ISSB meeting updates, work-plan project pages, jurisdiction profiles |
| EU | EUR-Lex and the Official Journal, EFRAG, finance.ec.europa.eu, taxation-customs.ec.europa.eu, Council, Parliament |
| US | sec.gov, federalregister.gov, ww2.arb.ca.gov rulemaking and board pages, supremecourt.gov |
| UK | gov.uk, legislation.gov.uk, fca.org.uk, frc.org.uk |
| Other jurisdictions | the named regulator only: frascanada.ca, aasb.gov.au, asic.gov.au, acra.gov.sg, sgx.com, hkex.com.hk, fsa.go.jp, ssb-j.jp, uaecma.gov.ae, qfma.org.qa |

**Never** cite another tracker, a consultancy note, a law-firm alert or a news article as
the source of record. Search may be used to discover that something happened. The entry
must then be verified against, and cite, the official page.

## What to change

**`meta.last_updated`** - the refresh date. Drives the header stamp.

**`updates[]`** - prepend anything new, newest first. Every field is required:
`date`, `framework`, `workstream`, `headline`, `detail` (2 to 4 self-contained sentences;
each renders as its own bullet), `so_what` (one imperative sentence for a reporting team),
`significance`, `source_url`, `source_title`, `source_publisher`, `last_verified`.

**`frameworks[].workstreams[]`** - when a workstream moves, update `phase`, `status_label`,
`summary`, `key_points` (the first three are always visible, so lead with the
decision-relevant ones), `next_milestone`, `stat`, and re-assess `confidence`, both the
level and the reasoning. Always bump `last_verified`.

**`timeline_milestones[]`** - move `projected` to `upcoming` to `completed` as things land.
Adjust dates only when an official source says so. Dates must be `YYYY-MM-DD` or `YYYY-MM`.

**`changes[]`** - flip `status` from `proposed` to `final` on adoption; add a row when a new
official proposal lands.

**`matrix[]`** - `x` is derived from confidence and phase by the published rule in
`matrix_x_rule`; recompute it rather than setting it by hand. `y` is the editorial effort
estimate and only changes when the scope of work genuinely changes.

**`jurisdictions[]` and `scope_tests[]`** - only on a real legislative or regulatory change.

## Confidence rules

- **confirmed** - final published text or a formally adopted decision.
- **high** - official draft, adopted-but-not-in-force act, or formally announced plan, with a procedural step remaining.
- **medium** - outcome genuinely open: live consultation, contested vote, pending litigation.
- **low** - early signal only: scoping work or political intent.

Never rate above **high** while a document says "subject to change". Never above **medium**
while a court ruling could flip the outcome. Where official sources conflict, take the more
recent, use the more conservative formulation, and say so in the reasoning.

## The rule that matters most

**If no official source publishes a date, there is no date.** Do not infer one from past
practice, do not carry one over from a superseded plan, and do not present an expectation as
a deadline. Write the milestone without a date and say "no date published". The August 2026
audit removed 22 dates for exactly this reason.

## Validate before opening the PR

```bash
python3 -m json.tool data/data.json > /dev/null && echo OK
python3 -m http.server 8000   # then click through every view
```

Check that `meta.last_updated` changed, that no `date` field is anything other than
`YYYY-MM-DD` or `YYYY-MM`, and that every new entry has a `source_url` on an official
domain from the table above.

## Editorial rules

- Write for a smart reader who does not know the acronyms. New acronym? Add it to
  `acronyms` (full name) and `glossary` (one plain sentence) rather than explaining it inline.
- Vendor-neutral. No EcoLedger product references anywhere in the dataset.
- Distinguish published plans ("Q2 2027, per the development plan") from statutory dates
  ("effective 1 January 2027").
- A quiet week is a real result. An empty "past week" bucket is more trustworthy than filler.
- No em dashes.
