#!/usr/bin/env python3
"""
设计资讯摘要 - 完整管道
===================
用  法: python3 digest_pipeline.py <articles.json>
  测试: python3 digest_pipeline.py --test-data --no-send

工作流程:
  1. 读取文章数据（正式 JSON；测试数据需显式开启）
  2. 加载已有图片缓存，只对未缓存的新文章下载图片
  3. 每张图片: 下载 → 居中裁切 3:2 (600×400) → JPEG q75 → base64
  4. 过滤: 排除无图文章, 同一来源≤5篇, 同一分类同来源≤2篇
  5. 选择 Editor's Pick（视觉冲击力最强的文章）
  6. 构建 CID (Content-ID) 嵌入的 HTML 邮件
  7. 可选生成预览或通过 QQ 邮箱 SMTP 发送

JSON 输入格式 (articles.json):
[
  {
    "id": "唯一标识符",
    "title_cn": "中文标题",
    "summary_cn": "中文摘要",
    "url": "https://原文链接",
    "source": "来源网站名",
    "category": "设计理论|视觉参考|AIGC|创意文化|商业与品牌",
    "og_image": "https://封面图片URL（可选，留空则自动从文章页抓取）"
  },
  ...
]

依赖: Python 3, Pillow (pip install Pillow --break-system-packages)
"""
import json, os, sys, re, html as htmlmod, base64, io, ssl, subprocess, time
from urllib.request import Request, urlopen
from concurrent.futures import ThreadPoolExecutor, as_completed
import smtplib
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from datetime import datetime
try:
    from PIL import Image
except ImportError:
    print("❌ 缺少 Pillow。请先运行: python3 -m pip install Pillow --break-system-packages")
    sys.exit(2)

Image.MAX_IMAGE_PIXELS = 24_000_000

# ═══════════════════ 配置 ═══════════════════
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(SCRIPT_DIR, "img_cache.json")
FAIL_CACHE_PATH = os.path.join(SCRIPT_DIR, "img_failures.json")
OUTPUT_HTML = os.environ.get("DESIGN_DIGEST_OUTPUT_HTML", os.path.join(SCRIPT_DIR, "design_digest_latest.html"))
OUTPUT_READER_HTML = os.environ.get("DESIGN_DIGEST_OUTPUT_READER_HTML",
    os.path.join(SCRIPT_DIR, "design_digest_reader.html"))
LOCK_PATH = os.path.join(SCRIPT_DIR, ".digest_pipeline.lock")
IMAGES_DIR = os.path.join(SCRIPT_DIR, "images")
IMAGE_BASE_URL = os.environ.get("DESIGN_DIGEST_IMAGE_BASE_URL", "")  # 如: https://raw.githubusercontent.com/.../images

SMTP_HOST = os.environ.get("DESIGN_DIGEST_SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.environ.get("DESIGN_DIGEST_SMTP_PORT", "465"))
SMTP_USER = os.environ.get("DESIGN_DIGEST_SMTP_USER", "isawuonce@qq.com")
SMTP_PASS = os.environ.get("DESIGN_DIGEST_SMTP_PASS", "")
if not SMTP_PASS:
    print("❌ SMTP_PASS 未设置，请设置环境变量 DESIGN_DIGEST_SMTP_PASS")
TO_ADDRS_RAW = os.environ.get("DESIGN_DIGEST_TO_ADDRS", SMTP_USER)
TO_ADDRS = [addr.strip() for addr in TO_ADDRS_RAW.split(",") if addr.strip()]

_NOW_ENV = os.environ.get("DESIGN_DIGEST_NOW")
if _NOW_ENV:
    NOW = datetime.strptime(_NOW_ENV, "%Y-%m-%d")
else:
    NOW = datetime.now()
DATE_STR = NOW.strftime("%Y.%m.%d")
DATE_CN = NOW.strftime("%Y年%m月%d日")
DATE_EMAIL = NOW.strftime("%Y 年 %m 月 %d 日")
DATE_SHORT = NOW.strftime("%m/%d")

CAT_ORDER = ["设计理论", "视觉参考", "AIGC", "创意文化", "商业与品牌"]
CATEGORY_COLORS = {
    "设计理论": "#4f6fd9",
    "视觉参考": "#1fa6a0",
    "AIGC": "#7b61ff",
    "创意文化": "#f08a24",
    "商业与品牌": "#e8507f",
}
DEFAULT_CATEGORY_COLOR = "#80858f"

MAX_INPUT_ARTICLES = int(os.environ.get("DESIGN_DIGEST_MAX_ARTICLES", "20"))
MIN_SEND_ARTICLES = int(os.environ.get("DESIGN_DIGEST_MIN_SEND_ARTICLES", "5"))
MAX_IMAGE_FAILURES = int(os.environ.get("DESIGN_DIGEST_MAX_IMAGE_FAILURES", "2"))
FAILURE_COOLDOWN_SECONDS = int(os.environ.get("DESIGN_DIGEST_FAILURE_COOLDOWN_SECONDS", str(24 * 3600)))
MAX_OG_WORKERS = int(os.environ.get("DESIGN_DIGEST_MAX_OG_WORKERS", "4"))
MAX_IMAGE_WORKERS = int(os.environ.get("DESIGN_DIGEST_MAX_IMAGE_WORKERS", "4"))
PAGE_TIMEOUT_SECONDS = int(os.environ.get("DESIGN_DIGEST_PAGE_TIMEOUT_SECONDS", "6"))
IMAGE_TIMEOUT_SECONDS = int(os.environ.get("DESIGN_DIGEST_IMAGE_TIMEOUT_SECONDS", "8"))
SMTP_TIMEOUT_SECONDS = int(os.environ.get("DESIGN_DIGEST_SMTP_TIMEOUT_SECONDS", "30"))
LOCK_STALE_SECONDS = int(os.environ.get("DESIGN_DIGEST_LOCK_STALE_SECONDS", str(30 * 60)))
MAX_IMAGE_BYTES = int(os.environ.get("DESIGN_DIGEST_MAX_IMAGE_BYTES", str(8 * 1024 * 1024)))

# ═══════════════════ 通用工具 ═══════════════════

def is_http_url(url):
    return isinstance(url, str) and url.startswith(("http://", "https://"))

def load_json_dict(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"  ⚠️ 读取 {os.path.basename(path)} 失败，已忽略: {e}")
        return {}

def atomic_write_json(path, data):
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp_path, path)

def is_valid_cached_image(value):
    return isinstance(value, str) and len(value) > 100

def acquire_run_lock():
    now = time.time()
    if os.path.exists(LOCK_PATH):
        age = now - os.path.getmtime(LOCK_PATH)
        if age < LOCK_STALE_SECONDS:
            raise RuntimeError(f"已有 digest_pipeline 正在运行，锁文件未过期: {LOCK_PATH}")
        print("  ⚠️ 发现过期运行锁，已清理")
        try:
            os.remove(LOCK_PATH)
        except FileNotFoundError:
            pass
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w") as f:
        f.write(f"pid={os.getpid()}\nstarted_at={datetime.now().isoformat()}\n")

def release_run_lock():
    try:
        os.remove(LOCK_PATH)
    except FileNotFoundError:
        pass

def should_skip_failed_image(article_id, failures):
    rec = failures.get(article_id)
    if not isinstance(rec, dict):
        return False
    attempts = int(rec.get("attempts", 0) or 0)
    last_failed_at = float(rec.get("last_failed_at", 0) or 0)
    if attempts < MAX_IMAGE_FAILURES:
        return False
    return time.time() - last_failed_at < FAILURE_COOLDOWN_SECONDS

def record_image_failure(failures, article_id, reason):
    rec = failures.get(article_id, {})
    if not isinstance(rec, dict):
        rec = {}
    failures[article_id] = {
        "attempts": int(rec.get("attempts", 0) or 0) + 1,
        "last_failed_at": int(time.time()),
        "reason": str(reason)[:200],
    }

def clear_image_failure(failures, article_id):
    failures.pop(article_id, None)

def normalize_article_id(value, index):
    raw = str(value or f"article_{index + 1}").strip().lower()
    normalized = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")
    return normalized or f"article_{index + 1}"

def normalize_articles(data):
    if not isinstance(data, list):
        raise ValueError("articles.json 必须是数组")

    if len(data) > MAX_INPUT_ARTICLES:
        print(f"  ⚠️ 输入 {len(data)} 篇，超过上限 {MAX_INPUT_ARTICLES}，只处理前 {MAX_INPUT_ARTICLES} 篇")
        data = data[:MAX_INPUT_ARTICLES]

    seen_urls = set()
    seen_ids = set()
    normalized = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            print(f"  ~ 跳过第 {index + 1} 项：不是对象")
            continue
        url = (item.get("url") or "").strip()
        if not is_http_url(url):
            print(f"  ~ 跳过第 {index + 1} 项：URL 无效")
            continue
        if url in seen_urls:
            print(f"  ~ 跳过重复 URL: {url}")
            continue
        seen_urls.add(url)

        article = dict(item)
        aid = normalize_article_id(article.get("id"), index)
        if aid in seen_ids:
            aid = f"{aid}_{index + 1}"
        seen_ids.add(aid)
        article["id"] = aid
        article["url"] = url
        article["source"] = str(article.get("source") or "Unknown").strip()
        article["category"] = str(article.get("category") or "设计参考").strip()
        normalized.append(article)

    return normalized

# ═══════════════════ 1. 加载文章 ═══════════════════

def load_articles(json_path=None, use_test_data=False):
    """加载文章：正式运行必须传入 JSON；测试数据需显式开启"""
    if json_path and os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        articles = normalize_articles(data)
        print(f"  📄 从 {json_path} 加载 {len(articles)} 篇有效文章")
        return articles

    if not use_test_data:
        raise FileNotFoundError("缺少 articles.json。正式运行不会自动使用内置测试文章。")

    # 内置默认（开发/测试用）
    print("  📄 使用内置默认文章 (10 篇)")
    return normalize_articles([
        {"id":"int_trends","title_cn":"2026年平面设计趋势：值得收藏的5大方向","summary_cn":"低油墨复印美学、视觉索引拼贴、微图形技术蓝图、墨渍Logo、混合拼贴字体——五大新兴平面设计趋势详解。","url":"https://www.itsnicethat.com/features/forward-thinking-graphic-trends-2026-graphic-design-120126","source":"Its Nice That","category":"设计理论"},
        {"id":"int_hellcare","title_cn":"「Hellcare Regular」字体：戏仿医生天书的黑色幽默","summary_cn":"Wieden+Kennedy 艺术总监创造了一款模仿医生潦草笔迹的字体，用排版设计批判美国医疗体系的混乱与冷漠。","url":"https://www.itsnicethat.com/articles/parker-jones-rajshree-saraf-hellcare-regular-graphic-design-project-130526","source":"Its Nice That","category":"视觉参考"},
        {"id":"cb_trends","title_cn":"Stills 2026 趋势报告：以人为本的设计崛起","summary_cn":"剪贴簿美学、赛博哥特、未来中世纪风格、感官叙事……2026年设计趋势强调大胆、有温度、以人为本的创意方向。","url":"https://www.creativeboom.com/insight/stills-trends-report-demonstrates-how-bold-human-centred-design-is-defining-2026/","source":"Creative Boom","category":"设计理论"},
        {"id":"cb_bham","title_cn":"伯明翰设计节2026：主题「变革」恰逢其时","summary_cn":"超过60位演讲者、三大主题板块、8000张免费门票——在全球化变局中探讨设计的应变之道。","url":"https://www.creativeboom.com/news/the-theme-of-this-years-birmingham-design-festival-couldnt-be-more-apt/","source":"Creative Boom","category":"创意文化"},
        {"id":"hb_bottega","title_cn":"Bottega Veneta 2026秋冬广告：威尼斯的粗粝之美","summary_cn":"Chris Rhodes 掌镜，以粗粝而亲密的视角展现威尼斯与新系列的原始美感。","url":"https://hypebeast.com/2026/5/bottega-veneta-fall-2026-chris-rhodes-photographed-louise-trotter-campaign","source":"Hypebeast","category":"视觉参考"},
        {"id":"hb_apc","title_cn":"A.P.C. × fragment：从头到脚的日本原色丹宁","summary_cn":"以 Charlie Chaplin 和 Paul Newman 为灵感的丹宁联名系列，闪电吉他Logo贯穿始终。","url":"https://hypebeast.com/2026/5/apc-paris-fragment-collaboration-ss26-collection-release-info","source":"Hypebeast","category":"商业与品牌"},
        {"id":"hb_synth","title_cn":"Tame Impala 限量透明合成器：音乐与工业设计的交响","summary_cn":"Kevin Parker 的 Telepathic Instruments 发布透明 Orchid Arctic 限量版，全球仅3000台。","url":"https://hypebeast.com/2026/5/kevin-parker-tame-impala-telepathic-instruments-clear-orchid-arctic-release-info","source":"Hypebeast","category":"创意文化"},
        {"id":"hb_flea","title_cn":"Hypebeast Flea 登陆广州：七天社群创意盛宴","summary_cn":"30+创意单位、11000+参与者，从艺术装置到跑步社群，Hypebeast 的20周年庆典在广州掀起文化热潮。","url":"https://hypebeast.com/zh/newsroom/hypebeast-flea-makes-its-guangzhou-debut-with-a-seven-day-celebration-of-community-and-creativity","source":"Hypebeast","category":"创意文化"},
        {"id":"dr_select","title_cn":"Dribbble Select：2026年度最佳设计作品精选","summary_cn":"从网页设计到品牌识别，Dribbble 精选年度最佳作品。","url":"https://dribbble.com/resources/agencies/ultimate-dribbble-select-best-shots","source":"Dribbble","category":"视觉参考"},
        {"id":"bh_trends","title_cn":"2026设计趋势视觉指南：AI与人类创造力的平衡","summary_cn":"10大设计趋势：节点式AI工具、粗野主义美学、沉浸式3D、新极简主义、情感驱动设计。","url":"https://www.behance.net/gallery/239027109/Design-Trends-2026","source":"Behance","category":"设计理论"},
    ])

# ═══════════════════ 2. 图片处理 ═══════════════════

def center_crop_to_ratio(img, target_w=600, target_h=400):
    """居中裁切为 3:2 比例 (600×400)"""
    src_w, src_h = img.size
    target_ratio = target_w / target_h
    src_ratio = src_w / src_h
    if src_ratio > target_ratio:
        new_w = int(src_h * target_ratio)
        offset = (src_w - new_w) // 2
        img = img.crop((offset, 0, offset + new_w, src_h))
    elif src_ratio < target_ratio:
        new_h = int(src_w / target_ratio)
        offset = (src_h - new_h) // 2
        img = img.crop((0, offset, src_w, offset + new_h))
    return img.resize((target_w, target_h), Image.LANCZOS)

def download_image(url, timeout=IMAGE_TIMEOUT_SECONDS):
    """下载图片，先尝试 urllib 再 curl"""
    if not is_http_url(url):
        return None
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read(MAX_IMAGE_BYTES + 1)
        if 1000 <= len(raw) <= MAX_IMAGE_BYTES:
            return raw
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["curl", "-s", "--fail", "--location", "--tlsv1.2", "--max-time", str(timeout),
             "-A", "Mozilla/5.0", url],
            capture_output=True, timeout=timeout+3
        )
        if 1000 <= len(result.stdout) <= MAX_IMAGE_BYTES:
            return result.stdout
    except Exception:
        pass
    return None

def clean_img_url(url):
    """Remove tracking parameters that cause 404s (esp. Hypebeast cbr parameter)."""
    url = re.sub(r'[\?&](cbr=)[^&]+', '', url)
    url = url.rstrip('?&')
    return url

def process_one_image(article_id, img_url):
    """下载单张图片 → 裁切3:2 → JPEG压缩 → base64 ∕ 文件"""
    try:
        img_url = clean_img_url(img_url)
        raw = download_image(img_url)
        if not raw:
            return (article_id, "")
        img = Image.open(io.BytesIO(raw))
        img = center_crop_to_ratio(img, 600, 400)
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=75, optimize=True)
        jpeg_bytes = buf.getvalue()
        b64 = base64.b64encode(jpeg_bytes).decode()
        # 保存文件用于公开 URL 托管
        try:
            os.makedirs(IMAGES_DIR, exist_ok=True)
            with open(os.path.join(IMAGES_DIR, f"{article_id}.jpg"), "wb") as f:
                f.write(jpeg_bytes)
        except OSError:
            pass
        return (article_id, f"data:image/jpeg;base64,{b64}")
    except Exception as e:
        print(f"    ✗ {article_id}: {e}")
        return (article_id, "")

def fetch_og_image_from_page(url, timeout=PAGE_TIMEOUT_SECONDS):
    """从文章页面提取 og:image URL（后备方案）"""
    if not is_http_url(url):
        return ""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        })
        with urlopen(req, timeout=timeout, context=ctx) as resp:
            data = resp.read(150000).decode('utf-8', errors='replace')
        patterns = [
            r'<meta\s+[^>]*property="og:image"[^>]*content="([^"]+)"',
            r'<meta\s+[^>]*content="([^"]+)"[^>]*property="og:image"',
            r'<meta\s+[^>]*name="twitter:image"[^>]*content="([^"]+)"',
        ]
        for pat in patterns:
            m = re.search(pat, data, re.IGNORECASE)
            if m:
                return htmlmod.unescape(m.group(1))
        m = re.search(r'"image"\s*:\s*"([^"]+)"', data)
        if m:
            return htmlmod.unescape(m.group(1))
    except Exception:
        pass
    return ""

def process_all_images(articles):
    """
    加载缓存 → 确定需要下载的文章 → 并行下载 → 保存缓存
    返回: img_cache dict
    """
    cache = load_json_dict(CACHE_PATH)
    failures = load_json_dict(FAIL_CACHE_PATH)
    ok = sum(1 for v in cache.values() if is_valid_cached_image(v))
    print(f"  💾 图片缓存: {ok}/{len(cache)} 有效")

    # 确定需要下载的文章（无缓存或缓存为空）
    to_download = []
    skipped_failures = 0
    for a in articles:
        aid = a["id"]
        cached = cache.get(aid, "")
        if is_valid_cached_image(cached):
            continue  # 已有
        if cached:
            cache.pop(aid, None)
        elif aid in cache:
            record_image_failure(failures, aid, "legacy empty cache value")
            cache.pop(aid, None)
        if should_skip_failed_image(aid, failures):
            skipped_failures += 1
            continue
        to_download.append(a)

    if not to_download:
        if skipped_failures:
            print(f"  ⏸️ {skipped_failures} 张图片处于失败冷却期，本次不重试")
            atomic_write_json(FAIL_CACHE_PATH, failures)
        print("  ✅ 所有图片已缓存，无需下载")
        return cache

    print(f"\n  ⬇️ 需要处理 {len(to_download)} 篇文章的图片")
    if skipped_failures:
        print(f"  ⏸️ {skipped_failures} 张图片处于失败冷却期，本次不重试")

    # 先尝试用文章中提供的 og_image URL
    image_urls = {}
    for a in to_download:
        img_url = a.get("og_image", "") or ""
        if is_http_url(img_url):
            image_urls[a["id"]] = img_url
        elif img_url:
            record_image_failure(failures, a["id"], "invalid og_image url")
            image_urls[a["id"]] = ""
        else:
            image_urls[a["id"]] = ""  # 稍后尝试从页面抓取

    # 对没有 og_image 的文章，从页面抓取
    need_fetch = [a for a in to_download if not image_urls.get(a["id"])]
    if need_fetch:
        print(f"\n  🔍 抓取 {len(need_fetch)} 篇文章的 og:image...")
        with ThreadPoolExecutor(max_workers=MAX_OG_WORKERS) as ex:
            fut_map = {ex.submit(fetch_og_image_from_page, a["url"]): a["id"] for a in need_fetch}
            for fut in as_completed(fut_map):
                aid = fut_map[fut]
                try:
                    url = fut.result()
                    image_urls[aid] = url if is_http_url(url) else ""
                    if not image_urls[aid]:
                        record_image_failure(failures, aid, "og:image not found")
                    print(f"    {'✓' if url else '~'} {aid}: og:image {'found' if url else 'not found'}")
                except Exception as e:
                    record_image_failure(failures, aid, f"og:image fetch failed: {e}")
                    print(f"    ✗ {aid}: {e}")

    # 过滤出有图片 URL 的
    to_process = {aid: url for aid, url in image_urls.items() if is_http_url(url)}
    print(f"\n  ⬇️ 并行下载 {len(to_process)} 张图片...")

    with ThreadPoolExecutor(max_workers=MAX_IMAGE_WORKERS) as ex:
        fut_map = {ex.submit(process_one_image, aid, url): aid for aid, url in to_process.items()}
        for fut in as_completed(fut_map):
            aid, b64 = fut.result()
            if b64:
                cache[aid] = b64
                clear_image_failure(failures, aid)
                print(f"    ✓ {aid}: {len(b64)//1024}KB")
            else:
                cache.pop(aid, None)
                record_image_failure(failures, aid, "image download or processing failed")
                print(f"    ~ {aid}: 下载/处理失败")

    # 保存缓存
    atomic_write_json(CACHE_PATH, cache)
    atomic_write_json(FAIL_CACHE_PATH, failures)
    print(f"  💾 缓存已更新 ({len(cache)} 张)")

    return cache

# ═══════════════════ 3. 过滤 ═══════════════════

def filter_and_enrich(articles, img_cache):
    """过滤出有图文章，并添加 title_cn/summary_cn"""
    valid = []
    for a in articles:
        b64 = img_cache.get(a["id"], "")
        if not b64 or len(b64) < 100:
            continue
        # 确保中文字段存在
        if "title_cn" not in a or not a["title_cn"]:
            a["title_cn"] = a.get("title_en", a.get("title", ""))
        if "summary_cn" not in a or not a["summary_cn"]:
            a["summary_cn"] = a.get("summary_en", a.get("summary", ""))
        valid.append(a)

    # 来源限制（同一来源不超过5篇）
    source_count = {}
    filtered = []
    for a in valid:
        src = a["source"]
        source_count.setdefault(src, 0)
        if source_count[src] < 5:
            filtered.append(a)
            source_count[src] += 1

    # 分类内同来源限制（同一分类同一来源不超过2篇）
    cat_src_count = {}
    result = []
    for a in filtered:
        key = (a.get("category", ""), a["source"])
        cat_src_count.setdefault(key, 0)
        if cat_src_count[key] < 2:
            result.append(a)
            cat_src_count[key] += 1

    return result

def select_editor_pick(articles):
    """选出 Editor's Pick（视觉最强的），从列表移除"""
    if not articles:
        return None, []
    priority = ["hb_bottega", "bh_trends", "hb_arket", "dr_select"]
    for pid in priority:
        for i, a in enumerate(articles):
            if a["id"] == pid:
                return articles.pop(i), articles
    return articles.pop(0), articles

# ═══════════════════ 4. HTML 生成（CID） ═══════════════════

def build_html(editor, grouped):
    """生成参考 QQ 邮箱阅读页的单栏 HTML 邮件，图片用 cid: 引用"""
    all_items = [editor]
    ordered_rest = []
    for cat in CAT_ORDER:
        ordered_rest.extend(grouped.get(cat, []))
    for cat, items in grouped.items():
        if cat not in CAT_ORDER:
            ordered_rest.extend(items)
    all_items.extend(ordered_rest)

    sources = " · ".join(sorted(set(a["source"] for a in all_items)))
    total_count = len(all_items)

    def article_card(a, is_first=False):
        u = htmlmod.escape(a["url"])
        aid = htmlmod.escape(a["id"])
        title = htmlmod.escape(a["title_cn"])
        summary = htmlmod.escape(a["summary_cn"])
        excerpt = htmlmod.escape(a.get("excerpt_cn", ""))
        source = htmlmod.escape(a["source"])
        category_name = a.get("category", "设计参考")
        category = htmlmod.escape(category_name)
        category_color = CATEGORY_COLORS.get(category_name, DEFAULT_CATEGORY_COLOR)
        title_size = "20px" if is_first else "18px"
        body_pad = "20px 24px 26px" if is_first else "18px 22px 24px"
        editor_badge = '''
                                <p style="margin:0 0 10px;">
                                    <span style="display:inline-block;background:#202124;color:#ffffff;border-radius:999px;padding:4px 12px;font-size:10px;line-height:1;font-weight:600;letter-spacing:1.5px;">EDITOR\'S PICK</span>
                                </p>''' if is_first else ""
        excerpt_html = f'''
                                <div style="margin:10px 0 14px;padding-top:12px;border-top:1px solid #eee;font-size:14px;line-height:1.8;color:#4a4f56;">{excerpt}</div>''' if excerpt else ""
        return f'''
            <tr>
                <td style="padding:0 0 26px;">
                    <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;background:#ffffff;">
                        <tr>
                            <td>
                                <a href="{u}" style="display:block;text-decoration:none;">
                                    <img src="{IMAGE_BASE_URL + '/' + aid + '.jpg' if IMAGE_BASE_URL else 'cid:' + aid}" alt="{title}" style="width:100%;height:auto;display:block;border:0;outline:none;text-decoration:none;">
                                </a>
                            </td>
                        </tr>
                        <tr>
                            <td class="card-body" style="padding:{body_pad};">
                                {editor_badge}
                                <p style="margin:0 0 12px;">
                                    <span style="display:inline-block;background:{category_color};color:#ffffff;border-radius:999px;padding:6px 14px;font-size:12px;line-height:1;font-weight:600;">{category}</span>
                                </p>
                                <h2 class="article-title" style="margin:0 0 10px;font-size:{title_size};line-height:1.42;font-weight:700;color:#202124;">
                                    <a href="{u}" style="color:#202124;text-decoration:none;">{title}</a>
                                </h2>
                                <p style="margin:0 0 10px;font-size:14px;line-height:1.75;color:#656a73;">
                                    <a href="{u}" style="color:#656a73;text-decoration:none;">{summary}</a>
                                </p>
                                {excerpt_html}
                                <p style="margin:0;font-size:12px;line-height:1.5;color:#a1a7b0;">{source}</p>
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>'''

    cards_html = article_card(editor, True)
    for article in ordered_rest:
        cards_html += article_card(article)

    return f'''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
<style>
@media only screen and (max-width:600px){{
  .container{{width:100%!important;max-width:100%!important}}
  .header-pad{{padding:42px 16px 24px!important}}
  .content-pad{{padding:0 14px 28px!important}}
  .card-body{{padding:18px 18px 22px!important}}
  .article-title{{font-size:17px!important;line-height:1.45!important}}
}}
</style>
</head>
<body style="margin:0;padding:0;background:#f2f2f2;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Noto Sans SC','Microsoft YaHei',Arial,sans-serif;">
<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;background:#f2f2f2;">
    <tr>
        <td align="center">
            <table role="presentation" cellpadding="0" cellspacing="0" class="container" style="width:100%;max-width:720px;border-collapse:collapse;background:#f2f2f2;">
                <tr>
                    <td class="header-pad" style="padding:52px 24px 26px;text-align:center;">
                        <h1 style="margin:0 0 12px;font-size:26px;line-height:1.3;font-weight:800;color:#202124;">设计资讯摘要</h1>
                        <p style="margin:0 0 8px;font-size:15px;line-height:1.5;color:#8d939c;">{DATE_EMAIL} · 精选 {total_count} 篇</p>
                        <p style="margin:0;font-size:14px;line-height:1.6;color:#b1b6bd;">每 3 天 · 从 5 个设计网站为你筛选</p>
                    </td>
                </tr>
                <tr>
                    <td class="content-pad" style="padding:0 24px 34px;">
                        <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;">
                            {cards_html}
                        </table>
                    </td>
                </tr>
                <tr>
                    <td style="padding:0 24px 44px;text-align:center;">
                        <p style="margin:0 0 16px;font-size:14px;line-height:1.7;color:#656a73;">如果喜欢，下期再见 ✦</p>
                        <p style="margin:0;font-size:11px;line-height:1.7;color:#a8adb5;">DESIGN DIGEST · 每3天推送 · 数据来源：{htmlmod.escape(sources)}</p>
                    </td>
                </tr>
            </table>
        </td>
    </tr>
</table>
</body>
</html>'''

# ═══════════════════ 5. 交互式阅读页面 ═══════════════════

def build_reader_html(editor, grouped, img_cache):
    """生成单文件交互式阅读页面，文章数据以 JSON 嵌入，JS 实现详情/返回功能"""
    all_items = [editor]
    ordered_rest = []
    for cat in CAT_ORDER:
        ordered_rest.extend(grouped.get(cat, []))
    for cat, items in grouped.items():
        if cat not in CAT_ORDER:
            ordered_rest.extend(items)
    all_items.extend(ordered_rest)

    sources = " · ".join(sorted(set(a["source"] for a in all_items)))
    total_count = len(all_items)

    # Prepare JSON data for JS
    articles_data = []
    for a in all_items:
        img_b64 = img_cache.get(a["id"], "")
        articles_data.append({
            "id": a["id"],
            "title_cn": a["title_cn"],
            "summary_cn": a.get("summary_cn", ""),
            "excerpt_cn": a.get("excerpt_cn", a.get("summary_cn", "")),
            "url": a["url"],
            "source": a["source"],
            "category": a.get("category", "设计参考"),
            "img": img_b64,
        })

    articles_json = json.dumps(articles_data, ensure_ascii=False)

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
<title>设计资讯摘要 · {DATE_CN}</title>
<style>
:root {{
  --bg: #f2f2f2;
  --card-bg: #ffffff;
  --text: #202124;
  --text-secondary: #656a73;
  --text-muted: #a1a7b0;
  --max-w: 720px;
  --radius: 0;
  font-family: -apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans SC","Microsoft YaHei",Arial,sans-serif;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:var(--bg); color:var(--text); -webkit-font-smoothing:antialiased; }}
.container {{ max-width:var(--max-w); margin:0 auto; padding:52px 24px 44px; }}
.header {{ text-align:center; margin-bottom:26px; }}
.header h1 {{ font-size:26px; font-weight:800; margin-bottom:12px; }}
.header .meta {{ font-size:15px; color:var(--text-secondary); margin-bottom:8px; }}
.header .tagline {{ font-size:14px; color:var(--text-muted); }}

.article-card {{
  background:var(--card-bg); margin-bottom:26px; cursor:pointer;
  transition:box-shadow 0.2s, transform 0.15s;
}}
.article-card:hover {{
  box-shadow:0 4px 16px rgba(0,0,0,0.08);
  transform:translateY(-1px);
}}
.article-card img {{
  width:100%; height:auto; display:block;
}}
.card-body {{ padding:20px 24px 26px; }}
.card-body.is-first {{ padding:20px 24px 26px; }}
.editor-badge {{
  display:inline-block; background:var(--text); color:#fff;
  border-radius:999px; padding:4px 12px; font-size:10px; line-height:1;
  font-weight:600; letter-spacing:1.5px; margin-bottom:10px;
}}
.cat-chip {{
  display:inline-block; color:#fff; border-radius:999px;
  padding:6px 14px; font-size:12px; line-height:1; font-weight:600;
  margin-bottom:12px;
}}
.article-card h2 {{
  font-size:20px; line-height:1.42; font-weight:700; margin-bottom:10px;
}}
.article-card p.summary {{
  font-size:14px; line-height:1.75; color:var(--text-secondary); margin-bottom:14px;
}}
.article-card p.source {{
  font-size:12px; line-height:1.5; color:var(--text-muted);
}}

/* Detail overlay */
.detail-overlay {{
  position:fixed; top:0; left:0; width:100%; height:100%;
  z-index:1000; overflow-y:auto; background:var(--bg);
  display:none;
}}
.detail-overlay.open {{ display:block; }}
.detail-inner {{
  max-width:var(--max-w); margin:0 auto; padding:24px;
}}
.detail-back {{
  display:inline-flex; align-items:center; gap:6px;
  background:none; border:none; cursor:pointer;
  font-size:15px; color:var(--text-secondary); padding:8px 0;
  margin-bottom:20px; font-family:inherit;
}}
.detail-back:hover {{ color:var(--text); }}
.detail-image {{
  width:100%; height:auto; display:block; margin-bottom:24px;
}}
.detail-category {{
  display:inline-block; color:#fff; border-radius:999px;
  padding:6px 14px; font-size:12px; line-height:1; font-weight:600;
  margin-bottom:12px;
}}
.detail-body h2 {{
  font-size:24px; line-height:1.38; font-weight:700; margin-bottom:16px;
}}
.detail-body .detail-source {{
  font-size:13px; color:var(--text-muted); margin-bottom:20px;
}}
.detail-body .detail-excerpt {{
  font-size:16px; line-height:1.8; color:var(--text-secondary);
}}
.detail-body .detail-excerpt p {{
  margin-bottom:16px;
}}
.detail-read-more {{
  display:inline-block; margin-top:24px; padding:12px 28px;
  background:var(--text); color:#fff; text-decoration:none;
  font-size:14px; font-weight:600; border-radius:0;
  transition:opacity 0.2s;
}}
.detail-read-more:hover {{ opacity:0.85; }}

/* Footer */
.footer {{ text-align:center; padding:0 24px 44px; }}
.footer p {{ font-size:14px; color:var(--text-secondary); margin-bottom:16px; }}
.footer .copyright {{ font-size:11px; color:#a8adb5; }}

@media (max-width:600px) {{
  .container {{ padding:42px 16px 24px; }}
  .card-body {{ padding:18px 18px 22px; }}
  .article-card h2 {{ font-size:17px; }}
  .detail-body h2 {{ font-size:20px; }}
}}
</style>
</head>
<body>

<div class="container" id="list-view">
  <div class="header">
    <h1>设计资讯摘要</h1>
    <p class="meta">{DATE_EMAIL} · 精选 {total_count} 篇</p>
    <p class="tagline">每 3 天 · 从 5 个设计网站为你筛选</p>
  </div>
  <div id="card-grid"></div>
</div>

<div class="detail-overlay" id="detail-overlay">
  <div class="detail-inner">
    <button class="detail-back" id="back-btn">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
      返回列表
    </button>
    <div id="detail-content"></div>
  </div>
</div>

<div class="footer">
  <p>如果喜欢，下期再见 ✦</p>
  <p class="copyright">DESIGN DIGEST · 每3天推送 · 数据来源：{htmlmod.escape(sources)}</p>
</div>

<script>
var ARTICLES = {articles_json};
var CAT_COLORS = {json.dumps(CATEGORY_COLORS, ensure_ascii=False)};

function getCatColor(cat) {{
  return CAT_COLORS[cat] || '#80858f';
}}

function renderCards() {{
  var grid = document.getElementById('card-grid');
  grid.innerHTML = '';
  ARTICLES.forEach(function(a, i) {{
    var isFirst = i === 0;
    var badge = isFirst ? '<span class="editor-badge">EDITOR\\'S PICK</span>' : '';
    var titleSize = isFirst ? '20px' : '18px';
    var card = document.createElement('div');
    card.className = 'article-card';
    card.onclick = function() {{ openDetail(i); }};
    card.innerHTML =
      '<img src="' + a.img + '" alt="' + esc(a.title_cn) + '">' +
      '<div class="card-body' + (isFirst ? ' is-first' : '') + '">' +
        badge +
        '<span class="cat-chip" style="background:' + getCatColor(a.category) + '">' + esc(a.category) + '</span>' +
        '<h2 style="font-size:' + titleSize + '">' + esc(a.title_cn) + '</h2>' +
        '<p class="summary">' + esc(a.summary_cn) + '</p>' +
        '<p class="source">' + esc(a.source) + '</p>' +
      '</div>';
    grid.appendChild(card);
  }});
}}

function openDetail(index) {{
  var a = ARTICLES[index];
  var catColor = getCatColor(a.category);
  var excerpt = a.excerpt_cn || a.summary_cn;
  // Convert newlines to paragraphs
  var paragraphs = excerpt.split('\\n').filter(function(p) {{ return p.trim(); }});
  var excerptHtml = paragraphs.map(function(p) {{ return '<p>' + esc(p) + '</p>'; }}).join('');

  document.getElementById('detail-content').innerHTML =
    '<img class="detail-image" src="' + a.img + '" alt="' + esc(a.title_cn) + '">' +
    '<span class="detail-category" style="background:' + catColor + '">' + esc(a.category) + '</span>' +
    '<div class="detail-body">' +
      '<h2>' + esc(a.title_cn) + '</h2>' +
      '<p class="detail-source">' + esc(a.source) + '</p>' +
      '<div class="detail-excerpt">' + excerptHtml + '</div>' +
      '<a class="detail-read-more" href="' + esc(a.url) + '" target="_blank">阅读原文 ↗</a>' +
    '</div>';

  document.getElementById('detail-overlay').classList.add('open');
  document.body.style.overflow = 'hidden';
  window.scrollTo(0, 0);
}}

function closeDetail() {{
  document.getElementById('detail-overlay').classList.remove('open');
  document.body.style.overflow = '';
}}

document.getElementById('back-btn').onclick = closeDetail;

// Close on overlay background click
document.getElementById('detail-overlay').onclick = function(e) {{
  if (e.target === document.getElementById('detail-overlay')) {{
    closeDetail();
  }}
}};

// Escape HTML entities
function esc(s) {{
  if (!s) return '';
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}}

renderCards();
</script>
</body>
</html>'''


# ═══════════════════ 7. SMTP 发送 ═══════════════════

def send_email(html_content, editor, grouped, subject=None):
    """通过 SMTP 发送 CID 嵌入图片的邮件"""
    img_cache = load_json_dict(CACHE_PATH)

    def get_binary(aid):
        b64 = img_cache.get(aid, "")
        if not b64:
            return None
        b64_data = b64.split(",", 1)[1] if "," in b64 else b64
        return base64.b64decode(b64_data)

    msg_related = MIMEMultipart("related")
    msg_related.attach(MIMEText(html_content, "html", "utf-8"))

    all_arts = [editor]
    for v in grouped.values():
        all_arts.extend(v)

    attached = 0
    skip_cid = bool(IMAGE_BASE_URL)  # 使用公开 URL 时跳过 CID 附件
    for a in all_arts:
        if skip_cid:
            continue
        data = get_binary(a["id"])
        if data:
            try:
                part = MIMEImage(data)
                part.add_header("Content-ID", f"<{a['id']}>")
                part.add_header("Content-Disposition", "inline")
                msg_related.attach(part)
                attached += 1
            except Exception as e:
                print(f"  ✗ {a['id']}: CID 附件失败 - {e}")

    msg = MIMEMultipart("mixed")
    msg["From"] = f"pichan <{SMTP_USER}>"
    msg["To"] = TO_ADDRS[0]
    if len(TO_ADDRS) > 1:
        msg["Bcc"] = ", ".join(TO_ADDRS[1:])
    msg["Subject"] = subject or f"设计资讯摘要 · {DATE_CN}"
    msg.attach(msg_related)

    print(f"  📧 发送: {attached} 张 CID 图片, {len(all_arts)} 篇文章")
    # Try configured port first, then fallback
    alt_port = 587 if SMTP_PORT == 465 else 465
    for port in [SMTP_PORT, alt_port]:
        use_ssl = (port == 465)
        try:
            if use_ssl:
                server = smtplib.SMTP_SSL(SMTP_HOST, port, timeout=SMTP_TIMEOUT_SECONDS)
            else:
                server = smtplib.SMTP(SMTP_HOST, port, timeout=SMTP_TIMEOUT_SECONDS)
                server.starttls()
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, TO_ADDRS, msg.as_string())
            server.quit()
            print("  ✅ 邮件发送成功!")
            break
        except (smtplib.SMTPException, OSError, ssl.SSLError, TimeoutError) as e:
            port_label = f"端口{port} (SSL)" if use_ssl else f"端口{port} (STARTTLS)"
            print(f"  ⚠ {port_label} 失败: {str(e)[:80]}")
            if port == 587:
                print(f"  ❌ 所有 SMTP 方式均失败")
                raise

# ═══════════════════ 8. 主流程 ═══════════════════

def parse_args(argv):
    use_test_data = "--test-data" in argv
    no_send = "--no-send" in argv
    json_paths = [arg for arg in argv if not arg.startswith("--")]
    json_path = json_paths[0] if json_paths else None
    return json_path, use_test_data, no_send

def main():
    print("=" * 56)
    print(f"  设计资讯摘要 · {DATE_CN}")
    print("=" * 56)

    try:
        acquire_run_lock()
    except (RuntimeError, OSError) as e:
        print(f"❌ {e}")
        return 3

    try:
        json_path, use_test_data, no_send = parse_args(sys.argv[1:])

        # 1. 加载文章
        print("\n📝 加载文章...")
        try:
            articles = load_articles(json_path, use_test_data=use_test_data)
        except Exception as e:
            print(f"❌ 加载文章失败: {e}")
            return 2
        print(f"    {len(articles)} 篇候选")

        if not articles:
            print("❌ 没有有效候选文章，取消发送")
            return 2

        # 2. 处理图片（缓存优先）
        print("\n🖼️ 处理图片...")
        img_cache = process_all_images(articles)
        ok_img = sum(1 for v in img_cache.values() if is_valid_cached_image(v))
        print(f"    {ok_img} 张图片可用")

        # 3. 过滤
        print("\n🔎 过滤...")
        valid = filter_and_enrich(articles, img_cache)
        print(f"    {len(valid)} 篇通过过滤（有图+来源/分类限制）")

        if len(valid) < MIN_SEND_ARTICLES:
            print(f"❌ 可发送文章只有 {len(valid)} 篇，低于下限 {MIN_SEND_ARTICLES}，取消发送")
            return 2

        # 4. 选择 Editor's Pick
        editor, rest = select_editor_pick(valid)
        if not editor:
            print("❌ 没有可用文章，取消发送")
            return 2
        print(f"\n⭐ Editor's Pick: {editor['title_cn'][:30]}...")

        # 5. 分组
        grouped = {}
        for a in rest:
            grouped.setdefault(a.get("category", ""), []).append(a)

        print("\n📊 分类:")
        for cat in CAT_ORDER:
            if cat in grouped:
                print(f"    {cat}: {len(grouped[cat])} 篇")

        # 6. 生成 HTML
        print("\n📄 生成 HTML...")
        html = build_html(editor, grouped)
        with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"    📧 邮件 HTML: {len(html.encode())//1024}KB -> {OUTPUT_HTML}")

        # 6b. 生成交互式阅读页面
        reader_html = build_reader_html(editor, grouped, img_cache)
        with open(OUTPUT_READER_HTML, "w", encoding="utf-8") as f:
            f.write(reader_html)
        print(f"    📖 阅读页面: {len(reader_html.encode())//1024}KB -> {OUTPUT_READER_HTML}")

        # 7. 发送
        total = 1 + sum(len(v) for v in grouped.values())
        if no_send:
            print("\n📧 已开启 --no-send，仅生成 HTML，不发送邮件")
        else:
            subject = f"设计资讯摘要 · {DATE_SHORT} · 精选 {total} 篇"
            print("\n📧 发送邮件...")
            try:
                send_email(html, editor, grouped, subject)
            except Exception as e:
                print(f"  ❌ 发送失败: {e}")
                print(f"  💡 HTML 已保存: {OUTPUT_HTML}")
                return 1

        print(f"\n✅ 完成! {total} 篇文章, {ok_img} 张图片")
        return 0
    finally:
        release_run_lock()


if __name__ == "__main__":
    sys.exit(main())
