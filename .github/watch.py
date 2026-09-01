#!/usr/bin/env python3
"""Cheap change detection for the official pages the tracker watches.

The expensive part of the Monday refresh was never the searching, it was that a
week where nothing happened cost the same as a week where everything did: nine
model conversations re-reading nine sources from scratch to conclude "no change".

This does that part deterministically and for free. It fetches each official
news or updates page, reduces it to the set of links it offers, and compares
that to last week's set. A page with no new links did not publish anything, and
that is a fact rather than a judgement, so no model is needed to establish it.

What the model then gets is not "go and look" but "these four items are new
since last Monday, verify them". Shorter prompt, fewer searches, far fewer
tokens, and a better-aimed question.

Modes:
  --probe   fetch everything and report what is reachable. Writes nothing.
  --seed    write the current state as the baseline. Reports nothing new.
  (none)    print the diff as JSON on stdout.
"""
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "watch-state.json")

UA = ("Mozilla/5.0 (compatible; EcoLedgerStandardsTracker/1.0; "
      "+https://tracker.theecoledger.com)")
TIMEOUT = int(os.environ.get("WATCH_TIMEOUT", "30"))

# (family, frameworks covered, url). Several pages may back one family; the
# family counts as changed if any of its pages changed. Every host here is on
# refresh.py's official-domain allowlist, so nothing can enter the dataset from
# a source the gate would reject anyway.
# (family, frameworks covered, url). Several pages may back one family; the
# family counts as changed if any of its pages changed. Every host here is on
# refresh.py's official-domain allowlist, so nothing can enter the dataset from
# a source the gate would reject anyway.
#
# Some of these bodies sit behind a bot filter and return 403 to any plain
# fetch. Where they publish a feed we use that instead: a feed exists precisely
# to be read by machines, so it is the front door rather than a way round the
# back. Where neither works the family simply falls back to the model, which is
# the old behaviour and costs money but never loses coverage.
WATCH = [
    ("ghgp", ("ghgp",), "https://ghgprotocol.org/feed"),
    ("ghgp", ("ghgp",), "https://ghgprotocol.org/newsroom"),

    ("iso", ("iso",), "https://www.iso.org/feed/news.rss"),
    ("iso", ("iso",), "https://www.iso.org/news.html"),

    ("sbti", ("sbti",), "https://sciencebasedtargets.org/news"),
    ("sbti", ("sbti",), "https://sciencebasedtargets.org/resources"),

    ("issb", ("issb",), "https://www.ifrs.org/news-and-events/news/"),
    ("issb", ("issb",), "https://www.ifrs.org/projects/open-for-comment/"),

    # eur-lex's direct-access page is a search form and offers no content links,
    # so it told us nothing. EFRAG and the Commission cover the EU here, and the
    # model still checks the Official Journal when this family moves.
    ("eu", ("eu",), "https://www.efrag.org/en/news"),
    ("eu", ("eu",), "https://finance.ec.europa.eu/news_en"),

    ("us", ("us",), "https://www.sec.gov/news/pressreleases"),
    # CARB's /news and programme pages build their listings in JavaScript, so a
    # plain fetch sees zero links. These two render server-side.
    ("us", ("us",), "https://ww2.arb.ca.gov/news-releases"),
    ("us", ("us",), "https://ww2.arb.ca.gov/rulemaking"),

    ("uk", ("uk",), "https://www.fca.org.uk/news"),
    ("uk", ("uk",), "https://www.frc.org.uk/news-and-events/news/"),

    ("apac", ("au", "sg", "hk", "jp"), "https://aasb.gov.au/news/"),
    ("apac", ("au", "sg", "hk", "jp"), "https://asic.gov.au/newsroom/"),
    ("apac", ("au", "sg", "hk", "jp"), "https://www.acra.gov.sg/news-events"),
    ("apac", ("au", "sg", "hk", "jp"), "https://www.hkex.com.hk/News/News-Release"),
    ("apac", ("au", "sg", "hk", "jp"), "https://www.fsa.go.jp/en/news/index.html"),

    # FRAS Canada refuses automated fetches at the host level and QFMA's news
    # path has moved. Left in so the probe keeps testing them: if either opens
    # up, this family stops costing money. Until then it falls back to the model.
    ("other", ("ca", "ae", "qa"), "https://www.frascanada.ca/en/cssb/news-listing"),
    ("other", ("ca", "ae", "qa"), "https://www.qfma.org.qa/English/Pages/default.aspx"),
]

# Links every page has and no page means: nav, social, legal, utility.
NOISE = re.compile(
    r"(facebook|twitter|x\.com|linkedin|youtube|instagram|/login|/signin|/search"
    r"|/cookie|/privacy|/accessibility|/sitemap|/contact|/rss|\.css|\.js$"
    r"|/terms|mailto:|tel:|javascript:|/cart|/account)", re.I)

ANCHOR = re.compile(r"<a\b[^>]*?href=[\"']([^\"'>]+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
# RSS and Atom carry the same thing in a cleaner shape: one entry, one link,
# one title. No layout noise at all, which is why a feed is worth preferring.
RSS_ITEM = re.compile(r"<(?:item|entry)\b.*?</(?:item|entry)>", re.I | re.S)
RSS_LINK = re.compile(r"<link[^>]*?(?:href=[\"']([^\"']+)[\"'][^>]*/?>|>([^<]+)</link>)", re.I)
RSS_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
TAGS = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def clean(fragment):
    return WS.sub(" ", TAGS.sub(" ", fragment)).strip()


def fetch(url):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/html;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
        raw = r.read(3_000_000)
        enc = r.headers.get_content_charset() or "utf-8"
    return raw.decode(enc, "replace")


def items_of(url, html):
    """A page reduced to the content links it offers, as {fingerprint: item}.

    Links rather than raw text, because raw text catches every rotating banner
    and 'last reviewed' stamp and would report a change every single week.
    A body publishing something new adds a link; that is the signal.
    """
    host = urllib.parse.urlparse(url).netloc.lower()
    out = {}

    entries = RSS_ITEM.findall(html)
    if entries:
        for e in entries:
            lm = RSS_LINK.search(e)
            tm = RSS_TITLE.search(e)
            if not (lm and tm):
                continue
            link = (lm.group(1) or lm.group(2) or "").strip()
            title = clean(CDATA.sub(r"\1", tm.group(1)))
            if not link or len(title) < 10:
                continue
            path = urllib.parse.urlparse(urllib.parse.urljoin(url, link)).path
            key = hashlib.sha1(
                (path + "|" + title.lower()).encode("utf-8")).hexdigest()[:12]
            out[key] = {"label": title[:300],
                        "url": urllib.parse.urljoin(url, link)}
        return out

    for href, inner in ANCHOR.findall(html):
        href = href.strip()
        if not href or href.startswith("#") or NOISE.search(href):
            continue
        absolute = urllib.parse.urljoin(url, href).split("#")[0]
        p = urllib.parse.urlparse(absolute)
        if p.scheme not in ("http", "https"):
            continue
        # Off-site links are other people's news, not this body's.
        if p.netloc.lower() != host and not p.netloc.lower().endswith("." + host):
            continue
        text = clean(inner)
        if len(text) < 20 or NOISE.search(text):
            continue
        key = hashlib.sha1(
            (p.path + "|" + text.lower()).encode("utf-8")).hexdigest()[:12]
        out[key] = {"label": text[:300], "url": absolute}
    return out


def check(entry):
    family, fws, url = entry
    try:
        html = fetch(url)
    except Exception as e:                                       # noqa: BLE001
        return {"family": family, "fws": list(fws), "url": url,
                "error": f"{type(e).__name__}: {str(e)[:120]}"}
    return {"family": family, "fws": list(fws), "url": url,
            "items": items_of(url, html)}


def run_all():
    with cf.ThreadPoolExecutor(8) as ex:
        return list(ex.map(check, WATCH))


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"pages": {}}


def diff():
    """Report what is new, WITHOUT saving.

    Saving is deliberately separate. If the baseline advanced here and the model
    call for that family then failed, this week's new links would be marked as
    already seen and would never be looked at again. Only a family that came
    through cleanly gets its baseline moved on, via save().
    """
    results = run_all()
    today = dt.date.today().isoformat()
    pages = load_state().get("pages", {})
    report = {"date": today, "families": {}, "errors": [], "_snapshots": {}}

    for r in results:
        fam = r["family"]
        slot = report["families"].setdefault(
            fam, {"fws": r["fws"], "new": [], "checked": [], "failed": []})
        if r.get("error"):
            slot["failed"].append({"url": r["url"], "error": r["error"]})
            report["errors"].append(f"{fam} {r['url']}: {r['error']}")
            continue
        if len(r["items"]) < 3:
            slot["failed"].append({"url": r["url"],
                                   "error": "page yielded too few links to trust"})
            report["errors"].append(f"{fam} {r['url']}: too few links to trust")
            continue

        slot["checked"].append(r["url"])
        report["_snapshots"][r["url"]] = {
            "family": fam, "keys": sorted(r["items"].keys()),
            "seen": today, "count": len(r["items"])}
        known = set((pages.get(r["url"]) or {}).get("keys") or [])
        if known:                      # no baseline means everything looks new
            for k, v in r["items"].items():
                if k not in known:
                    slot["new"].append({"label": v["label"], "url": v["url"],
                                        "page": r["url"]})
    return report


def save(report, families):
    """Move the baseline on, but only for families that completed."""
    state = load_state()
    pages = state.get("pages", {})
    kept = 0
    for url, snap in (report.get("_snapshots") or {}).items():
        if snap["family"] not in families:
            continue
        pages[url] = {k: v for k, v in snap.items() if k != "family"}
        kept += 1
    state["pages"] = pages
    state["last_run"] = report.get("date")
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    return kept


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    if mode == "--probe":
        results = run_all()
        good = [r for r in results if not r.get("error") and len(r["items"]) >= 3]
        poor = [r for r in results if r not in good]
        print(f"{len(good)} of {len(results)} pages usable. "
              f"An unusable page just means its family falls back to the model, "
              f"which is the old behaviour: more expensive, never less covered.")
        if poor:
            print("\nNOT USABLE")
            for r in sorted(poor, key=lambda x: (x["family"], x["url"])):
                why = r.get("error") or f"only {len(r['items'])} content links"
                print(f"  {r['family']:6} {r['url']}\n         {why}")
        print("\nALL PAGES")
        ok = bad = 0
        print(f"{'family':7} {'links':>6}  url")
        print("-" * 100)
        for r in sorted(results, key=lambda x: (x["family"], x["url"])):
            if r.get("error"):
                bad += 1
                print(f"{r['family']:7} {'ERR':>6}  {r['url']}")
                print(f"{'':16}{r['error']}")
            else:
                n = len(r["items"])
                good = n >= 3
                ok += 1 if good else 0
                bad += 0 if good else 1
                flag = "" if good else "   <- too few links to be usable"
                print(f"{r['family']:7} {n:>6}  {r['url']}{flag}")
        print("-" * 100)
        print(f"{ok} usable, {bad} not. An unusable page just means its family "
              f"always goes to the model, which is the old behaviour.")
        return 0

    report = diff()
    if mode == "--seed":
        kept = save(report, set(report["families"]))
        print(f"Baseline written for {kept} pages. "
              f"From now on only links added after today are reported.")
        for e in report["errors"]:
            print("  unreadable:", e)
        return 0

    report.pop("_snapshots", None)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
