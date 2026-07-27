#!/usr/bin/env python3
"""
设计资讯摘要 - 自动发现 + DeepSeek 翻译 + 精选 + 发送
=======================================================
一次性完成：发现文章 → DeepSeek 翻译/写摘要 → 保存 articles.json → 调用 pipeline 发送

依赖:
  pip install feedparser

环境变量:
  DEEPSEEK_API_KEY    (必需)
  DESIGN_DIGEST_TO_ADDRS  (可选, 默认 isawuonce@qq.com)
  SMTP_PASS           (必需, QQ邮箱SMTP授权码)
"""

import json, os, sys, re, html as htmlmod, time
from urllib.request import Request, urlopen
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ARTICLES_PATH = os.path.join(SCRIPT_DIR, "articles.json")
LOG_PATH = os.path.join(SCRIPT_DIR, "discovery.log")
HISTORY_PATH = os.path.join(SCRIPT_DIR, "sent_urls.json")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

MAX_CANDIDATES = 20
MAX_PER_SOURCE = 8

# ── 日志 ──
def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except:
        pass

# ── HTTP 请求 ──
def fetch(url, timeout=15, max_bytes=300*1024):
    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        resp = urlopen(req, timeout=timeout)
        data = resp.read(max_bytes)
        return data.decode("utf-8", errors="replace")
    except Exception as e:
        log(f"  ⚠ fetch {url[:60]}: {str(e)[:60]}")
        return ""

# ── 元数据提取 ──
def og(html, prop):
    m = re.search(rf'<meta[^>]+(?:property|name)="{prop}"[^>]+content="([^"]*)"', html, re.I)
    if not m:
        m = re.search(rf'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="{prop}"', html, re.I)
    return htmlmod.unescape(m.group(1)).strip() if m else ""

def og_image(html):
    img = og(html, "og:image") or og(html, "twitter:image") or ""
    if not img:
        return ""
    img = htmlmod.unescape(img)
    img = re.sub(r'[\?&](w=|h=|fit=|crop=|auto=|q=|dpr=|cbr=)[^&]+', '', img)
    img = img.rstrip('?&')
    return img

def is_default_image(url):
    return any(d in url.lower() for d in ["social.jpg", "placeholder", "default", "logo", "favicon"])

def fetch_article_detail(url):
    """Get og:title, og:description, og:image from article page."""
    html = fetch(url, max_bytes=200*1024)
    if not html:
        return {}
    return {
        "title": og(html, "og:title") or "",
        "desc": og(html, "og:description") or "",
        "img": og_image(html) or "",
    }

# ── RSS 解析 ──
def parse_rss(url, source_name, link_pattern=None):
    """Try to parse RSS feed. Returns list of candidate dicts."""
    log(f"  RSS: {source_name} ({url})")
    try:
        import feedparser
        feed = feedparser.parse(url)
        entries = []
        for entry in feed.entries[:8]:
            title = entry.get("title", "")
            link = entry.get("link", "")
            desc = entry.get("summary", "") or entry.get("description", "") or ""
            # Try to get image from media_content or media_thumbnail
            img = ""
            if hasattr(entry, "media_content") and entry.media_content:
                for mc in entry.media_content:
                    if mc.get("url"):
                        img = mc["url"]
                        break
            if not img and hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
                img = entry.media_thumbnail[0].get("url", "")
            # Clean description
            desc = re.sub(r'<[^>]+>', '', desc).strip()[:200]
            if title and link and not is_default_image(img):
                entries.append({"url": link, "title": title, "desc": desc, "img": img, "source": source_name})
        log(f"  → RSS 获取 {len(entries)} 篇")
        return entries
    except ImportError:
        log(f"  → feedparser 未安装，跳过")
        return []
    except Exception as e:
        log(f"  → RSS 解析失败: {str(e)[:60]}")
        return []

# ── HTML 抓取（针对无 RSS 的网站） ──
def scrape_itsnicethat():
    log(f"  抓取 It's Nice That (articles)")
    html = fetch("https://www.itsnicethat.com/articles")
    if not html:
        return []
    entries = []
    seen = set()
    for m in re.finditer(r'href="(/articles/[^"\.]+)"', html):
        url = "https://www.itsnicethat.com" + m.group(1)
        if url in seen:
            continue
        seen.add(url)
        if len(entries) >= 8:
            break
        # Try to get title from nearby
        snippet = html[max(0,m.start()-100):m.end()+200]
        title_m = re.search(r'<h[23][^>]*>([^<]+)</h[23]>', snippet)
        title = htmlmod.unescape(title_m.group(1)).strip() if title_m else ""
        # Try to get image
        img_m = re.search(r'<img[^>]+src="([^"]+)"', snippet)
        img = htmlmod.unescape(img_m.group(1)) if img_m else ""
        if title and url and not is_default_image(img):
            entries.append({"url": url, "title": title, "desc": "", "img": img, "source": "It's Nice That"})
    log(f"  → 抓取 {len(entries)} 篇")
    return entries

# ── DeepSeek 翻译 ──
def call_deepseek(prompt, max_tokens=8192):
    if not DEEPSEEK_API_KEY:
        log("  ❌ DEEPSEEK_API_KEY 未设置")
        return None
    data = json.dumps({
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你是一个专业的设计编辑，擅长用中文撰写设计文章摘要和评论。文章解读有观点、有判断，不写空洞的推荐语。禁止使用「值得一看」「不容错过」等套路句式。必须返回纯JSON，不要markdown包裹。"},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }).encode()
    req = Request(DEEPSEEK_URL, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
    })
    try:
        resp = urlopen(req, timeout=60)
        body = json.loads(resp.read())
        return body["choices"][0]["message"]["content"]
    except Exception as e:
        log(f"  ❌ DeepSeek API 失败: {str(e)[:100]}")
        return None

def json_try_parse(text):
    """尝试解析 JSON，如果失败则尝试修复常见问题。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Repair: try to truncate at last complete object
    for i in range(len(text), 0, -1):
        if text[i-1] == '}':
            try:
                return json.loads(text[:i])
            except json.JSONDecodeError:
                continue
    return None

def translate_articles(candidates):
    """Call DeepSeek per-article to translate & write summaries/excerpts."""
    if not candidates:
        return []

    results = []
    for i, c in enumerate(candidates):
        log(f"  翻译 ({i+1}/{len(candidates)}): {c['title'][:40]}...")

        prompt = f"""请翻译以下设计文章。保留关键品牌/人名/项目名。

1. 中文标题
2. 中文摘要（35-70汉字），说明这篇在讲什么、为什么值得关注
3. 中文摘录（200-500汉字），展开介绍核心观点、设计亮点和判断价值

要求：不要以"值得一看"、"不容错过"等套路句式结尾。收尾要自然、言之有物。

返回 JSON 格式:
{{
  "index": {i},
  "title_cn": "...",
  "summary_cn": "...",
  "excerpt_cn": "..."
}}

原文标题: {c['title']}
原文描述: {(c.get('desc') or '')[:300]}"""

        result = call_deepseek(prompt)
        if not result:
            log(f"  ✗ 第{i+1}篇翻译失败，使用原文标题")
            continue

        try:
            parsed = json_try_parse(result)
            if parsed is None:
                log(f"  ✗ 第{i+1}篇解析失败，使用原文标题")
                continue
            parsed["index"] = i  # ensure index
            results.append(parsed)
            log(f"  ✓ 第{i+1}篇完成")
        except Exception as e:
            log(f"  ✗ 第{i+1}篇异常: {e}")
            continue

    if results:
        log(f"  ✓ DeepSeek 翻译完成: {len(results)}/{len(candidates)} 篇")
    else:
        log("  ❌ 所有翻译均失败")
    return results

# ── 主流程 ──
def main():
    log("=" * 50)
    log("设计资讯摘要 - 自动发现 + 翻译")

    # 判断是否已有 articles.json（用于重试场景）
    if os.path.exists(ARTICLES_PATH):
        log(f"articles.json 已存在（{os.path.getsize(ARTICLES_PATH)} bytes），跳过发现阶段")
        return 0

    # ── 1. 发现阶段 ──
    log("第1步开始：扫描网站")
    all_candidates = []

    # RSS feeds
    rss_configs = [
        ("Hypebeast", "https://hypebeast.com/design/feed", parse_rss),
        ("Creative Boom", "https://www.creativeboom.com/feed/", parse_rss),
        ("Dribbble", "https://dribbble.com/stories.rss", parse_rss),
    ]
    for name, url, parser in rss_configs:
        entries = parser(url, name)
        all_candidates.extend(entries[:8])

    # HTML scraping for It's Nice That
    itsnicethat = scrape_itsnicethat()
    all_candidates.extend(itsnicethat[:8])

    log(f"第1步完成：共 {len(all_candidates)} 篇候选")

    if not all_candidates:
        log("❌ 没有候选文章，退出")
        return 1

    # ── 2. 获取详情（og:image） ──
    log("获取详情页元数据...")
    def get_detail(c):
        detail = fetch_article_detail(c["url"])
        return {
            "id": re.sub(r'[^a-z0-9_]', '_', c["url"].rstrip("/").split("/")[-1][:40].lower()) or f"art_{abs(hash(c['url'])) % 100000}",
            "title": detail.get("title") or c.get("title", ""),
            "desc": detail.get("desc") or c.get("desc", ""),
            "img": detail.get("img") or c.get("img", ""),
            "url": c["url"],
            "source": c["source"],
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        enriched = list(pool.map(get_detail, all_candidates))

    # Filter: needs real image
    enriched = [r for r in enriched if r["img"] and not is_default_image(r["img"])]

    # Deduplicate by URL
    seen = set()
    unique = []
    for r in enriched:
        if r["url"] not in seen:
            seen.add(r["url"])
            unique.append(r)

    log(f"详情页处理完成：{len(unique)} 篇有效候选")

    if not unique:
        log("❌ 没有有效候选文章")
        return 1

    # Limit per source
    source_count = {}
    limited = []
    for r in unique:
        sc = source_count.get(r["source"], 0)
        if sc < MAX_PER_SOURCE:
            limited.append(r)
            source_count[r["source"]] = sc + 1
    log(f"来源去重后：{len(limited)} 篇")

    # ── 3. DeepSeek 翻译 ──
    log("第2步开始：DeepSeek 翻译与精选")
    translated = translate_articles(limited)

    if not translated:
        # Fallback: use English titles with index as id
        log("使用原文标题作为后备")
        results = []
        for i, r in enumerate(limited):
            suffix = f" (via {r['source']})" if "/" not in r["title"] else ""
            results.append({
                "id": r["id"],
                "title_cn": r["title"],
                "summary_cn": f"来自{r['source']}的设计文章",
                "excerpt_cn": r.get("desc", f"来自{r['source']}的设计文章，点击阅读原文查看详情。"),
                "url": r["url"],
                "source": r["source"],
                "category": guess_category(r["title"], r.get("desc", "")),
                "og_image": r["img"],
            })
    else:
        # Merge translations with original data
        trans_map = {}
        for t in translated:
            idx = t.get("index")
            if idx is not None:
                trans_map[idx] = t

        results = []
        for i, r in enumerate(limited):
            t = trans_map.get(i, {})
            results.append({
                "id": r["id"],
                "title_cn": t.get("title_cn", r["title"]),
                "summary_cn": t.get("summary_cn", f"来自{r['source']}的设计文章"),
                "excerpt_cn": t.get("excerpt_cn", r.get("desc", "")),
                "url": r["url"],
                "source": r["source"],
                "category": guess_category(t.get("title_cn", r["title"]), r.get("desc", "")),
                "og_image": r["img"],
            })

    # ── 4. 精选 ──
    # 跨期去重：排除已经发过的文章
    history = load_sent_urls()
    before = len(results)
    results = [r for r in results if r["url"] not in history]
    skipped = before - len(results)
    if skipped:
        log(f"  跨期去重：跳过 {skipped} 篇已发过的文章")

    # Dedup by topic
    deduped = deduplicate_by_topic(results)

    # Limit: total 8-12, at least 3 sources
    final = select_final(deduped)
    log(f"第2步完成：精选了 {len(final)} 篇")

    if not final:
        log("❌ 精选后没有文章")
        return 1

    # ── 5. 保存 articles.json ──
    log("第3步：保存 articles.json")
    with open(ARTICLES_PATH, "w", encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False, indent=2)
    log(f"已保存 {len(final)} 篇 → {ARTICLES_PATH}")

    # ── 6. 运行 pipeline ──
    log("第4步开始：运行 pipeline")
    pipeline = os.path.join(SCRIPT_DIR, "digest_pipeline.py")
    if not os.path.exists(pipeline):
        log(f"❌ pipeline 未找到: {pipeline}")
        return 1

    import subprocess
    env = os.environ.copy()
    env["DESIGN_DIGEST_NOW"] = time.strftime("%Y-%m-%d")
    result = subprocess.run(
        [sys.executable, pipeline, ARTICLES_PATH, "--no-send"],
        capture_output=False, env=env,
    )
    if result.returncode == 0:
        log("第4步完成：pipeline 成功（图片和 HTML 已生成，等待 git 提交后发信）")
        # 记录已发文章 URL 到历史，避免下期重复
        save_sent_urls([a["url"] for a in final])
        # Do NOT remove articles.json yet - workflow needs it for send step
        return 0
    else:
        log(f"第4步失败：pipeline exit code {result.returncode}")
        return 1

# ── 跨期去重 ──

def load_sent_urls():
    """Load history of already-sent article URLs."""
    if not os.path.exists(HISTORY_PATH):
        return set()
    try:
        with open(HISTORY_PATH, "r") as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()

def save_sent_urls(new_urls):
    """Append new URLs to the sent history, keep only last 100 to allow fresh articles in."""
    existing = load_sent_urls()
    existing.update(new_urls)
    # Keep only most recent 100 to avoid exhausting all candidates
    trimmed = sorted(existing)[-100:]
    try:
        with open(HISTORY_PATH, "w") as f:
            json.dump(trimmed, f, ensure_ascii=False)
        log(f"  历史已更新：{len(trimmed)} 条（裁剪前 {len(existing)} 条）")
    except Exception as e:
        log(f"  ⚠ 保存历史失败: {e}")

# ── 辅助函数 ──
def guess_category(title, desc=""):
    text = (title + " " + desc).lower()
    if any(w in text for w in ["ai", "人工智能", "generative", "chatgpt", "machine learning", "gpt"]):
        return "AIGC"
    if any(w in text for w in ["brand", "品牌", "logo", "identity", "包装", "retail", "联名", "collaboration", "marketing", "campaign"]):
        return "商业与品牌"
    if any(w in text for w in ["artist", "艺术", "exhibition", "展览", "illustration", "插画", "photography", "摄影", "design week", "文化", "festival"]):
        return "创意文化"
    if any(w in text for w in ["typography", "排版", "ui", "ux", "design system", "设计系统", "trend", "趋势", "methodology", "grid", "layout"]):
        return "设计理论"
    if any(w in text for w in ["3d", "animation", "motion", "visual", "graphic", "平面", "poster", "poster"]):
        return "视觉参考"
    return "设计理论"

def deduplicate_by_topic(articles):
    """Remove articles on the same topic (e.g. two '2026 trends' articles)."""
    topics = []
    result = []
    for a in articles:
        t = a.get("title_cn", "") + " " + a.get("summary_cn", "")
        # Extract Chinese keywords (first 8 meaningful chars)
        keywords = re.findall(r'[一-鿿]{2,}', t)
        key_set = set(k for k in keywords if len(k) >= 3)
        is_dup = False
        for existing in topics:
            overlap = key_set & existing
            if len(overlap) >= 2:
                is_dup = True
                break
        if not is_dup:
            topics.append(key_set)
            result.append(a)
    return result

def select_final(articles):
    """Select 10-12 articles, at least 3 sources, max 6 per source."""
    if len(articles) <= 12:
        return articles

    # Limit per source (max 6)
    sc = {}
    result = []
    for a in articles:
        s = sc.get(a["source"], 0)
        if s < 6:
            result.append(a)
            sc[a["source"]] = s + 1

    # Ensure at least 10
    if len(result) < 10:
        return result

    # If still > 12, trim to 12
    if len(result) > 12:
        groups = {}
        for a in result:
            groups.setdefault(a["source"], []).append(a)
        final = []
        for src, items in groups.items():
            final.extend(items[:2])
        remaining = []
        for src, items in groups.items():
            remaining.extend(items[2:])
        final.extend(remaining[:max(0, 12 - len(final))])
        return final
    return result


if __name__ == "__main__":
    sys.exit(main())
