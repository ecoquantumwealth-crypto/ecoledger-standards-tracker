# EcoLedger Standards Tracker

A free, vendor-neutral tracker of every major change to corporate emissions accounting and
disclosure standards, scoped to the reader's own business.

Live at **https://tracker.theecoledger.com**

## What's here

```
index.html        the whole tracker: one file, no build step, no dependencies
data/data.json    all the content, and the only file the weekly refresh touches
netlify.toml      publish settings and cache headers
```

The page is a renderer. It fetches `data/data.json` at load and draws everything from it,
so a content update never requires a code change or a redeploy of the page itself.

## Coverage

Seven framework families (GHG Protocol, ISO, SBTi, ISSB/IFRS, EU, US, UK) plus seven more
jurisdictions (Canada, Australia, Singapore, Hong Kong, Japan, UAE, Qatar). 167 dated
updates, 89 old-versus-new change rows, 182 timeline milestones, 11 scope tests.

## Sourcing rule

**Every entry is verified against, and links to, the standard-setter's or regulator's own
publication.** No third-party trackers, consultancy notes or news reports are used as a
source of record. Search may be used to discover that something happened; the entry must
then rest on the official page.

Every workstream carries a confidence rating with its reasoning visible, and a
`last_verified` date. Anything unchecked for more than 45 days labels itself stale on the
page rather than passing silently.

188 load-bearing claims were independently re-checked on 2026-08-25: 163 confirmed,
3 corrected, 22 dates removed because no official source publishes them.

## Run it locally

```bash
python3 -m http.server 8000
# then open http://localhost:8000
```

Opening `index.html` straight off disk will not work: browsers block `fetch` on `file://`.

## Deploy

Netlify, connected to this repo. Build command empty, publish directory `/`.
Every push to `main` redeploys. Because the weekly refresh only ever edits
`data/data.json`, a content update is a one-file commit.

DNS lives in Cloudflare: `CNAME tracker -> <site>.netlify.app`, set to **DNS only**
(grey cloud). Proxying breaks Netlify's certificate provisioning.

## Weekly refresh

See [UPDATE.md](UPDATE.md). An agent re-checks the primary sources every Monday and opens
a pull request. Nothing goes live until a human merges it.

## Disclaimer

Indicative guide, not legal, accounting or assurance advice. Confirm obligations against
the primary sources linked on every entry before relying on them.
