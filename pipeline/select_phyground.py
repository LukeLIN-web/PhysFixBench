"""Pick PhysFixBench candidates from the PhyGround human study: prompt alignment (SA) rated high, at least one
physics law rated low. Scores are the mean over the post-QC annotators of the PhyGround human-eval database (the
leaderboard's per-video aggregation). A video whose every law is low is dropped as a whole-video collapse
(the curation criteria in the README).

--db is the PhyGround human-eval database (human_eval_filtered.db: videos, assignments, annotations,
annotation_items, excluded_annotators); --videos is the directory the rating app served videos from, one
subdirectory per model.

    python3 pipeline/select_phyground.py --db <human_eval_filtered.db> --videos <videos dir> \
        --sa-min 3.5 --law-max 2.5 --out pipeline/out/phyground_candidates_sa3.5.json
"""

import argparse
import collections
import json
import os
import sqlite3

GENERAL = ("SA", "PTV", "persistence")


def video_path(videos, dataset, filename):
    """The file the rating app served: <videos>/<model>/<file>. The two LTX models' entries there are now broken
    symlinks into <model>-humaneval/, so fall back to that directory."""
    for d in (dataset, f"{dataset}-humaneval"):
        p = os.path.join(videos, d, filename)
        if os.path.exists(p):
            return p
    return None


def load(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    videos = {vid: dict(filename=fn, model=ds, prompt=pr, laws=json.loads(pl))
              for vid, fn, ds, pr, pl in conn.execute("SELECT id, filename, dataset, prompt, physical_laws FROM videos")}
    rows = conn.execute("""
        SELECT a.video_id, an.id, ai.dimension, ai.law, ai.score, an.note
        FROM assignments a
        JOIN annotations an ON an.assignment_id = a.id
        JOIN annotation_items ai ON ai.annotation_id = an.id
        WHERE a.status = 'completed'
          AND a.annotator_id NOT IN (SELECT annotator_id FROM excluded_annotators)""").fetchall()
    conn.close()
    scores = collections.defaultdict(lambda: collections.defaultdict(list))
    notes = collections.defaultdict(dict)
    for vid, anid, dim, law, score, note in rows:
        scores[vid][law or dim].append(score)
        if note and note.strip():
            notes[vid][anid] = note.strip()
    return videos, scores, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="PhyGround human_eval_filtered.db")
    ap.add_argument("--videos", required=True, help="video directory, one subdirectory per model")
    ap.add_argument("--sa-min", type=float, default=4.0, help="keep videos whose mean SA is at least this")
    ap.add_argument("--law-max", type=float, default=2.5, help="a law counts as low when its mean is at most this")
    ap.add_argument("--keep-collapse", action="store_true", help="keep videos whose every law is low")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    videos, scores, notes = load(args.db)
    out = []
    for vid, sc in scores.items():
        mean = {k: round(sum(v) / len(v), 3) for k, v in sc.items()}
        laws = {k: m for k, m in mean.items() if k not in GENERAL}
        if not laws or mean.get("SA", 0) < args.sa_min:
            continue
        low = sorted((k for k, m in laws.items() if m <= args.law_max), key=laws.get)
        if not low or (len(low) == len(laws) and not args.keep_collapse):
            continue
        v = videos[vid]
        out.append(dict(
            video_id=vid, model=v["model"], filename=v["filename"], prompt=v["prompt"],
            path=video_path(args.videos, v["model"], v["filename"]),
            n_annotators=len(sc["SA"]),
            general={k: mean[k] for k in GENERAL if k in mean},
            law_mean=laws, law_scores={k: sc[k] for k in laws}, low_laws=low,
            min_law=laws[low[0]], sa_minus_min_law=round(mean["SA"] - laws[low[0]], 3),
            notes=list(notes[vid].values()),
        ))
    out.sort(key=lambda r: (-r["sa_minus_min_law"], r["video_id"]))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)

    by_model = collections.Counter(r["model"] for r in out)
    by_law = collections.Counter(l for r in out for l in r["low_laws"])
    print(f"{len(out)} candidates (SA >= {args.sa_min}, some law <= {args.law_max}"
          f"{'' if args.keep_collapse else ', not every law low'}); missing mp4: {sum(r['path'] is None for r in out)}")
    print("by model:", dict(by_model.most_common()))
    print("low laws:", dict(by_law.most_common()))
    print(f"single annotator: {sum(r['n_annotators'] == 1 for r in out)}; with a note: {sum(bool(r['notes']) for r in out)}")


if __name__ == "__main__":
    main()
