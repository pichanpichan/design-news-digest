---
name: design-news-digest
description: 每3天从设计网站自动收集最新资讯，DeepSeek翻译成中文摘要，通过GitHub Actions生成并发送HTML邮件
---

# 设计资讯摘要推送任务

## 架构

```
GitHub Actions (每3天 9:00 CST)
  └─ discover_and_translate.py
       ├─ RSS 抓取：Hypebeast / Creative Boom / Dribbble
       ├─ HTML 抓取：It's Nice That
       ├─ DeepSeek 翻译（逐篇API）：中文标题 + 摘要 + 摘录
       └─ digest_pipeline.py --no-send → 生成 HTML + images/
  └─ git push images/ → GitHub (公开仓库 raw URL)
  └─ digest_pipeline.py --send-only → Gmail SMTP → 收件人
```

**仓库**: https://github.com/pichanpichan/design-news-digest（公开）
**发信方式**: Gmail SMTP (587 STARTTLS)，图片用 `raw.githubusercontent.com` 公开URL
**收件人**: `isawuonce@qq.com`, `isawuonce@gmail.com`

---

## 文件结构

| 文件 | 作用 |
|---|---|
| `discover_and_translate.py` | 入口脚本：RSS发现 → DeepSeek翻译 → 保存articles.json → 调用pipeline(--no-send) |
| `digest_pipeline.py` | 核心管道：图片下载→裁切→保存文件+base64缓存→生成HTML→SMTP发送 |
| `.github/workflows/digest.yml` | GitHub Actions 工作流定义（三步：生成→推送→发送） |
| `articles.json` | 精选结果（临时文件，生成后提交给pipeline） |
| `img_cache.json` | 图片 base64 缓存（持久化，避免重复下载） |
| `img_failures.json` | 失败图片记录（冷却机制防重复请求） |
| `images/` | 裁切后的 JPEG 文件（git 管理，通过 raw URL 公开访问） |
| `SKILL.md` | 本文件（任务说明文档） |
| `design_digest_latest.html` | 本次邮件HTML（每次覆盖） |
| `design_digest_reader.html` | 交互式阅读页面（每次覆盖，含内嵌base64图片） |

---

## GitHub Actions 工作流

`.github/workflows/digest.yml` 分三步执行：

### ① 发现 + 翻译 + 生成（不发送）
- `discover_and_translate.py` 运行：
  - RSS 抓取 3 个站点（Hypebeast Design, Creative Boom, Dribbble Stories）
  - HTML 抓取 It's Nice That 文章列表页
  - 过滤：去重、去低质图片、限制每站最多 6 篇
  - DeepSeek API 逐篇翻译（每篇单独调 API，避免 JSON 截断）
    - 翻译标题为中文
    - 写 35-70 字摘要
    - 写 200-500 字摘录
  - 分类、选题去重、精选 8-12 篇
  - 调用 `digest_pipeline.py articles.json --no-send`
    - 下载未缓存图片 → 居中裁切 600×400 → JPEG q75
    - 保存 `images/{article_id}.jpg` 文件
    - 更新 `img_cache.json` / `img_failures.json`
    - 生成 `design_digest_latest.html`（邮件HTML，图片引用 `raw.githubusercontent.com` URL）
    - 生成 `design_digest_reader.html`（交互阅读页，内嵌 base64 图片）
  - 不发送邮件（`--no-send`）

### ② 推送图片到公开仓库
- `git add images/ img_cache.json img_failures.json && git push`
- 确保新图片通过 `raw.githubusercontent.com` 可用

### ③ 发送邮件
- `digest_pipeline.py --send-only`：
  - 读取刚刚生成的 `design_digest_latest.html`
  - 通过 Gmail SMTP (587 STARTTLS) 发送
  - 图片已在上一步推送到 GitHub，可在线加载

---

## 运行方式

### 自动触发
每 3 天 9:00 CST（cron: `0 1 */3 * *` UTC）

### 手动触发
去 https://github.com/pichanpichan/design-news-digest/actions → 点「Run workflow」

### 本地测试（需要 API Key）
```bash
DEEPSEEK_API_KEY="sk-xxx" \
DESIGN_DIGEST_TO_ADDRS="isawuonce@qq.com" \
DESIGN_DIGEST_SMTP_PASS="xxx" \
DESIGN_DIGEST_SMTP_HOST="smtp.gmail.com" \
DESIGN_DIGEST_SMTP_PORT=587 \
DESIGN_DIGEST_SMTP_USER="isawuonce@gmail.com" \
python3 discover_and_translate.py
```

---

## 发现阶段细节

### RSS 源
| 网站 | Feed URL |
|---|---|
| Hypebeast Design | `https://hypebeast.com/design/feed` |
| Creative Boom | `https://www.creativeboom.com/feed/` |
| Dribbble Stories | `https://dribbble.com/stories.rss` |

It's Nice That 无 RSS，通过抓取 `/articles` 列表页提取链接。

### 详情页元数据提取
- 优先 `og:title`、`og:description`、`og:image`
- 必须去除 tracking 参数（`&cbr=1` 等），否则 Hypebeast 图片返回 404
- 排除默认图（`social.jpg`、placeholder 等）

---

## DeepSeek 翻译

- **模型**: `deepseek-chat`
- **模式**: 逐篇调用（每篇文章单独调一次 API）
- **温度**: 0.3
- **输出**: 中文标题 + 35-70字摘要 + 200-500字摘录
- **质量要求**：
  - 保留关键品牌/人名/项目名
  - 摘录要有观点和判断，展开介绍核心观点和设计亮点
  - 不要以「值得一看」「不容错过」等套路句式结尾
  - 翻译失败时自动降级为英文原题，不影响其他文章
- **费用**: ≈ ¥0.02/月

---

## 精选规则

1. 最终保留 8-12 篇
2. 每个来源最多 5 篇
3. 同一分类中，同一细分领域最多 2 篇
4. **选题去重**：同一期内核心选题明显的文章只保留一篇
5. 至少 3 个不同来源
6. 选择 1 篇 Editor's Pick：图片强、设计判断价值高

### 分类

| 分类 | 邮件胶囊色 |
|---|---|
| 设计理论 | `#4f6fd9` 蓝色 |
| 视觉参考 | `#1fa6a0` 青绿 |
| AIGC | `#7b61ff` 紫色 |
| 创意文化 | `#f08a24` 橙色 |
| 商业与品牌 | `#e8507f` 粉色 |

---

## 图片处理

- 统一居中裁切为 600×400（3:2 比例）
- JPEG quality 75, optimize=True
- 同时保存两份：
  - `img_cache.json`：base64 字符串（用于 reader 页面和 CID 回退）
  - `images/{article_id}.jpg`：文件（通过 git push 到 GitHub，供邮件使用）
- 图片通过 `raw.githubusercontent.com` 公开 URL 引用，无需 CID 附件

---

## 邮件 UI 参考
- 参考 `QQ邮箱.pdf` 风格
- 浅灰背景、居中、宽度 720px
- 单栏白底卡片：大图 + 分类胶囊 + 黑色标题 + 灰色摘要 + 摘录 + 来源
- 移动端保持单栏，内边距 14-16px
- 无图文章不展示

---

## 故障排查

### GitHub Actions 失败
1. 去 https://github.com/pichanpichan/design-news-digest/actions 查看最新运行
2. 常见失败原因：
   - **图片 404**：检查 `images/` 目录是否有对应文件
   - **翻译失败**：检查 DeepSeek API 余额
   - **SMTP 失败**：检查 Gmail App Password 是否过期
3. 点击「Re-run jobs」即可重试

### 本地调试
```bash
# 只生成 HTML，不发送
python3 digest_pipeline.py articles.json --no-send

# 只发送已有 HTML
python3 digest_pipeline.py --send-only

# 指定日期（覆盖环境内系统时间）
DESIGN_DIGEST_NOW=2026-05-24 python3 digest_pipeline.py articles.json
```

---

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | DeepSeek API Key（必填） |
| `DESIGN_DIGEST_SMTP_HOST` | `smtp.qq.com` | SMTP 服务器 |
| `DESIGN_DIGEST_SMTP_PORT` | `465` | SMTP 端口 |
| `DESIGN_DIGEST_SMTP_USER` | `isawuonce@qq.com` | SMTP 用户名 |
| `DESIGN_DIGEST_SMTP_PASS` | — | SMTP 密码/授权码 |
| `DESIGN_DIGEST_TO_ADDRS` | SMTP_USER | 收件人（逗号分隔） |
| `DESIGN_DIGEST_IMAGE_BASE_URL` | `""` | 图片公开 URL 前缀（GHA 中设为 raw.githubusercontent.com） |
| `DESIGN_DIGEST_NOW` | 系统日期 | 覆盖日期 |

---

## GitHub Secrets

仓库中加密存储的凭证（不会出现在任何日志或代码中）：
- `DEEPSEEK_API_KEY` — DeepSeek API Key
- `SMTP_PASS` — Gmail App Password
- `DESIGN_DIGEST_TO_ADDRS` — 收件人列表

---

## 注意事项
- 仓库是**公开**的，raw.githubusercontent.com URL 才能被邮件客户端加载
- Gmail SMTP 需要 App Password（不是登录密码）
- 图片文件太大时（单文件 >50MB）GitHub 会拒绝 push，当前每张约 20-40KB
- 每 3 天跑一次，每次约 1-2 分钟
- 定时任务错过不补跑，但可手动触发 workflow_dispatch
