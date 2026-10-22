"""Merge several gpt_verify.py passes over the same candidates (PhyGround or VideoPhy2) into one ranked list for human
review.

A single GPT-5.5 call flips on borderline videos, so each video gets one keep/reject vote per pass. Rank by keep
votes, then by mean severity and mean confidence over the keep votes; take the top --top videos that have at least
one keep vote. A video every pass kept is "confident"; one some passes rejected is "borderline" and carries every
pass's verdict so a reviewer can see what the disagreement was about. The fixing fields come from
the keep vote with the highest confidence.

VideoPhy2 has the same prompt rendered by several generators, and some of its prompts are also PhyGround's.
--one-per-prompt keeps only the best-ranked video of each prompt; --seen takes another run's <out>.json, and a video
whose prompt was already picked there ranks after the other videos with the same number of keep votes.

    python3 pipeline/vote.py pipeline/out/gpt55_sa3.5 pipeline/out/gpt55_sa3.5_r2 pipeline/out/gpt55_sa3.5_r3 \
        --top 100 --out pipeline/out/phyground_top100
    python3 pipeline/vote.py pipeline/out/gpt55_vp2 pipeline/out/gpt55_vp2_r2 pipeline/out/gpt55_vp2_r3 --top 100 \
        --one-per-prompt --seen pipeline/out/phyground_top100.json --out pipeline/out/videophy2_top100
"""

import argparse
import collections
import csv
import glob
import json
import os

from gpt_verify import claim_checks, human_brief, source


def norm(prompt):
    return " ".join(prompt.lower().split())


def mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(sum(xs) / len(xs), 3) if xs else None


def verdict_line(g):
    """One pass's call in a line: keep with its error, or the reject reason with the evidence it rests on."""
    if g.get("keep") is True:
        e = (g.get("errors") or [{}])[0]
        return f"KEEP sev={g.get('severity')} [{e.get('t_start')}-{e.get('t_end')}s] {e.get('description', '')}"
    checks = "; ".join(f"{k}={x.get('verdict')}: {x.get('evidence', '')}" for k, x in claim_checks(g))
    return f"REJECT {g.get('reject_reason')}: {checks}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("passes", nargs="+", help="gpt_verify.py --out-dir of each pass")
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--one-per-prompt", action="store_true", help="keep only the best-ranked video of each prompt")
    ap.add_argument("--seen", nargs="*", default=[], help="<out>.json of other runs; their prompts rank later")
    ap.add_argument("--out", required=True, help="output prefix; writes <out>.json and <out>.csv")
    args = ap.parse_args()

    votes = collections.defaultdict(list)
    for d in args.passes:
        for f in glob.glob(os.path.join(d, "records", "*.json")):
            rec = json.load(open(f))
            if rec.get("gpt"):
                votes[os.path.basename(f)[:-5]].append(rec)
    n_pass = len(args.passes)
    short = [k for k, v in votes.items() if len(v) < n_pass]
    if short:
        print(f"warning: {len(short)} videos have fewer than {n_pass} parsed votes, e.g. {short[:3]}")

    seen = {norm(x["flawed_video"]["prompt"]) for f in args.seen for x in json.load(open(f))}
    items, brief = [], {}
    for key, recs in votes.items():
        keeps = [r["gpt"] for r in recs if r["gpt"].get("keep") is True]
        rejects = [r["gpt"] for r in recs if r["gpt"].get("keep") is not True]
        c = recs[0]["candidate"]
        best = max(keeps, key=lambda g: (g.get("confidence") or 0, g.get("severity") or 0)) if keeps else None
        errs = best.get("errors") or [] if best else []
        cats = set()
        for e in errs:  # GPT-5.5 writes a category either as one string or as a list
            cat = e.get("category") or []
            cats.update([cat] if isinstance(cat, str) else cat)
        src = source(c)
        checks = "rule_checks" if src == "videophy2" else "law_checks"
        brief[key] = human_brief(c)
        items.append(dict(
            key=key, keep_votes=len(keeps), n_votes=len(recs),
            tier="confident" if len(keeps) == n_pass else "borderline",
            mean_severity=mean([g.get("severity") for g in keeps]),
            mean_confidence=mean([g.get("confidence") for g in keeps]),
            reject_reasons=[g.get("reject_reason") for g in rejects],
            flawed_video=dict(path=c["path"], model=c["model"], filename=c["filename"], prompt=c["prompt"],
                              **{f"{src}_video_id": c["video_id"]}),
            prompt_seen=norm(c["prompt"]) in seen,
            human=c["human"] if src == "videophy2" else dict(
                n_annotators=c["n_annotators"], general=c["general"], law_mean=c["law_mean"], low_laws=c["low_laws"],
                notes=c["notes"]),
            error_evidence=[dict(t_start=e.get("t_start"), t_end=e.get("t_end"),
                                 key_timestamps=e.get("key_timestamps")) for e in errs],
            error_description=" | ".join(e.get("description", "") for e in errs),
            error_category=sorted(cats),
            violated_laws=sorted({l for e in errs for l in e.get("laws") or []}),
            correction_instruction=best.get("correction_instruction") if best else None,
            protected_content=best.get("protected_content") if best else None,
            **{checks: best.get(checks) if best else None},
            votes=[verdict_line(r["gpt"]) for r in recs],
        ))
    items.sort(key=lambda x: (-x["keep_votes"], x["prompt_seen"], -(x["mean_severity"] or 0),
                              -(x["mean_confidence"] or 0), x["key"]))
    picked, prompts = [], set()
    for x in items:
        p = norm(x["flawed_video"]["prompt"])
        if x["keep_votes"] > 0 and not (args.one_per_prompt and p in prompts):
            picked.append(x)
            prompts.add(p)
    n_kept = len(picked)
    picked = picked[:args.top]
    for rank, x in enumerate(picked, 1):
        x["rank"] = rank

    with open(args.out + ".json", "w") as f:
        json.dump(picked, f, indent=1, ensure_ascii=False)
    cols = ["rank", "tier", "keep_votes", "mean_severity", "mean_confidence", "model", "filename", "prompt",
            "human", "error_category", "t_span", "error_description", "correction_instruction",
            "protected_content", "votes", "path"]
    with open(args.out + ".csv", "w", newline="") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        for x in picked:
            w.writerow(dict(
                rank=x["rank"], tier=x["tier"], keep_votes=f"{x['keep_votes']}/{x['n_votes']}",
                mean_severity=x["mean_severity"], mean_confidence=x["mean_confidence"],
                model=x["flawed_video"]["model"], filename=x["flawed_video"]["filename"],
                prompt=x["flawed_video"]["prompt"],
                human=brief[x["key"]],
                error_category=" ".join(x["error_category"]),
                t_span=" ".join(f"{e['t_start']}-{e['t_end']}" for e in x["error_evidence"]),
                error_description=x["error_description"], correction_instruction=x["correction_instruction"],
                protected_content=x["protected_content"], votes="\n".join(x["votes"]),
                path=x["flawed_video"]["path"]))

    dist = collections.Counter(x["keep_votes"] for x in items)
    print(f"{len(items)} videos; keep-vote distribution: {dict(sorted(dist.items(), reverse=True))}")
    print(f"{n_kept} with a keep vote{' (one per prompt)' if args.one_per_prompt else ''}, "
          f"{sum(x['prompt_seen'] for x in picked)} of the picked share a prompt with --seen")
    print(f"picked {len(picked)}: {dict(collections.Counter(x['tier'] for x in picked))}; "
          f"by model {dict(collections.Counter(x['flawed_video']['model'] for x in picked).most_common())}")
    if picked:
        last = picked[-1]
        print(f"cutoff: rank {last['rank']} has {last['keep_votes']} votes, severity {last['mean_severity']}")


if __name__ == "__main__":
    main()
