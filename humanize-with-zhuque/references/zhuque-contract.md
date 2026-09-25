# 腾讯朱雀文本检测契约

## 官方入口

- 网页：<https://matrix.tencent.com/ai-detect/ai_gen>
- 中国站 API 文档：<https://cloud.tencent.com/document/product/1552/137539>
- 国际站参考文档：<https://intl.cloud.tencent.com/zh/document/product/1145/82374>
- 核对日期：2026-09-25

网页配额和界面可能变化。每次运行读取页面当前显示，不把旧版规范里的固定次数写入自动化逻辑。

## 正式 API

API 通过 EdgeOne Makers 提供。截至上述核对日期，中国站文档说明文本检测每月有 50 万 token 免费额度，图片检测或超额需联系企业服务。额度、计费、权限和可用区域可能变化；每次使用前以当前账号控制台和当期文档为准。需要用户自己的 API Key，`ZHUQUE_GATEWAY` 通常设为 `https://ai-gateway.edgeone.link`。

- 路由：`POST /v1/providers/zhuque-text/classify`
- Header：`Authorization: Bearer ...`
- Body：`{"text": "完整正文", "is_merge": false}`
- 同步文本调用建议超时至少 30 秒。
- 超时或连接中断后无法确定服务端是否已处理时，先核对服务商状态；不得无界重试。

密钥只从 `ZHUQUE_API_KEY` 环境变量读取。网关从 `ZHUQUE_GATEWAY` 读取。不得把密钥写入 Skill、命令行参数、日志、测试夹具或检测报告。

命令行只有显式加入 `--live-api` 才会发起一次可能消耗额度或产生费用的请求；缺少该标志时必须失败关闭。`--live-api` 与 `--response` 不得同时使用，且联网模式必须提供 `--output`。脚本会先写入不含正文的 `SUBMISSION_INTENT`，再发起请求。

## 响应映射

成功同步响应的关键字段：

```json
{
  "status": "success",
  "labels_ratio": {"0": 0.81, "1": 0.0001, "2": 0.1899},
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
labels_ratio["0"] >= 0.80
labels_ratio["2"] < 0.20
labels_ratio["1"] <= 0.001
segment_labels 中没有 label == 1
```

不要要求 `labels_ratio["1"] == 0`。官方示例在没有 `label == 1` 分段时仍可能返回 `0.0001`，属于平滑或舍入。本 Skill 允许的聚合 AI 噪声上限为 `0.001`（0.1%）；超过即不通过，即使分段漏标也不能放行。任何 `label == 1` 分段同样直接不通过。`0.001` 是本 Skill 为落实“没有确定 AI”而采用的保守产品策略，不是腾讯官方承诺的平滑上限。

`softmax_confidence` 和 `ratio_confidence` 不是用户设定的验收条件，不得代替上述条件。

## 结果状态

- `PASS`：响应有效且所有门槛通过。
- `REVISE`：响应有效，但至少一个内容门槛未通过。
- `PENDING`：异步任务尚未完成。
- `BLOCKED`：缺少密钥、无权限、配额或限流等外部条件阻止继续。
- `RESULT_UNKNOWN`：请求可能已经提交、消耗额度或产生费用，但因超时、网络错误或服务端错误没有拿到可判定结果；核实服务商状态前不得重试。
- `ERROR`：响应缺字段、类型错误、数值越界、比例和明显异常、分段缺失或服务报错。

未知、错误或不完整结果必须失败关闭，不能视为通过。

脚本输出中的 `scope` 固定为 `zhuque_detector_gate`。其中的 `PASS` 只代表朱雀子门禁通过，不代表事实守恒、文种评分和交付文件检查已经通过。

## 结果与正文绑定

- API 直连模式由同一进程提交正文并立即解析返回值。即便如此，脚本仍会要求每个返回分段具有精确 `text` 和 `[start, end]`，范围不重叠、与正文逐字一致，并覆盖全部非空白字符；否则直接 `ERROR`。
- 离线 `--response` 模式必须同时提供 `--input`。每个分段都要含精确的 `text` 和 `[start, end]`，这些范围须与正文逐字一致并覆盖全部非空白字符。
- 保存的响应顶层必须加入正文的 `input_sha256`；哈希不匹配或缺失直接报错。哈希用于拦截不同空白或不同候选稿复用结果，但同一份不受信任 JSON 里的自报哈希仍不能单独证明结果来源，绝不替代完整分段校验。
- 哈希不匹配、分段错位、结果来自旧稿或无法证明对应关系时一律 `ERROR`，不能沿用旧分数给新稿判定通过。
- 离线分段校验只能证明 JSON 内容与正文对应，不能证明该 JSON 确由腾讯生成，也不能证明检测时间。网页模式须保留当前可见结果的截图或浏览器会话记录；脚本 `checked_at` 只是本地评估时间。需要强来源绑定时使用同进程官方 API。

## 网页回退

网页模式会把文章正文发送给腾讯，也可能出现登录、配额或验证码。只使用可见 UI；不绕过验证码，不调用私有接口，不自动点击赞踩。若页面无法提供三类比例或分段标签，结果不足以证明通过，应标为 BLOCKED 而不是 PASS。

全文结果才可用于最终验收。分块检测只能定位问题，不能把多个分块比例加权后冒充全文结论。
