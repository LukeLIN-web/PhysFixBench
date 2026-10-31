"""Turn the voted PhyGround / VideoPhy2 lists (vote.py) into the PhysFixBench release format of the Physion-300
package: one jsonl and one csv per source under data/, the videos under videos/, and a review page (with a local
copy that plays videos/ and an HF copy that plays the dataset's resolve URLs).

The Chinese fixing fields and the PhysFixBench labels (error_family, interaction_primitive, scenario_domain,
review_error_category) come from pipeline/zh/<source>.json, written from each record's English error description,
correction instruction and protected content; the time span of every problem is the one GPT-5.5 gave, so it is
prefixed here rather than in the Chinese text. Every source record keeps the Physion-300 fields first, in their order,
then its own provenance fields (prompt, GPT votes, English evidence, human labels).

    python3 pipeline/export_physfixbench.py --hf-repo juyil/PhysFixBench-PhyGround-VideoPhy2

A list is out/<name>_top100.json with its Chinese fields in zh/<name>.json, written to data/physfixbench_<name>.jsonl;
its source (PhyGround or VideoPhy2) is read from the records. The second PhyGround batch, verified with gpt-6-astra
through Codex, goes out as its own list and review page:

    python3 pipeline/export_physfixbench.py --lists phyground_b2 --page review_phyground_b2 \
        --verifier "gpt-6-astra (Codex)" --title "PhyGround 第二批-100" --hf-repo <HF dataset>
"""

import argparse
import collections
import csv
import html
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
LAWS = json.load(open(os.path.join(HERE, "phyground_laws.json")))
SOURCES = ["phyground", "videophy2"]
SOURCE_ZH = {"phyground": "PhyGround", "videophy2": "VideoPhy2"}
GENERATOR = {
    "veo-3.1": "Veo 3.1", "wan2.2-i2v-a14b": "Wan 2.2 I2V-A14B", "wan2.2-ti2v-5b": "Wan 2.2 TI2V-5B",
    "cosmos-predict2.5-2b": "Cosmos-Predict2.5 2B", "cosmos-predict2.5-14b": "Cosmos-Predict2.5 14B",
    "ltx-2-19b-dev-1stage": "LTX-2 19B", "ltx-2.3-22b-dev-1stage": "LTX-2.3 22B", "omniweaving": "OmniWeaving",
    "wan": "Wan", "cosmos": "Cosmos", "ray2": "Ray2", "cogvideo": "CogVideoX", "hunyuan": "HunyuanVideo",
    "sora": "Sora",
}
# Chinese labels as the Physion-300 review page prints them
LABEL_ZH = {
    "error_family": {"temporal_causal": "时序/因果", "persistence_identity": "物体永存/同一", "motion_force": "力与运动",
                     "material_state": "材质/状态", "contact_interaction": "接触/交互", "geometry_structure": "几何/碰撞",
                     "other": "其他"},
    "interaction_primitive": {"articulation_open_close": "开合操作", "pour_flow": "倾倒/流动",
                              "pick_place_transfer": "拿取转移", "deformation_state_change": "形变/状态变化",
                              "body_motion": "肢体运动", "other_multistep": "多步操作", "collision_impact": "碰撞冲击",
                              "throw_projectile": "抛射"},
    "scenario_domain": {"fluid_thermal_optical_phenomena": "流体/热/光学现象", "human_animal_body_motion": "人/动物肢体运动",
                        "deformable_materials": "可变形材料", "tool_machine_operation": "工具/机械操作",
                        "rigid_object_dynamics": "刚体动力学"},
    "review_error_category": {"material_state_change": "材质状态变化", "object_persistence": "物体永存",
                              "force_motion_causality": "力/运动/因果", "fluid": "流体", "soft_body": "软体",
                              "solidity_interpenetration": "固体贯穿", "contact_support": "接触/支撑", "other": "其他"},
    "obviousness": {"obvious": "明显", "subtle": "细微", "unknown": "未分级"},
}
CSV_COLS = ["slot", "record_id", "video_file", "generator", "error_family", "interaction_primitive", "scenario_domain",
            "obviousness", "confidence", "needs_human", "问题(problems)", "正确表现(expected)", "保持不变(keep)",
            "审核结论(pass/edit/reject)", "修改后内容", "备注"]


def sec(t):
    s = f"{t:.2f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def span(e):
    if e["t_start"] == e["t_end"]:
        return f"第{sec(e['t_start'])}s左右"
    return f"第{sec(e['t_start'])}s–{sec(e['t_end'])}s"


def compose(problems, expected, keep):
    """The fixing prompt exactly as the Physion-300 package composes it."""
    return ("【存在的问题】\n" + "\n".join(f"{i}. {p}" for i, p in enumerate(problems, 1))
            + "\n【应修改为】\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(expected, 1))
            + f"\n请在保持{keep}的前提下，修正上述物理问题并重新生成视频。")


def human_annotation(src, x):
    """The source's own human labels, as Physion-300's annotation field carries Physion-Eval's: VideoPhy2's violated-rule
    sentences verbatim; PhyGround has no free-text label, so its per-law ratings in one line (plus any annotator note)."""
    h = x["human"]
    if src == "videophy2":
        return list(h["rules"].values())
    g = h["general"]
    laws = ", ".join(f"{k} {m:g}" for k, m in sorted(h["law_mean"].items(), key=lambda kv: kv[1]))
    s = (f"PhyGround human ratings (mean of {h['n_annotators']} annotator(s), 1-5): SA {g.get('SA'):g}, "
         f"PTV {g.get('PTV'):g}, persistence {g.get('persistence'):g}; laws: {laws}. "
         f"Low-rated: {', '.join(h['low_laws'])}.")
    return " ".join([s] + [f"Annotator note: {n}" for n in h["notes"]])


def human_categories(src, x):
    h = x["human"]
    if src == "videophy2":
        return "; ".join(dict.fromkeys(l for ls in h["rule_laws"].values() for l in ls))
    return "; ".join(LAWS[k]["name"] for k in h["low_laws"])


def build(src, x, zh):
    fv = x["flawed_video"]
    stem = os.path.splitext(fv["filename"])[0]
    record_id = f"phyground::{fv['model']}::{stem}" if src == "phyground" else f"videophy2::test::{stem}"
    video_file = (x["key"] if src == "phyground" else stem) + ".mp4"
    checks = x["law_checks"] if src == "phyground" else x["rule_checks"]
    problems = [f"{span(e)}：{p}" for e, p in zip(x["error_evidence"], zh["problems"], strict=True)]
    expected = list(zh["expected"])
    obvious = "obvious" if x["mean_severity"] >= 4 else "subtle"
    return dict(
        dir_name=f"{x['rank']:04d}_{record_id.replace('::', '_')}",
        record_id=record_id,
        slot=x["rank"],
        generator=GENERATOR[fv["model"]],
        error_family=zh["error_family"],
        glitch_category_official=human_categories(src, x),
        scenario_domain=zh["scenario_domain"],
        interaction_primitive=zh["interaction_primitive"],
        review_error_category=zh["review_error_category"],
        obviousness=obvious,
        confidence=x["mean_confidence"],
        needs_human_frame_step_confirmation=x["tier"] == "borderline",
        all_claimed_errors_visible=all(c["verdict"] == "supported" for c in checks or []),
        annotation=human_annotation(src, x),
        fix_problems=problems,
        fix_expected=expected,
        fix_keep=zh["keep"],
        fixing_prompt_zh=compose(problems, expected, zh["keep"]),
        source=src,
        video_file=video_file,
        generator_id=fv["model"],
        prompt=fv["prompt"],
        gpt_keep_votes=f"{x['keep_votes']}/{x['n_votes']}",
        gpt_tier=x["tier"],
        gpt_severity=x["mean_severity"],
        error_evidence=x["error_evidence"],
        error_description_en=x["error_description"].split(" | "),
        correction_instruction_en=x["correction_instruction"],
        protected_content_en=x["protected_content"],
        error_category_en=x["error_category"],
        violated_laws_en=x["violated_laws"],
        human_labels=x["human"],
        claim_checks=checks,
        gpt_votes=x["votes"],
    ), fv["path"]


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLS)
        for r in rows:
            w.writerow([r["slot"], r["record_id"], r["video_file"], r["generator"], r["error_family"],
                        r["interaction_primitive"], r["scenario_domain"], r["obviousness"], r["confidence"],
                        r["needs_human_frame_step_confirmation"], "\n".join(r["fix_problems"]),
                        "\n".join(r["fix_expected"]), r["fix_keep"], "", "", ""])


def link_video(src_path, dst):
    if os.path.exists(dst):
        os.remove(dst)
    try:
        os.link(src_path, dst)
    except OSError:
        shutil.copy2(src_path, dst)


E = html.escape


def card(r, video_url):
    uid = f"{r['source']}-{r['slot']}"
    lab = lambda field: LABEL_ZH[field][r[field]]
    nh = '<span class="badge b-nh">建议逐帧人工确认</span>' if r["needs_human_frame_step_confirmation"] else ""
    ann = r["annotation"] if isinstance(r["annotation"], str) else "\n".join(f"• {a}" for a in r["annotation"])
    claims = "".join(
        f'<div class="claim"><span class="cv cv-{E(c["verdict"])}">{E(c["verdict"])}</span> '
        f'<b>{E(str(c.get("law") or c.get("rule")))}</b>'
        f'<span class="et">  ⏱ {", ".join(f"{t:g}s" for t in c.get("timestamps") or [])}</span><br>'
        f'<span class="et">{E(c.get("evidence", ""))}</span></div>' for c in r["claim_checks"] or [])
    errs = "".join(f'<div class="claim"><b>{E(span(e))}</b> <span class="et">{E(d)}</span></div>'
                   for e, d in zip(r["error_evidence"], r["error_description_en"]))
    votes = "\n".join(r["gpt_votes"])
    return f"""<div class="card" id="card-{uid}" data-uid="{uid}" data-rid="{E(r['record_id'])}" data-source="{r['source']}"
     data-family="{r['error_family']}" data-interaction="{r['interaction_primitive']}"
     data-generator="{E(r['generator'])}" data-obv="{r['obviousness']}">
  <div class="head">
    <span class="idx">{SOURCE_ZH[r['source']]} #{r['slot']}</span>
    <span class="rid">{E(r['video_file'])}</span>
    <span class="badge b-gen">{E(r['generator'])}</span>
    {nh}
    <span class="reviewstate" id="state-{uid}"></span>
  </div>
  <div class="cols">
    <div class="leftcol">
      <video controls preload="none" src="{E(video_url)}"></video>
      <div class="ann"><b>生成 prompt：</b>{E(r['prompt'])}</div>
    </div>
    <div class="meta">
      <div class="badges">
        <span class="badge b-fam">{lab('error_family')}</span>
        <span class="badge b-int">{lab('interaction_primitive')}</span>
        <span class="badge b-sc">{lab('scenario_domain')}</span>
        <span class="badge b-cat">{lab('review_error_category')}</span>
        <span class="badge obv-{r['obviousness']}">难度：{lab('obviousness')}</span>
        <span class="badge b-conf">GPT 保留票 {r['gpt_keep_votes']} · 置信度 {r['confidence']:g}</span>
      </div>
      <div class="promptbox">
        <div class="promptlabel">❶ 存在的物理问题（每行一条，编号自动对应）</div>
        <textarea data-uid="{uid}" class="fp-problems" rows="{len(r['fix_problems'])}">{E(chr(10).join(r['fix_problems']))}</textarea>
        <div class="promptlabel">❷ 应有的正确表现（与左侧问题逐行一一对应）</div>
        <textarea data-uid="{uid}" class="fp-expected" rows="{len(r['fix_expected'])}">{E(chr(10).join(r['fix_expected']))}</textarea>
        <div class="promptlabel">❸ 保持不变</div>
        <input data-uid="{uid}" class="fp-keep" value="{E(r['fix_keep'])}">
      </div>
      <div class="reviewrow">
        <b>你的审核：</b>
        <label><input type="radio" name="verdict-{uid}" value="pass"> 通过</label>
        <label><input type="radio" name="verdict-{uid}" value="edit"> 需修改（改在上方框中）</label>
        <label><input type="radio" name="verdict-{uid}" value="reject"> 驳回（视频/问题不合格）</label>
      </div>
      <div class="reviewrow"><input class="comment" data-uid="{uid}" placeholder="备注（可选：驳回原因 / 修改说明）"></div>
      <details><summary>{SOURCE_ZH[r['source']]} 人工标注原文</summary><div class="ann">{E(ann)}</div></details>
      <details><summary>英文核实证据（{E(VERIFIER)} 对每条人工标注的裁决 + 错误描述 + 三遍投票）</summary>
        {claims}{errs}<div class="et notebox">{E(votes)}</div></details>
    </div>
  </div>
</div>
"""


STYLE = """
 body{font-family:-apple-system,"PingFang SC","Segoe UI",Arial,sans-serif;margin:0;background:#f5f6f8;color:#1c2230}
 header{position:sticky;top:0;z-index:10;background:#fff;border-bottom:1px solid #dde0e6;padding:10px 16px}
 h1{font-size:16px;margin:0 0 8px}
 .filters{display:flex;gap:10px;flex-wrap:wrap;align-items:center;font-size:13px}
 select,.filters input{padding:4px 8px;font-size:13px}
 #count{font-weight:600;color:#1a7f37}
 #progress{font-size:13px;color:#5b6472}
 .exportbtn{background:#1a7f37;color:#fff;border:0;border-radius:6px;padding:6px 14px;font-size:13px;font-weight:600;cursor:pointer}
 main{padding:14px 16px;max-width:1520px;margin:0 auto}
 .card{background:#fff;border:1px solid #dde0e6;border-radius:10px;margin-bottom:14px;padding:12px}
 .head{display:flex;gap:10px;align-items:center;margin-bottom:8px;font-size:13px;color:#5b6472;flex-wrap:wrap}
 .idx{font-weight:700;color:#111;font-size:14px}
 .cols{display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap}
 .leftcol{width:480px;max-width:100%}
 video{width:480px;max-width:100%;border-radius:8px;background:#000}
 .meta{flex:1;min-width:min(420px,100%);font-size:13px;line-height:1.55}
 .badges{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
 .badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:600;background:#eef1f5;color:#333}
 .b-gen{background:#e8eefc;color:#25408f} .b-fam{background:#e6f6ec;color:#1a7f37}
 .b-int{background:#f3e8fc;color:#6d28d9} .b-sc{background:#eef1f5;color:#5b6472;font-weight:400}
 .b-nh{background:#fdeceb;color:#b42318}
 .obv-obvious{background:#e6f6ec;color:#1a7f37} .obv-subtle{background:#fdf3e3;color:#b4550f} .obv-unknown{background:#eef0f3;color:#5b6472}
 .b-cat{background:#f0f9ff;color:#0369a1}
 .promptbox{margin:8px 0} .promptlabel{font-weight:700;margin-bottom:4px}
 textarea.fp-problems,textarea.fp-expected{width:100%;box-sizing:border-box;font-size:13.5px;line-height:1.6;
   border:1px solid #c9d2e0;border-radius:8px;padding:7px 10px;font-family:inherit;background:#fcfdff;margin-bottom:6px}
 textarea.fp-problems{border-left:4px solid #b42318}
 textarea.fp-expected{border-left:4px solid #1a7f37;background:#f7fcf9}
 input.fp-keep{width:100%;box-sizing:border-box;font-size:13px;line-height:1.5;border:1px solid #c9d2e0;
   border-radius:8px;padding:6px 10px;font-family:inherit;background:#fcfdff;border-left:4px solid #25408f}
 textarea:focus,input.fp-keep:focus{outline:2px solid #9ec1f7;border-color:#7aa2ea}
 .reviewrow{margin:6px 0;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
 .reviewrow label{white-space:nowrap;font-weight:600}
 .comment{flex:1;min-width:260px;padding:5px 9px;border:1px solid #c9d2e0;border-radius:6px;font-size:13px}
 .reviewstate{font-weight:700;padding:1px 10px;border-radius:10px;font-size:12px}
 .st-pass{background:#e6f6ec;color:#1a7f37} .st-edit{background:#fdf3e3;color:#b4550f} .st-reject{background:#fdeceb;color:#b42318}
 .ann{background:#fafbfc;border:1px solid #eef0f3;border-radius:8px;padding:8px 10px;margin:6px 0;white-space:pre-wrap;color:#444}
 details{margin-top:6px} summary{cursor:pointer;color:#25408f;font-weight:600}
 .claim{border-left:3px solid #ddd;padding:4px 8px;margin:6px 0;background:#fafbfc;border-radius:0 6px 6px 0}
 .cv{font-size:11px;font-weight:700;padding:1px 7px;border-radius:10px}
 .cv-supported{background:#e6f6ec;color:#1a7f37} .cv-not_supported{background:#fdf3e3;color:#b4550f}
 .cv-unclear,.cv-not_in_video{background:#eef0f3;color:#5b6472}
 .et{color:#444;font-size:12.5px} .notebox{margin-top:8px;white-space:pre-wrap}
"""

SCRIPT = r"""
const $=id=>document.getElementById(id);
const LS="__LS__";
let store={};try{store=JSON.parse(localStorage.getItem(LS)||'{}');}catch(e){}
const uids=[...document.querySelectorAll('.card')].map(c=>c.dataset.uid);
function save(){try{localStorage.setItem(LS,JSON.stringify(store));}catch(e){}}
function paint(u){
  const st=$('state-'+u); const r=store[u];
  st.textContent=''; st.className='reviewstate';
  if(r&&r.verdict){const m={pass:['通过','st-pass'],edit:['需修改','st-edit'],reject:['驳回','st-reject']}[r.verdict];
    st.textContent='已审核：'+m[0]; st.classList.add(m[1]);}
}
[['.fp-problems','problems'],['.fp-expected','expected'],['.fp-keep','keep'],['.comment','comment']].forEach(([sel,field])=>{
  document.querySelectorAll(sel).forEach(t=>{
    const u=t.dataset.uid;
    if(store[u]&&store[u][field]!=null) t.value=store[u][field];
    t.addEventListener('input',()=>{store[u]=Object.assign(store[u]||{},{[field]:t.value});save();});
  });
});
uids.forEach(u=>{
  const radios=document.querySelectorAll('input[name="verdict-'+u+'"]');
  radios.forEach(r=>{
    if(store[u]&&store[u].verdict===r.value) r.checked=true;
    r.addEventListener('change',()=>{store[u]=Object.assign(store[u]||{},{verdict:r.value});save();paint(u);updateProg();});
  });
  paint(u);
});
function updateProg(){
  let done=0;const c={pass:0,edit:0,reject:0};
  uids.forEach(u=>{if(store[u]&&store[u].verdict){done++;c[store[u].verdict]++;}});
  $('progress').textContent=' 已审核 '+done+'/'+uids.length+'（通过 '+c.pass+' · 需修改 '+c.edit+' · 驳回 '+c.reject+'），内容自动保存在本浏览器';
}
function apply(){
  const src=$('f-source').value, fam=$('f-family').value, int_=$('f-interaction').value, gen=$('f-generator').value,
        obv=$('f-obv').value, nh=$('f-nh').checked, unr=$('f-unreviewed').checked;
  let shown=0;
  document.querySelectorAll('.card').forEach(card=>{
    const u=card.dataset.uid;
    const ok=(!src||card.dataset.source===src)&&(!fam||card.dataset.family===fam)&&(!int_||card.dataset.interaction===int_)&&
             (!gen||card.dataset.generator===gen)&&(!obv||card.dataset.obv===obv)&&
             (!nh||card.querySelector('.b-nh'))&&(!unr||!(store[u]&&store[u].verdict));
    card.style.display=ok?'':'none'; if(ok)shown++;
  });
  $('count').textContent=shown+' / '+uids.length+' 条';
}
['f-source','f-family','f-interaction','f-generator','f-obv','f-nh','f-unreviewed'].forEach(id=>$(id).addEventListener('change',apply));
$('export').addEventListener('click',()=>{
  const fval=(u,sel,field)=>{const v=store[u]&&store[u][field];return (v!=null?v:document.querySelector('#card-'+u+' '+sel).value)||'';};
  const lines=t=>t.split('\n').map(x=>x.trim()).filter(Boolean);
  const rows=uids.filter(u=>store[u]&&store[u].verdict).map(u=>{
    const card=$('card-'+u);
    const probs=lines(fval(u,'.fp-problems','problems'));
    const exps=lines(fval(u,'.fp-expected','expected'));
    const keep=fval(u,'.fp-keep','keep').trim();
    const composed='【存在的问题】\n'+probs.map((x,i)=>(i+1)+'. '+x).join('\n')+
      '\n【应修改为】\n'+exps.map((x,i)=>(i+1)+'. '+x).join('\n')+
      '\n请在保持'+keep+'的前提下，修正上述物理问题并重新生成视频。';
    return {source:card.dataset.source, slot:+u.split('-')[1], record_id:card.dataset.rid, verdict:store[u].verdict,
            problems:probs, expected:exps, keep:keep, fixing_prompt_zh:composed, comment:store[u].comment||''};
  });
  const blob=new Blob([JSON.stringify(rows,null,2)],{type:'application/json'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download="__EXPORT__";a.click();
});
apply();updateProg();
"""


def options(field, used):
    return "".join(f'<option value="{k}">{LABEL_ZH[field][k]}</option>' for k in LABEL_ZH[field] if k in used)


VERIFIER = "GPT-5.5"


def page(rows, video_url, name, title=None):
    gens = sorted({r["generator"] for r in rows})
    used = lambda f: {r[f] for r in rows}
    n = collections.Counter(r["source"] for r in rows)
    title = title or " + ".join(f"{SOURCE_ZH[s]}-{n[s]}" for s in SOURCES if n[s])
    ls = "physfixbench_phyground_videophy2_review_v1" if name == "review_phyground_videophy2" else f"physfixbench_{name}_v1"
    script = SCRIPT.replace("__LS__", ls).replace("__EXPORT__", name.replace("review_", "physfixbench_", 1)
                                                                    + "_review_export.json")
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PhysFixBench · {title} · 人工审核页</title>
<style>{STYLE}</style></head>
<body>
<header>
 <h1>PhysFixBench · {title} · 人工审核页（{E(VERIFIER)} 三遍核实保留的物理缺陷视频；每条都需人工确认）</h1>
 <div class="filters">
  <label>数据源 <select id="f-source"><option value="">全部</option>{''.join(f'<option value="{s}">{SOURCE_ZH[s]}</option>' for s in SOURCES if n[s])}</select></label>
  <label>问题类别 <select id="f-family"><option value="">全部</option>{options('error_family', used('error_family'))}</select></label>
  <label>视频内容 <select id="f-interaction"><option value="">全部</option>{options('interaction_primitive', used('interaction_primitive'))}</select></label>
  <label>生成模型 <select id="f-generator"><option value="">全部</option>{''.join(f'<option value="{E(g)}">{E(g)}</option>' for g in gens)}</select></label>
  <label>难度 <select id="f-obv"><option value="">全部</option>{options('obviousness', used('obviousness'))}</select></label>
  <label><input type="checkbox" id="f-nh"> 仅看建议逐帧确认</label>
  <label><input type="checkbox" id="f-unreviewed"> 仅看未审核</label>
  <button class="exportbtn" id="export">导出审核结果 JSON</button>
  <span id="count"></span><span id="progress"></span>
 </div>
</header>
<main>
{''.join(card(r, video_url(r)) for r in rows)}
</main>
<script>{script}</script>
</body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=REPO, help="package root: data/, videos/ and the review pages go here")
    ap.add_argument("--hf-repo", required=True, help="HF dataset the videos are uploaded to, for the online page")
    ap.add_argument("--no-videos", action="store_true", help="skip linking the videos into <out-dir>/videos")
    ap.add_argument("--lists", nargs="+", default=SOURCES, help="out/<name>_top100.json lists to export")
    ap.add_argument("--page", default="review_phyground_videophy2", help="review page name, without .html")
    ap.add_argument("--verifier", default="GPT-5.5", help="the model that verified the lists, as the page names it")
    ap.add_argument("--title", help="page title; default names each source and its count")
    args = ap.parse_args()
    global VERIFIER
    VERIFIER = args.verifier

    os.makedirs(os.path.join(args.out_dir, "data"), exist_ok=True)
    os.makedirs(os.path.join(args.out_dir, "videos"), exist_ok=True)
    rows = []
    for name in args.lists:
        voted = json.load(open(os.path.join(HERE, "out", f"{name}_top100.json")))
        zh = json.load(open(os.path.join(HERE, "zh", f"{name}.json")))
        src = "phyground" if "phyground_video_id" in voted[0]["flawed_video"] else "videophy2"
        missing = [x["key"] for x in voted if x["key"] not in zh]
        assert not missing, f"{name}: no Chinese annotation for {missing[:3]}"
        recs = []
        for x in voted:
            r, path = build(src, x, zh[x["key"]])
            recs.append(r)
            if not args.no_videos:
                link_video(path, os.path.join(args.out_dir, "videos", r["video_file"]))
        assert len({r["video_file"] for r in recs}) == len(recs)
        base = os.path.join(args.out_dir, "data", f"physfixbench_{name}")
        with open(base + ".jsonl", "w") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        write_csv(base + ".csv", recs)
        rows += recs
        print(f"{name} ({src}): {len(recs)} records -> {base}.jsonl/.csv")
        for field in ["error_family", "interaction_primitive", "scenario_domain", "obviousness"]:
            c = collections.Counter(r[field] for r in recs)
            print(f"  {field}: " + " · ".join(f"{LABEL_ZH[field][k]} {v}" for k, v in c.most_common()))
        print(f"  generator: {dict(collections.Counter(r['generator'] for r in recs).most_common())}")
        print(f"  needs_human_frame_step_confirmation: {sum(r['needs_human_frame_step_confirmation'] for r in recs)}; "
              f"all claims visible: {sum(r['all_claimed_errors_visible'] for r in recs)}; "
              f"problems per record: {dict(sorted(collections.Counter(len(r['fix_problems']) for r in recs).items()))}")

    hf = f"https://huggingface.co/datasets/{args.hf_repo}/resolve/main/videos/"
    for fname, url in [(args.page + ".html", lambda r: "videos/" + r["video_file"]),
                       (args.page + "_hf.html", lambda r: hf + r["video_file"])]:
        with open(os.path.join(args.out_dir, fname), "w") as f:
            f.write(page(rows, url, args.page, args.title))
        print(f"wrote {fname}")


if __name__ == "__main__":
    main()
