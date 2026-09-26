# 腾讯朱雀文本检测契约

## 官方入口

- 网页：<https://matrix.tencent.com/ai-detect/ai_gen>
- 中国站 API 文档：<https://cloud.tencent.com/document/product/1552/137539>
- 国际站参考文档：<https://intl.cloud.tencent.com/zh/document/product/1145/82374>
- 核对日期：2026-09-26

网页配额和界面可能变化。每次运行读取页面当前显示，不把旧版规范里的固定次数写入自动化逻辑。

## 通道选择

- 默认使用官网网页免费通道。仅传入 `--input` 时，`zhuque_gate.py` 必须返回 `PENDING / WEBPAGE_RESULT_REQUIRED` 和官网地址，不得联网，也不得索要 API Key。
- 即使环境变量中已有 API 凭据，也不能自动改走 API。
- 只有用户明确选择 API 或 API-only、授权当前额度与潜在费用，并显式加入 `--live-api` 时，才允许调用 API。
- API 缺凭据只阻断 `official_api` 适配器。除非用户要求 API-only，否则继续官网网页，不得把它表述为“朱雀不可用”。
- API 请求进入 `RESULT_UNKNOWN` 后不得自动切换网页或重试，因为该请求可能已经处理并计入额度。

## 可选正式 API

API 通过 EdgeOne Makers 提供。截至上述核对日期，中国站文档说明文本检测每月有 50 万 token 免费额度，图片检测或超额需联系企业服务。额度、计费、权限和可用区域可能变化；每次使用前以当前账号控制台和当期文档为准。需要用户自己的 API Key，`ZHUQUE_GATEWAY` 通常设为 `https://ai-gateway.edgeone.link`。

- 路由：`POST /v1/providers/zhuque-text/classify`
- Header：`Authorization: Bearer ...`
- Body：`{"text": "完整正文", "is_merge": false}`
- 同步文本调用建议超时至少 30 秒。
- 超时或连接中断后无法确定服务端是否已处理时，先核对服务商状态；不得无界重试。

密钥只从 `ZHUQUE_API_KEY` 环境变量读取。网关从 `ZHUQUE_GATEWAY` 读取。不得把密钥写入 Skill、命令行参数、日志、测试夹具或检测报告。

命令行只有显式加入 `--live-api` 才会发起一次可能消耗额度或产生费用的请求。没有 `--live-api` 或 `--response` 时进入默认网页交接状态，不是错误。`--live-api` 与 `--response` 不得同时使用，且联网模式必须提供新的、不可变的 `--output` 路径。脚本会先写入不含正文的 `SUBMISSION_INTENT`，再检查 API 配置并发起请求；该路径若已存在，必须拒绝覆盖并且不得再次请求。请求返回后若主输出仍被其他进程锁定，必须在同目录以唯一 `*.recovery-*.json` 保存完整结果，并保留可能计费和禁止重试标志，人工归并前不得重试。

## 响应映射

成功同步响应的关键字段：

```json
{
  "status": "success",
  "labels_ratio": {"0": 1.0, "1": 0.0, "2": 0.0},
  "segment_labels": [
    {"label": 0, "conf": 0.93, "order": 1, "position": [0, 120], "text": "..."}
  ]
}
```

- `labels_ratio["0"]`：人工内容占比。
- `labels_ratio["1"]`：AI 内容占比。
- `labels_ratio["2"]`：疑似 AI 内容占比。
- `segment_labels[].label == 0`：人工段落。
- `segment_labels[].label == 1`：确定 AI 段落。
- `segment_labels[].label == 2`：疑似 AI 段落。

异步响应状态为 `QUEUED`、`PROCESSING`、`SUCCEEDED` 或 `FAILED`。`SUCCEEDED` 的 `Output` 是字符串化 JSON，只解析一次后再应用相同门槛。

## 本 Skill 的验收门槛

同时满足：

```text
status == success
labels_ratio["0"] == 1
labels_ratio["2"] == 0
labels_ratio["1"] == 0
segment_labels 中所有分段均为 label == 0
```

本 Skill 按“人工内容 100%”的字面要求严格验收，不接受平滑或舍入噪声。即使 AI 或疑似 AI 比例只有 `0.0001` 也不通过；任何 `label == 1` 或 `label == 2` 分段也直接不通过。汇总比例与分段结果互相矛盾时失败关闭，不用其中一项覆盖另一项。

检测证书和边界判断使用输出中的 `passed`、`failed_checks` 与 `*_percent_exact` 字段。兼容保留的数值型百分比字段经过四位小数显示舍入，不能单独作为是否达到 100% 的依据。

`softmax_confidence` 和 `ratio_confidence` 不是用户设定的验收条件，不得代替上述条件。

## 结果状态

- `PASS`：响应有效且所有门槛通过。
- `REVISE`：响应有效，但至少一个内容门槛未通过。
- `PENDING`：异步任务尚未完成，或默认官网网页仍等待可见结果。`WEBPAGE_RESULT_REQUIRED` 不是检测失败。
- `BLOCKED`：`blocker_scope` 指定的通道或总体条件阻止继续。`official_api` 缺凭据不代表官网网页不可用。
- `RESULT_UNKNOWN`：请求可能已经提交、消耗额度或产生费用，但因超时、网络错误或服务端错误没有拿到可判定结果；核实服务商状态前不得重试。
- `ERROR`：响应缺字段、类型错误、数值越界、比例和明显异常、分段缺失或服务报错。

未知、错误或不完整结果必须失败关闭，不能视为通过。

脚本输出中的 `scope` 固定为 `zhuque_detector_gate`。其中的 `PASS` 只代表朱雀子门禁通过，不代表事实守恒、文种评分和交付文件检查已经通过。

每次实际检测尝试都作为不可变事件保留。当前朱雀子门禁采用与当前正文 SHA-256 和完整分段覆盖绑定的最新有效结果。后续网页 `PASS` 或 `REVISE` 可以使早期适配器级 `BLOCKED` 不再是当前状态，但不得删除历史记录，也不得清除隐私授权、缺失事实、事实守恒、文种或格式等总体阻塞。此归并规则绝不适用于仍未核实的 `RESULT_UNKNOWN`。

## 结果与正文绑定

- API 直连模式由同一进程提交正文并立即解析返回值。即便如此，脚本仍会要求每个返回分段具有精确 `text` 和 `[start, end]`，范围不重叠、与正文逐字一致，并覆盖全部非空白字符；否则直接 `ERROR`。
- 离线 `--response` 模式必须同时提供 `--input`。每个分段都要含精确的 `text` 和 `[start, end]`，这些范围须与正文逐字一致并覆盖全部非空白字符。
- 网页可见结果应同时加入 `--web-response`，使输出标记为 `adapter == "official_webpage"`。该标记是操作员对可见页面的声明，不是脚本自行验证的来源证明；必须另存截图或浏览器会话记录。
- 保存的响应顶层必须加入正文的 `input_sha256`；哈希不匹配或缺失直接报错。哈希用于拦截不同空白或不同候选稿复用结果，但同一份不受信任 JSON 里的自报哈希仍不能单独证明结果来源，绝不替代完整分段校验。
- 哈希不匹配、分段错位、结果来自旧稿或无法证明对应关系时一律 `ERROR`，不能沿用旧分数给新稿判定通过。
- 离线分段校验只能证明 JSON 内容与正文对应，不能证明该 JSON 确由腾讯生成，也不能证明检测时间。网页模式须保留当前可见结果的截图或浏览器会话记录；脚本 `checked_at` 只是本地评估时间。需要强来源绑定时使用同进程官方 API。

## 默认官网网页通道

网页模式会把文章正文发送给腾讯。优先使用页面当前显示的访客或登录用户免费次数；存在可用次数时不需要 API Key。页面也可能出现登录、配额或验证码。只使用可见 UI；不绕过验证码，不调用私有接口，不自动点击赞踩。若页面无法提供三类比例或分段标签，结果不足以证明通过，应标为 BLOCKED 而不是 PASS。

全文结果才可用于最终验收。分块检测只能定位问题，不能把多个分块比例加权后冒充全文结论。
