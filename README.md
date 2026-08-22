# 🤖 AI 日报 · 云端自动推送

无需电脑开机：GitHub Actions 定时抓取新闻 + GitHub Trending → 大模型生成报告 → Server酱推送到手机微信，报告自动存档回本仓库供周报/月报分析。

## 推送计划（北京时间）

| 任务 | 时间 | 内容 |
|---|---|---|
| 🤖 AI 早报 | 每天 09:00 | 近24小时要闻 + GitHub Trending + 新星观察 + 明日关注 |
| 🌙 AI 晚报 | 每天 21:00 | 当天下午要闻 + Trending 变化 |
| ⚡ 重大快讯 | 每小时检查 | 仅重大消息/爆火项目才推送，普通新闻不打扰 |
| 📊 AI 周报 | 周日 20:00 | 本周要闻回顾 + GitHub 周榜 + 趋势分析 |
| 📚 AI 月报 | 每月1日 09:35 | 基于当月存档的数据分析：热点排行/事件时间线/项目增长对比/下月展望 |

> GitHub Actions 定时任务可能有几分钟到半小时的排队延迟，属正常现象。

## 部署步骤（一次性）

1. 在 GitHub 创建一个新仓库（建议 **Public**，Actions 时长免费不限量；Private 每月 2000 分钟也基本够用）。
2. 把本目录所有文件推送到该仓库。
3. 在仓库 **Settings → Secrets and variables → Actions** 中添加 Secrets：

| Secret | 必填 | 说明 |
|---|---|---|
| `SENDKEY` | ✅ | Server酱 SendKey（Server酱³ 为 `sctp` 开头，[sct.ftqq.com](https://sct.ftqq.com) 获取） |
| `LLM_API_KEY` | 建议 | OpenAI 兼容 API Key，默认对接智谱 [open.bigmodel.cn](https://open.bigmodel.cn)。不填则退化为"模板模式"（只罗列原始标题，无 AI 摘要） |
| `LLM_BASE_URL` | 可选 | 其他 OpenAI 兼容服务地址（默认 `https://open.bigmodel.cn/api/paas/v4`） |
| `LLM_MODEL` | 可选 | 模型名（默认 `glm-4-flash` 免费；追求质量可用 `glm-4.6` 等） |

4. 到 **Actions** 页确认工作流已启用，点 `AI Daily Report → Run workflow` 手动跑一次验证推送。

## 本地测试

```bash
pip install -r requirements.txt
python scripts/report.py --mode morning   # 不设 SENDKEY 时只生成存档不推送
```

## 目录结构

```
scripts/report.py      # 全部逻辑：采集(RSS/HN/Trending) → LLM 生成 → 推送 → 存档
.github/workflows/     # 4 个定时工作流
reports/YYYY-MM/       # 报告存档（供周报/月报汇总分析）
```

## 自定义

- **新闻源**：编辑 `scripts/report.py` 顶部的 `RSS_SOURCES` 列表。
- **推送时间**：编辑 workflows 里的 `cron`（UTC 时间，北京时间 = UTC + 8）。
- **快讯灵敏度**：`run_flash()` 中的 star 阈值（默认日增 ≥800 且总量 ≤3万 判定为爆火新星）。

## 安全设计

- 所有出站请求经过统一防护：仅允许 http/https、域名解析后阻断内网/环回/保留地址（防 SSRF）、禁用自动重定向。
- 推送接口主机限定 `*.push.ft07.com` / `sctapi.ftqq.com` 白名单。
- SendKey 与 API Key 均存放在 GitHub Secrets，不进代码库。
