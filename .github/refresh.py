#!/usr/bin/env python3
"""Monday refresh for the EcoLedger Standards Tracker.

Runs unattended in GitHub Actions. Asks Claude to check the primary sources for
anything published since meta.last_updated, then merges whatever survives a hard
validation gate into data/data.json.

The gate is the point of this script. Nothing reaches the dataset unless:
  * its source_url is on OFFICIAL_DOMAINS, and
  * every date parses as YYYY-MM or YYYY-MM-DD, and
  * every required field is present and non-empty.
Anything that fails is dropped and reported, never guessed at and never softened.

Exit codes:  0 = finished (with or without changes)   1 = could not run
"""
import json, os, re, sys, time, urllib.request, urllib.error
from datetime import date, datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "data.json")
REPORT = os.path.join(ROOT, "refresh-report.md")

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
# Search results land in context and every later turn re-sends them, so the search
# cap is the single biggest lever on what a run costs. 10 per family is enough to
# check a handful of official pages; 30 was open-ended and could run into dollars.
# max_uses is a CEILING, not a target: the model stops when it has what it needs.
# Set it high enough that a busy week is not truncated, and cap the whole run so
# one runaway family cannot drain the account.
MAX_SEARCHES = int(os.environ.get("ANTHROPIC_MAX_SEARCHES", "10"))
RUN_SEARCH_BUDGET = int(os.environ.get("ANTHROPIC_RUN_SEARCH_BUDGET", "70"))

# Published rates, only used to put a number on the run report.
PRICE_IN, PRICE_OUT, PRICE_SEARCH = 2.0 / 1e6, 10.0 / 1e6, 10.0 / 1000
USAGE = {"in": 0, "out": 0, "searches": 0}


def note_usage(r):
    u = r.get("usage") or {}
    USAGE["in"] += (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
    USAGE["out"] += u.get("output_tokens") or 0
    USAGE["searches"] += ((u.get("server_tool_use") or {}).get("web_search_requests") or 0)


def spend():
    return (USAGE["in"] * PRICE_IN + USAGE["out"] * PRICE_OUT
            + USAGE["searches"] * PRICE_SEARCH)
SEARCH_TOOL = os.environ.get("ANTHROPIC_SEARCH_TOOL", "web_search_20250305")
# An identity-linked key is rejected with HTTP 400 unless the request names the
# workspace it acts in. Harmless to send when the key does not need it.
WORKSPACE = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
API_URL = "https://api.anthropic.com/v1/messages"

DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")

# A source of record must be the standard-setter's or regulator's own domain.
OFFICIAL_DOMAINS = (
    "ghgprotocol.org", "iso.org",
    "sciencebasedtargets.org", "files.sciencebasedtargets.org",
    "ifrs.org", "efrag.org",
    "eur-lex.europa.eu", "europa.eu",
    "sec.gov", "federalregister.gov", "ww2.arb.ca.gov", "arb.ca.gov",
    "supremecourt.gov",
    "gov.uk", "legislation.gov.uk", "fca.org.uk", "frc.org.uk",
    "frascanada.ca", "aasb.gov.au", "asic.gov.au",
    "acra.gov.sg", "sgx.com", "hkex.com.hk",
    "fsa.go.jp", "ssb-j.jp", "uaecma.gov.ae", "qfma.org.qa",
)

# One family per body. Nine bodies sharing four buckets meant the last bucket
# was asked to cover twelve regulators on one search budget, which is where
# coverage quietly fails. Narrow scopes also keep each conversation's context
# small, and context is what the token bill is made of.
FAMILIES = [
    ("ghgp", ("ghgp",), "the GHG Protocol (ghgprotocol.org)"),
    ("iso", ("iso",), "ISO (iso.org): the 14060 family, ISO 14068, and ISO/TC 207/SC 7"),
    ("sbti", ("sbti",), "the SBTi (sciencebasedtargets.org, files.sciencebasedtargets.org)"),
    ("issb", ("issb",), "the ISSB and IFRS Foundation (ifrs.org)"),
    ("eu", ("eu",), "the EU: EUR-Lex and the Official Journal, EFRAG (efrag.org), the Commission (finance.ec.europa.eu), Council and Parliament"),
    ("us", ("us",), "the US: the SEC (sec.gov), the Federal Register (federalregister.gov), CARB (ww2.arb.ca.gov, arb.ca.gov), the EPA and the federal courts"),
    ("uk", ("uk",), "the UK: the FCA (fca.org.uk), the FRC (frc.org.uk), gov.uk and legislation.gov.uk"),
    ("apac", ("au","sg","hk","jp"), "Australia (aasb.gov.au, asic.gov.au), Singapore (acra.gov.sg, sgx.com), Hong Kong (hkex.com.hk) and Japan (fsa.go.jp, ssb-j.jp), each via its named regulator only"),
    ("other", ("ca","ae","qa"), "Canada (frascanada.ca), the UAE (uaecma.gov.ae) and Qatar (qfma.org.qa), each via its named regulator only"),
]

SOURCING_RULE = """SOURCING RULE, ABSOLUTE:
- Every fact must come from the standard-setter's or regulator's OWN publication, and must cite that page's URL.
- You may use web search to DISCOVER that something happened. You must then verify it against the official page and cite that official page.
- You are FORBIDDEN from citing any other tracker, consultancy note, law-firm alert or news article as a source of record. If you cannot find an official source for a claim, DROP the claim.
- IF NO OFFICIAL SOURCE PUBLISHES A DATE, THERE IS NO DATE. Never infer a date from past practice, never carry one over from a superseded plan, never present an expectation as a deadline.
- A quiet week is a real and useful result. If nothing moved, return an empty list. Never invent activity to justify the run."""


def log(msg):
    print(msg, flush=True)


def api(messages, tools, max_tokens=8000, tries=3):
    body = json.dumps({
        "model": MODEL, "max_tokens": max_tokens,
        "messages": messages, "tools": tools,
    }).encode()
    headers = {
        "content-type": "application/json",
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
    }
    if WORKSPACE:
        headers["anthropic-workspace-id"] = WORKSPACE
    req = urllib.request.Request(API_URL, data=body, method="POST", headers=headers)
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read()[:400].decode('utf-8', 'replace')}"
            if e.code in (400, 401, 403, 404):
                break                      # a bad key or model will not fix itself
        except Exception as e:             # noqa: BLE001
            last = str(e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Anthropic API call failed: {last}")


def ask(label, scope, since, today, leads=None):
    if leads:
        listed = "\n".join(f"  - {x['label']}  <{x['url']}>" for x in leads[:40])
        brief = (
            "These items appeared on that body's OWN pages since the last run. They are "
            "link text and nothing more: some will be genuine new publications, some will "
            "be reposts, reordering, or pages unrelated to reporting standards.\n\n"
            + listed +
            "\n\nWork through them. Open the official page behind anything that looks like "
            "a real change and report only what that page itself supports. You may report "
            "something the list missed, but you do not need to hunt from scratch: this list "
            "is what actually changed on the site."
        )
    else:
        brief = (f"Find anything published, adopted or announced between {since} and "
                 f"{today} inclusive by {scope}.")

    prompt = f"""You are running the weekly refresh of the EcoLedger Standards Tracker, a free public tracker of changes to corporate emissions accounting and disclosure standards.

Today is {today}. The dataset was last verified {since}.

{brief}

{SOURCING_RULE}

Reply with NOTHING BUT a single JSON object, no prose before or after, no markdown fence:

{{"updates":[{{"date":"YYYY-MM-DD","framework":"one of ghgp|iso|sbti|issb|eu|us|uk|ca|au|sg|hk|jp|ae|qa","headline":"one line naming the acting body and leading with what changed","detail":"2 to 4 self-contained sentences","so_what":"one imperative sentence for a corporate reporting team","significance":"high|medium|low","source_url":"the official page","source_title":"...","source_publisher":"..."}}],
 "milestones":[{{"date":"YYYY-MM-DD or YYYY-MM","framework":"...","label":"...","kind":"deadline|update","state":"upcoming","source_url":"the official page"}}],
 "notes":"anything you could not verify, any place two official sources disagreed, and any date you refused to state because no official source published one"}}

Only include a milestone if an official source publishes its date. Return empty arrays if nothing moved.

"kind" is not optional and the distinction matters more than it looks:
  deadline = a reporting entity has to do something by that date. First reporting
             periods, effective dates a company must apply, filing and submission
             deadlines, assurance start dates, transposition deadlines.
  update   = a dated event where the standard-setter or regulator is the one
             acting and the reader has nothing to file. Publications, adoptions,
             board meetings, consultations opening or closing, comment deadlines,
             calls for evidence, expressions of interest, target publication dates.
Test it by asking who has to act. If the answer is the body rather than the
reader, it is an update, even when the word "deadline" appears in its own title."""
    left = max(0, RUN_SEARCH_BUDGET - USAGE["searches"])
    # With leads there is a shortlist to verify, so a few searches do it. Without
    # leads the body blocks our fetches and this is a full sweep, but the scope is
    # a single organisation, so it does not need the old open-ended ceiling.
    cap = min(6 if leads else MAX_SEARCHES, left)
    if cap == 0:
        log(f"  ! {label}: whole-run search budget spent, skipping")
        return {"updates": [], "milestones": [],
                "notes": f"{label}: not checked, the run's search budget was already spent"}
    log(f"  asking about {label} (up to {cap} searches) ...")
    r = api([{"role": "user", "content": prompt}],
            [{"type": SEARCH_TOOL, "name": "web_search", "max_uses": cap}])
    note_usage(r)
    text = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        log(f"  ! {label}: no JSON in the reply, skipping")
        return {"updates": [], "milestones": [], "notes": f"{label}: model returned no JSON"}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        log(f"  ! {label}: JSON did not parse ({e}), skipping")
        return {"updates": [], "milestones": [], "notes": f"{label}: unparseable JSON"}


def host_ok(url):
    m = re.match(r"^https://([^/]+)", str(url or ""))
    if not m:
        return False
    h = m.group(1).lower().split(":")[0]
    return any(h == d or h.endswith("." + d) for d in OFFICIAL_DOMAINS)


def gate_update(u, today, rejects):
    why = []
    for f in ("date", "framework", "headline", "detail", "so_what",
              "significance", "source_url", "source_title", "source_publisher"):
        if not str(u.get(f, "")).strip():
            why.append(f"missing {f}")
    if u.get("date") and not DATE_RE.match(str(u["date"])):
        why.append(f"bad date {u['date']!r}")
    if u.get("date") and str(u["date"]) > today:
        why.append(f"date {u['date']} is in the future")
    if u.get("significance") not in ("high", "medium", "low"):
        why.append(f"bad significance {u.get('significance')!r}")
    if not host_ok(u.get("source_url")):
        why.append(f"source not on an official domain: {u.get('source_url')!r}")
    if why:
        rejects.append(f"- **{str(u.get('headline', '?'))[:80]}** — {'; '.join(why)}")
        return None
    return {
        "date": str(u["date"]), "framework": str(u["framework"]),
        "workstream": str(u.get("workstream", "")),
        "headline": str(u["headline"]).strip(), "detail": str(u["detail"]).strip(),
        "so_what": str(u["so_what"]).strip(), "significance": u["significance"],
        "source_url": str(u["source_url"]), "source_title": str(u["source_title"]),
        "source_publisher": str(u["source_publisher"]), "last_verified": today,
    }


def gate_milestone(m, rejects):
    why = []
    for f in ("date", "framework", "label", "source_url"):
        if not str(m.get(f, "")).strip():
            why.append(f"missing {f}")
    if m.get("kind") not in ("deadline", "update"):
        why.append(f"kind must be 'deadline' or 'update', got {m.get('kind')!r}")
    if m.get("date") and not DATE_RE.match(str(m["date"])):
        why.append(f"bad date {m['date']!r}")
    if not host_ok(m.get("source_url")):
        why.append(f"source not on an official domain: {m.get('source_url')!r}")
    if why:
        rejects.append(f"- milestone **{str(m.get('label', '?'))[:70]}** — {'; '.join(why)}")
        return None
    return {"date": str(m["date"]), "framework": str(m["framework"]),
            "label": str(m["label"]).strip(),
            "kind": "deadline" if m.get("kind") == "deadline" else "update",
            "state": m.get("state") or "upcoming",
            "source_url": str(m["source_url"])}


def main():
    if not API_KEY:
        log("FAIL: ANTHROPIC_API_KEY is not set. Add it under Settings > Secrets and variables > Actions.")
        return 1

    with open(DATA, encoding="utf-8") as f:
        d = json.load(f)

    today = datetime.now(timezone.utc).date().isoformat()
    since = str(d.get("meta", {}).get("last_updated", ""))
    if not DATE_RE.match(since):
        log(f"FAIL: meta.last_updated is not a usable date: {since!r}")
        return 1
    if since == today:
        log("meta.last_updated is already today. Nothing to do.")
        return 0

    log(f"refreshing {since} -> {today}, model {MODEL}")
    found_u, found_m, notes, rejects = [], [], [], []
    # The cheap pass first. A page that published nothing has nothing to ask
    # about, and establishing that costs one fetch rather than a conversation.
    watcher = None
    watch = {}
    watch_err = []
    wreport = None
    try:
        import watch as watcher
        wreport = watcher.diff()
        watch = wreport.get("families") or {}
        watch_err = wreport.get("errors") or []
        moved = sum(len(v["new"]) for v in watch.values())
        log(f"watch: {len(watch)} families, {moved} new links, "
            f"{len(watch_err)} unreadable pages")
    except Exception as e:                                       # noqa: BLE001
        log(f"watch: unavailable ({e}); every family goes to the model")
        notes.append(f"Change detection did not run ({e}), so every source was checked "
                     f"the slow way. Coverage is unaffected; the run just cost more.")

    verified = set()          # frameworks whose check actually completed
    failed = []
    skipped = []
    for label, fws, scope in FAMILIES:
        w = watch.get(label)
        leads = None
        if w is not None and not w["failed"] and not w.get("unseeded"):
            if not w["new"]:
                # Every page for this body fetched cleanly and offered nothing new.
                # That is a check that passed, not a check that was skipped.
                log(f"  = {label}: no new links on {len(w['checked'])} official pages")
                verified.update(fws)
                skipped.append(label)
                continue
            leads = w["new"]
            log(f"  + {label}: {len(leads)} new links on its own pages")
        elif w is not None and w.get("unseeded"):
            log(f"  ? {label}: {len(w['unseeded'])} pages seen for the first time, "
                f"checking the slow way to start the baseline honestly")
        try:
            r = ask(label, scope, since, today, leads)
        except Exception as e:                                   # noqa: BLE001
            log(f"  ! {label}: {e}")
            notes.append(f"{label}: the check did not complete ({e})")
            failed.append(label)
            continue
        verified.update(fws)
        found_u += r.get("updates") or []
        found_m += r.get("milestones") or []
        if r.get("notes"):
            notes.append(f"**{label}** — {r['notes']}")

    clean_u = [x for x in (gate_update(u, today, rejects) for u in found_u) if x]
    clean_m = [x for x in (gate_milestone(m, rejects) for m in found_m) if x]

    seen_u = {(u["date"], u["headline"].lower()) for u in d.get("updates", [])}
    new_u = [u for u in clean_u if (u["date"], u["headline"].lower()) not in seen_u]
    seen_m = {(str(m["date"]), m["label"].lower()) for m in d.get("timeline_milestones", [])}
    new_m = [m for m in clean_m if (str(m["date"]), m["label"].lower()) not in seen_m]

    d["updates"] = sorted(new_u + d.get("updates", []), key=lambda u: u["date"], reverse=True)
    d["timeline_milestones"] = sorted(d.get("timeline_milestones", []) + new_m, key=lambda m: str(m["date"]))
    # The first live run failed every one of the nine checks on a bad auth header
    # and still stamped the whole dataset as verified today. A tracker that says
    # "verified" about something it never looked at is worse than one that says
    # nothing, so freshness is now only ever claimed for what actually completed.
    if not verified:
        log(f"FAIL: every source check failed ({', '.join(failed)}). "
            "Nothing written; the tracker keeps last week's verified data.")
        return 1
    if not failed:
        d["meta"]["last_updated"] = today
        if "coverage_window_end" in d["meta"]:
            d["meta"]["coverage_window_end"] = today
    for fam in d.get("frameworks", []):
        if fam.get("id") not in verified:
            continue
        for w in fam.get("workstreams", []):
            w["last_verified"] = today

    with open(DATA, "w", encoding="utf-8") as f:
        # indent=2 because that is how data.json is stored. Writing indent=1
        # re-indented all 9,498 lines every run, so the approval diff was 9,481
        # changed lines of whitespace and you could not see what you were
        # approving. A review gate you cannot read is not a review gate.
        json.dump(d, f, indent=2, ensure_ascii=False)

    lines = [f"# Monday refresh, {today}", "",
             f"Checked {since} to {today}. "
             f"**{len(new_u)} new update{'s' if len(new_u) != 1 else ''}, "
             f"{len(new_m)} new milestone{'s' if len(new_m) != 1 else ''}.**", ""]
    if new_u:
        lines.append("## What changed")
        for u in new_u:
            lines.append(f"- **{u['date']} · {u['framework'].upper()}** {u['headline']}  \n"
                         f"  {u['so_what']}  \n  [{u['source_publisher']}]({u['source_url']})")
    else:
        lines.append("A quiet week. Nothing verifiable moved on any tracked source.")
    if new_m:
        lines += ["", "## New dated milestones"] + \
                 [f"- {m['date']} · {m['framework'].upper()} — {m['label']}" for m in new_m]
    if rejects:
        lines += ["", "## Rejected by the gate, not published",
                  "These failed the sourcing or date rules and were dropped rather than guessed at.", ""] + rejects
    if failed:
        lines += ["", "## Not checked this week",
                  f"**{len(failed)} of {len(FAMILIES)} source checks did not complete: "
                  f"{', '.join(failed)}.** Those frameworks keep their previous "
                  "verification date and the dataset's own last_updated has not moved. "
                  "The tracker will show them as ageing rather than as freshly checked."]
    if notes:
        lines += ["", "## Caveats"] + [f"- {n}" for n in notes]
    if skipped:
        lines += ["", "## Verified without needing the model",
                  ", ".join(skipped) + " \u2014 every official page fetched cleanly and "
                  "offered no new links, so there was nothing to interpret. These were "
                  "checked, not skipped."]
    if watch_err:
        lines += ["", "## Pages the watcher could not read"] + \
                 [f"- {e}" for e in watch_err] + \
                 ["", "Those families were checked the slow way instead, so coverage is "
                  "unaffected. It only costs more."]
    lines += ["", "## What this run cost",
              f"- {USAGE['searches']} web searches across {len(FAMILIES)} sources "
              f"(ceiling {MAX_SEARCHES} each, {RUN_SEARCH_BUDGET} for the run)",
              f"- {USAGE['in']:,} input tokens, {USAGE['out']:,} output tokens",
              f"- **about ${spend():.2f}** at published rates for {MODEL}",
              "",
              "If searches came in at the ceiling, the run was probably truncated and the "
              "ceiling should go up. If it came in well under, the ceiling can come down."]
    lines += ["", f"_Ran unattended. {len(found_u)} candidate updates seen, "
                  f"{len(clean_u)} passed the gate, {len(new_u)} were not already in the dataset._"]
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    if watcher is not None and wreport is not None:
        done = {lab for lab, _f, _s in FAMILIES if lab not in failed}
        try:
            log(f"watch: baseline advanced for {watcher.save(wreport, done)} pages")
        except Exception as e:                                   # noqa: BLE001
            log(f"watch: could not save baseline ({e}); next run re-reports these links")

    log(f"done: {len(new_u)} updates, {len(new_m)} milestones, {len(rejects)} rejected, "
        f"{USAGE['searches']} searches, about ${spend():.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
