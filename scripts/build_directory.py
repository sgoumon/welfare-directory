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

def build_queries(cfg):
    base = []
    if cfg.get("work_types"):
        base.append("type:" + "|".join(cfg["work_types"]))
    if cfg.get("from_year"):
        base.append(f"publication_year:>{int(cfg['from_year']) - 1}")

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
    for group in cfg.get("welfare_related_terms") or []:
        add("D", ["title_and_abstract.search:(" + " OR ".join(quote(t) for t in group) + ")",
                  "topics.id:" + topics])
    return queries


# --------------------------------------------------------------------- classification

class Classifier:
    def __init__(self, cfg):
        species = cfg.get("species") or {}
        self.species = {name: term_regex(terms) for name, terms in species.items()}
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

    def tag_species(self, text):
        return [name for name, rx in self.species.items() if rx and rx.search(text)]


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

    for q in build_queries(cfg):
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


# --------------------------------------------------------------------- aggregate

def load_exclusions():
    ids = set()
    if EXCLUDE.exists():
        for line in EXCLUDE.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                ids.add(line.rsplit("/", 1)[-1])
    return ids


def aggregate(cfg):
    insts_seen = load_json(INSTS_SEEN, {})
    details = load_json(INST_DETAILS, {})
    excluded = load_exclusions()
    rollup = cfg.get("subunit_rollup", "when_parent_present")
    species_names = list((cfg.get("species") or {}).keys())
    sp_bit = {n: 1 << i for i, n in enumerate(species_names)}

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

    insts, authors = {}, {}
    n_works = 0
    years = []
    with gzip.open(WORKS, "rt", encoding="utf-8") as f:
        for line in f:
            w = json.loads(line)
            n_works += 1
            y = w.get("y") or 0
            if y:
                years.append(y)
            sps = [s for s in (w.get("sp") or []) if s in sp_bit]
            for aid, name, orcid, inst_ids in w.get("au") or []:
                if aid in excluded or (orcid and orcid in excluded):
                    continue
                a = authors.setdefault(aid, {"n": name, "o": orcid, "w": 0, "y0": y or 9999, "y1": 0,
                                             "ny": 0, "sp": {}, "i": {}})
                a["w"] += 1
                for s in sps:
                    a["sp"][s] = a["sp"].get(s, 0) + 1
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
                    inst = insts.setdefault(iid, {"works": set(), "authors": set(), "sp": {}, "y1": 0})
                    if w["id"] not in inst["works"]:
                        for s in sps:
                            inst["sp"][s] = inst["sp"].get(s, 0) + 1
                    inst["works"].add(w["id"])
                    inst["authors"].add(aid)
                    inst["y1"] = max(inst["y1"], y)
                    prev = a["i"].get(iid, 0)
                    a["i"][iid] = max(prev, y)

    def species_bits(counts, n_papers):
        """Tag a species only if it makes up >= species_min_share of the papers,
        so a big university isn't tagged with every species it ever touched."""
        need = max(1, n_papers * float(cfg.get("species_min_share", 0.05)))
        bits = 0
        for s, c in counts.items():
            if c >= need:
                bits |= sp_bit[s]
        return bits

    inst_list = sorted(insts.items(), key=lambda kv: -len(kv[1]["works"]))
    inst_index = {iid: k for k, (iid, _) in enumerate(inst_list)}
    out_inst = []
    countries = set()
    for iid, v in inst_list:
        d = info(iid)
        countries.add(d["c"])
        out_inst.append([d["n"] or iid, d["city"], d["c"], d["ror"], d["web"], len(v["works"]),
                         len(v["authors"]), species_bits(v["sp"], len(v["works"])), v["y1"], iid,
                         round(d["lat"], 3) if d["lat"] is not None else None,
                         round(d["lon"], 3) if d["lon"] is not None else None])
    out_res = []
    for aid, a in sorted(authors.items(), key=lambda kv: -kv[1]["w"]):
        inst_sorted = sorted(a["i"].items(), key=lambda kv: -kv[1])  # most recent first
        out_res.append([a["n"], a["o"], a["w"], a["y0"] if a["y0"] != 9999 else None,
                        a["y1"] or None, [[inst_index[i], yr] for i, yr in inst_sorted if i in inst_index],
                        species_bits(a["sp"], a["w"]), aid])

    result = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "papers": n_works,
        "years": [min(years), max(years)] if years else None,
        "species": species_names,
        "countries": sorted(c for c in countries if c),
        "inst_fields": ["name", "city", "country", "ror", "web", "papers", "researchers", "species_bits", "last_year", "openalex", "lat", "lon"],
        "res_fields": ["name", "orcid", "papers", "first_year", "last_year", "institutions [index, last year there]", "species_bits", "openalex"],
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

def new_state():
    return {"started": now_iso(), "complete": False, "queries": {}}


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
    if state and state.get("complete"):
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(state["completed_at"])).days
        if age < REFRESH_DAYS:
            print(f"Directory is up to date (built {age} days ago). Nothing to do.")
            return 0
        fresh = True
    if fresh:
        print("Starting a fresh build")
        for p in (WORKS, INSTS_SEEN):
            if p.exists():
                p.unlink()
        state = new_state()
        save_json(STATE, state)

    key = os.environ.get("OPENALEX_API_KEY")
    if not key and args.base_url == DEFAULT_BASE:
        print("ERROR: set the OPENALEX_API_KEY environment variable (free key: https://openalex.org/settings/api)")
        return 1
    api = OpenAlex(args.base_url, key, os.environ.get("OPENALEX_MAILTO"))

    try:
        finished = fetch(cfg, api, state, args.max_pages)
        if not finished:
            print(f"Paused. Approximate API cost this run: ${api.spent:.3f}")
            return 0
        fetch_institution_details(api, load_json(INSTS_SEEN, {}))
    except BudgetExhausted as e:
        print(f"Paused: {e}. Progress saved; the next run will continue. Cost this run: ${api.spent:.3f}")
        return 0

    aggregate(cfg)
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
