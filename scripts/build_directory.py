#!/usr/bin/env python3
"""
Build the Animal Welfare Research Directory from OpenAlex.

Steps
  1. Fetch candidate papers from OpenAlex using the rules in config.yaml.
     Progress is saved after every page, so if the free daily API budget
     runs out the next run simply continues where this one stopped.
  2. When all queries are finished, look up institution details
     (city, homepage, parent organisation).
  3. Aggregate papers into institutions and researchers and write
     docs/directory.json, which the search page loads.

Usage
  OPENALEX_API_KEY=xxxx python scripts/build_directory.py
  python scripts/build_directory.py --aggregate-only   # rebuild JSON from cache
  python scripts/build_directory.py --restart          # discard progress, start over

Only the Python standard library and PyYAML are needed.
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"
EXCLUDE = ROOT / "exclude.txt"
DATA = ROOT / "data"
STATE = DATA / "state.json"
WORKS = DATA / "works.jsonl.gz"
INSTS_SEEN = DATA / "institutions_seen.json"
INST_DETAILS = DATA / "institution_details.json"
AUTHOR_STATS = DATA / "author_stats.json"
OUTPUT = ROOT / "docs" / "directory.json"

DEFAULT_BASE = "https://api.openalex.org"
PER_PAGE = 200
REFRESH_DAYS = 28  # start a fresh build when the last complete one is older than this

WORK_FIELDS = ",".join([
    "id", "doi", "title", "publication_year", "type", "primary_location",
    "authorships", "topics", "keywords", "abstract_inverted_index",
])
INST_FIELDS = "id,display_name,country_code,type,lineage,ror,homepage_url,geo"


class BudgetExhausted(Exception):
    pass


# --------------------------------------------------------------------- helpers

def short(oa_id):
    """'https://openalex.org/W123' -> 'W123'"""
    if not oa_id:
        return None
    return oa_id.rstrip("/").rsplit("/", 1)[-1]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def term_regex(terms):
    """Whole-word, case-insensitive regex for a list of words/phrases."""
    parts = sorted({re.escape(t.lower()).replace(r"\ ", r"[\s\-]+") for t in terms}, key=len, reverse=True)
    if not parts:
        return None
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + r")(?![a-z0-9])")


def abstract_text(inv):
    """Rebuild abstract text from OpenAlex's inverted index {word: [positions]}."""
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def quote(term):
    return '"' + term.replace('"', "") + '"'


# --------------------------------------------------------------------- API

class OpenAlex:
    def __init__(self, base, api_key, mailto=None):
        self.base = base.rstrip("/")
        self.api_key = api_key
        self.mailto = mailto
        self.spent = 0.0

    def get(self, path, params):
        params = dict(params)
        if self.api_key:
            params["api_key"] = self.api_key
        if self.mailto:
            params["mailto"] = self.mailto
        url = f"{self.base}{path}?{urllib.parse.urlencode(params, safe=':|,()<>*')}"
        delay = 2
        for attempt in range(6):
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    data = json.loads(r.read().decode("utf-8"))
                cost = (data.get("meta") or {}).get("cost_usd") or 0
                self.spent += float(cost)
                return data
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    # Usually a short-term "slow down"; wait and retry. Only after
                    # several refusals do we treat it as the daily budget being used up.
                    if attempt < 5:
                        try:
                            wait = float(e.headers.get("Retry-After") or 0)
                        except ValueError:
                            wait = 0
                        wait = max(wait, 15 * 2 ** attempt)  # 15, 30, 60, 120, 240 s
                        print(f"  OpenAlex asked to slow down (429); waiting {wait:.0f} s", flush=True)
                        time.sleep(wait)
                        continue
                    raise BudgetExhausted("OpenAlex kept returning 429 (rate limit or daily budget reached)")
                if e.code in (500, 502, 503, 504) and attempt < 5:
                    time.sleep(delay)
                    delay *= 2
                    continue
                body = e.read().decode("utf-8", "replace")[:500]
                raise RuntimeError(f"HTTP {e.code} for {path}: {body}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < 5:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise RuntimeError(f"Network error for {path}: {e}") from None

    def remaining_budget(self):
        """Daily budget left in USD, or None if it can't be checked."""
        try:
            data = self.get("/rate-limit", {})
            return float(data["rate_limit"]["daily_remaining_usd"])
        except Exception:
            return None


# --------------------------------------------------------------------- queries

def build_queries(cfg, from_year):
    base = []
    if cfg.get("work_types"):
        base.append("type:" + "|".join(cfg["work_types"]))
    if from_year:
        base.append(f"publication_year:>{int(from_year) - 1}")

    queries = []

    def add(kind, extra, search=True):
        filt = ",".join(base + extra)
        qid = kind + "-" + hashlib.sha1(filt.encode()).hexdigest()[:10]
        queries.append({"id": qid, "kind": kind, "filter": filt, "search": search})

    ids = list((cfg.get("core_journals") or {}).keys())
    for i in range(0, len(ids), 50):
        add("A", ["primary_location.source.id:" + "|".join(ids[i:i + 50])], search=False)

    for group in cfg.get("strong_phrases") or []:
        add("B", ["title_and_abstract.search:(" + " OR ".join(quote(t) for t in group) + ")"])

    for group in cfg.get("welfare_with_animal") or []:
        add("C", ['title_and_abstract.search:("welfare" AND (' + " OR ".join(quote(t) for t in group) + "))"])

    topics = "|".join((cfg.get("welfare_topics") or {}).keys())
    d_groups = list(cfg.get("welfare_related_terms") or [])
    for terms in (cfg.get("indicators") or {}).values():
        d_groups += [terms[i:i + 8] for i in range(0, len(terms), 8)]
    for group in d_groups:
        add("D", ["title_and_abstract.search:(" + " OR ".join(quote(t) for t in group) + ")",
                  "topics.id:" + topics])
    return queries


# --------------------------------------------------------------------- classification

class Classifier:
    def __init__(self, cfg):
        species = cfg.get("species") or {}
        self.species = {name: term_regex(terms) for name, terms in species.items()}
        self.indicators = {name: term_regex(terms) for name, terms in (cfg.get("indicators") or {}).items()}
        # phrases that must not count for a species, e.g. "guinea pig" for Pigs
        self.species_not = {name: term_regex(terms) for name, terms in (cfg.get("species_not") or {}).items()}
        all_species_terms = [t for terms in species.values() for t in terms]
        self.animal = term_regex((cfg.get("animal_terms") or []) + all_species_terms)
        self.exclusion = term_regex(cfg.get("exclusion_phrases") or [])
        strong = [t for g in (cfg.get("strong_phrases") or []) for t in g]
        self.strong = term_regex(strong)
        self.core = set((cfg.get("core_journals") or {}).keys())
        self.topics = set((cfg.get("welfare_topics") or {}).keys())

    def include(self, kind, text, work):
        """The API already guaranteed the query's own search terms matched;
        here we apply the extra conditions of each rule."""
        if kind == "A":
            return True
        if kind == "B":
            return bool(self.animal and self.animal.search(text)) or self.has_welfare_topic(work)
        if kind == "C":
            if self.exclusion and self.exclusion.search(text):
                return bool(self.strong and self.strong.search(text))
            return True
        if kind == "D":
            return True
        return False

    def has_welfare_topic(self, work):
        return any(short(t.get("id")) in self.topics for t in (work.get("topics") or []))

    def tag_indicators(self, text):
        return [name for name, rx in self.indicators.items() if rx and rx.search(text)]

    def tag_species(self, text):
        tags = []
        for name, rx in self.species.items():
            t = text
            neg = self.species_not.get(name)
            if neg:
                t = neg.sub(" ", t)
            if rx and rx.search(t):
                tags.append(name)
        return tags


def compact_work(work, clf, kind, insts_seen):
    title = work.get("title") or ""
    abstract = abstract_text(work.get("abstract_inverted_index"))
    keywords = " ".join(k.get("display_name", "") for k in (work.get("keywords") or []))
    # inclusion uses only what the authors wrote (title + abstract);
    # OpenAlex's auto-generated keywords are used only for species tags
    text = f"{title} {abstract}".lower()
    if not clf.include(kind, text, work):
        return None
    tag_text = f"{text} {keywords.lower()}"

    src = ((work.get("primary_location") or {}).get("source") or {})
    authors = []
    for a in work.get("authorships") or []:
        au = a.get("author") or {}
        aid = short(au.get("id"))
        if not aid:
            continue
        inst_ids = []
        for inst in a.get("institutions") or []:
            iid = short(inst.get("id"))
            if not iid:
                continue
            inst_ids.append(iid)
            if iid not in insts_seen:
                insts_seen[iid] = {
                    "n": inst.get("display_name"),
                    "c": inst.get("country_code"),
                    "t": inst.get("type"),
                    "l": [short(x) for x in (inst.get("lineage") or [])],
                }
        orcid = au.get("orcid")
        authors.append([aid, au.get("display_name") or a.get("raw_author_name"),
                        orcid.rsplit("/", 1)[-1] if orcid else None, inst_ids])
    return {
        "id": short(work.get("id")),
        "y": work.get("publication_year"),
        "t": title,
        "doi": work.get("doi"),
        "s": short(src.get("id")),
        "sn": src.get("display_name"),
        "sp": clf.tag_species(tag_text),
        "ind": clf.tag_indicators(tag_text),
        "r": kind,
        "au": authors,
    }


# --------------------------------------------------------------------- fetch phase

def fetch(cfg, api, state, max_pages=None):
    clf = Classifier(cfg)
    insts_seen = load_json(INSTS_SEEN, {})
    have = set()
    if WORKS.exists():
        with gzip.open(WORKS, "rt", encoding="utf-8") as f:
            for line in f:
                have.add(json.loads(line)["id"])
    max_results = int(cfg.get("max_results_per_query") or 150000)
    pages_this_run = 0

    # a build keeps fetching with the year it started with; a later from_year
    # is applied when the directory is assembled (see aggregate)
    for q in build_queries(cfg, state.get("from_year", cfg.get("from_year"))):
        qs = state["queries"].setdefault(q["id"], {"cursor": "*", "done": False, "pages": 0, "kind": q["kind"], "filter": q["filter"]})
        if qs["done"] or qs.get("failed"):
            continue
        print(f"[{q['kind']}] {q['filter'][:140]}")
        while qs["cursor"]:
            if max_pages is not None and pages_this_run >= max_pages:
                print("Stopping: --max-pages reached")
                return False
            # every 50 pages, check the remaining daily budget
            if pages_this_run % 50 == 0 and api.api_key:
                left = api.remaining_budget()
                if left is not None:
                    print(f"  budget left today: ${left:.3f}")
                    if left < 0.01:
                        raise BudgetExhausted("Daily budget nearly used up")
            time.sleep(0.2)  # be gentle with the API
            data = api.get("/works", {"filter": q["filter"], "per-page": PER_PAGE,
                                      "cursor": qs["cursor"], "select": WORK_FIELDS})
            meta = data.get("meta") or {}
            if qs["pages"] == 0:
                count = meta.get("count") or 0
                oql = ((meta.get("x_query") or {}).get("oql") or "")
                understood = (not q["search"]) or (not oql) or ("title or abstract" in oql)
                if count > max_results or not understood:
                    qs["failed"] = f"count={count}; query={oql or '?'}"
                    print(f"  !! SKIPPED: OpenAlex did not understand this query ({qs['failed']}). "
                          f"Try splitting the term group in config.yaml.")
                    save_json(STATE, state)
                    break
                qs["count"] = count
                print(f"  {count} candidate papers")
            new = []
            for w in data.get("results") or []:
                wid = short(w.get("id"))
                if wid in have:
                    continue
                rec = compact_work(w, clf, q["kind"], insts_seen)
                if rec:
                    new.append(rec)
                    have.add(wid)
            if new:
                with gzip.open(WORKS, "at", encoding="utf-8") as f:
                    for rec in new:
                        f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            qs["pages"] += 1
            qs["cursor"] = meta.get("next_cursor")
            pages_this_run += 1
            if not data.get("results"):
                qs["cursor"] = None
            save_json(INSTS_SEEN, insts_seen)
            save_json(STATE, state)
        else:
            qs["done"] = True
            save_json(STATE, state)
            print(f"  done ({qs['pages']} pages). Papers kept so far: {len(have)}")
    return True


# --------------------------------------------------------------------- institution details

def fetch_institution_details(api, insts_seen):
    details = load_json(INST_DETAILS, {})
    needed = set(insts_seen)
    for v in insts_seen.values():
        needed.update(x for x in v.get("l") or [] if x)
    missing = sorted(i for i in needed if i not in details)
    print(f"Institution details: {len(missing)} to fetch")
    for i in range(0, len(missing), 50):
        chunk = missing[i:i + 50]
        data = api.get("/institutions", {"filter": "openalex_id:" + "|".join(chunk),
                                         "per-page": 50, "select": INST_FIELDS})
        for r in data.get("results") or []:
            geo = r.get("geo") or {}
            details[short(r["id"])] = {
                "n": r.get("display_name"), "c": r.get("country_code"), "t": r.get("type"),
                "l": [short(x) for x in (r.get("lineage") or [])],
                "ror": (r.get("ror") or "").rsplit("/", 1)[-1] or None,
                "web": r.get("homepage_url"), "city": geo.get("city"),
                "lat": geo.get("latitude"), "lon": geo.get("longitude"),
            }
        for iid in chunk:  # remember lookups that returned nothing
            details.setdefault(iid, None)
        save_json(INST_DETAILS, details)
    return details


# --------------------------------------------------------------------- author stats (welfare focus)

def orcid_aliases(cfg):
    """OpenAlex sometimes splits one person into several profiles. Profiles
    that carry the same ORCID on our papers are merged: every profile ID is
    mapped to the one with the most matching papers."""
    min_year = int(cfg.get("from_year") or 0)
    per_orcid = {}  # orcid -> {author id: papers}
    with gzip.open(WORKS, "rt", encoding="utf-8") as f:
        for line in f:
            w = json.loads(line)
            y = w.get("y") or 0
            if min_year and y and y < min_year:
                continue
            for au in w.get("au") or []:
                aid, orcid = au[0], au[2]
                if orcid:
                    d = per_orcid.setdefault(orcid, {})
                    d[aid] = d.get(aid, 0) + 1
    alias = {}
    for d in per_orcid.values():
        if len(d) > 1:
            main = max(d, key=lambda a: (d[a], a))
            for a in d:
                if a != main:
                    alias[a] = main
    return alias


def count_author_papers(cfg):
    """Matching papers per author since from_year (before any filtering)."""
    min_year = int(cfg.get("from_year") or 0)
    counts = {}
    with gzip.open(WORKS, "rt", encoding="utf-8") as f:
        for line in f:
            w = json.loads(line)
            y = w.get("y") or 0
            if min_year and y and y < min_year:
                continue
            for au in w.get("au") or []:
                counts[au[0]] = counts.get(au[0], 0) + 1
    alias = orcid_aliases(cfg)
    merged = {}
    for a, c in counts.items():
        m = alias.get(a, a)
        merged[m] = merged.get(m, 0) + c
    # a profile is "needed" if its merged person passes the minimum
    return {a: merged[alias.get(a, a)] for a in counts}


def fetch_author_stats(cfg, api):
    """Total number of works per year for each candidate researcher, used for
    the welfare-focus share. Only authors who pass the minimum paper count."""
    if not cfg.get("researcher_min_focus"):
        return
    stats = load_json(AUTHOR_STATS, {})
    min_papers = int(cfg.get("researcher_min_papers") or 1)
    need = sorted(a for a, c in count_author_papers(cfg).items() if c >= min_papers and a not in stats)
    print(f"Researcher publication totals: {len(need)} to fetch", flush=True)
    for i in range(0, len(need), 50):
        chunk = need[i:i + 50]
        data = api.get("/authors", {"filter": "openalex_id:" + "|".join(chunk), "per-page": 50,
                                    "select": "id,counts_by_year"})
        for r in data.get("results") or []:
            stats[short(r["id"])] = {str(c["year"]): c.get("works_count", 0) for c in (r.get("counts_by_year") or [])}
        for a in chunk:
            stats.setdefault(a, None)  # looked up, nothing returned
        if (i // 50) % 20 == 0:
            save_json(AUTHOR_STATS, stats)
    save_json(AUTHOR_STATS, stats)


# --------------------------------------------------------------------- aggregate

def load_exclusions():
    """Single IDs (author A..., ORCID, institution I..., paper W...) are removed
    entirely. A line 'A123 W456' detaches only that author from that paper
    (for papers OpenAlex wrongly attributed to someone)."""
    ids, pairs = set(), set()
    if EXCLUDE.exists():
        for line in EXCLUDE.read_text(encoding="utf-8").splitlines():
            parts = [p.rsplit("/", 1)[-1] for p in line.split("#", 1)[0].split()]
            if len(parts) == 1:
                ids.add(parts[0])
            elif len(parts) == 2:
                a, w = sorted(parts)  # 'A...' sorts before 'W...'
                pairs.add((a, w))
    return ids, pairs


def aggregate(cfg):
    insts_seen = load_json(INSTS_SEEN, {})
    details = load_json(INST_DETAILS, {})
    excluded, excluded_pairs = load_exclusions()
    alias = orcid_aliases(cfg)  # merge split profiles that share an ORCID
    members = {}
    if alias:
        print(f"Merged {len(alias)} duplicate researcher profiles sharing an ORCID")
    rollup = cfg.get("subunit_rollup", "when_parent_present")
    species_names = list((cfg.get("species") or {}).keys())
    sp_bit = {n: 1 << i for i, n in enumerate(species_names)}
    indicator_names = list((cfg.get("indicators") or {}).keys())
    ind_bit = {n: 1 << i for i, n in enumerate(indicator_names)}

    def info(iid):
        d = details.get(iid) or {}
        s = insts_seen.get(iid) or {}
        return {"n": d.get("n") or s.get("n"), "c": d.get("c") or s.get("c"),
                "l": d.get("l") or s.get("l") or [iid], "city": d.get("city"),
                "ror": d.get("ror"), "web": d.get("web"), "lat": d.get("lat"), "lon": d.get("lon")}

    def root(iid):
        lineage = info(iid)["l"]
        for anc in lineage:
            if (details.get(anc) or {}).get("l") == [anc]:
                return anc
        return lineage[0] if lineage else iid

    def resolve(inst_ids):
        ids = [i for i in inst_ids if i not in excluded]
        if rollup == "never":
            return ids
        if rollup == "always":
            return list(dict.fromkeys(root(i) for i in ids))
        present = set(ids)
        out = []
        for i in ids:
            ancestors = [a for a in info(i)["l"] if a != i]
            if any(a in present for a in ancestors):
                continue  # parent already listed on this paper
            out.append(i)
        return out

    min_year = int(cfg.get("from_year") or 0)
    insts, authors = {}, {}
    n_works = 0
    years = []
    with gzip.open(WORKS, "rt", encoding="utf-8") as f:
        for line in f:
            w = json.loads(line)
            y = w.get("y") or 0
            if min_year and y and y < min_year:
                continue  # older than from_year in config.yaml
            if w["id"] in excluded:
                continue  # paper removed via exclude.txt
            n_works += 1
            if y:
                years.append(y)
            sps = [s for s in (w.get("sp") or []) if s in sp_bit]
            inds = [s for s in (w.get("ind") or []) if s in ind_bit]
            seen = set()
            for aid, name, orcid, inst_ids in w.get("au") or []:
                orig = aid
                aid = alias.get(aid, aid)
                members.setdefault(aid, set()).add(orig)
                if aid in excluded or orig in excluded or (orcid and orcid in excluded) \
                        or (orig, w["id"]) in excluded_pairs or aid in seen:
                    continue
                seen.add(aid)
                a = authors.setdefault(aid, {"n": name, "o": orcid, "w": 0, "y0": y or 9999, "y1": 0,
                                             "ny": 0, "sp": {}, "ind": {}, "i": {}})
                a["w"] += 1
                for s in sps:
                    a["sp"][s] = a["sp"].get(s, 0) + 1
                for s in inds:
                    a["ind"][s] = a["ind"].get(s, 0) + 1
                if y and y >= a["ny"]:
                    a["ny"] = y
                    if name:
                        a["n"] = name  # use most recent spelling
                if orcid:
                    a["o"] = orcid
                if y:
                    a["y0"] = min(a["y0"], y)
                    a["y1"] = max(a["y1"], y)
                for iid in resolve(inst_ids):
                    inst = insts.setdefault(iid, {"works": set(), "authors": set(), "sp": {}, "ind": {}, "y1": 0})
                    if w["id"] not in inst["works"]:
                        for s in sps:
                            inst["sp"][s] = inst["sp"].get(s, 0) + 1
                        for s in inds:
                            inst["ind"][s] = inst["ind"].get(s, 0) + 1
                    inst["works"].add(w["id"])
                    inst["authors"].add(aid)
                    inst["y1"] = max(inst["y1"], y)
                    prev = a["i"].get(iid, 0)
                    a["i"][iid] = max(prev, y)

    def tag_bits(counts, n_papers, bitmap, share_key):
        """Tag only if it makes up >= the configured share of the papers,
        so a big university isn't tagged with everything it ever touched."""
        need = max(1, n_papers * float(cfg.get(share_key, cfg.get("species_min_share", 0.05))))
        bits = 0
        for s, c in counts.items():
            if c >= need:
                bits |= bitmap[s]
        return bits

    def species_bits(counts, n_papers):
        return tag_bits(counts, n_papers, sp_bit, "species_min_share")

    def indicator_bits(counts, n_papers):
        return tag_bits(counts, n_papers, ind_bit, "indicator_min_share")

    # ---- which researchers are listed
    min_papers = int(cfg.get("researcher_min_papers") or 1)
    min_focus = float(cfg.get("researcher_min_focus") or 0)
    stats = load_json(AUTHOR_STATS, {}) if min_focus else {}
    min_year_f = int(cfg.get("from_year") or 0)

    exempt = int(cfg.get("researcher_focus_exempt_papers") or 0)

    def keep(aid, a):
        if a["w"] < min_papers:
            return False
        if exempt and a["w"] >= exempt:
            return True  # clearly active in welfare; also protects mixed-up profiles
        if min_focus:
            sts = [stats.get(m) for m in members.get(aid, {aid})]
            if sts and all(sts):  # unknown totals -> keep (benefit of the doubt)
                total = sum(n for st in sts for y, n in st.items() if int(y) >= min_year_f)
                if total > 0 and a["w"] / total < min_focus:
                    return False
        return True

    kept = {aid for aid, a in authors.items() if keep(aid, a)}
    print(f"Researchers listed: {len(kept)} of {len(authors)} "
          f"(min {min_papers} papers" + (f", welfare focus >= {min_focus:.0%}" if min_focus else "") + ")")
    for v in insts.values():
        v["listed"] = len(v["authors"] & kept)

    inst_list = sorted(insts.items(), key=lambda kv: -len(kv[1]["works"]))
    inst_index = {iid: k for k, (iid, _) in enumerate(inst_list)}
    out_inst = []
    countries = set()
    for iid, v in inst_list:
        d = info(iid)
        countries.add(d["c"])
        out_inst.append([d["n"] or iid, d["city"], d["c"], d["ror"], d["web"], len(v["works"]),
                         v["listed"], species_bits(v["sp"], len(v["works"])), v["y1"], iid,
                         round(d["lat"], 3) if d["lat"] is not None else None,
                         round(d["lon"], 3) if d["lon"] is not None else None,
                         indicator_bits(v["ind"], len(v["works"]))])
    out_res = []
    for aid, a in sorted(authors.items(), key=lambda kv: -kv[1]["w"]):
        if aid not in kept:
            continue
        inst_sorted = sorted(a["i"].items(), key=lambda kv: -kv[1])  # most recent first
        out_res.append([a["n"], a["o"], a["w"], a["y0"] if a["y0"] != 9999 else None,
                        a["y1"] or None, [[inst_index[i], yr] for i, yr in inst_sorted if i in inst_index],
                        species_bits(a["sp"], a["w"]), aid, indicator_bits(a["ind"], a["w"])])

    result = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "papers": n_works,
        "years": [min(years), max(years)] if years else None,
        "species": species_names,
        "indicators": indicator_names,
        "countries": sorted(c for c in countries if c),
        "inst_fields": ["name", "city", "country", "ror", "web", "papers", "researchers", "species_bits", "last_year", "openalex", "lat", "lon", "indicator_bits"],
        "res_fields": ["name", "orcid", "papers", "first_year", "last_year", "institutions [index, last year there]", "species_bits", "openalex", "indicator_bits"],
        "inst": out_inst,
        "res": out_res,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    save_json(OUTPUT, result)
    print(f"Wrote {OUTPUT.relative_to(ROOT)}: {n_works} papers, {len(out_inst)} institutions, "
          f"{len(out_res)} researchers ({OUTPUT.stat().st_size / 1e6:.1f} MB)")


# --------------------------------------------------------------------- quality check

def write_sample(n):
    """Random sample of included papers to check by hand (is each one really welfare?)."""
    import csv, random
    with gzip.open(WORKS, "rt", encoding="utf-8") as f:
        works = [json.loads(line) for line in f]
    rule_names = {"A": "core journal", "B": "strong phrase", "C": "welfare + animal", "D": "related term + topic"}
    out = DATA / "sample_check.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["is_welfare (fill in y/n)", "title", "year", "journal", "rule", "doi"])
        for w in random.sample(works, min(n, len(works))):
            wr.writerow(["", w["t"], w["y"], w["sn"], rule_names.get(w["r"], w["r"]), w["doi"]])
    print(f"Wrote {out.relative_to(ROOT)} with {min(n, len(works))} random papers")


# --------------------------------------------------------------------- main

# Settings that change which papers are collected (or their tags): changing
# them requires a fresh build. Everything else is a display setting, applied
# by re-assembling the directory from papers already collected.
FETCH_KEYS = ["work_types", "core_journals", "strong_phrases", "welfare_with_animal", "exclusion_phrases",
              "welfare_related_terms", "indicators", "welfare_topics", "animal_terms", "species",
              "species_not", "max_results_per_query"]
DISPLAY_KEYS = ["from_year", "species_min_share", "indicator_min_share", "subunit_rollup",
                "researcher_min_papers", "researcher_min_focus", "researcher_focus_exempt_papers"]
NEW_DISPLAY_KEYS = ["researcher_min_papers", "researcher_min_focus", "researcher_focus_exempt_papers"]  # added 27 Nov 2026


def _hash(obj):
    return hashlib.sha1(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:12]


def fetch_hash(cfg):
    return _hash({k: cfg.get(k) for k in FETCH_KEYS})


def display_hash(cfg):
    return _hash({k: cfg.get(k) for k in DISPLAY_KEYS})


def rules_hash(cfg):
    """Older fingerprint (all settings except from_year); kept only to
    recognise builds started before fetch_hash existed."""
    rules = {k: v for k, v in cfg.items() if k != "from_year" and k not in NEW_DISPLAY_KEYS}
    return _hash(rules)


def new_state(cfg):
    return {"started": now_iso(), "complete": False, "queries": {},
            "from_year": cfg.get("from_year"), "fetch_hash": fetch_hash(cfg)}


def make_api(args):
    key = os.environ.get("OPENALEX_API_KEY")
    if not key and args.base_url == DEFAULT_BASE:
        print("ERROR: set the OPENALEX_API_KEY environment variable (free key: https://openalex.org/settings/api)")
        return None
    return OpenAlex(args.base_url, key, os.environ.get("OPENALEX_MAILTO"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--aggregate-only", action="store_true", help="only rebuild docs/directory.json from cached papers")
    ap.add_argument("--restart", action="store_true", help="discard saved progress and start a fresh build")
    ap.add_argument("--sample", type=int, metavar="N", help="write N random included papers to data/sample_check.csv")
    ap.add_argument("--max-pages", type=int, default=None, help="stop after N API pages (for testing)")
    ap.add_argument("--base-url", default=os.environ.get("OPENALEX_BASE_URL", DEFAULT_BASE))
    args = ap.parse_args()

    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    DATA.mkdir(exist_ok=True)

    if args.sample:
        write_sample(args.sample)
        return 0
    if args.aggregate_only:
        aggregate(cfg)
        return 0

    state = load_json(STATE, None)
    fresh = args.restart or state is None
    if state is not None:
        if "fetch_hash" not in state:  # build started with an older version of this script
            old = state.get("rules_hash")
            if old is None or old == rules_hash(cfg):
                state["fetch_hash"] = fetch_hash(cfg)
            else:
                state["fetch_hash"] = "changed"
        state.setdefault("from_year", 2010)
        lowered = cfg.get("from_year") and state["from_year"] and int(cfg["from_year"]) < int(state["from_year"])
        if state["fetch_hash"] != fetch_hash(cfg) or lowered:
            print("The search rules in config.yaml have changed: starting a fresh build")
            fresh = True
    if not fresh and state and state.get("complete"):
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(state["completed_at"])).days
        if age < REFRESH_DAYS:
            if state.get("display_hash") != display_hash(cfg):
                print("Display settings changed: rebuilding the directory from papers already collected")
                api = make_api(args)
                if api is None:
                    return 1
                try:
                    fetch_author_stats(cfg, api)
                except BudgetExhausted as e:
                    print(f"Paused: {e}. The current directory stays online; the next run will continue.")
                    return 0
                aggregate(cfg)
                state["display_hash"] = display_hash(cfg)
                save_json(STATE, state)
                return 0
            print(f"Directory is up to date (built {age} days ago). Nothing to do.")
            return 0
        fresh = True
    if fresh:
        print("Starting a fresh build")
        for p in (WORKS, INSTS_SEEN):
            if p.exists():
                p.unlink()
        state = new_state(cfg)
        save_json(STATE, state)

    api = make_api(args)
    if api is None:
        return 1

    try:
        finished = fetch(cfg, api, state, args.max_pages)
        if not finished:
            print(f"Paused. Approximate API cost this run: ${api.spent:.3f}")
            return 0
        fetch_institution_details(api, load_json(INSTS_SEEN, {}))
        fetch_author_stats(cfg, api)
    except BudgetExhausted as e:
        print(f"Paused: {e}. Progress saved; the next run will continue. Cost this run: ${api.spent:.3f}")
        return 0

    aggregate(cfg)
    state["display_hash"] = display_hash(cfg)
    state["complete"] = True
    state["completed_at"] = now_iso()
    save_json(STATE, state)
    failed = [q for q in state["queries"].values() if q.get("failed")]
    if failed:
        print(f"WARNING: {len(failed)} queries were skipped; see data/state.json ('failed').")
    print(f"Build complete. Approximate API cost this run: ${api.spent:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
