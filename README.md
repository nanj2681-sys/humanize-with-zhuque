# Humanize with Zhuque

一个面向 Codex 的中文文章去 AI 味插件，内含同名 Skill。它把事实守恒、公文文种检查、腾讯朱雀全文检测和定向重写组合成有停止条件、可审计的工作流。

> 朱雀结果只是文本特征检测信号，不能证明作者身份、原创性或版权归属。

> 本项目是社区工具，不是腾讯官方项目，与腾讯无隶属、认证或背书关系。

## 能做什么

- 根据材料生成或改写中文文章，尤其适合政府报告、公文、调研报告、讲话稿和工作总结。
- 在送检前建立事实台账，保护日期、数字、金额、政策名称、条件、责任边界和语义强度。
- 默认使用腾讯朱雀官网的免费网页通道检测完整正文；官方 API 仅在用户明确选择时启用。
- 只重写疑似 AI 或确定 AI 的片段，并在每轮修改后重新检查事实。
- 只有全部门禁通过后才允许标记为最终稿。

默认检测门槛：

- 人工内容比例 `== 100%`
- 疑似 AI 比例 `== 0%`
- AI 内容比例 `== 0%`
- 所有分段均为 `label == 0`，不接受确定 AI 或疑似 AI 分段

该门槛按字面值严格执行，不保留平滑噪声容差；朱雀显示 `99.99%` 人工内容也不会通过。
检测报告以 `passed`、`failed_checks` 和 `*_percent_exact` 为准，避免接近 100% 的数值因显示舍入被误读。

## 仓库结构

```text
humanize-with-zhuque/
├── .codex-plugin/plugin.json
├── skills/
│   └── humanize-with-zhuque/
│       ├── SKILL.md
│       ├── agents/openai.yaml
│       ├── references/
│       │   ├── government-report-rules.md
│       │   └── zhuque-contract.md
│       ├── scripts/
│       │   ├── fact_guard.py
│       │   └── zhuque_gate.py
│       └── tests/test_workflow.py
├── LICENSE
└── THIRD_PARTY_NOTICES.md
```

## 安装

要求 Python 3.11 或更高版本。

仓库根目录现在是标准 Codex 插件，可登记到 Codex marketplace 后安装。插件清单位于 `.codex-plugin/plugin.json`，实际 Skill 位于 `skills/humanize-with-zhuque/`。安装或更新插件后，请新建 Codex 任务，使新的 Skill 版本生效。

如只需要传统的独立 Skill，可使用：

```bash
git clone https://github.com/nanj2681-sys/humanize-with-zhuque.git
cp -R humanize-with-zhuque/skills/humanize-with-zhuque ~/.codex/skills/
```

插件安装与独立 Skill 安装应二选一，不要同时启用两份 `humanize-with-zhuque`，以免 Codex 发现重复能力或后续更新发生版本漂移。

重新打开 Codex 后，可直接调用：

```text
$humanize-with-zhuque 请根据这些材料写一篇调研报告，按朱雀流程循环检测，达到全部门槛后再给我最终定稿。
```

## 工作流

```text
材料或原稿
  -> 文种与敏感性检查
  -> 建立事实台账
  -> 去 AI 味改写
  -> 事实守恒门禁
  -> 朱雀全文检测
  -> 定向修改未通过片段
  -> 最终全文复检
  -> 文档格式检查与定稿
```

默认每个批准批次最多提交六次，最后一次保留给最终完整正文。遇到验证码、登录、网页配额耗尽或请求结果不明时停止，不绕过平台限制，也不盲目重试。缺少 API 凭据只阻断 API 通道，不影响继续使用官网网页；用户明确要求 API-only 时除外。

## 辅助脚本

先创建已被 Git 忽略的运行目录，文章、事实台账和检测结果都放在其中：

```bash
mkdir -p run-artifacts
```

建立事实清单：

```bash
python3 skills/humanize-with-zhuque/scripts/fact_guard.py snapshot \
  --input run-artifacts/original-canonical.txt \
  --output run-artifacts/fact-ledger.json \
  --protect-file run-artifacts/protected-terms.txt
```

检查候选稿：

```bash
python3 skills/humanize-with-zhuque/scripts/fact_guard.py check \
  --input run-artifacts/candidate-01.txt \
  --manifest run-artifacts/fact-ledger.json \
  --output run-artifacts/fact-check-01.json
```

默认网页通道先生成不联网的浏览器交接状态：

```bash
python3 skills/humanize-with-zhuque/scripts/zhuque_gate.py \
  --input run-artifacts/candidate-01.txt \
  --output run-artifacts/detection-current.json
```

它应返回 `PENDING / WEBPAGE_RESULT_REQUIRED` 和官网地址，不会调用网络，也不需要 API Key。随后使用可见浏览器打开[腾讯朱雀 AI 检测助手](https://matrix.tencent.com/ai-detect/ai_gen)，读取页面当前免费次数，提交完整正文并保留截图或浏览器记录。

将网页可见结果规范化后，离线评估并更新当前状态：

```bash
python3 skills/humanize-with-zhuque/scripts/zhuque_gate.py \
  --response run-artifacts/visible-zhuque-result.json \
  --web-response \
  --input run-artifacts/candidate-01.txt \
  --output run-artifacts/detection-current.json
```

`--web-response` 将结果适配器标记为 `official_webpage`，但脚本本身不能证明来源，仍须保留当前网页截图或浏览器记录。

官方 API 是显式可选通道。只有用户明确选择并授权额度及潜在费用时，才加入 `--live-api` 并从环境变量读取配置：

```bash
export ZHUQUE_GATEWAY="https://ai-gateway.edgeone.link"
export ZHUQUE_API_KEY="<YOUR_API_KEY>"

python3 skills/humanize-with-zhuque/scripts/zhuque_gate.py \
  --live-api \
  --input run-artifacts/candidate-01.txt \
  --output run-artifacts/detection-api-attempt-01.json
```

即使环境中已有密钥，默认调用仍然走官网网页。API 缺凭据时，脚本会把阻塞范围标记为 `official_api` 并给出官网网页通道，不能据此声称“朱雀不可用”。每次 API 调用必须使用新的不可变输出文件；已有 `SUBMISSION_INTENT`、`RESULT_UNKNOWN` 或标记为可能消耗额度的事件不会被覆盖，也不会再次发起请求。若请求返回后主输出文件仍被其他进程锁定，脚本会在同目录写入唯一的 `*.recovery-*.json` 保存完整结果，并要求人工核对后再重试。API 调用可能消耗免费额度或产生费用；不要把密钥写入仓库、命令行参数、日志或检测报告。

`detection-current.json` 表示当前正文的最新朱雀子门禁。网页得到与当前正文 SHA-256 和完整分段覆盖绑定的有效结果后，可以替代早期 API 适配器阻断成为当前状态；历史证据仍保留，`RESULT_UNKNOWN` 以及隐私、事实、文种和格式阻塞不得被覆盖。

## 测试

```bash
python3 -m unittest discover \
  -s skills/humanize-with-zhuque/tests \
  -v
```

当前 48 项测试覆盖官网网页默认选择、API 显式启用及回退状态、受保护 API 事件不可覆盖、并发写入、恢复记录与失败脱敏、严格 100% 阈值边界、精确小数、汇总与分段矛盾、响应与正文绑定、旧结果复用、复合中文数量、日期金额保护、异常输入和失败关闭行为。

## 安全与限制

- 不会为了降低检测分数改写事实、法律含义、政策力度或责任边界。
- 机械事实检查不能替代人工核对人名、归属、条件、否定、时态及上下文含义。
- 涉密、内部、未公开或含个人信息的材料，不应在未获授权时发送给外部检测服务。
- 网页模式只操作可见界面，不绕过验证码、登录、配额或限流。
- 离线 JSON 校验能证明结果与正文内容对应，但不能单独证明 JSON 确由腾讯生成或仍然新鲜。
- 不应用于学术作弊、冒充纯人工创作，或规避法律、合同、学校、平台要求的 AI 使用披露。
- `humanizer-chinese` 是可选的外部 Skill，本仓库不捆绑其代码；未安装时核心流程仍可使用。

## 官方资料

以下接口和额度信息于 2026-09-26 核对，之后可能变化，请以官方当期文档为准。

- [腾讯朱雀 AI 检测助手](https://matrix.tencent.com/ai-detect/ai_gen)
- [腾讯云 EdgeOne Makers：使用朱雀模型](https://cloud.tencent.com/document/product/1552/137539)

## 开源许可

本项目采用 [MIT License](LICENSE)。上游项目的版权与许可声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
