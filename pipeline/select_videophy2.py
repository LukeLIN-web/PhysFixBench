"""Pick PhysFixBench candidates from the VideoPhy2 test set (--csv: videophy2_test.csv of the VideoPhy2 test release;
--videos: its videos, saved as vp2_<csv row index>_<generator>.mp4): semantic adherence (SA) rated high, physical commonsense (PC) rated low, and at least one
physics rule the annotator marked violated. The rules come two ways: picked from the rule list shown with the video
(physics_rules_unfollowed, ids V1, V2, ...) or written by the annotator (human_violated_rules, ids W1, W2, ...);
gpt_verify.py checks each one against the frames. VideoCrafter's low-resolution output is dropped (the curation
criteria in the README).

    python3 pipeline/select_videophy2.py --csv <videophy2_test.csv> --videos <videos dir> --pc-max 2 \
        --out pipeline/out/videophy2_candidates_pc2.json
"""

import argparse
import ast
import collections
import csv
import json
import os

def literal(s):
    s = s.strip()
    return ast.literal_eval(s) if s and s != "None" else None


def video_id(model, idx):
    """The id the videos are saved under: the csv row index and the generator."""
    return f"vp2_{idx:04d}_{model}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="videophy2_test.csv")
    ap.add_argument("--videos", required=True, help="directory of vp2_<row>_<generator>.mp4")
    ap.add_argument("--sa-min", type=int, default=4, help="keep videos whose SA is at least this")
    ap.add_argument("--pc-max", type=int, default=3, help="keep videos whose PC is at most this")
    ap.add_argument("--drop-models", default="videocrafter", help="comma-separated generators to leave out")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    drop = set(args.drop_models.split(",")) if args.drop_models else set()
    out = []
    for idx, row in enumerate(csv.DictReader(open(args.csv))):
        sa, pc, model = int(row["sa"]), int(row["pc"]), row["model_name"]
        if sa < args.sa_min or pc > args.pc_max or model in drop:
            continue
        listed = literal(row["physics_rules_unfollowed"]) or []
        written = [r for r in literal(row["human_violated_rules"]) or [] if r not in listed]
        if not listed and not written:
            continue
        rules = {f"V{i}": r for i, r in enumerate(listed, 1)} | {f"W{i}": r for i, r in enumerate(written, 1)}
        law_of = literal(row["metadata_rules"]) or {}  # rule -> physical law(s), assigned by Gemini-Flash
        rule_laws = {k: [law_of[r]] if isinstance(law_of.get(r), str) else law_of.get(r) or [] for k, r in rules.items()}
        vid = video_id(model, idx)
        path = os.path.join(args.videos, vid + ".mp4")
        out.append(dict(
            source="videophy2", video_id=vid, model=model, filename=vid + ".mp4", prompt=row["caption"],
            path=path if os.path.exists(path) else None,
            human=dict(SA=sa, PC=pc, rules=rules, rule_laws=rule_laws),
            category=row["category"], action=row["action"], is_hard=int(row["is_hard"] or 0),
        ))
    out.sort(key=lambda r: (r["human"]["PC"], -r["human"]["SA"], r["video_id"]))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)

    print(f"{len(out)} candidates (SA >= {args.sa_min}, PC <= {args.pc_max}, some rule violated, without "
          f"{sorted(drop)}); missing mp4: {sum(r['path'] is None for r in out)}; "
          f"distinct prompts: {len({r['prompt'] for r in out})}")
    print("by PC:", dict(sorted(collections.Counter(r["human"]["PC"] for r in out).items())))
    print("by model:", dict(collections.Counter(r["model"] for r in out).most_common()))
    print("rules per video:", dict(sorted(collections.Counter(len(r["human"]["rules"]) for r in out).items())))


if __name__ == "__main__":
    main()
