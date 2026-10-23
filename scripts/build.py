#!/usr/bin/env python3
"""Rebuild site/index.html from sfendorsements.com + openballot.app.

Precedence for each (organization, race) cell:
    data/overrides.json  >  openballot.app  >  sfendorsements.com

If the merged data matches data/snapshot.json, nothing is written (exit 0).
Otherwise snapshot + page are rewritten and a Markdown change report is
written to --report for use as a pull-request body.

Usage: python scripts/build.py [--report build/report.md] [--offline]
  --offline  reuse the raw downloads in build/cache instead of fetching
"""
import argparse, base64, datetime, html as H, json, os, re, sys, time
import urllib.parse, urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(ROOT, *a)
UA = {"User-Agent": "sfelection26-endorsements-bot (+https://sfelection26.escf.com)"}
SFE_URL = "https://sfendorsements.com/"
OB_API = "https://www.openballot.app/api/public/graphql/"
OB_ELECTION = "2816b495-7026-436a-98ef-e5fd69d40841"
OB_GUIDE_PAGE = "https://www.openballot.app/elections/california-general-election-2026/guides/"
SECTION_ORDER = ["San Francisco propositions", "California propositions", "San Francisco offices",
                 "Congress & State Assembly", "Statewide offices"]
BLOC_ORDER = {"mod": 0, "prog": 1, "civic": 2, "gop": 3}


class BuildError(Exception):
    pass


def load(path):
    with open(P(path)) as f:
        return json.load(f)


def save(path, obj):
    os.makedirs(os.path.dirname(P(path)), exist_ok=True)
    with open(P(path), "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False, sort_keys=False)
        f.write("\n")


def fetch(url, cache_name, offline):
    cache = P("build", "cache", cache_name)
    if offline:
        with open(cache, "rb") as f:
            return f.read()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                body = r.read()
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "wb") as f:
                f.write(body)
            return body
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                raise BuildError(f"Could not fetch {url}: {e}")
            time.sleep(3 * (attempt + 1))


# ---------------------------------------------------------------- sources

def parse_sfe(raw):
    """Return ({table_header: {org: [values]}}, {org: about_url}, judicial {org: [Yes/No...]})."""
    t = raw.decode("utf-8", "replace")
    t = t[t.find("react-main"):]
    tables = re.findall(r"<table.*?</table>", t, flags=re.S)
    if len(tables) < 10:
        raise BuildError(f"sfendorsements.com layout changed: found {len(tables)} tables")

    def clean(c):
        c = re.sub(r"<br\s*/?>", "\n", c)
        c = re.sub(r"<[^>]+>", "\n", c)
        c = H.unescape(c)
        return [x.strip() for x in c.split("\n") if x.strip() and x.strip() != "Has reasoning"]

    cols, judicial, about = {}, {}, {}
    for tb in tables:
        rows = [[clean(c) for c in re.findall(r"<t[dh][ >].*?</t[dh]>", r, flags=re.S)]
                for r in re.findall(r"<tr.*?</tr>", tb, flags=re.S)]
        rows = [r for r in rows if r]
        # skip caption rows ("Major statewide races") above the real header row
        while rows and not " ".join(rows[0][0]).startswith("Organization"):
            rows = rows[1:]
        if not rows:
            continue
        hdr = [" ".join(c) for c in rows[0]]
        if hdr[0].startswith("Organization and link"):
            for tr in re.findall(r"<tr.*?</tr>", tb, flags=re.S):
                m = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', tr, flags=re.S)
                if m:
                    about[H.unescape(re.sub("<[^>]+>", "", m.group(2))).strip()] = m.group(1)
            continue
        if hdr[0] != "Organization":
            raise BuildError(f"sfendorsements.com table header not recognised: {hdr[:3]}")
        is_judicial = any(", Supreme Court" in h or "Court of Appeal" in h for h in hdr[1:])
        for r in rows[1:]:
            org = r[0][0] if r[0] else None
            if not org:
                continue
            for ci, h in enumerate(hdr[1:], start=1):
                v = r[ci] if ci < len(r) else []
                if not v:
                    continue
                if is_judicial:
                    judicial.setdefault(org, []).append(v[0])
                else:
                    cols.setdefault(h, {})[org] = v
    return cols, about, judicial


def ob_query(name, variables, cache_name, offline):
    url = OB_API + name + "?v=" + urllib.parse.quote(json.dumps(variables, separators=(",", ":")))
    data = json.loads(fetch(url, cache_name, offline))
    if "data" not in data:
        raise BuildError(f"openballot {name} returned no data: {str(data)[:200]}")
    return data["data"]


def fetch_ob(race_slugs, offline):
    """Return (brands {name: {...}}, positions {brand: {slug: value}}, srcs {(brand, slug): url}, candidates {slug: [names]})."""
    guides = ob_query("ComparePageContent_AddableGuidesQuery",
                      {"electionId": OB_ELECTION, "jurisdictionKey": "sf"}, "ob_guides.json", offline)["guides"]
    items = ob_query("ComparePageContent_BallotItemsQuery", {"electionId": OB_ELECTION},
                     "ob_items.json", offline)["ballotItems"]
    sf_guides = [g for g in guides if "sf" in (g.get("primaryJurisdictions") or [])]
    if len(sf_guides) < 20:
        raise BuildError(f"openballot returned only {len(sf_guides)} San Francisco guides")

    brands = {}
    for g in sf_guides:
        b = g["brand"]
        logo = (g.get("logoImage") or b.get("logoImage") or {}) or {}
        brands[b["name"]] = {
            "guide": g["id"], "slug": b["slug"], "desc": b.get("shortDescription"),
            "url": g.get("sourceUrl"), "logo_url": ((logo.get("sourceUrl") or {}).get("url")),
            "tags": [t["tag"]["slug"] for t in b.get("slotTags") or []],
        }

    def is_sf(it):
        blob = json.dumps(it.get("race") or it.get("measure"))
        return "state:ca" in blob and ("county:san_francisco" in blob or "county:" not in blob and "place:" not in blob)

    by_slug = {}
    for it in items:
        if it["slug"] in race_slugs and is_sf(it):
            if it["slug"] in by_slug:
                raise BuildError(f"openballot: ambiguous ballot item {it['slug']}")
            by_slug[it["slug"]] = it
    missing = set(race_slugs) - set(by_slug)
    if missing:
        raise BuildError(f"openballot: ballot items not found: {sorted(missing)}")
    slug_of = {it["id"]: s for s, it in by_slug.items()}
    cand = {c["id"]: c["person"]["fullName"] for it in by_slug.values() if it["race"]
            for c in it["race"]["candidacies"]}
    candidates = {s: [c["person"]["fullName"] for c in it["race"]["candidacies"]]
                  for s, it in by_slug.items() if it["race"]}

    gid_to_brand = {v["guide"]: k for k, v in brands.items()}
    ids = [it["id"] for it in by_slug.values()]
    gids = list(gid_to_brand)
    positions, srcs = {}, {}
    for k in range(0, len(gids), 7):
        chunk = gids[k:k + 7]
        res = ob_query("ComparePageContent_ViewpointsCachedQuery", {"ballotItemIds": ids, "guideIds": chunk},
                       f"ob_vp_{k // 7:02d}.json", offline)
        for g in res["guides"]:
            brand = gid_to_brand.get(g["id"])
            for v in g["targetViewpoints"]:
                slug = slug_of.get(v["targetBallotItemId"])
                if not brand or not slug:
                    continue
                sel = v["selections"]
                if not sel:
                    continue
                if any(s["candidacyId"] for s in sel):
                    val = [cand.get(s["candidacyId"]) for s in sel if s["weight"] > 0 and s["candidacyId"]]
                    val = [x for x in val if x]
                    if not val:
                        continue
                else:
                    w = [s["weight"] for s in sel]
                    if all(x > 0 for x in w):
                        val = "Yes"
                    elif all(x < 0 for x in w):
                        val = "No"
                    else:
                        continue  # neutral / no position
                positions.setdefault(brand, {})[slug] = val
                if v.get("sourceUrl"):
                    srcs[(brand, slug)] = v["sourceUrl"]
        if not offline:
            time.sleep(0.5)
    if sum(len(v) for v in positions.values()) < 200:
        raise BuildError("openballot returned suspiciously few positions")
    return brands, positions, srcs, candidates


# ---------------------------------------------------------------- merging

def name_key(n):
    t = re.sub(r'"[^"]*"', "", n).replace(" III", "").replace(" Jr.", "").strip().lower().split()
    return t[-1] if t else n.lower()


def tidy_name(n, aliases):
    n = aliases.get(n, n)
    return re.sub(r"\s+[A-Z]\.(?=\s)", "", n)  # drop middle initials: "Shirley N. Weber" -> "Shirley Weber"


def canon_list(vals, preferred, aliases):
    """Map names to the spelling already used in the race (by last name), else tidy them."""
    out = []
    for v in vals:
        v = tidy_name(v, aliases)
        out.append(preferred.get(name_key(v), v))
    return out


def same(a, b):
    if isinstance(a, list) and isinstance(b, list):
        return sorted(map(name_key, a)) == sorted(map(name_key, b))
    return a == b


def build(offline):
    cfg_orgs, cfg_races, ov = load("data/orgs.json"), load("data/races.json"), load("data/overrides.json")
    aliases = ov.get("names", {})
    races = cfg_races["races"]
    sfe_cols, sfe_about, sfe_judicial = parse_sfe(fetch(SFE_URL, "sfe.html", offline))
    brands, ob_pos, ob_src, ob_cands = fetch_ob({r["ob"] for r in races if r.get("ob")}, offline)

    notes = {"new_orgs": [], "skipped": [], "conflicts": [], "warnings": [], "unused_overrides": []}

    # --- organizations: registry plus anything new on either source.
    # The same group often appears on both sources under slightly different names
    # ("District 11 Democratic Club" vs "San Francisco District 11 Democratic Club"),
    # so new names are matched on a normalised form before anything is added.
    orgs = [dict(o) for o in cfg_orgs["orgs"]]
    excl = set(cfg_orgs.get("exclude_openballot", [])) | set(cfg_orgs.get("exclude_sfendorsements", []))
    sfe_with_data = {org for col in sfe_cols.values() for org in col} | set(sfe_judicial)

    def org_key(n):
        n = n.lower().replace("&", " and ")
        n = re.sub(r"[^a-z0-9 ]", " ", n)
        n = re.sub(r"\b(san francisco|sf|the|of|lgbtq|local|association|assn)\b", " ", n)
        n = re.sub(r"\bdemocratic\b", "dem", n)
        return " ".join(n.split())

    def find(key):
        return next((o for o in orgs if key in {org_key(x) for x in (o.get("sfe"), o.get("ob"), o["name"]) if x}), None)

    for name in sorted(sfe_with_data - {o["sfe"] for o in orgs if o.get("sfe")} - excl):
        match = find(org_key(name))
        if match and not match.get("sfe"):
            match["sfe"] = name
            notes["new_orgs"].append(f"{match['name']}: linked to its sfendorsements.com listing \"{name}\"")
        elif not match:
            orgs.append({"name": name, "sfe": name, "ob": None, "bloc": None, "featured": False, "auto": True})
            notes["new_orgs"].append(f"{name} (sfendorsements.com)")
    for name in sorted(set(ob_pos) - {o["ob"] for o in orgs if o.get("ob")} - excl):
        tags = brands[name]["tags"]
        if not tags or {"individual", "informational"} & set(tags):
            notes["skipped"].append(name)
            continue
        match = find(org_key(name))
        if match and not match.get("ob"):
            match["ob"] = name
            if match.get("auto"):
                match["name"] = name  # OpenBallot's name is usually the fuller one
            notes["new_orgs"].append(f"{match['name']}: linked to its openballot.app listing \"{name}\"")
        elif not match:
            orgs.append({"name": name, "sfe": None, "ob": name, "bloc": None, "featured": False, "auto": True})
            notes["new_orgs"].append(f"{name} (openballot.app)")
    for o in cfg_orgs["orgs"]:
        if o.get("sfe") and o["sfe"] not in sfe_with_data and o.get("ob") not in ob_pos:
            notes["warnings"].append(f"{o['name']}: no positions found on either source")
    dupes = sorted({o["name"] for o in orgs if sum(p["name"] == o["name"] for p in orgs) > 1})
    if dupes:
        raise BuildError(f"duplicate organization names: {dupes}. Link them in data/orgs.json.")
    by_name = {o["name"]: o for o in orgs}

    # --- cells
    cells = {}  # race id -> {org name: (value, src)}
    for r in races:
        rid = r["id"]
        cells[rid] = {}
        # preferred spellings: sfendorsements first (familiar names), then openballot ballot names
        pref = {}
        for col in [sfe_cols.get(r.get("sfe"), {})]:
            for vals in col.values():
                for v in vals:
                    pref.setdefault(name_key(v), tidy_name(v, aliases))
        for o in orgs:
            sv = ov_v = None
            if r.get("judicial"):
                jv = sfe_judicial.get(o.get("sfe"))
                if jv and all(x == "Yes" for x in jv):
                    sv = "Yes"
            elif o.get("sfe") and r.get("sfe"):
                raw = sfe_cols.get(r["sfe"], {}).get(o["sfe"])
                if raw:
                    sv = raw[0] if r["kind"] == "prop" else canon_list(raw, pref, aliases)
            obv = ob_pos.get(o.get("ob"), {}).get(r.get("ob")) if o.get("ob") and r.get("ob") else None
            if obv is not None and r["kind"] == "cand":
                obv = canon_list(obv, pref, aliases)
            if sv is not None and obv is not None and not same(sv, obv):
                notes["conflicts"].append(f"{o['name']} / {rid}: sfendorsements says {fmt(sv)}, openballot says {fmt(obv)} (using openballot)")
            elif isinstance(sv, list) and isinstance(obv, list):
                obv = sv  # same picks: keep sfendorsements' order (it preserves rankings like #1/#2)
            if isinstance(obv, list) and sv is None:
                obv = sorted(obv, key=name_key)  # stable order so reruns don't churn
            val = obv if obv is not None else sv
            src = ob_src.get((o.get("ob"), r.get("ob"))) if obv is not None else None
            if val is not None:
                cells[rid][o["name"]] = (val, src)

    # --- overrides
    for p in ov.get("positions", []):
        if p["race"] not in cells or p["org"] not in by_name:
            notes["unused_overrides"].append(f"{p['org']} / {p['race']}: race or organization not found")
            continue
        cur = cells[p["race"]].get(p["org"])
        if cur and p["value"] is not None and same(cur[0], p["value"]):
            notes["unused_overrides"].append(f"{p['org']} / {p['race']}: sources now agree ({fmt(p['value'])}); override can be removed")
        if p["value"] is None:
            cells[p["race"]].pop(p["org"], None)
        else:
            cells[p["race"]][p["org"]] = (p["value"], p.get("source"))

    # --- bloc for new organizations: compare picks with existing progressive / moderate groups
    def agree(a, b):
        if isinstance(a, str) or isinstance(b, str):
            return 1.0 if a == b else 0.0
        return 1.0 if set(map(name_key, a)) & set(map(name_key, b)) else 0.0
    for o in orgs:
        if o.get("bloc"):
            continue
        score, n_pos = {}, sum(1 for c in cells.values() if o["name"] in c)
        for bl in ("prog", "mod"):
            tot = n = 0
            for c in cells.values():
                if o["name"] not in c:
                    continue
                for other, (v, _) in c.items():
                    if by_name[other].get("bloc") == bl and not by_name[other].get("auto"):
                        tot += agree(c[o["name"]][0], v); n += 1
            score[bl] = tot / n if n else None
        if n_pos < 4 or None in score.values():
            o["bloc"] = "civic"
        else:
            d = score["prog"] - score["mod"]
            o["bloc"] = "prog" if d > .15 else "mod" if d < -.15 else "civic"
        notes["new_orgs"] = [x + (f" -> sorted as {o['bloc']}" if x.startswith(o["name"] + " (") else "")
                             for x in notes["new_orgs"]]

    # --- organization links, descriptions, logos
    for o in orgs:
        b = brands.get(o.get("ob")) or {}
        o["url"] = o.get("url") or b.get("url") or sfe_about.get(o.get("sfe")) or (
            OB_GUIDE_PAGE + b["slug"] if b.get("slug") else SFE_URL)
        o["desc"] = b.get("desc")
        o["logo_slug"] = b.get("slug")
        o["logo_url"] = b.get("logo_url")

    # --- ordering: featured first, then bloc, then registry order
    order = sorted(range(len(orgs)), key=lambda i: (not orgs[i].get("featured"),
                   0 if orgs[i].get("featured") else BLOC_ORDER[orgs[i]["bloc"]], bool(orgs[i].get("auto")), i))
    orgs = [orgs[i] for i in order]

    snapshot = {
        "orgs": [{k: o.get(k) for k in ("name", "bloc", "featured", "url", "desc")} for o in orgs],
        "races": [{"id": r["id"], "positions": {o["name"]: cells[r["id"]][o["name"]][0]
                                               for o in orgs if o["name"] in cells[r["id"]]},
                   "sources": {o["name"]: cells[r["id"]][o["name"]][1]
                               for o in orgs if o["name"] in cells[r["id"]] and cells[r["id"]][o["name"]][1]}}
                  for r in races],
        "notice": ov.get("notice", ""),
        "races_meta": [{k: r.get(k) for k in ("id", "section", "kind", "desc", "note")} for r in races],
    }
    return snapshot, orgs, notes


def fmt(v):
    return ", ".join(v) if isinstance(v, list) else str(v)


# ---------------------------------------------------------------- output

def logo_data_uri(o):
    slug = o.get("logo_slug")
    if not slug or o.get("no_logo"):
        return None
    path = P("assets", "logos", slug + ".png")
    if not os.path.exists(path) and o.get("logo_url"):
        try:
            from io import BytesIO
            from PIL import Image
            with urllib.request.urlopen(urllib.request.Request(urllib.parse.quote(o["logo_url"], safe=":/?=&%"),
                                                               headers=UA), timeout=30) as r:
                im = Image.open(BytesIO(r.read())).convert("RGBA")
            im.thumbnail((56, 56))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            im.save(path, "PNG")
        except Exception as e:  # noqa: BLE001
            print(f"  logo for {o['name']} not available: {e}", file=sys.stderr)
            return None
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()


def render(snapshot, orgs, updated):
    idx = {o["name"]: i for i, o in enumerate(orgs)}
    page_orgs = [{"full": o["name"], "short": o["name"], "bloc": o["bloc"], "mine": bool(o.get("featured")),
                  "url": o["url"], "desc": o.get("desc"), "logo": logo_data_uri(o)} for o in orgs]
    meta = {m["id"]: m for m in snapshot["races_meta"]}
    page_races = []
    for r in snapshot["races"]:
        m = meta[r["id"]]
        pr = {"section": m["section"], "name": r["id"], "label": r["id"], "kind": m["kind"],
              "pos": {str(idx[k]): v for k, v in r["positions"].items()},
              "src": {str(idx[k]): v for k, v in r["sources"].items()}}
        if m.get("desc"):
            pr["desc"] = m["desc"]
        if m.get("note"):
            pr["note"] = m["note"]
        page_races.append(pr)
    data = json.dumps({"orgs": page_orgs, "races": page_races}, ensure_ascii=False).replace("</", "<\\/")
    with open(P("template", "page.html")) as f:
        page = f.read()
    for k, v in {"__DATA__": data, "__N__": str(len(orgs)), "__UPDATED__": H.escape(updated),
                 "__NOTICE__": H.escape(snapshot["notice"])}.items():
        page = page.replace(k, v)
    os.makedirs(P("site"), exist_ok=True)
    with open(P("site", "index.html"), "w") as f:
        f.write(page)


def diff_report(old, new, notes, updated):
    lines = [f"Automated data refresh ({updated}).", ""]
    total = lambda s: sum(len(r["positions"]) for r in s["races"]) if s else 0
    t_old, t_new = total(old), total(new)
    if old and t_new < t_old * 0.9:
        lines += [f"**WARNING: total positions dropped from {t_old} to {t_new}.** A source may have changed "
                  "its layout or lost data. Check carefully before merging.", ""]
    lines.append(f"Positions: {t_old} -> {t_new}. Organizations: {len(old['orgs']) if old else 0} -> {len(new['orgs'])}.")
    lines.append("")
    changes = []
    old_r = {r["id"]: r["positions"] for r in (old or {}).get("races", [])}
    for r in new["races"]:
        a, b = old_r.get(r["id"], {}), r["positions"]
        for org in sorted(set(a) | set(b)):
            if org in a and org in b and same(a[org], b[org]):
                continue
            changes.append(f"| {r['id']} | {org} | {fmt(a[org]) if org in a else ''} | {fmt(b[org]) if org in b else ''} |")
    if changes:
        lines += ["### Position changes", "", "| Race / measure | Organization | Before | After |", "|---|---|---|---|", *changes, ""]
    sections = [("new_orgs", "New organizations (please check the bloc and that they belong)"),
                ("skipped", "Skipped OpenBallot guides (look like individuals or explainers; add to data/orgs.json to include)"),
                ("conflicts", "Source conflicts"),
                ("unused_overrides", "Overrides to review"),
                ("warnings", "Warnings")]
    for key, title in sections:
        if notes[key]:
            lines += [f"### {title}", "", *[f"- {x}" for x in notes[key]], ""]
    lines += ["Merging publishes the site. To correct a position, edit `data/overrides.json` on `main` "
              "(not on this branch, which each run regenerates), then re-run the workflow."]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default=P("build", "report.md"))
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--force", action="store_true", help="rewrite the page even if data is unchanged")
    a = ap.parse_args()
    try:
        snapshot, orgs, notes = build(a.offline)
    except BuildError as e:
        print(f"BUILD FAILED: {e}", file=sys.stderr)
        sys.exit(1)
    old = load("data/snapshot.json") if os.path.exists(P("data", "snapshot.json")) else None
    comparable = lambda s: {k: v for k, v in s.items() if k != "updated"} if s else None
    if comparable(old) == snapshot and not a.force:
        print("No changes.")
        return
    updated = datetime.datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%B %-d, %Y at %-I:%M %p PT")
    if comparable(old) == snapshot and old:
        updated = old.get("updated", updated)
    render(snapshot, orgs, updated)
    report = diff_report(old, snapshot, notes, updated)
    save("data/snapshot.json", {**snapshot, "updated": updated})
    os.makedirs(os.path.dirname(a.report), exist_ok=True)
    with open(a.report, "w") as f:
        f.write(report + "\n")
    print(report)


if __name__ == "__main__":
    main()
