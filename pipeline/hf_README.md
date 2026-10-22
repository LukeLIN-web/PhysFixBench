---
license: other
license_name: research-only-mixed-sources
language:
- zh
- en
pretty_name: PhysFixBench · PhyGround + VideoPhy2
configs:
- config_name: phyground
  data_files: data/physfixbench_phyground.jsonl
- config_name: videophy2
  data_files: data/physfixbench_videophy2.jsonl
---

# PhysFixBench · PhyGround-91 + VideoPhy2-100（draft v1，待人工审核）

191 条带物理缺陷的生成视频及结构化中文修复标注（fixing prompts），用于物理视频修复（physics video correction）的基准构建。格式与 [PhysFixBench · Physion-300](https://huggingface.co/datasets/HuggingFriends/PhysFixBench) 相同：同样的 jsonl / csv 字段、同样的修复指令写法、同样的人工审核页。本包为 draft 版本，所有标注等待人工审核（pass / edit / reject）。

## 数据来源与质控

- **PhyGround（91 条）**：PhyGround 人工评测（剔除未过质检的标注者，每条视频取各标注者均分）中，语义对齐 SA ≥ 3.5、至少一条物理定律均分 ≤ 2.5、且并非所有定律都低（整体崩坏不收）的视频。覆盖 8 个生成模型。
- **VideoPhy2（100 条）**：VideoPhy2 test 集中 SA ≥ 4、物理常识 PC ≤ 3、标注者至少写了一条违反的物理规则的视频；去掉分辨率低的 VideoCrafter。同一 prompt 只留排名最高的一条；与 PhyGround 名单 prompt 重复的，排在同票数的其他视频之后。
- **GPT-5.5 逐帧核实**：每条候选按 4 fps 抽帧并标时间戳，连同生成 prompt 和人工标签交给 GPT-5.5。人工标签（PhyGround 的低分定律；VideoPhy2 的违反规则原句）逐条当作待核实的说法，判 supported / not_supported（VideoPhy2 另有 not_in_video，用来识别讲的东西视频里根本没有的标注噪声），并写出缺陷时间段、描述、修复指令和需保持的内容；或者给出拒收理由：无物理错误、只有语义问题、原标注误报、整体崩坏、画质差、不可修。
- **三遍投票**：单次调用不稳定，每条跑三遍，按保留票数排序，同票数再按平均严重度、平均置信度排序。三遍都保留的记为 confident，否则记为 borderline。至少一票保留才进名单。票数不代表判对，所以每一条都需要人工审。
- **中文修复标注**：「问题 / 正确表现 / 保持不变」三段和四个分类标签（`error_family` / `interaction_primitive` / `scenario_domain` / `review_error_category`）由 Claude 根据 GPT-5.5 的英文错误描述、修复指令、保持内容和生成 prompt 改写、归类，**Claude 没有看视频**，准确度以 GPT-5.5 的英文为上限。每条问题的时间段直接沿用 GPT-5.5 给的（4 fps 精度，所以有 0.25s 级的值）。英文原文保存在 `error_description_en` / `correction_instruction_en` / `protected_content_en` 中。

## 文件

```
videos/<video_file>                       191 个原始生成视频（H.264，共约 255 MB）
data/physfixbench_phyground.jsonl         PhyGround 每条一行完整标注（机器可读）
data/physfixbench_phyground.csv           同内容 CSV（Excel 可开，含待填审核列）
data/physfixbench_videophy2.jsonl         VideoPhy2，同上
data/physfixbench_videophy2.csv
review_phyground_videophy2.html           人工审核页（本地打开即用；与 videos/ 同级）
review_phyground_videophy2_hf.html        HF 在线版（视频走 HF resolve URL）
```

## 标注字段（jsonl）

前 18 个字段与 Physion-300 同名、同顺序、同取值集合；含义有差异的地方如下。

| 字段 | 本包中的含义 |
|---|---|
| `dir_name` | `<slot 四位>_<record_id 中 :: 换成 _>` |
| `record_id` | PhyGround：`phyground::<模型>::<视频名>`；VideoPhy2：`videophy2::test::vp2_<csv 行号>_<模型>` |
| `slot` | 在本源名单中的排名（1 = 票数与严重度最高） |
| `generator` | 生成模型（原始 id 在 `generator_id`） |
| `error_family` / `interaction_primitive` / `scenario_domain` / `review_error_category` | 与 Physion-300 同一套取值，由 Claude 根据 GPT-5.5 的英文描述和生成 prompt 归类（未看视频） |
| `glitch_category_official` | 源数据集自带的物理标签：PhyGround 为人工判低分的定律名；VideoPhy2 为违反规则所对应的物理定律（VideoPhy2 数据集自带标签，由 Gemini-Flash 标注） |
| `obviousness` | GPT-5.5 保留票的平均严重度（1–5，「错误对观众有多明显」）≥ 4 记为 `obvious`，否则 `subtle` |
| `confidence` | GPT-5.5 保留票的平均置信度 |
| `needs_human_frame_step_confirmation` | 三遍结论不一致（borderline）时为 `true` |
| `all_claimed_errors_visible` | 被采用的那一遍里，每条人工标注都被判为 supported |
| `annotation` | VideoPhy2：标注者给出的违反规则原句（字符串数组，逐字）。PhyGround 没有文字标注，这里是各维度和各定律人工均分的一行英文摘要，附标注者备注原文 |
| `fix_problems` | 「第X.Xs–Y.Ys：问题」逐条；时间段来自 GPT-5.5，问题文字由 Claude 写 |
| `fix_expected` | 正确表现，与 problems 逐条一一对应 |
| `fix_keep` | 修复时需保持不变的内容 |
| `fixing_prompt_zh` | 由上述三段拼成的完整修复指令（拼法与 Physion-300 完全相同） |

附加字段（Physion-300 没有）：

| 字段 | 含义 |
|---|---|
| `source` | `phyground` / `videophy2` |
| `video_file` | `videos/` 下的文件名 |
| `prompt` | 生成该视频用的文本 prompt |
| `gpt_keep_votes` / `gpt_tier` / `gpt_severity` | 三遍中保留的票数、confident / borderline、保留票的平均严重度 |
| `error_evidence` | 每条错误的起止时间和关键时间戳（与 `fix_problems` 一一对应） |
| `error_description_en` / `correction_instruction_en` / `protected_content_en` | GPT-5.5 看抽帧写的英文原文（中文三段的来源） |
| `error_category_en` / `violated_laws_en` | GPT-5.5 给的错误类别和违反的定律 |
| `human_labels` | 源数据集人工标签原样：PhyGround 为 SA / PTV / persistence 和各定律均分、低分定律、备注；VideoPhy2 为 SA / PC、违反规则原句（V = 从规则表勾选，W = 标注者自写）及其定律标签 |
| `claim_checks` | GPT-5.5 对每条人工标注的逐帧核实（verdict、证据、时间戳） |
| `gpt_votes` | 三遍各自的结论一行 |

修复指令示例：

```
【存在的问题】
1. 第0.25s–3.75s：松开的马克杯向上漂浮，后闪现到砖上且未摔碎
【应修改为】
1. 马克杯应连续加速下落，撞砖后摔成陶瓷碎片
请在保持固定镜头、白色投影幕、木地板、砖块位置、顶部夹具与光照保持不变的前提下，修正上述物理问题并重新生成视频。
```

## 人工审核方式

1. 打开 `review_phyground_videophy2.html`（与 `videos/` 放在同一目录），或直接使用 HF 在线版 `review_phyground_videophy2_hf.html`。
2. 播放视频，对照 ❶问题 / ❷正确表现 / ❸保持不变 三块；可直接在框中修改文字。展开「英文核实证据」可看 GPT-5.5 对每条人工标注的裁决和三遍投票。
3. 每条选择 **通过 / 需修改 / 驳回**，可填备注；结果自动暂存在浏览器。可按数据源、问题类别、视频内容、生成模型、难度筛选。
4. 点右上角「导出审核结果 JSON」，回收 `physfixbench_phyground_videophy2_review_export.json`。

## 分布概览

**PhyGround（91 条）**
- 问题类别：力与运动 32 · 材质/状态 27 · 物体永存/同一 17 · 几何/碰撞 9 · 时序/因果 3 · 接触/交互 3
- 视频内容：碰撞冲击 46 · 倾倒/流动 14 · 抛射 12 · 肢体运动 8 · 形变/状态变化 5 · 多步操作 4 · 拿取转移 2
- 难度：明显 44 · 细微 47；三遍都保留 51 · 边界 40
- 生成模型：Veo 3.1 16 · Wan 2.2 I2V-A14B 13 · Cosmos-Predict2.5 2B 12 · LTX-2 19B 12 · OmniWeaving 12 · LTX-2.3 22B 10 · Cosmos-Predict2.5 14B 10 · Wan 2.2 TI2V-5B 6

**VideoPhy2（100 条）**
- 问题类别：力与运动 30 · 材质/状态 28 · 物体永存/同一 20 · 接触/交互 10 · 时序/因果 7 · 几何/碰撞 4 · 其他 1
- 视频内容：碰撞冲击 29 · 形变/状态变化 20 · 肢体运动 15 · 倾倒/流动 10 · 抛射 10 · 多步操作 9 · 拿取转移 6 · 开合操作 1
- 难度：明显 56 · 细微 44；三遍都保留 89 · 边界 11
- 生成模型：Wan 31 · Cosmos 29 · Ray2 15 · CogVideoX 12 · HunyuanVideo 11 · Sora 2

PhyGround 名单不足 100 条（至少一票保留的只有 91 条），补足方式待定。

## License / 使用限制

视频分别来自 PhyGround 的生成视频和 VideoPhy2 test 集，遵循各自原始发布的许可及对应生成模型的使用条款，仅限非商业研究与评测用途。结构化中文标注为 PhysFixBench 项目所有（研究用途）。

## 代码

筛选、GPT-5.5 核实、投票和导出脚本在 GitHub：<https://github.com/LukeLIN-web/PhysFixBench>（`pipeline/`）。
