"""Hand each candidate to GPT-5.5 with its human labels: GPT-5.5 sees the video at 4 fps with timestamps and the
generation prompt, checks each human claim of a physics error against the frames, and writes the PhysFixBench record
(error evidence, description, category, correction instruction) or a reject reason. The claims depend
on the source: for PhyGround (select_phyground.py) each low-rated law, given with the per-law human means and the law
definitions annotators were shown; for VideoPhy2 (select_videophy2.py) each violated-rule sentence, given with the
SA/PC scores.

GPT-5.5 is reached through any OpenAI-compatible /v1/chat/completions endpoint (--relay, bearer token read from
--token-file). The PhyGround law names and the questions annotators were shown are in phyground_laws.json.

    python3 pipeline/gpt_verify.py --cands pipeline/out/phyground_candidates_sa3.5.json --out-dir pipeline/out/gpt55_sa3.5
    python3 pipeline/gpt_verify.py --cands pipeline/out/videophy2_candidates_pc2.json --out-dir pipeline/out/gpt55_vp2
"""

import argparse
import base64
import collections
import concurrent.futures
import csv
import glob
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RELAY = "http://127.0.0.1:18990/v1/chat/completions"
TOKEN_FILE = os.path.expanduser("~/tmp/master-relay/token")
CRIT = json.load(open(os.path.join(HERE, "phyground_laws.json")))
CATEGORIES = ["impenetrability", "object_permanence", "material_or_state_change", "force_and_motion",
              "collision_response", "fluid", "soft_body_deformation", "contact_and_support", "optics", "other"]
REJECT = ["no_physics_error", "semantic_only", "false_positive", "global_collapse", "low_visual_quality",
          "not_fixable"]

TASK = f"""You are curating PhysFixBench, a benchmark for physics-aware video correction: a model is given a \
generated video that contains a physics error and must output a corrected video that fixes only that error. We want \
videos that are worth fixing and can be fixed:
1. Only physics matters. Whether the video follows the text prompt is NOT a criterion; a video whose only problem is \
semantic (wrong object, missing action, prompt not followed) is rejected.
2. The physics error must be explicit and localizable: a concrete violation you can point to in specific frames \
(interpenetration, objects appearing/vanishing, unexplained motion or stopping, wrong material response, impossible \
fluid behaviour, ...).
3. The video must be otherwise usable. Set visual_quality_ok to false, and reject with low_visual_quality, when the \
main subjects are hard to make out in many frames: heavy smearing or blur that hides object shapes, people or objects \
melting/morphing between frames, flickering or garbled textures, fragments that are shapeless blobs. Be strict: a \
benchmark viewer must be able to see clearly what is wrong.
4. Reject with global_collapse when the error is not confined: several unrelated physics errors across the video, or \
the main subjects lose their shape. A keepable video has one (or a few closely related) violations in a specific \
event and time span, so a corrected video can change that part and keep the rest."""

NOT_ERRORS = """Do not invent errors that are not visible, and do not count normal occlusion, camera motion, \
reflections, motion blur or ordinary deformation as physics errors."""

# Per source: what the human labels are (ratings), how GPT-5.5 reports its check of each human claim (checks), and
# how an error links back to the labels (links). A VideoPhy2 rule can be about objects the video does not show (the
# labels are noisy), hence the not_in_video verdict.
SOURCES = {
    "phyground": dict(
        ratings=f"""You get frames sampled at 4 fps, each labelled with its timestamp, plus human ratings from our \
study. Humans rated each dimension 1-5 (5 fully plausible, 4 mostly, 3 partially, 2 largely implausible, 1 completely \
implausible); the numbers are means over a few annotators and can be wrong. Treat a low law score as a claim to \
verify: look for the violation in the frames and mark it supported only if you can see it, citing timestamps. \
{NOT_ERRORS}""",
        checks="""  "law_checks": [{"law": "<law key>", "human_mean": <number>, "verdict": "supported" | "not_supported" | \
"unclear", "evidence": "<what you see, frame by frame>", "timestamps": [<seconds>, ...]}],  // every low-rated law
  "other_errors": [<same shape, "law": "<law key or other>", for clear physics errors humans did not flag>],""",
        links='"laws": ["<law key>", ...]'),
    "videophy2": dict(
        ratings=f"""You get frames sampled at 4 fps, each labelled with its timestamp, plus human labels from the \
VideoPhy2 study: semantic adherence (SA) and physical commonsense (PC), each rated 1-5 (5 fully adherent / fully \
plausible, 1 not at all), and the physics rules the annotators say the video violates, some picked from a rule list \
(ids V1, V2, ...) and some written in their own words (ids W1, W2, ...). The labels are noisy: a rule can be wrong, \
and a rule can be about objects or events that are not in this video at all. Treat each rule as a claim that the \
video breaks it: mark it supported only if you can see the violation in the frames, citing timestamps; not_supported \
if what it describes is in the video and looks physically fine; not_in_video if the objects or events it describes \
do not appear. {NOT_ERRORS}""",
        checks="""  "rule_checks": [{"rule": "<rule id>", "verdict": "supported" | "not_supported" | "not_in_video", \
"evidence": "<what you see, frame by frame>", "timestamps": [<seconds>, ...]}],  // every listed rule
  "other_errors": [{"law": "<physical law>", "evidence": "<what you see>", "timestamps": [<seconds>, ...]}],  \
// clear physics errors the annotators did not list""",
        links='"laws": ["<physical law, e.g. Gravity>", ...], "rules": ["<rule id>", ...]'),
}


def system_prompt(source):
    s = SOURCES[source]
    return f"""{TASK}

{s['ratings']}

Answer with one JSON object, no prose outside it:
{{
{s['checks']}
  "keep": true | false,
  "reject_reason": null | one of {json.dumps(REJECT)},  // the main reason
  "errors": [{{"t_start": <s>, "t_end": <s>, "key_timestamps": [<s>, ...], "description": "<the violation, \
concrete>", "category": {json.dumps(CATEGORIES)}, {s['links']}}}],  // empty if keep is false
  "correction_instruction": "<one instruction for an editing model: what physically should happen instead, changing \
only the erroneous part>",
  "protected_content": "<what must stay unchanged: scene, objects, camera, the correct parts of the motion>",
  "severity": <1-5, how obvious the error is to a viewer>,
  "visual_quality_ok": true | false,
  "confidence": <0-1>
}}"""


def extract_frames(path, frame_dir, fps, width):
    """Passes run in parallel share frame_dir, so extract into a private directory and rename it into place: a pass
    that reads frame_dir while another is still extracting would otherwise send a partial frame list."""
    if not glob.glob(os.path.join(frame_dir, "*.jpg")):
        tmp = tempfile.mkdtemp(prefix=os.path.basename(frame_dir) + ".", dir=os.path.dirname(frame_dir))
        subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", f"fps={fps},scale={width}:-2", "-q:v", "3",
                        os.path.join(tmp, "%03d.jpg")], check=True)
        try:
            os.rename(tmp, frame_dir)  # replaces a missing or empty frame_dir
        except OSError:  # another pass got there first
            shutil.rmtree(tmp)
    return sorted(glob.glob(os.path.join(frame_dir, "*.jpg")))


def source(cand):
    return cand.get("source", "phyground")  # PhyGround candidate files predate the field


def phyground_lines(cand):
    g = cand["general"]
    lines = [f"Generation prompt: {cand['prompt']}",
             f"Human ratings (mean of {cand['n_annotators']} annotator(s); individual scores in brackets):",
             f"- Prompt alignment (SA): {g.get('SA')}; Temporal coherence (PTV): {g.get('PTV')}; "
             f"Object persistence: {g.get('persistence')}"]
    for law, m in sorted(cand["law_mean"].items(), key=lambda kv: kv[1]):
        c = CRIT[law]
        flag = "  <-- LOW" if law in cand["low_laws"] else ""
        lines.append(f"- {law} ({c['name']}): {m} {cand['law_scores'][law]}{flag}\n    question: {c['question']}"
                     + (f" {c['note']}" if c['note'] else ""))
    for n in cand["notes"]:
        lines.append(f"Annotator note: {n}")
    lines.append(f"Low-rated laws to verify: {', '.join(cand['low_laws'])}")
    return lines


def videophy2_lines(cand):
    h = cand["human"]
    lines = [f"Generation prompt: {cand['prompt']}",
             f"Human ratings: semantic adherence (SA) {h['SA']}, physical commonsense (PC) {h['PC']}",
             "Physics rules the annotators say the video violates (V: picked from the rule list, W: written by an "
             "annotator; [law] as tagged in the dataset):"]
    for k, rule in h["rules"].items():
        laws = h["rule_laws"].get(k)
        lines.append(f"- {k}: {rule}" + (f" [{', '.join(laws)}]" if laws else ""))
    lines.append(f"Rules to verify: {', '.join(h['rules'])}")
    return lines


def user_content(cand, frames, fps):
    lines = {"phyground": phyground_lines, "videophy2": videophy2_lines}[source(cand)](cand)
    lines.append(f"The video has {len(frames)} frames at {fps} fps:")
    content = [{"type": "text", "text": "\n".join(lines)}]
    for i, f in enumerate(frames):
        with open(f, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
        content.append({"type": "text", "text": f"t={i / fps:.2f}s"})
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    return content


def call(body, token, relay, retries=6):
    data = json.dumps(body).encode()
    for attempt in range(retries):
        req = urllib.request.Request(relay, data=data, headers={"Authorization": f"Bearer {token}",
                                                                "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            msg = e.read()[:300]
            if e.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                raise RuntimeError(f"HTTP {e.code}: {msg}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries - 1:
                raise
            msg = str(e)
        wait = 20 * 2 ** attempt
        print(f"  retry in {wait}s ({msg!r})", flush=True)
        time.sleep(wait)


def run_one(cand, args, token):
    key = f"{cand['model']}__{os.path.splitext(cand['filename'])[0]}"
    out_path = os.path.join(args.out_dir, "records", key + ".json")
    if os.path.exists(out_path):
        return key, json.load(open(out_path)), True
    frames = extract_frames(cand["path"], os.path.join(args.frames_dir, f"{key}_fps{args.fps:g}_w{args.width}"),
                            args.fps, args.width)
    body = {"model": args.model, "reasoning_effort": args.effort, "max_completion_tokens": 20000,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system_prompt(source(cand))},
                         {"role": "user", "content": user_content(cand, frames, args.fps)}]}
    t0 = time.time()
    resp = call(body, token, args.relay)
    text = resp["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    rec = {"candidate": cand, "n_frames": len(frames), "fps": args.fps, "width": args.width, "model": resp.get("model"),
           "effort": args.effort, "latency_s": round(time.time() - t0, 1), "usage": resp.get("usage"),
           "gpt": parsed, "raw": None if parsed else text}
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(rec, f, indent=1, ensure_ascii=False)
    return key, rec, False


def human_brief(c):
    """A candidate's human labels for a review CSV cell: SA and the low laws with their means, or SA/PC and the
    violated rules."""
    if source(c) == "videophy2":
        h = c["human"]
        return "\n".join([f"SA={h['SA']} PC={h['PC']}"] + [f"{k}: {r}" for k, r in h["rules"].items()])
    return " ".join([f"SA={c['general'].get('SA')} ({c['n_annotators']} annotators)"]
                    + [f"{l}={c['law_mean'][l]}" for l in c["low_laws"]])


def claim_checks(g):
    """GPT-5.5's verdict on each human claim, as (claim id, check) pairs."""
    return [(x.get("law") or x.get("rule"), x) for x in g.get("law_checks") or g.get("rule_checks") or []]


def write_summary(out_dir):
    """One row per record in out_dir/records: the human labels next to GPT-5.5's call, for review."""
    cols = ["key", "keep", "reject_reason", "severity", "confidence", "visual_quality_ok", "model", "video_id",
            "human", "verdicts", "categories", "t_span", "description", "correction_instruction", "prompt", "path"]
    rows = []
    for f in sorted(glob.glob(os.path.join(out_dir, "records", "*.json"))):
        rec = json.load(open(f))
        c, g = rec["candidate"], rec["gpt"] or {}
        errs = g.get("errors") or []
        cats = set()
        for e in errs:  # GPT-5.5 writes a category either as one string or as a list
            cat = e.get("category") or []
            cats.update([cat] if isinstance(cat, str) else cat)
        rows.append(dict(
            key=os.path.basename(f)[:-5], keep=g.get("keep"), reject_reason=g.get("reject_reason"),
            severity=g.get("severity"), confidence=g.get("confidence"), visual_quality_ok=g.get("visual_quality_ok"),
            model=c["model"], video_id=c["video_id"], human=human_brief(c),
            verdicts=" ".join(f"{k}={x.get('verdict')}" for k, x in claim_checks(g)),
            categories=" ".join(sorted(cats)),
            t_span=" ".join(f"{e.get('t_start')}-{e.get('t_end')}" for e in errs),
            description=" | ".join(e.get("description", "") for e in errs),
            correction_instruction=g.get("correction_instruction") if g.get("keep") else "",
            prompt=c["prompt"], path=c["path"]))
    with open(os.path.join(out_dir, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        w.writerows(rows)
    return rows


def pilot(cands, n):
    """Round-robin over generators, taking each model's first candidate (PhyGround: largest SA-minus-law gap;
    VideoPhy2: lowest PC) and preferring a lowest law (VideoPhy2: a category) not yet picked, so a small pilot spans
    models and law types."""
    by_model = collections.defaultdict(list)
    for c in cands:
        by_model[c["model"]].append(c)
    kind = lambda c: c["low_laws"][0] if "low_laws" in c else c["category"]
    picked, seen = [], set()
    while len(picked) < n and any(by_model.values()):
        for m in sorted(by_model):
            if not by_model[m] or len(picked) >= n:
                continue
            pool = by_model[m]
            c = next((c for c in pool if kind(c) not in seen), pool[0])
            pool.remove(c)
            seen.add(kind(c))
            picked.append(c)
    return picked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cands", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--frames-dir", default=os.path.join(HERE, "out/frames"))
    ap.add_argument("--pilot", type=int, help="run a model- and law-diverse subset of this size")
    ap.add_argument("--ids", help="comma-separated video_ids to run")
    ap.add_argument("--limit", type=int, help="run only the first this many candidates")
    ap.add_argument("--fps", type=float, default=4)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--model", default="gpt-5.5")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--relay", default=RELAY, help="OpenAI-compatible chat completions URL")
    ap.add_argument("--token-file", default=TOKEN_FILE)
    args = ap.parse_args()

    cands = json.load(open(args.cands))
    if args.ids:
        ids = set(args.ids.split(","))
        cands = [c for c in cands if str(c["video_id"]) in ids]
    if args.limit:
        cands = cands[:args.limit]
    if args.pilot:
        cands = pilot(cands, args.pilot)
    token = open(args.token_file).read().strip()
    usage = collections.Counter()
    with concurrent.futures.ThreadPoolExecutor(args.workers) as ex:
        futs = {ex.submit(run_one, c, args, token): c for c in cands}
        for fut in concurrent.futures.as_completed(futs):
            c = futs[fut]
            try:
                key, rec, cached = fut.result()
            except Exception as e:  # one bad video should not stop the batch
                print(f"FAIL {c['model']}/{c['filename']}: {e}", flush=True)
                continue
            u = rec.get("usage") or {}
            if not cached:
                usage["in"] += u.get("prompt_tokens", 0)
                usage["out"] += u.get("completion_tokens", 0)
            g = rec["gpt"] or {}
            print(f"{'cached ' if cached else ''}{key}: keep={g.get('keep')} reject={g.get('reject_reason')} "
                  f"in={u.get('prompt_tokens')} out={u.get('completion_tokens')} {rec['latency_s']}s", flush=True)
    cost = usage["in"] * 5e-6 + usage["out"] * 30e-6
    print(f"new calls: {usage['in']} in / {usage['out']} out tokens = ${cost:.2f} at $5/$30 per M")
    rows = write_summary(args.out_dir)
    print(f"{len(rows)} records, keep {sum(r['keep'] is True for r in rows)}; reject:",
          dict(collections.Counter(str(r["reject_reason"]) for r in rows if r["keep"] is not True)))


if __name__ == "__main__":
    main()
