# Humanize with Zhuque

一个面向 Codex 的中文文章去 AI 味 Skill。它把事实守恒、公文文种检查、腾讯朱雀全文检测和定向重写组合成有停止条件、可审计的工作流。

> 朱雀结果只是文本特征检测信号，不能证明作者身份、原创性或版权归属。

> 本项目是社区工具，不是腾讯官方项目，与腾讯无隶属、认证或背书关系。

## 能做什么

- 根据材料生成或改写中文文章，尤其适合政府报告、公文、调研报告、讲话稿和工作总结。
- 在送检前建立事实台账，保护日期、数字、金额、政策名称、条件、责任边界和语义强度。
- 使用腾讯朱雀网页或官方 API 检测完整正文。
- 只重写疑似 AI 或确定 AI 的片段，并在每轮修改后重新检查事实。
- 只有全部门禁通过后才允许标记为最终稿。

默认检测门槛：

- 人工内容比例 `>= 80%`
- 疑似 AI 比例 `< 20%`
- 不存在 `label == 1` 的确定 AI 分段
- 聚合 AI 比例 `<= 0.1%`（本 Skill 的保守噪声容差，不是腾讯官方承诺）

## 仓库结构

```text
humanize-with-zhuque/
├── SKILL.md
├── agents/openai.yaml
├── references/
│   ├── government-report-rules.md
│   └── zhuque-contract.md
├── scripts/
│   ├── fact_guard.py
│   └── zhuque_gate.py
└── tests/test_workflow.py
```

## 安装

要求 Python 3.11 或更高版本。

```bash
git clone https://github.com/nanj2681-sys/humanize-with-zhuque.git
cp -R humanize-with-zhuque/humanize-with-zhuque ~/.codex/skills/
```

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

默认每个批准批次最多提交六次，最后一次保留给最终完整正文。遇到验证码、登录、配额、缺少凭据或请求结果不明时停止，不绕过平台限制，也不盲目重试。

## 辅助脚本

先创建已被 Git 忽略的运行目录，文章、事实台账和检测结果都放在其中：

```bash
mkdir -p run-artifacts
```

建立事实清单：

```bash
python3 humanize-with-zhuque/scripts/fact_guard.py snapshot \
  --input run-artifacts/original-canonical.txt \
  --output run-artifacts/fact-ledger.json \
  --protect-file run-artifacts/protected-terms.txt
```

检查候选稿：

```bash
python3 humanize-with-zhuque/scripts/fact_guard.py check \
  --input run-artifacts/candidate-01.txt \
  --manifest run-artifacts/fact-ledger.json \
  --output run-artifacts/fact-check-01.json
```

离线评估已绑定正文的朱雀响应：

```bash
python3 humanize-with-zhuque/scripts/zhuque_gate.py \
  --response run-artifacts/visible-zhuque-result.json \
  --input run-artifacts/candidate-01.txt \
  --output run-artifacts/detection-01.json
```

官方 API 模式必须显式加入 `--live-api`，并从环境变量读取配置：

```bash
export ZHUQUE_GATEWAY="https://ai-gateway.edgeone.link"
export ZHUQUE_API_KEY="<YOUR_API_KEY>"

python3 humanize-with-zhuque/scripts/zhuque_gate.py \
  --live-api \
  --input run-artifacts/candidate-01.txt \
  --output run-artifacts/detection-01.json
```

API 调用可能消耗免费额度或产生费用。不要把密钥写入仓库、命令行参数、日志或检测报告。

## 测试

```bash
python3 -m unittest discover \
  -s humanize-with-zhuque/tests \
  -v
```

当前测试覆盖阈值边界、精确小数、响应与正文绑定、旧结果复用、复合中文数量、日期金额保护、异常输入和失败关闭行为。

## 安全与限制

- 不会为了降低检测分数改写事实、法律含义、政策力度或责任边界。
- 机械事实检查不能替代人工核对人名、归属、条件、否定、时态及上下文含义。
- 涉密、内部、未公开或含个人信息的材料，不应在未获授权时发送给外部检测服务。
- 网页模式只操作可见界面，不绕过验证码、登录、配额或限流。
- 离线 JSON 校验能证明结果与正文内容对应，但不能单独证明 JSON 确由腾讯生成或仍然新鲜。
- 不应用于学术作弊、冒充纯人工创作，或规避法律、合同、学校、平台要求的 AI 使用披露。
- `humanizer-chinese` 是可选的外部 Skill，本仓库不捆绑其代码；未安装时核心流程仍可使用。

## 官方资料

以下接口和额度信息于 2026-09-25 核对，之后可能变化，请以官方当期文档为准。

- [腾讯朱雀 AI 检测助手](https://matrix.tencent.com/ai-detect/ai_gen)
- [腾讯云 EdgeOne Makers：使用朱雀模型](https://cloud.tencent.com/document/product/1552/137539)

## 开源许可

本项目采用 [MIT License](LICENSE)。上游项目的版权与许可声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
