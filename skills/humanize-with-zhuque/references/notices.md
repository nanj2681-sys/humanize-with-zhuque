# 声明与使用边界 / Notices and Scope

## 非官方声明

本技能是**社区工具**，**不是腾讯官方项目**，与腾讯及其「朱雀」文本检测产品**无隶属、
无认证、无背书关系**。「朱雀」为腾讯旗下产品名称，此处仅用于指代所调用的外部检测服务。

本技能不提供、不转售、不代理朱雀服务，也不代表朱雀服务的可用性、配额或检测结论。

## 检测结果的定位

朱雀结果是**文本特征检测信号**，**不能证明**作者身份、原创性或版权归属。
本技能在任何输出中都不会把检测结论表述为"证明人工写作"。

单次结果仅代表该次检测，不保证重复检测得到相同结论，也不推断分数变化的原因。
技能内记录本地评估时间与观察到的检测时间，两者分别标注，不互相替代。

## 使用边界（禁止用途）

- **不得用于编造事实**。技能设有事实守恒闸门：缺少的事实标记为 `【待核实】`/`【待补】`，
  绝不为了降低检测分数而虚构人名、数字、政策名称、责任主体或结论。
- **不得用于规避平台规则**。技能明确禁止绕过验证码、登录校验或频率限制，
  也不调用任何私有网页接口。
- **不得用于伪造他人作品或学术不端**。使用者应确保对所处理文本拥有合法处理权限。
- **涉密与个人信息**。标记为涉密、内部、未公开政策数据或含个人身份信息的文本，
  在脱敏或取得授权前不会送出。

## 达到门槛 ≠ 保证通过

技能设定的判定门槛（人工内容 100%、疑似 AI 与 AI 均为 0）是**内部验收标准**，
不是对任何第三方平台检测结果的承诺。若为达到门槛会改变事实、法律含义、责任边界、
政策强度或文种，技能会保留更稳妥的版本并明确报告"未通过"，不会声称定稿。

## 第三方组件归属

本项目的写作模式与去 AI 痕迹方法参考了以下 MIT 许可项目：

- [blader/humanizer](https://github.com/blader/humanizer)
- [jiji262/humanizer-chinese](https://github.com/jiji262/humanizer-chinese)

`humanizer-chinese` 为可选外部集成，**未打包在本技能内**。若运行环境未提供该技能，
本技能按自身规则直接执行，不因此中断。

### 上游 MIT 许可全文

```
MIT License

Copyright (c) 2025 Siqi Chen
Copyright (c) 2026 jiji262 (Chinese localization)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 本项目许可

MIT License，Copyright (c) 2026 nanj2681-sys。
