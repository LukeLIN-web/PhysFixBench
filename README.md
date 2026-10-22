# PhysFixBench · Physion-300（draft v1，待人工审核）

300 条带**确认物理缺陷**的文生视频样本及结构化中文修复标注（fixing prompts），用于物理视频修复（physics video correction）的基准构建。本包为 draft 版本，所有标注等待人工审核（pass / edit / reject）。

## 数据来源与质控

- 视频来自 **Physion-Eval** severity-5 候选池（seed 20261019 抽样的 1000 条队列），覆盖 5 个生成模型：Veo 3.1 Fast / Wan 2.2 / Hailuo 2.3 / Sora 2 / Kling 2.5（各 59–61 条）。
- 每条经 **6fps 全片抽帧 + 原生帧率（24/30fps）逐帧放大复核**：仅保留官方标注的物理缺陷被视觉证据确认存在的样本（true positive）；误报、语义对齐/control/美学问题一律剔除。
- 选样在确认 TP 池上按 **问题类别 × 视频内容 41 个联合格子分层**，稀有内容（碰撞、抛射、工具操作）均覆盖；保留明显/细微难度梯度（细微 cue 共 112 条，标记 `needs_human_frame_step_confirmation=true` 的 104 条建议逐帧复核）。

## 文件

```
videos/<record_id>.mp4            300 个原始生成视频（共约 0.8 GB）
data/physfixbench_physion300.jsonl   每条一行完整标注（机器可读）
data/physfixbench_physion300.csv     同内容 CSV（Excel 可开，含待填审核列）
index.html                        人工审核页（本地打开即用；与 videos/ 同级）
review_hf.html                    HF 在线版（视频走 HF resolve URL）
```

## 标注字段（jsonl）

| 字段 | 含义 |
|---|---|
| `slot` | 在 1000 条复审队列中的槽位号 |
| `record_id` | Physion-Eval 记录 id（视频文件名同此） |
| `generator` | 生成模型 |
| `error_family` / `glitch_category_official` | 问题类别（时序/因果、物体永存/同一、力与运动、材质/状态、接触/交互、几何/碰撞、其他） |
| `interaction_primitive` | 视频内容（开合操作、倾倒/流动、拿取转移、形变/状态变化、肢体运动、多步操作、碰撞冲击、抛射） |
| `scenario_domain` | 场景域（流体/热/光、人/动物肢体、刚体、可变形材料、工具机械） |
| `obviousness` / `confidence` / `needs_human_frame_step_confirmation` | 复审难度与置信度 |
| `annotation` | Physion-Eval 官方英文标注逐字原文（可能为字符串或字符串数组） |
| `fix_problems` | **结构化修复标注·问题**：「第X.Xs–Y.Ys：问题」逐条 |
| `fix_expected` | **结构化修复标注·正确表现**：与 problems 逐条一一对应 |
| `fix_keep` | 修复时需保持不变的内容 |
| `fixing_prompt_zh` | 由上述三段拼成的完整修复指令（直接发给 videogen） |

修复指令示例：

```
【存在的问题】
1. 第6.1s–6.2s：搅拌碗突然消失并变成塑料布
【应修改为】
1. 搅拌碗应持续留在台面原位
请在保持棕面团形变、手臂、石台、排插、木勺杯、消毒器、蓝电器、透明塑料布保持不变的前提下，修正上述物理问题并重新生成视频。
```

## 人工审核方式

1. 打开 `index.html`（与 `videos/` 放在同一目录；或直接使用 HF 在线版 `review_hf.html`）。
2. 播放视频，对照 ❶问题 / ❷正确表现 / ❸保持不变 三块；可直接在框中修改文字。
3. 每条选择 **通过 / 需修改 / 驳回**，可填备注；结果自动暂存在浏览器。
4. 点右上角「导出审核结果 JSON」，回收 `physfixbench_physion300_review_export.json`。

## 分布概览（300 条）

- 问题类别：时序/因果 92 · 物体永存 54 · 力与运动 48 · 材质/状态 47 · 接触/交互 34 · 几何/碰撞 23 · 其他 2
- 视频内容：开合 117 · 倾倒/流动 87 · 拿取转移 43 · 形变 19 · 肢体运动 14 · 多步 11 · 碰撞 5 · 抛射 4
- 难度：明显 175 · 细微 112 · 未分级 13

## License / 使用限制

视频源自 Physion-Eval，遵循其 **Custom Research Use Only License**：仅限非商业研究与评测用途；不得违反适用法律或第三方权利再分发。本数据包仅用于学术研究。结构化中文标注为 PhysFixBench 项目所有（研究用途）。

## 仓库说明（GitHub）

视频文件（`videos/`，约 0.8 GB）托管在 Hugging Face dataset：[https://huggingface.co/datasets/HuggingFriends/PhysFixBench](https://huggingface.co/datasets/HuggingFriends/PhysFixBench)（私有，需在 HF 仓库 Settings → Collaborators 中邀请合作者）。
- 在线审核（视频走 HF URL）：直接打开本仓库的 [`index.html`](./index.html)（需有 HF 访问权限并已登录），或启用 GitHub Pages 后访问。
- 离线审核：clone HF 数据集，把仓库文件放到数据目录，用其中的 `index.html`（相对路径版）。
