# -*- coding: utf-8 -*-
"""AI 日报云端生成器：抓取新闻源 + GitHub Trending → LLM 生成报告 → Server酱推送

模式:
  morning   早报 (9:00)   evening 晚报 (21:00)
  flash     每小时重大消息监测
  weekly    周报 (周日 20:00)   monthly 月度分析 (每月1日)

环境变量:
  LLM_API_KEY   大模型 API Key (OpenAI 兼容; 未配置时退化为模板拼装模式)
  LLM_BASE_URL  默认 https://open.bigmodel.cn/api/paas/v4 (智谱)
  LLM_MODEL     默认 glm-4-flash (免费, 可换 glm-4.6 等)
  SENDKEY       Server酱 SendKey (sctp... = Server酱³ / SCT... = Turbo)
"""
import argparse
import glob
import ipaddress
import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover
    TZ = timezone(timedelta(hours=8))

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "reports"
STATE_DIR = ROOT / "state"

RSS_SOURCES = [
    ("机器之心", "https://www.jiqizhixin.com/rss"),
    ("量子位", "https://www.qbitai.com/feed"),
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("36氪", "https://36kr.com/feed"),
]

# 固定抓取目标主机白名单; LLM_BASE_URL/RSS 允许自定义但强制公网 IP
ALLOWED_HOSTS = {
    "github.com", "hacker-news.firebaseio.com",
}
# Server酱推送主机白名单 (数字子域 = Server酱³, sctapi = Turbo)
PUSH_HOST_RE = r"(\d{1,8}\.push\.ft07\.com|sctapi\.ftqq\.com)"
# LLM API 主机白名单 (OpenAI 兼容服务商; 新增服务商在此追加)
LLM_ALLOWED_HOST_RE = (r"(api\.deepseek\.com|open\.bigmodel\.cn|api\.openai\.com|"
                       r"dashscope\.aliyuncs\.com|api\.moonshot\.cn|openrouter\.ai)")
LLM_DEFAULT_BASE = "https://open.bigmodel.cn/api/paas/v4"

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 ai-daily-bot/1.0"}
AI_KEYWORDS = re.compile(
    r"\b(AI|AGI|GPT|LLM|Claude|Gemini|Grok|Llama|Diffusion|OpenAI|Anthropic|DeepSeek|"
    r"Qwen|通义|文心|Kimi|智谱|GLM|Midjourney|Copilot|Chatbot|Agent|RAG)\b", re.I)


def now_bj():
    return datetime.now(TZ)


def log(msg):
    print(f"[{now_bj().strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------- 安全出站请求 --

def _assert_public_https(url, allow_custom_host=False, allow_host_regex=None):
    """校验 URL: 仅 http/https + 主机白名单/正则 + 域名解析后必须是公网 IP"""
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ValueError(f"非法协议: {u.scheme}")
    host = u.hostname or ""
    if not host:
        raise ValueError("缺少主机名")
    if allow_host_regex is not None:
        if not re.fullmatch(allow_host_regex, host):
            raise ValueError(f"主机不匹配白名单正则: {host}")
    elif not allow_custom_host and host not in ALLOWED_HOSTS:
        raise ValueError(f"主机不在白名单: {host}")
    infos = socket.getaddrinfo(host, u.port or 443, proto=socket.IPPROTO_TCP)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ValueError(f"禁止访问内网/保留地址: {host} -> {ip}")


def safe_get(url, timeout=30, allow_custom_host=False, allow_host_regex=None):
    _assert_public_https(url, allow_custom_host, allow_host_regex)
    return requests.get(url, headers=UA, timeout=timeout, allow_redirects=False)


def safe_post(url, timeout=60, allow_custom_host=False, allow_host_regex=None, **kw):
    _assert_public_https(url, allow_custom_host, allow_host_regex)
    return requests.post(url, timeout=timeout, allow_redirects=False, **kw)


# ---------------------------------------------------------------- 采集 --

def _parse_feed(name, url, cutoff, per_feed):
    """解析单个 RSS 源, 返回时间窗口内的条目"""
    out = []
    try:
        r = safe_get(url, timeout=12, allow_custom_host=True)
        feed = feedparser.parse(r.content)
        got = 0
        for e in feed.entries:
            if got >= per_feed:
                break
            title = (e.get("title") or "").strip()
            link = e.get("link") or ""
            if not title:
                continue
            t = None
            for key in ("published_parsed", "updated_parsed"):
                if getattr(e, key, None):
                    t = datetime.fromtimestamp(time.mktime(getattr(e, key)))
                    break
            if t:
                t = t.replace(tzinfo=TZ) if t.tzinfo is None else t.astimezone(TZ)
                if t < cutoff:
                    continue
            out.append({"source": name, "title": title, "link": link,
                        "time": t.strftime("%m-%d %H:%M") if t else ""})
            got += 1
    except Exception as exc:
        log(f"RSS {name} 失败: {exc}")
    return out


def fetch_rss(hours=24, per_feed=15):
    """并行拉取各 RSS 源, 返回 hours 小时内的条目(按标题去重)"""
    from concurrent.futures import ThreadPoolExecutor
    cutoff = now_bj() - timedelta(hours=hours)
    items = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for part in ex.map(lambda s: _parse_feed(s[0], s[1], cutoff, per_feed), RSS_SOURCES):
            items += part
    seen, deduped = set(), []
    for it in items:
        key = re.sub(r"\s+", "", it["title"])[:40]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)
    return deduped


def fetch_hn(limit=12):
    """Hacker News 热榜中与 AI 相关的条目"""
    try:
        ids = safe_get("https://hacker-news.firebaseio.com/v0/topstories.json",
                       timeout=20).json()[:30]
        out = []
        for i in ids:
            if len(out) >= limit:
                break
            try:
                it = safe_get(f"https://hacker-news.firebaseio.com/v0/item/{i}.json",
                              timeout=10).json()
                title = it.get("title", "")
                if AI_KEYWORDS.search(title):
                    out.append({"source": "HackerNews", "title": title,
                                "link": f"https://news.ycombinator.com/item?id={i}",
                                "time": ""})
            except Exception:
                continue
        return out
    except Exception as exc:
        log(f"HN 失败: {exc}")
        return []


def fetch_trending(since="daily", limit=20):
    """解析 GitHub Trending 页面"""
    try:
        r = safe_get(f"https://github.com/trending?since={since}", timeout=30)
        repos = []
        for block in r.text.split('<article class="Box-row">')[1:limit + 1]:
            m = re.search(r'<h2[^>]*>\s*<a[^>]*href="/([^"/]+/[^"/]+)"', block, re.S)
            if not m:
                continue
            repo = m.group(1)
            desc_m = re.search(r'<p class="col-9[^"]*">\s*(.*?)\s*</p>', block, re.S)
            desc = re.sub(r"<[^>]+>", "", desc_m.group(1)).strip() if desc_m else ""
            # 总 star 数在 /stargazers 锚点里; "N stars today" 是日增, 两者不能混淆
            total_m = re.search(r'/stargazers"[^>]*>(?:\s*<svg[^>]*>.*?</svg>)?\s*([\d,]+)',
                                block, re.S)
            total = int(total_m.group(1).replace(",", "")) if total_m else 0
            delta_m = re.search(r'([\d,+k]+)\s*stars?\s*(?:today|this week|this month)',
                                block, re.I)
            delta = delta_m.group(1) if delta_m else "?"
            lang_m = re.search(r'itemprop="programmingLanguage">([^<]+)<', block)
            repos.append({"repo": repo, "lang": lang_m.group(1) if lang_m else "-",
                          "total": total, "delta": delta, "desc": desc[:80]})
        log(f"Trending({since}) 抓到 {len(repos)} 个仓库")
        return repos
    except Exception as exc:
        log(f"Trending 失败: {exc}")
        return []


# ---------------------------------------------------------------- LLM --

def llm_chat(system, user, max_tokens=8000):
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        return None
    base = os.environ.get("LLM_BASE_URL", "").strip() or LLM_DEFAULT_BASE
    model = os.environ.get("LLM_MODEL", "").strip() or "glm-4-flash"
    url = f"{base.rstrip('/')}/chat/completions"
    if not re.fullmatch(LLM_ALLOWED_HOST_RE, urlparse(url).hostname or ""):
        log(f"LLM_BASE_URL 主机不在允许列表: {urlparse(url).hostname}")
        return None
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    for attempt in range(3):
        try:
            r = safe_post(url, timeout=120, allow_host_regex=LLM_ALLOWED_HOST_RE,
                          headers={"Authorization": f"Bearer {key}"}, json=payload)
            if r.status_code == 200:
                text = r.json()["choices"][0]["message"]["content"].strip()
                return re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", text).strip()
            log(f"LLM HTTP {r.status_code}: {r.text[:200]}")
        except Exception as exc:
            log(f"LLM 调用异常({attempt + 1}/3): {exc}")
    return None


# ---------------------------------------------------------------- 推送 --

def push(title, desp):
    """推送到 Server酱; URL 主机必须匹配 PUSH_HOST_RE 白名单正则"""
    key = os.environ.get("SENDKEY", "").strip()
    if not key:
        log("未配置 SENDKEY, 跳过推送")
        return False
    if key.startswith("sctp"):
        uid = key[4:].split("t", 1)[0]
        if not uid.isdigit():
            log("sctp SendKey 格式非法")
            return False
        url = f"https://{uid}.push.ft07.com/send/{key}.send"
    elif key.startswith("SCT"):
        url = f"https://sctapi.ftqq.com/{key}.send"
    else:
        log("SendKey 格式无法识别")
        return False
    try:
        r = safe_post(url, timeout=30, allow_host_regex=PUSH_HOST_RE,
                      data={"title": title, "desp": desp})
        ok = r.json().get("code", r.json().get("errno", -1)) == 0
        log(f"推送 {'成功' if ok else '失败'}: {r.text[:120]}")
        return ok
    except Exception as exc:
        log(f"推送异常: {exc}")
        return False


def save_report(filename, content):
    month_dir = REPORTS_DIR / now_bj().strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    path = month_dir / filename
    path.write_text(content, encoding="utf-8")
    log(f"已存档 {path.relative_to(ROOT)}")
    return path


def load_state():
    p = STATE_DIR / "trending.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            log(f"状态文件损坏, 重建: {exc}")
    return {}


def save_state(st):
    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / "trending.json").write_text(
        json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def _hours_ago(iso_ts):
    try:
        t = datetime.fromisoformat(iso_ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=TZ)
        return (now_bj() - t).total_seconds() / 3600
    except Exception:
        return 99.0


# ---------------------------------------------------------------- 各模式 --

NEWS_SYSTEM = (
    "你是一名专业的中文AI行业日报编辑。基于用户提供的原始素材(可能有重复、噪声、旧闻), "
    "筛选并撰写一份适合手机微信阅读的 Markdown 日报。要求:\n"
    "1. 只保留素材中最近24小时或正在发生的重要消息, 无日期依据的传闻要谨慎收录;\n"
    "2. 严格按用户给定的输出结构, 使用 Markdown 二级标题、编号列表和表格;\n"
    "3. 每条新闻一行: **加粗标题** — 一句话中文摘要, 附 [来源](链接);\n"
    "4. GitHub 表格列: 仓库 | 今日+star | 总star | 一句话简介(仓库名做成链接);\n"
    "5. 不要编造素材中没有的信息, 总长度控制在 4000 字符内, 不要输出任何额外解释。"
)


def run_daily(mode):
    bj = now_bj()
    hours = 24 if mode == "morning" else 12
    news = fetch_rss(hours=hours) + fetch_hn()
    trending = fetch_trending("daily")

    material = {"新闻素材": news or "(空)", "GitHub_Trending": trending or "(空)"}
    fallback = "⚠️ 本期由模板模式生成(LLM 未配置或调用失败)\n\n"
    fallback += f"## 📰 AI 要闻(近{hours}小时原始标题)\n"
    for n in news[:12]:
        fallback += f"- [{n['title']}]({n['link']}) ({n['source']})\n"
    fallback += "\n## 🐙 GitHub Trending\n\n| 仓库 | 今日+ | 总star |\n|---|---|---|\n"
    for t in trending[:10]:
        fallback += f"| [{t['repo']}](https://github.com/{t['repo']}) | +{t['delta']} | {t['total']:,} |\n"

    label = "早报" if mode == "morning" else "晚报"
    emoji = "🤖" if mode == "morning" else "🌙"
    header = f"{emoji} AI {label} · {bj.strftime('%Y-%m-%d')}\n\n"
    user = (f"今天是 {bj.strftime('%Y年%m月%d日 %A')}。请生成今日AI{label}。"
            f"输出结构:\n## 📰 AI 要闻(5-10条)\n## 🐙 GitHub 热点(表格取AI相关或前7名,"
            f"另加 ⭐新星观察(总star低但日增突出) 和 📈趋势速读一两句)\n## 🔭 明日关注(1-3条)\n\n素材JSON:\n"
            + json.dumps(material, ensure_ascii=False, indent=1)[:26000])

    body = llm_chat(NEWS_SYSTEM, user) or fallback
    save_report(f"daily-{bj:%Y-%m-%d}-{mode}.md", header + body + "\n")
    push(f"{emoji} AI{label} {bj:%m月%d日}", header + body)
    return 0


def _delta_num(delta):
    d = str(delta).replace(",", "").replace("+", "")
    if d.endswith("k"):
        return int(float(d[:-1]) * 1000)
    try:
        return int(float(d))
    except ValueError:
        return 0


def run_flash():
    """每小时监测: 与上次快照对比计算真实小时增速; 同一项目/新闻24小时内只评估一次"""
    bj = now_bj()
    state = load_state()
    prev_repos = state.get("repos", {})
    pushed = {k: v for k, v in state.get("pushed", {}).items() if _hours_ago(v) < 24}

    news = [n for n in fetch_rss(hours=1.5, per_feed=8)
            if f"news:{n['title'][:40]}" not in pushed]
    trending = fetch_trending("daily", limit=25)
    cur = {t["repo"]: t for t in trending}

    # Trending 的 "N stars today" 是当日累计值, 不能直接当增速;
    # 用总star差值(单调递增)对比上次快照, 得到近1小时真实增速
    viral = []
    for repo, t in cur.items():
        if repo in pushed:
            continue
        p = prev_repos.get(repo)
        if p:
            v_total = t["total"] - p.get("total", 0)
            v_today = _delta_num(t["delta"]) - p.get("today", 0)
            velocity = v_total if v_total > 0 else max(v_today, 0)
        else:
            velocity = 0  # 首次上榜无基准, 不判爆火, 只记快照
        if (velocity >= 300 and t["total"] <= 50000) or velocity >= 800:
            viral.append({**t, "velocity_1h": velocity})
    if viral:
        log(f"快速上升: {[(v['repo'], v['velocity_1h']) for v in viral]}")

    # 更新快照供下次对比
    state["repos"] = {repo: {"total": t["total"], "today": _delta_num(t["delta"]),
                             "ts": bj.isoformat(timespec="seconds")}
                      for repo, t in cur.items()}
    state["pushed"] = pushed
    state["last_run"] = bj.isoformat(timespec="seconds")
    save_state(state)

    if not news and not viral:
        log("近1.5小时无新素材且无快速上升项目, 跳过")
        return 0

    system = ("你是AI领域快讯判断器。判断素材中是否存在'引发较大关注的重磅消息':"
              "重大模型发布/重大融资并购/重大政策监管/重大安全事故/短时间内star快速上升的开源项目"
              "(注意velocity_1h是近1小时新增star, 只有大几十上百才值得关注, 当日累计值不代表当前热度)。"
              "普通产品更新、常规报道都不算。只输出JSON:"
              '{"push":true/false,"title":"快讯标题(30字内)","content":"Markdown正文,每条一行加粗标题+一句话摘要+来源链接"}')
    user = ("候选新闻: " + json.dumps(news[:20], ensure_ascii=False)[:8000]
            + "\nGitHub快速上升项目: " + json.dumps(viral, ensure_ascii=False)[:3000])
    resp = llm_chat(system, user, max_tokens=3000)

    # 候选已评估过, 24小时内不再重复评估(无论是否推送), 防止同一热点反复打扰
    for k in [f"news:{n['title'][:40]}" for n in news[:20]] + [v["repo"] for v in viral]:
        pushed[k] = bj.isoformat(timespec="seconds")
    state["pushed"] = pushed
    save_state(state)

    if resp:
        try:
            m = re.search(r"\{.*\}", resp, re.S)
            data = json.loads(m.group(0))
            if not data.get("push"):
                log("LLM 判定无重大消息, 不推送")
                return 0
            save_report(f"flash-{bj:%Y-%m-%d-%H%M}.md", data["content"])
            push(data["title"][:30], data["content"])
            return 0
        except Exception as exc:
            log(f"解析LLM快讯失败: {exc}")

    if viral:  # LLM 不可用时, 快速上升项目仍按阈值推送
        lines = [f"- **{v['repo']}** 近1小时 +{v['velocity_1h']} star (总 {v['total']:,}, 今日 +{v['delta']}) "
                 f"[链接](https://github.com/{v['repo']})" for v in viral[:5]]
        body = "## ⚡ GitHub 快速上升项目(近1小时)\n" + "\n".join(lines)
        save_report(f"flash-{bj:%Y-%m-%d-%H%M}.md", body)
        push("⚡ AI/GitHub 重大快讯", body)
        return 0
    log("无重大消息, 不推送")
    return 0


def run_weekly():
    bj = now_bj()
    files = []
    for i in range(7):
        d = bj - timedelta(days=i)
        files += sorted(glob.glob(str(REPORTS_DIR / f"{d:%Y-%m}" / f"daily-{d:%Y-%m-%d}-*.md")))
    digest = ""
    for f in files:
        digest += Path(f).read_text(encoding="utf-8")[:1200] + "\n---\n"

    trending = fetch_trending("weekly")
    system = ("你是中文AI行业周报编辑。基于本周每日报告存档和GitHub周榜素材, 写一份Markdown周报, "
              "结构: ## 📌 本周要闻回顾(按主题归类, 不是逐日罗列) ## 📈 本周趋势观察 "
              "## 🐙 GitHub 周度总结(表格: 仓库|周+star|总star|简介, 新星项目, 增长趋势分析) "
              "## 🔭 下周关注。控制在4000字符内, 不要额外解释。")
    user = (f"今天是 {bj:%Y年%m月%d日 %A}。本周日报存档:\n{digest[:24000]}\n\n"
            f"GitHub 周榜: {json.dumps(trending, ensure_ascii=False)[:8000]}")
    body = llm_chat(system, user) or "⚠️ 周报生成失败(LLM不可用且无存档素材)"
    header = f"📊 AI 周报 · {bj:%Y-%m-%d}\n\n"
    save_report(f"weekly-{bj:%Y-%m-%d}.md", header + body)
    push(f"📊 AI周报 {bj:%m月%d日}", header + body)
    return 0


def run_monthly():
    bj = now_bj()
    prev = bj.replace(day=1) - timedelta(days=1)
    files = sorted(glob.glob(str(REPORTS_DIR / f"{prev:%Y-%m}" / "*.md")))
    if not files:
        log("上月无存档, 跳过月报")
        return 0
    digest = ""
    for f in files:
        digest += f"# {Path(f).name}\n" + Path(f).read_text(encoding="utf-8")[:1000] + "\n---\n"

    system = ("你是中文AI行业数据分析师。基于上月日报/周报/快讯存档做月度分析, 结构: "
              "## 🔥 热点主题排行(哪些模型/公司/话题出现频率最高, 用列表+频次) "
              "## 📜 重大事件时间线 ## 🐙 GitHub 项目增长分析(持续增长 vs 昙花一现, 值得关注的赛道) "
              "## 🔮 下月展望。控制在4500字符内, 基于素材统计, 不要编造。")
    user = f"今天是 {bj:%Y年%m月%d日}, 请分析 {prev:%Y年%m月} 的素材:\n{digest[:26000]}"
    body = llm_chat(system, user) or "⚠️ 月报生成失败(LLM不可用)"
    header = f"📚 AI 月度数据报告 · {prev:%Y-%m}\n\n"
    save_report(f"monthly-{prev:%Y-%m}.md", header + body)
    push(f"📚 AI月报 {prev:%Y-%m}", header + body)
    return 0


MODES = {"morning": run_daily, "evening": run_daily,
         "flash": run_flash, "weekly": run_weekly, "monthly": run_monthly}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=list(MODES))
    args = ap.parse_args()
    log(f"模式: {args.mode}")
    if args.mode in ("morning", "evening"):
        sys.exit(MODES[args.mode](args.mode))
    sys.exit(MODES[args.mode]())


if __name__ == "__main__":
    main()
