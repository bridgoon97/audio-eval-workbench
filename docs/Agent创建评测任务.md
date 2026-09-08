# Agent 创建评测任务（操作手册）

面向**通用编码 Agent**：阅读本手册后，配合 `audio-eval agent ...` CLI，可以把用户已整理好的音频安全地编排为听鉴评测任务。所有逻辑位于 `workbench/agent_tasks.py` 与 `workbench/agent_cli.py`；本文描述交互口径，不是接口文档。

## 0. 硬性禁止事项（先读）

- **禁止静默转换**：不得自动选择通道、裁切区间、归一化、对齐或增益。非 16 kHz 源必须由用户在 manifest 中显式许可重采样；未许可时 `validate`/`prepare`/`apply` 都会拒绝。
- **禁止覆盖源文件**：`prepare` 只写新的输出目录；输出目录已存在且非空、或位于素材目录内时会被拒绝。
- **原始相对电平神圣**：只允许显式申请的采样率转换（soxr VHQ）、通道选择与截取；响度/对齐只能通过服务端 processing API 且会被服务端判据复核。
- **凭据卫生**：密码只经 stdin 或环境变量进入 CLI；manifest、mapping.json、state、日志、shell history、本仓库一律不得出现密码、Cookie、CSRF。错误信息不回显凭据。
- **盲评边界**：候选显示名可以真实（服务端按人匿名映射），但映射在任务关闭前不会揭晓；Agent 不得在回执之外自行猜测或标注“哪个是哪个”。
- **不越权**：CLI 只走公开 HTTP API；评测者不能创建任务，组织者只能管理自己的任务，管理员可管理全站。CLI 不提供任何超级接口。

## 1. 开始前必须向用户确认的问题（最小集合）

在生成 manifest 之前，逐项确认；**不要从文件名自行推断**：

1. **比较集合**：哪些候选属于同一输入与同一时间段？（不同问题/不同输入不能放进同一片段。）
2. **候选显示名与版本证据**：每个候选给用户看的名称，以及 commit/模型 SHA 等版本证据。
3. **通道**：每个源文件用哪个通道？（多通道文件必须显式给 0 起始的通道号；单声道也给 0。）
4. **截取区间**：是否统一截取某一段时间（秒）？不截取则整段。
5. **任务模式**：`development`（开发诊断，显示版本与波形）还是 `blind`（隐藏版本独立评测）？
6. **参与者**：哪些账号参与评测？（必须是已存在的精确账号名，歧义会被拒绝。）
7. **对齐/响度**：是否申请服务端对齐/响度处理？若申请，哪一个是参考候选？服务端判据可能拒绝（如低相关、不可应用），拒绝时默认停止发布。
8. **是否发布**：manifest `publish=true` 且 CLI `--publish` 同时满足才发布；只满足其一时保留草稿。

**可以从目录结构安全推断的**：仅“哪些文件属于同一候选组”这类用户已声明的目录约定（例如 `sources/<候选名>/<片段>.wav`），且推断结果必须回显给用户确认后写入 manifest。
**绝不能猜的**：通道、截取区间、重采样许可、参考候选、参与者、任务模式、publish。

## 2. 标准目录布局与 manifest

```
工作目录/
├── manifest.json                  # 你生成的任务清单
├── sources/
│   ├── aibf-v1/indoor-001.wav     # 源音频（任意采样率/通道；prepare 不改它们）
│   └── aibf-v2/indoor-001.wav
└── prepared/                      # prepare 的输出目录（必须是空目录）
    ├── mapping.json               # 源↔副本映射与 SHA
    └── samples/indoor-001/*.wav   # 16 kHz 单声道 float32 副本
```

manifest 字段以 `docs/task-manifest.schema.json` 为准，示例见 `docs/task-manifest.example.json`。要点：

- 所有路径相对 **manifest 所在目录**；`..`、绝对路径、逃逸目录会被拒绝。
- `samples[].key` / `candidates[].key` 是稳定标识（断点续传的锚点），生成后不要改。
- `candidates[].channel` 必填（0 起始）；`reference` 每题至多一个且仅在申请处理时需要。
- `publish` 缺省即 false；**不得仅因字段缺失推断为 true**。
- 凭据绝不写进 manifest。

## 3. 命令序列

```bash
# 0) 生成模板并按第 1 节问题填写
audio-eval agent manifest-init --output manifest.json

# 1) 离线校验（无服务、无副作用）；返回 JSON，problems 非空则先解决
audio-eval agent validate manifest.json

# 2) 生成 16 kHz 单声道合规副本与 mapping.json（绝不覆盖源）
audio-eval agent prepare manifest.json --output-dir prepared

# 3) 编排到正在运行的服务（幂等，可反复执行直至完成）
audio-eval agent apply manifest.json \
  --server http://127.0.0.1:8765 \
  --user 组织者账号 \
  --password-stdin \
  --state agent-state.json \
  --mapping prepared/mapping.json \
  --json

# 4) 查看进度
audio-eval agent status --state agent-state.json --json

# 5) 显式发布（双确认：manifest.publish=true 且 --publish）
audio-eval agent apply manifest.json --server http://127.0.0.1:8765 \
  --user 组织者账号 --password-stdin --state agent-state.json \
  --mapping prepared/mapping.json --publish --json
```

- `--server` 默认仅允许 `http://127.0.0.1`、`http://localhost` 或 `https://`；访问局域网明文 HTTP 必须显式 `--allow-insecure-http`（CLI 会提示风险）。
- 密码二选一：`--password-stdin`（终端输入）或 `--password-env MY_VAR`。没有 `--password` 参数，这是有意的。
- 首次 `apply` 必须给 `--mapping prepared/mapping.json`；之后 state 会记住。
- 服务版本与 CLI 必须一致；state 会记录实例编号与数据编号，连错服务会拒绝。

## 4. 幂等与失败恢复

- state 文件原子写（临时文件 + 替换），记录 task/sample/track ID、源与副本 SHA、阶段；**每个候选上传成功立即落盘**。
- 网络中断后直接重跑同一条 `apply`：已完成项跳过，只补剩余项；服务端不会出现重复任务/片段/候选。
- 重跑前若源文件字节或 manifest 关键字段（名称/版本/通道/路径/参考/处理意向/publish）发生变化，CLI 会停止并列出**字段级差异**，不会静默覆盖；确属有意变更时换新的 state 与输出目录重来。
- state 绑定服务实例编号与数据编号；连到另一实例/数据目录会拒绝。
- `status` 输出 JSON 进度，可用于向用户汇报“已完成/剩余”。

## 5. 发布前检查表（CLI 自动执行，Agent 应理解）

1. manifest 离线校验通过；mapping 与副本 SHA 复核一致。
2. 服务端任务仍为 `draft` 且当前账号可管理。
3. 片段数、每题候选数与服务端一致；每个候选的 SHA 逐一与服务端音频比对。
4. 参与者全部在受邀名单。
5. 对齐/响度：服务端判据拒绝时——默认停止（回执列出拒绝码）；仅当显式 `--keep-original-on-rejection` 才保留原始继续**草稿**，且不会静默发布混合口径。
6. `manifest.publish=true` **且** `--publish` 同时满足才调用发布；发布后任务冻结，不可继续修改。

## 6. 边界与口径

- **盲评身份**：`blind` 模式下参与者看到的是匿名候选；真实名称、原始/派生 SHA 记录在 mapping 与回执中供组织者追溯，但匿名映射在任务关闭前不可揭晓，Agent 不得提前推断。
- **相对电平**：prepare 的转换（重采样/通道/截取）不改变相对电平，也不做归一化；响度只经服务端 processing（活动段 RMS 固定增益，不是 LUFS）。
- **转换记录**：mapping.json 逐候选记录源/输出路径、通道、segment、采样率、样本数、双 SHA 与执行的转换说明。
- **服务端校验**：上传后服务端仍会校验采样率、长度与数值合法性；CLI 回执中的拒绝项必须如实转达用户。

## 7. 把结果转述给用户

`--json` 回执是给程序读的；转述给用户时至少包括：

- **任务 ID** 与服务地址（用户可在浏览器打开工作台核对）；
- 已创建的片段/候选清单（stable key → 显示名）；
- 参与者解析结果；
- 对齐/响度的实际 lag、增益与 reason code，或被拒绝项；
- 未解决问题（例如等待用户确认的转换规则、被拒绝的处理）；
- 当前状态：草稿还是已发布。

## 8. 未来 MCP 封装（设计小节）

本功能的逻辑都在可导入的 Python 函数中，MCP server 只需薄封装：

- `load_manifest` / `validate_manifest` → MCP tool `manifest_validate(manifest_text)`；
- `prepare_assets` → `manifest_prepare(manifest_text, output_dir)`；
- `ApplyState` + `run_apply` → `manifest_apply(...)`（凭据经 MCP 宿主的 secret 注入，不经模型上下文）；
- `ApplyState.load(...).payload` → `manifest_status(state_path)`。

CLI（`workbench/agent_cli.py`）只是这些函数的 argparse 包装；MCP 层不应复制任何编排逻辑。
