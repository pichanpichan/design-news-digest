---
name: design-news-digest
description: 每3天从设计网站收集最新资讯，筛选成中文设计摘要，并生成参考 QQ 邮箱视觉的 HTML 邮件
---

# 设计资讯摘要推送任务

## 目标
每 3 天自动从指定设计网站收集最新文章，筛选 8-12 篇真正值得看的内容，提取真实封面图，写成中文摘要，并用 `digest_pipeline.py` 生成一封参考「QQ邮箱.pdf」样式的 HTML 邮件，发送到 `isawuonce@qq.com`。

核心结果不是”抓得多”，而是：主题新、图片真实、摘要能帮我快速判断是否要点开原文。

---

## 日志

日志文件路径：
```text
/Users/pichan/Documents/Claude/Scheduled/design-news-digest/discovery.log
```

每条日志的命令格式：
```bash
echo “[$(date '+%Y-%m-%d %H:%M:%S')] 消息内容” >> /Users/pichan/Documents/Claude/Scheduled/design-news-digest/discovery.log
```

本轮开始前先初始化日志：
```bash
echo “[$(date '+%Y-%m-%d %H:%M:%S')] === 本轮开始 ===” > /Users/pichan/Documents/Claude/Scheduled/design-news-digest/discovery.log
```

---

## 0. 先判断是否已有候选文章

检查：
```bash
test -f /Users/pichan/Documents/Claude/Scheduled/design-news-digest/articles.json && echo exists || echo missing
```

- 如果输出 `exists`：用 Python 检查文件是否是当天生成的，排除旧批次残留：
  ```python
  import os, time
  mtime = os.path.getmtime(“/Users/pichan/Documents/Claude/Scheduled/design-news-digest/articles.json”)
  age_hours = (time.time() - mtime) / 3600
  print(“fresh” if age_hours < 4 else “stale”)
  ```
  - 如果输出 `fresh`：说明发现阶段刚完成，直接从第 4 步运行 pipeline。发送成功后删除 `articles.json`。
  - 如果输出 `stale`：说明是旧批次残留，**先删除旧文件**，再进入第 1 步重新收集。

- 如果输出 `missing`：从第 1 步开始收集。

---

## 第1步：扫描网站发现文章

log "第1步开始：扫描网站"

### 目标网站

| 网站 | 抓取 URL | 文章链接特征 | 备注 |
|---|---|---|---|
| Hypebeast Design | `https://hypebeast.com/design` | `/{yyyy}/{mm}/...` | 优先设计、空间、品牌、产品、文化内容 |
| It’s Nice That | `https://www.itsnicethat.com/` | `/articles/...` | 优先视觉、摄影、插画、平面与创意项目 |
| Creative Boom | `https://www.creativeboom.com/` | `/inspiration/`、`/insight/`、`/news/` | 优先设计趋势、案例、行业观察 |
| Dribbble Stories | `https://dribbble.com/stories` | `/stories/...` 或资源文章 | 优先设计系统、品牌、UI/UX、视觉趋势 |
| Behance | WebSearch：`Behance featured projects {current_year}` | `behance.net/gallery/...` | 只取精选项目或趋势项目 |

### 抓取规则

1. 每个站点只做一次首页/列表页抓取，提取 3-5 个候选链接，总候选不超过 20 篇。
2. 详情页只抓必要元数据：标题、摘要、封面图、发布时间、来源。
3. 优先使用 `og:title`、`og:description`、`og:image`；如果 Hypebeast 图链含 HTML 实体，必须 `html.unescape()`。
4. 如果没有真实图片、图片是站点默认 `social.jpg`、图片 404、或文章内容明显和设计无关，直接排除。
5. URL 去重；同一选题重复时保留来源更权威、图片更好、摘要信息更完整的一篇。

抓取完成后立即记录结果：
```bash
log "第1步完成：抓取了 X 个网站，共 Y 篇候选"
```
（把 X 和 Y 替换为实际数值。）

---

## 第2步：翻译、分类、精选

log "第2步开始：翻译与精选"

### 分类

- **设计理论**：趋势、方法论、排版、UX/UI、设计系统、设计行业观察
- **视觉参考**：插画、摄影、3D、动画、平面设计、空间/产品视觉
- **AIGC**：AI 设计工具、生成式视觉、创作流程变化
- **创意文化**：艺术家、展览、设计节、文化评论、创意社群
- **商业与品牌**：品牌设计、营销创意、包装、联名、零售体验

### 精选规则

1. 最终保留 8-12 篇。
2. 每个来源最多 5 篇。
3. 同一分类中，同一细分领域最多 2 篇。
4. **选题去重**：同一期里，如果两篇文章的核心选题明显相同（如同为"2026 趋势"、"年度报告"、"春节营销"等），只保留更优的那一篇，不要并排放同一选题的两篇。
5. 至少保留 3 个不同来源；如果候选不足，优先降低单站数量而不是硬凑低质文章。
5. 每篇摘要写 1 句中文，控制在 35-70 个汉字，说明”这篇为什么值得看”，不要写成泛泛的新闻翻译。
6. 每篇还要写一段较长的摘录 excerpt_cn，200-500 字，展开介绍这篇文章讲了什么、有什么设计判断价值，不要直接翻译原文，而是用你自己的话讲清楚”核心观点+为什么值得了解”。这段摘录会在二级页面完整展示。
7. 标题可以意译，但必须保留关键品牌、人名、项目名或展览名。
8. 选择 1 篇 Editor’s Pick：优先图片强、设计判断价值高、适合作为邮件第一张大图的文章。

精选完成后记录结果：
```bash
log "第2步完成：精选了 N 篇，Editor’s Pick: 文章ID"
```

---

## 第3步：保存 articles.json

log "第3步：保存 articles.json"

写入覆盖：
```text
/Users/pichan/Documents/Claude/Scheduled/design-news-digest/articles.json
```

JSON 格式：
```json
[
  {
    "id": "source_slug_unique",
    "title_cn": "中文标题",
    "summary_cn": "中文摘要（35-70字，邮件卡片用）",
    "excerpt_cn": "中文摘录（200-500字，二级详情页用）",
    "url": "原文链接",
    "source": "来源名称",
    "category": "设计理论",
    "og_image": "真实封面图片URL"
  }
]
```

字段要求：
- `id` 只用小写英文、数字、下划线，且全局唯一；它会被用作 CID 图片 ID。
- `og_image` 必须尽量写入；pipeline 有后备抓取，但自动任务里不要依赖后备。
- 不要把无图文章写入 JSON。
- 不要写占位图、站点 logo、追踪参数图或明显错误的图片。

---

## 第4步：运行 pipeline

log "第4步开始：运行 pipeline ($(wc -c < /Users/pichan/Documents/Claude/Scheduled/design-news-digest/articles.json) bytes)"

只使用现有脚本生成 HTML 和发送邮件，不要临时手写 HTML 或另写发送逻辑。

先确认 Pillow 可用：
```bash
python3 -c "import PIL"
```

如果缺少 Pillow，再安装：
```bash
python3 -m pip install Pillow --break-system-packages
```

运行（注意设置 TZ=Asia/Shanghai 使日期正确）：
```bash
TZ=Asia/Shanghai DESIGN_DIGEST_TO_ADDRS="isawuonce@qq.com,isawuonce@gmail.com" python3 /Users/pichan/Documents/Claude/Scheduled/design-news-digest/digest_pipeline.py /Users/pichan/Documents/Claude/Scheduled/design-news-digest/articles.json
```

如果脚本成功（exit code 0）：
```bash
log "第4步完成：pipeline 成功，邮件已发送"
```

如果脚本因为网络错误失败：最多手动重跑 1 次。第二次仍失败就停止，记录：
```bash
log "第4步失败：pipeline 重跑后仍失败"
```
不要继续循环执行，也不要重新生成同一批候选文章。脚本会用 `img_cache.json` 复用成功图片，并用 `img_failures.json` 记录失败图片，避免反复打同一个失败链接。

如只想检查 HTML，不发送邮件：
```bash
python3 /Users/pichan/Documents/Claude/Scheduled/design-news-digest/digest_pipeline.py /Users/pichan/Documents/Claude/Scheduled/design-news-digest/articles.json --no-send
```

如只想生成临时预览、不覆盖 `design_digest_latest.html`：
```bash
DESIGN_DIGEST_OUTPUT_HTML=/private/tmp/design_digest_preview.html python3 /Users/pichan/Documents/Claude/Scheduled/design-news-digest/digest_pipeline.py /Users/pichan/Documents/Claude/Scheduled/design-news-digest/articles.json --no-send
```

发送成功后清理：
```bash
rm -f /Users/pichan/Documents/Claude/Scheduled/design-news-digest/articles.json
log "本轮完成：articles.json 已清理"
```

---

## UI 设计参考

邮件 HTML 参考桌面文件：
```text
/Users/pichan/Desktop/QQ邮箱.pdf
```

视觉方向：
- QQ 邮箱内阅读效果优先：浅灰背景、内容居中、主内容宽度约 720px。
- 顶部是居中标题：`设计资讯摘要`，下一行显示日期与精选篇数，再下一行显示“每 3 天 · 从 5 个设计网站为你筛选”。
- 主推荐和普通文章都用单栏白底卡片：顶部大图，下方是分类胶囊、黑色标题、灰色摘要、浅灰来源。
- 分类胶囊使用固定栏目色，不要全部用粉色：设计理论蓝色、视觉参考青绿色、AIGC 紫色、创意文化橙色、商业与品牌粉色；未知分类用中性灰。
- 不再使用旧版 OOO 风格的细字母 header、双栏网格和黑框按钮。
- 移动端保持单栏，卡片贴近屏幕但保留 14-16px 内边距。
- 无图文章不展示；不要出现空白图块或占位块。

---

## 质量检查

发送前确认：
1. `design_digest_latest.html`（邮件 HTML）已生成。
2. `design_digest_reader.html`（交互式阅读页面）已生成。
3. 页面里没有 `social.jpg`、`placeholder`、空 `cid:` 或空标题。
4. 文章数在 8-12 篇；如果不足，邮件仍可发送，但不要为了凑数加入低质内容。
5. 至少 3 个来源；如果不足，在摘要中不要伪装来源覆盖面。
6. 生成的邮件主题为 `设计资讯摘要 · MM/DD · 精选 N 篇`。
7. 每篇文章都应有 `excerpt_cn` 字段（200-500 字），用于详情页展示。
8. SMTP 授权码不要写入新笔记、报告或日志；脚本内部处理发送。
9. 如果日志显示已有运行锁、失败冷却、文章数低于下限或 SMTP 网络失败，停止本轮任务并报告原因，不要在同一次自动化里反复运行。

---

## 技术说明

- HTML 图片用 `<img src="cid:article_id">`，图片作为 MIME inline 附件。
- 图片统一居中裁切为 600×400，保证邮件卡片稳定。
- `img_cache.json` 是持久缓存，避免每次重复下载图片。
- `img_failures.json` 会记录失败图片，失败达到上限后进入冷却期，防止网络错误被放大成大量重复请求。
- `.digest_pipeline.lock` 防止上一轮还没结束时又启动下一轮。
- `design_digest_latest.html` 是本次邮件的可检查版本。
- `design_digest_reader.html` 是交互式阅读页面，每篇文章可点击查看完整摘录（excerpt_cn）并返回列表，也可在浏览器直接打开。
- SMTP 使用 QQ 邮箱 SSL 发送；如果后续迁移机器，优先用环境变量配置邮箱账号和授权码。
- `DESIGN_DIGEST_TO_ADDRS` 环境变量可设置多个收件人，用逗号分隔（默认发给 `isawuonce@qq.com`）。如需增加订阅邮箱，在定时任务配置中追加即可，例如 `DESIGN_DIGEST_TO_ADDRS=isawuonce@qq.com,newuser@qq.com`。