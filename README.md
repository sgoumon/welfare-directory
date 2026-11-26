# Animal Welfare Research Directory

A searchable directory of institutions and researchers working on animal welfare science,
generated automatically from publication records in [OpenAlex](https://openalex.org).
Built for the tool box of sebastiengoumon.com.

## How it works

```
OpenAlex API ──► scripts/build_directory.py ──► docs/directory.json ──► docs/index.html (search page)
                       ▲                                                     │
                 config.yaml (rules)                              embedded in Google Sites
                 exclude.txt (removals)
```

1. **Collect papers** (2020 onwards). A paper is included if it meets **any one** of these rules, all editable in `config.yaml`,
   matched in the title and abstract, in any journal:
   - **A.** core journals (currently switched off: no journal is included automatically)
   - **B.** a strong welfare phrase ("animal welfare", "welfare assessment", "grimace scale"…) and a mention of an animal
   - **C.** "welfare" + an animal word (farmed, companion, laboratory, zoo and wild animals, plus class words like *mammal* or *bird*),
     minus papers about economic/social welfare
   - **D.** a welfare indicator or procedure (behavioural, physiological, health, cognitive/affective; castration, stunning…)
     **and** classified by OpenAlex under a welfare topic
2. **Aggregate.** Authors' affiliations on those papers become institutions; departments are merged into their university when both are listed.
3. **Publish.** A small JSON file feeds a search page with filters for country and species.

Inclusion is decided only from the title and abstract (what authors wrote), not OpenAlex's auto-generated keywords.

## One-time setup (about 30 minutes)

1. **OpenAlex API key (free).** Create an account at openalex.org and copy your key from
   <https://openalex.org/settings/api>. A free key gives about $1/day of API use; this project only uses the free allowance.
2. **GitHub repository.** Create a free GitHub account, then a new **public** repository
   (e.g. `welfare-directory`). Upload all files in this folder, keeping the folder structure.
   Tip: the `.github` folder is hidden on Mac/Windows. If it doesn't upload, use
   *Add file → Create new file*, type `.github/workflows/update.yml` as the name and paste the file's contents.
3. **Store the key.** In the repository: *Settings → Secrets and variables → Actions → New repository secret*.
   Name: `OPENALEX_API_KEY`, value: your key.
4. **Turn on the website.** *Settings → Pages → Build and deployment → Deploy from a branch →* branch `main`, folder `/docs`.
   Your page will be at `https://YOUR-USERNAME.github.io/welfare-directory/`.
5. **Add your email.** In `docs/index.html`, replace `CONTACT_EMAIL` (in the line `const CONTACT_EMAIL = ...`) with the address for corrections.
6. **First build.** *Actions → Update directory → Run workflow.* The first full build may take **a few days**:
   when the daily budget runs out the script saves its progress, and the daily schedule continues the next day.
   After that it rebuilds automatically every 28 days.
7. **Embed in Google Sites.** On your tool box page: *Insert → Embed → By URL*, paste the GitHub Pages address,
   choose "Whole page", then drag the embed box to make it tall (about 1,200 px works well).

## Checking quality (recommended after the first build)

Download the repository (or clone it) and run:

```
pip install pyyaml
python scripts/build_directory.py --sample 100
```

This writes `data/sample_check.csv` with 100 random included papers. Mark each `y`/`n`.
If too many `n`, look at the *rule* column to see which rule lets them in, and tighten that part of `config.yaml`.
To check what is **missed**, search the page for groups you know.

After changing the rules in `config.yaml`, the next run notices and starts a fresh build automatically
(the old directory stays online until the new one is complete). Raising `from_year` needs no rebuild.

## Corrections and removals

Add the person's OpenAlex author ID (e.g. `A5061303796`, shown in their OpenAlex profile link) or ORCID to `exclude.txt`.
Institution IDs (`I...`) can be excluded the same way. The change takes effect at the next build, or immediately with
`python scripts/build_directory.py --aggregate-only` followed by a commit of `docs/directory.json`.

## Files

| File | Purpose |
|---|---|
| `config.yaml` | which papers count as welfare research; species tags; institution handling |
| `exclude.txt` | removal requests |
| `scripts/build_directory.py` | fetches from OpenAlex and builds the data |
| `docs/index.html` | the search page |
| `docs/directory.json` | generated data (don't edit by hand) |
| `data/` | progress and cached papers between runs |
| `.github/workflows/update.yml` | daily schedule on GitHub Actions |

## Things to verify

- Built and tested on 24 Nov 2026 against a mock of the OpenAlex API using real response formats; the live API could
  not be reached from the build environment for a full run. Watch the first workflow log for errors.
- If a query is listed as **SKIPPED** in the log, OpenAlex did not understand it (this happened once in testing with a
  long list of terms). Split that term group in `config.yaml` into two smaller groups.
- The workflow uses `actions/checkout@v5` and `actions/setup-python@v6`, which run on Node.js 24.

Data: OpenAlex (CC0). Papers counted from `from_year` in `config.yaml` (currently 2020).
