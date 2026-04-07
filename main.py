"""
AI 媒体信息自动抓取 & 推送脚本
================================
数据来源：
  1. arXiv 学术论文 (cs.AI 分类)
  2. 微信公众号文章 (通过搜狗微信搜索)

流程：抓取 -> 过滤(24h内) -> 去重 -> LLM摘要 -> 生成HTML存档 -> 推送飞书
"""

import os
import re
import json
import time
import hashlib
import logging
from datetime import datetime, timezone, timedelta
import requests
import feedparser
import schedule
import anthropic
from dotenv import load_dotenv

# ============================================================
# 初始化
# ============================================================
load_dotenv()  # 从 .env 文件加载配置

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ============================================================
# ★ 在这里填写你的配置 ★
# （推荐用 .env 文件，不要直接写在代码里）
# ============================================================

MINIMAX_API_KEY   = os.getenv("MINIMAX_API_KEY", "")      # MiniMax API Key
WECOM_WEBHOOK_URL = os.getenv("WECOM_WEBHOOK_URL", "")    # 企业微信机器人
FEISHU_WEBHOOK_URL= os.getenv("FEISHU_WEBHOOK_URL", "")   # 飞书机器人（二选一）

# ---- 指定公众号列表（高权重，优先展示）----
# 填公众号的中文名称，搜狗会按名称匹配
PRIORITY_ACCOUNTS = [
    "量子位",
    "机器之心",
    "36氪",
    "AI前线",
    "新智元",
    "通往AGI之路",
    "AI科技评论",
    "智东西",
    "数字生命卡兹克",
]

# ---- 通用关键词搜索（普通权重，覆盖更广）----
WECHAT_KEYWORDS = [
    "AI效率工具",
    "ChatGPT工作流",
    "AI数据分析",
    "大模型 业务分析",
    "prompt engineering 实战",
]

# ---- arXiv 关键词 ----
ARXIV_KEYWORDS = [
    "business analytics",
    "prompt engineering",
    "retrieval augmented generation",
    "workflow optimization",
    "large language model agent",
]

# ---- 过滤时间窗口（小时）----
HOURS_WINDOW = 24

# ============================================================
# 工具函数
# ============================================================

def _dedup_key(title: str, url: str) -> str:
    """生成去重用的哈希 key"""
    return hashlib.md5(f"{title}{url}".encode()).hexdigest()


def _is_recent(published_parsed, hours: int = HOURS_WINDOW) -> bool:
    """判断文章是否在时间窗口内"""
    if not published_parsed:
        return True  # 无时间信息则保留
    pub_dt = datetime(*published_parsed[:6], tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return pub_dt >= cutoff


# ============================================================
# 1. 抓取 arXiv 论文
# ============================================================

def fetch_arxiv() -> list[dict]:
    """
    通过 arXiv 官方 API 抓取 cs.AI 分类下的最新论文。
    无需登录，完全免费。
    """
    log.info("开始抓取 arXiv 论文...")
    results = []
    seen = set()

    for keyword in ARXIV_KEYWORDS:
        url = (
            "https://export.arxiv.org/api/query"
            f"?search_query=cat:cs.AI+AND+all:{requests.utils.quote(keyword)}"
            "&sortBy=submittedDate&sortOrder=descending&max_results=5"
        )
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                key = _dedup_key(entry.title, entry.link)
                if key in seen:
                    continue
                if not _is_recent(entry.get("published_parsed")):
                    continue
                seen.add(key)
                results.append({
                    "source": "arXiv",
                    "title": entry.title.replace("\n", " "),
                    "authors": ", ".join(a.name for a in entry.get("authors", [])[:3]),
                    "abstract": entry.summary[:500] + "...",
                    "url": entry.link,
                })
            log.info(f"  arXiv [{keyword}]: 获取 {len(feed.entries)} 篇，保留 {len(results)} 篇")
        except Exception as e:
            log.warning(f"  arXiv [{keyword}] 抓取失败，跳过: {e}")

        time.sleep(4)  # arXiv 限速 3 req/s，保守用 4 秒间隔

    return results


# ============================================================
# 2. 抓取微信公众号文章（通过搜狗微信搜索）
# ============================================================

_SOGOU_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://weixin.sogou.com/",
}

def _parse_sogou_articles(html: str, seen: set, priority: bool = False) -> list[dict]:
    """从搜狗微信搜索结果 HTML 中提取文章列表。"""
    results = []

    # 提取文章标题 + 链接
    titles = re.findall(
        r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        html, re.DOTALL
    )
    # 提取摘要：过滤掉短于30字的无效段落
    abstracts = [
        re.sub(r'<[^>]+>', '', p).strip()
        for p in re.findall(r'<p[^>]*>(.*?)</p>', html, re.DOTALL)
        if len(re.sub(r'<[^>]+>', '', p).strip()) > 30
    ]
    # 提取公众号名称：从 s-p 块开头提取（格式：公众号名 + timeConvert(...)）
    sp_blocks = re.findall(r'class="s-p">(.*?)</p>', html, re.DOTALL)
    account_names = []
    for block in sp_blocks:
        name = re.sub(r'document\.write.*', '', re.sub(r'<[^>]+>', '', block)).strip()
        account_names.append(name if name else "微信公众号")

    for i, (raw_url, raw_title) in enumerate(titles):
        title = re.sub(r'<[^>]+>', '', raw_title).strip()
        title = title.replace('&ldquo;', '"').replace('&rdquo;', '"').replace('&amp;', '&')
        if not title:
            continue
        link = (
            f"https://weixin.sogou.com{raw_url}"
            if raw_url.startswith("/link")
            else raw_url
        )
        key = _dedup_key(title, link)
        if key in seen:
            continue
        seen.add(key)
        account = account_names[i] if i < len(account_names) else "微信公众号"
        abstract = abstracts[i] if i < len(abstracts) else ""
        results.append({
            "source": f"公众号/{account}",
            "title": title,
            "authors": account,
            "abstract": abstract[:300],
            "url": link,
            "priority": priority,  # True = 指定公众号，False = 关键词搜索
        })

    return results


def fetch_priority_accounts() -> list[dict]:
    """
    抓取指定公众号的最新 AI 相关文章（高权重）。
    逻辑：用"公众号名 + AI"作关键词搜索，再按公众号名过滤匹配结果。
    """
    log.info("开始抓取指定公众号（高权重）...")
    results = []
    seen = set()

    for account in PRIORITY_ACCOUNTS:
        url = (
            "https://weixin.sogou.com/weixin"
            f"?type=2&query={requests.utils.quote(account + ' AI')}&ie=utf8"
        )
        try:
            resp = requests.get(url, headers=_SOGOU_HEADERS, timeout=15)
            resp.raise_for_status()
            articles = _parse_sogou_articles(resp.text, seen, priority=True)
            # 只保留来源匹配该公众号名的文章
            matched = [a for a in articles if account in a["authors"]]
            results.extend(matched)
            log.info(f"  指定公众号 [{account}]: 匹配 {len(matched)} 篇")
        except Exception as e:
            log.warning(f"  指定公众号 [{account}] 抓取失败，跳过: {e}")

        time.sleep(2)

    return results


def fetch_wechat_sogou() -> list[dict]:
    """
    按关键词搜索全网公众号文章（普通权重）。
    """
    log.info("开始抓取微信公众号（关键词搜索）...")
    results = []
    seen = set()

    for keyword in WECHAT_KEYWORDS:
        url = (
            "https://weixin.sogou.com/weixin"
            f"?type=2&query={requests.utils.quote(keyword)}&ie=utf8"
        )
        try:
            resp = requests.get(url, headers=_SOGOU_HEADERS, timeout=15)
            resp.raise_for_status()
            articles = _parse_sogou_articles(resp.text, seen, priority=False)
            results.extend(articles)
            log.info(f"  关键词搜索 [{keyword}]: 获取 {len(articles)} 篇")
        except Exception as e:
            log.warning(f"  关键词搜索 [{keyword}] 抓取失败，跳过: {e}")

        time.sleep(2)

    return results


# ============================================================
# 4. LLM 摘要（GPT-4o-mini）
# ============================================================

def summarize_with_llm(items: list[dict]) -> str:
    """
    将抓取到的内容批量传给 MiniMax M2.7，生成面向业务分析师的摘要。
    通过 Anthropic 兼容端点调用。
    """
    if not items:
        return "今日暂无符合条件的 AI 相关内容。"

    if not MINIMAX_API_KEY:
        log.warning("未配置 MINIMAX_API_KEY，跳过 LLM 摘要，直接输出原始标题")
        return _format_raw(items)

    # MiniMax Anthropic 兼容端点（超时 300 秒，处理长 prompt）
    client = anthropic.Anthropic(
        api_key=MINIMAX_API_KEY,
        base_url="https://api.minimaxi.com/anthropic",
        timeout=300,
    )

    # 构建输入文本，高权重内容标注 [重点]
    # 重点公众号取 4 条，其他取 6 条，控制在 API 处理能力内
    priority_items = [it for it in items if it.get("priority")][:4]
    other_items = [it for it in items if not it.get("priority")][:6]
    filtered = priority_items + other_items

    content_text = ""
    for i, item in enumerate(filtered, 1):
        tag = "【重点公众号】" if item.get("priority") else "【关键词搜索】"
        # 清理特殊字符，防止破坏 JSON
        clean_title = item['title'][:150].replace('"', '\\"').replace('\n', ' ')
        clean_abstract = item['abstract'][:200].replace('"', '\\"').replace('\n', ' ')
        clean_url = item['url'].replace('\n', ' ')
        content_text += (
            "\n[" + str(i) + "] " + tag + " 来源: " + item["source"] + "\n"
            "标题: " + clean_title + "\n"
            "摘要: " + clean_abstract + "\n"
            "链接: " + clean_url
        )

    prompt = f"""你是一位服务于业务分析师团队的 AI 信息助手，负责为团队挑选和解读最有价值的 AI 资讯。
以下是今日从 arXiv、微信公众号抓取的 AI 相关内容，每条标注了【重点公众号】或【关键词搜索】。

请完成以下任务（全程用中文输出，除了 AI、arXiv、PDF、API 等专业术语保留英文）：
1. 优先选取【重点公众号】的内容，再补充【关键词搜索】中有价值的内容，共保留 15 条以内
2. 将每条内容归入以下三个类别之一：
   - 🔬 AI底层技术：模型进展、论文成果、架构创新、训练方法突破等
   - 🛠 效率工具：效率提升工具、工作流技巧、实操教程、Prompt 方法等
   - 💰 AI商业变现：具体公司/创业者通过 AI 赚到真金白银的案例，包含收入数据、用户增长、商业模式等
3. 对每条内容用 2 句话总结：这件事是什么 + 对业务分析师有什么参考价值（summary 字段中禁止使用双引号，用「」代替）
4. 严格按以下 JSON 格式输出，不要输出其他任何内容：

{{
  "AI底层技术": [
    {{"title": "文章标题", "summary": "2句话摘要", "url": "链接", "priority": true或false}},
    ...
  ],
  "AI工具应用": [...],
  "AI商业变现": [...]
}}

今日内容如下：
{content_text}"""

    try:
        log.info("调用 MiniMax M2.7 生成摘要...")
        response = client.messages.create(
            model="MiniMax-M2.7",
            max_tokens=6000,
            messages=[{"role": "user", "content": prompt}],
        )
        # M2.7 是思考模型，找第一个 TextBlock
        raw = next(b.text for b in response.content if hasattr(b, "text"))
        log.info(f"LLM 原始返回（前300字）: {raw[:300]}")
        # 去掉 markdown 代码块包裹
        raw = re.sub(r'^```json\s*', '', raw, flags=re.MULTILINE).strip()
        raw = re.sub(r'^```\s*$', '', raw, flags=re.MULTILINE).strip()

        def _try_parse(text: str):
            """尝试解析 JSON，失败时自动修复后再试"""
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
            # 兜底：把 string value 内未转义的 " 替换成全角「
            # 策略：逐字符扫描，遇到在 string value 内的 " 就转义
            result = []
            i = 0
            in_string = False
            escape_next = False
            while i < len(text):
                c = text[i]
                if escape_next:
                    result.append(c)
                    escape_next = False
                    i += 1
                    continue
                if c == '\\' and in_string:
                    result.append(c)
                    escape_next = True
                    i += 1
                    continue
                if c == '"':
                    if not in_string:
                        in_string = True
                        result.append(c)
                    else:
                        # 结束引号，检查后面是否合理（如 , } ] : 空格 换行）
                        nxt = text[i+1] if i+1 < len(text) else ''
                        if nxt in (',', '}', ']', ':', ' ', '\n', '\r', '\t'):
                            in_string = False
                            result.append(c)
                        else:
                            # string 内的引号，转义它
                            result.append('\\"')
                            i += 1
                            continue
                else:
                    result.append(c)
                i += 1
            try:
                return json.loads(''.join(result))
            except json.JSONDecodeError:
                return None

        result = _try_parse(raw)
        if result is not None:
            return result

        # 完全解析失败，取每个分类下的第一条，手动提取 title/summary/url
        log.warning("JSON 解析失败，尝试用正则提取内容...")
        categorized = {}
        categories = ["AI底层技术", "AI工具应用", "AI商业变现"]
        for cat in categories:
            cat_match = re.search(rf'"{re.escape(cat)}"\s*:\s*\[(.*?)\]', raw, re.DOTALL)
            if not cat_match:
                categorized[cat] = []
                continue
            articles = []
            items_text = cat_match.group(1)
            for item_match in re.finditer(r'\{"title"\s*:\s*"([^"]*)"\s*,"summary"\s*:\s*"([^"]*)"\s*,"url"\s*:\s*"([^"]*)"', items_text):
                articles.append({
                    "title": item_match.group(1),
                    "summary": item_match.group(2),
                    "url": item_match.group(3),
                    "priority": False
                })
            categorized[cat] = articles
        log.info(f"正则提取结果: { {k: len(v) for k, v in categorized.items()} }")
        return categorized if any(categorized.values()) else None
    except Exception as e:
        log.error(f"LLM 调用失败: {e}")
        return None


def _format_raw(items: list[dict]) -> str:
    """LLM 不可用时的降级格式化"""
    lines = ["**今日 AI 资讯（未经 AI 摘要）**\n"]
    for item in items[:10]:
        lines.append(f"• [{item['title']}]({item['url']})  —— {item['source']}")
    return "\n".join(lines)


# ============================================================
# 5. 推送到企业微信 / 飞书
# ============================================================

def push_to_wecom(text: str):
    """推送 Markdown 消息到企业微信群机器人"""
    if not WECOM_WEBHOOK_URL:
        log.warning("未配置 WECOM_WEBHOOK_URL，跳过企业微信推送")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": f"## 🤖 AI 日报 · {today}\n\n{text}"
        }
    }
    try:
        resp = requests.post(WECOM_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        log.info(f"企业微信推送成功: {resp.json()}")
    except Exception as e:
        log.error(f"企业微信推送失败: {e}")


def push_to_feishu(categorized: dict, today: str, page_url: str):
    """飞书推送：每类一句亮点 + 网页链接"""
    if not FEISHU_WEBHOOK_URL:
        return
    if not categorized:
        log.warning("无内容可推送")
        return

    CATEGORY_ICONS = {
        "AI底层技术": "🔬",
        "AI工具应用": "🛠",
        "AI商业变现": "💰",
    }

    # 每类取第一条作为亮点预览
    highlights = ""
    for category, articles in categorized.items():
        if not articles:
            continue
        icon = CATEGORY_ICONS.get(category, "📌")
        first = articles[0]
        star = "⭐" if first.get("priority") else "📌"
        highlights += f"{icon} **{category}**\n{star} {first['title']}\n\n"

    content = f"{highlights}[📄 查看完整日报 →]({page_url})"

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": f"🤖 AI 日报 · {today}"}},
            "elements": [{"tag": "markdown", "content": content}]
        }
    }
    try:
        resp = requests.post(FEISHU_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        log.info(f"飞书推送成功: {resp.json().get('msg', 'ok')}")
    except Exception as e:
        log.error(f"飞书推送失败: {e}")


# ============================================================
# 6. 生成 HTML 日报页面
# ============================================================

GITHUB_PAGES_URL = "https://wangziyan617-sudo.github.io/AI-daily"

def generate_html(categorized: dict, today: str, page_url: str = "") -> str:
    """生成一天的日报 HTML 页面"""
    CATEGORY_ICONS = {
        "AI底层技术": "🔬",
        "AI工具应用": "🛠",
        "AI商业变现": "💰",
    }

    sections_html = ""
    for category, articles in categorized.items():
        if not articles:
            continue
        icon = CATEGORY_ICONS.get(category, "📌")
        cards_html = ""
        for a in articles:
            star = "⭐" if a.get("priority") else ""
            cards_html += f"""
            <div class="card">
                <div class="card-title">
                    {star} <a href="{a['url']}" target="_blank">{a['title']}</a>
                </div>
                <div class="card-summary">{a.get('summary', '')}</div>
            </div>"""
        sections_html += f"""
        <section class="category">
            <h2>{icon} {category}</h2>
            {cards_html}
        </section>"""

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 日报 · {today}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 860px; margin: 0 auto; padding: 24px; background: #f5f5f7; color: #1d1d1f; }}
  h1 {{ font-size: 28px; font-weight: 700; margin-bottom: 4px; }}
  .date {{ color: #6e6e73; font-size: 14px; margin-bottom: 32px; }}
  h2 {{ font-size: 20px; font-weight: 600; margin: 32px 0 16px;
        padding-bottom: 8px; border-bottom: 2px solid #e5e5ea; }}
  .card {{ background: #fff; border-radius: 12px; padding: 18px 20px;
           margin-bottom: 12px; box-shadow: 0 1px 4px rgba(0,0,0,.06); }}
  .card-title {{ font-size: 15px; font-weight: 600; margin-bottom: 8px; }}
  .card-title a {{ color: #1d1d1f; text-decoration: none; }}
  .card-title a:hover {{ color: #0071e3; }}
  .card-summary {{ font-size: 14px; color: #3a3a3c; line-height: 1.6; }}
  footer {{ text-align: center; color: #6e6e73; font-size: 12px; margin-top: 48px; }}
</style>
</head>
<body>
<h1>🤖 AI 日报</h1>
<div class="date">{today} · 由 MiniMax M2.7 生成</div>
{sections_html}
<footer>数据来源：量子位 · 机器之心 · 36氪 · arXiv · 搜狗微信</footer>
</body>
</html>"""


def generate_index_html(reports: list[tuple], today: str, today_url: str) -> str:
    """生成首页：展示今日日报 + 所有历史存档"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    rows = ""
    for date, url in reports:
        mark = "（今日）" if date == today_str else ""
        rows += f'<li><a href="{url}">{date} {mark}</a></li>'

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 日报 · 历史存档</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 680px; margin: 0 auto; padding: 40px 24px; background: #f5f5f7; color: #1d1d1f; }}
  h1 {{ font-size: 32px; font-weight: 700; margin-bottom: 8px; }}
  .subtitle {{ color: #6e6e73; font-size: 14px; margin-bottom: 32px; }}
  .today-card {{ background: #0071e3; border-radius: 16px; padding: 24px; margin-bottom: 32px; }}
  .today-card a {{ color: #fff; font-size: 18px; font-weight: 600; text-decoration: none; display: block; }}
  .today-card .today-label {{ color: rgba(255,255,255,0.7); font-size: 13px; margin-bottom: 8px; }}
  h2 {{ font-size: 13px; font-weight: 600; margin: 32px 0 16px; color: #6e6e73; text-transform: uppercase; letter-spacing: 1px; }}
  ul {{ list-style: none; padding: 0; }}
  li a {{ display: block; background: #fff; padding: 14px 20px; border-radius: 10px;
           margin-bottom: 8px; text-decoration: none; color: #1d1d1f;
           box-shadow: 0 1px 4px rgba(0,0,0,.06); font-size: 15px; }}
  li a:hover {{ background: #f0f0f5; }}
</style>
</head>
<body>
<h1>🤖 AI 日报</h1>
<div class="subtitle">工作日 10:00 自动更新 · 由 MiniMax M2.7 生成摘要</div>

<div class="today-card">
  <div class="today-label">今日 · {today_str}</div>
  <a href="{today_url}">查看今日 AI 日报 →</a>
</div>

<h2>历史日报</h2>
<ul>
{rows}
</ul>
</body>
</html>"""


def save_html(categorized: dict, today: str):
    """将 HTML 写入 docs/ 目录，并更新包含历史存档的 index.html"""
    os.makedirs("docs", exist_ok=True)

    today_url = f"./{today}.html"
    html = generate_html(categorized, today, GITHUB_PAGES_URL + "/" + today + ".html")

    # 今日文件
    filepath = f"docs/{today}.html"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"HTML 日报已生成: {filepath}")

    # 扫描 docs/ 下所有历史日报，构建存档列表
    reports = []
    if os.path.isdir("docs"):
        for fname in os.listdir("docs"):
            if fname.endswith(".html") and fname != "index.html":
                date_part = fname[:-5]
                url = f"./{fname}"
                reports.append((date_part, url))
    # 按日期倒序
    reports.sort(reverse=True)

    # 更新首页 index.html（带历史存档）
    index = generate_index_html(reports, today, today_url)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(index)
    log.info(f"首页已更新，共 {len(reports)} 份历史存档")




def run_pipeline():
    """完整的抓取 -> 摘要 -> 推送流程"""
    log.info("=" * 50)
    log.info("开始执行 AI 日报流程")
    log.info("=" * 50)

    # Step 1: 抓取所有来源，高权重（指定公众号）放前面
    priority_items = fetch_priority_accounts()
    keyword_items = fetch_arxiv() + fetch_wechat_sogou()
    all_items = priority_items + keyword_items

    log.info(f"共抓取 {len(all_items)} 条内容（24h内）")

    if not all_items:
        log.info("今日无新内容，跳过推送")
        return

    # Step 2: LLM 摘要（返回按类别分组的字典）
    categorized = summarize_with_llm(all_items)

    if not categorized:
        log.error("LLM 摘要失败，跳过推送")
        return

    today = datetime.now().strftime("%Y-%m-%d")

    # Step 3: 生成 HTML 并保存到 docs/
    save_html(categorized, today)

    # Step 4: 飞书推送简短卡片 + 网页链接
    page_url = f"{GITHUB_PAGES_URL}/{today}.html"
    push_to_feishu(categorized, today, page_url)

    log.info("流程执行完毕")


# ============================================================
# 定时调度（本地运行模式）
# ============================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--now":
        # 立即执行一次（用于测试）
        run_pipeline()
    else:
        # 每个工作日 10:00 自动执行
        log.info("定时任务已启动，每天 10:00 执行（工作日）")
        log.info("如需立即测试，请运行: python main.py --now")

        schedule.every().monday.at("10:00").do(run_pipeline)
        schedule.every().tuesday.at("10:00").do(run_pipeline)
        schedule.every().wednesday.at("10:00").do(run_pipeline)
        schedule.every().thursday.at("10:00").do(run_pipeline)
        schedule.every().friday.at("10:00").do(run_pipeline)

        while True:
            schedule.run_pending()
            time.sleep(60)
