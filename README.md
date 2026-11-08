# yuanshen_wiki · genshin_dsh_wiki

> 原神观测枢 Wiki 本地资料库：API 采集 → 全文检索 → 笔记式前端 → MCP 插件 → AI 梗概 / 探索式笔记

仓库：https://github.com/kenneykun/genshin_dsh_wiki

## 快速开始

```bash
git clone https://github.com/kenneykun/genshin_dsh_wiki.git
cd genshin_dsh_wiki
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

.venv\Scripts\python.exe -m src.crawl_api            # 采集全站（约 5 分钟，8 并发）
.venv\Scripts\python.exe -m web.build_db             # 建检索索引（约 12 秒）
.venv\Scripts\python.exe -m src.build_graph          # 建关联图谱（约 10 秒）
.venv\Scripts\python.exe -m web.server --open        # 打开网页（默认 8321 端口）
```

> **数据不入库**：`data/`（约 300 MB 的 JSON、正文、SQLite 索引）与 `.venv/` 已在 `.gitignore` 中排除，克隆后按上面四步自行采集即可。`web/static/covers/` 中的封面 PNG 随仓库提供，重跑 `src.make_covers` 可再生成。

可选的 AI 能力需要 DeepSeek 凭据（详见后文「资料库助手」）：

```bash
.venv\Scripts\python.exe -m src.gen_notes -l 100 --workers 10      # 批量梗概卡
.venv\Scripts\python.exe -m src.explore_notes -n 20 --agents 4     # 探索式笔记
.venv\Scripts\python.exe -m src.make_covers --explore -l 20        # 生成封面
```

## 项目定位

系统性地爬取米哈游「原神·观测枢 Wiki」(`baike.mihoyo.com`) 中的公开游戏文本（角色/武器/圣遗物/敌人/材料/书籍/任务等），以结构化 JSON + 纯文本形式存储在本地，方便检索、分析、考据。

## 数据源

| 来源 | URL | 说明 |
|------|-----|------|
| 列表 API | `https://api-static.mihoyo.com/common/blackboard/ys_obc/v1/home/content/list` | 频道条目列表 (JSON, 无 Cloudflare) |
| 内容详情 | `https://baike.mihoyo.com/ys/obc/content/{content_id}/detail` | 各条目详情页 (Nuxt SPA) |

## 目标数据范围（24 个分类, 2252 条索引）

角色 / 武器 / 圣遗物 / 角色逸闻 / 敌人 / 材料 / 食物 / 书籍 / 任务 / NPC / 动物 / 尘歌壶 / 秘境 / 成就 / 教程 / 名片 / 装扮 / 地图 / 文本 / 组织 / 头像 / 七圣召唤 / 深境螺旋

## 技术栈

| 库 | 用途 |
|----|------|
| `httpx` | API 端点请求（主链路，支持连接池并发） |
| `beautifulsoup4` + `lxml` | HTML 解析 |
| `loguru` | 结构化日志（文件轮转） |
| `tenacity` | 请求重试与指数退避 |
| `tqdm` | CLI 进度条 |
| `tkinter` | GUI 查看器 & 集成爬取面板 |
| `playwright` | 旧链路：渲染 Nuxt SPA（**可选**，主流程已改用 JSON API） |

## 架构

```
┌─────────────────────────────────────────────────┐
│                  入口层                           │
│  main.py (CLI)  /  viewer.py (GUI 3-Tab)        │
├─────────────────────────────────────────────────┤
│                  调度层 (Scheduler)               │
│  断点续爬 · 失败重试 · 增量 · 强制 · 回调通知        │
├─────────────────────────────────────────────────┤
│                  下载层 (Downloader)              │
│  httpx (API) + Playwright (SPA 渲染)              │
│  domcontentloaded → wait_for_selector → 超时降级  │
├─────────────────────────────────────────────────┤
│                  解析层 (Parser)                  │
│  ListParser · CharacterParser · WeaponParser     │
│  ArtifactParser · DetailParser · NoiseFilter     │
├─────────────────────────────────────────────────┤
│                  存储层 (Storage)                 │
│  分类目录 · JSON · 纯文本 · progress.json        │
├─────────────────────────────────────────────────┤
│                  日志层 (Logger)                  │
│  loguru → app.log / crawl.log / viewer.log       │
│  10MB 轮转 · 30天保留 · 异步写入                  │
└─────────────────────────────────────────────────┘
```

### 爬取流程

```
【主链路 · JSON API（推荐）】约 40 条/秒
1. 读取 index.json 全站索引，按分类过滤出未完成条目
2. 逐条请求官方 JSON 接口（旧/新两套模型自动切换）
3. 把 JSON 归一成 HTML 片段，复用既有解析器产出同一 schema
4. 分类目录存 JSON + text/ 存纯文本 + 进度批量落盘

【旧链路 · Playwright 渲染】约 16 秒/条，仅作兜底
1. API 获取分类条目索引 → 去重存入 index.json
2. 按分类 → 生成渲染队列 (已有文件跳过)
3. Playwright 渲染每个详情页 (~16秒/页)
4. 分类解析器提取结构化字段 + 章节文本
5. 噪声过滤 + 分类目录存储 + 纯文本导出
6. 进度写入 progress.json (断点续爬)
```

#### 双通道接口

| 通道 | 适用 | 接口 | 返回 |
|------|------|------|------|
| 旧模型 | `content_id` 较小（如 111 诺艾尔） | `api-static.mihoyo.com/common/blackboard/ys_obc/v1/content/info?content_id=X&app_sn=ys_obc` | `data.content.contents[].text`（站点 HTML） |
| 新模型 | `entry_page_id` 较大（5xxxxx 起） | `act-api-takumi-static.mihoyo.com/hoyowiki/genshin/wapi/entry_page?entry_page_id=X` | `data.page.modules[].components[].data`（JSON 串） |

两条通道数据同源，区别只是 wiki 迁移前后两代页面模型；`src/api_client.py` 会按 id 猜测先试一条，
返回 `-2010 数据不存在` 时自动切另一条，避免漏抓。

## 反爬策略

| 策略 | 实现 |
|------|------|
| User-Agent | 移动端 Chrome Mobile（触发 SSR） |
| 请求间隔 | 随机 1~3 秒 |
| 重试 | 指数退避 (2s→4s→8s...) |
| 单线程 | 全部顺序请求 |
| 占位识别 | 检测"数据加载中"，2秒快速跳过 |

## 项目结构

```
yuanshen_wiki/
├── README.md
├── requirements.txt
├── viewer.py                  # Tkinter GUI 查看器（旧版，可保留）
├── web/                       # 浏览器前端
│   ├── server.py              # 标准库 HTTP 服务（API + 静态资源）
│   ├── db.py                  # SQLite 索引：建库 / 检索 / 统计
│   ├── build_db.py            # 重建索引 CLI
│   └── static/                # index.html + app.js + styles.css（无构建步骤）
├── mcp_server/                # MCP 服务器（给其他 AI 项目）
│   ├── server.py              # 工具 / 资源 / 提示词定义
│   ├── smoke_test.py          # 用官方客户端跑通的冒烟测试
│   ├── README.md              # 客户端配置说明
│   └── config.example.json
├── plugins/dsh-wiki-mcp/      # DSH 插件包（把 MCP 注册进 Harness）
├── docs/screenshots/          # 界面截图
├── src/
│   ├── config.py              # 全局配置 (URL/参数/24分类/路径/请求参数)
│   ├── logger.py              # 统一日志模块 (app/crawl/viewer 三文件)
│   ├── api_client.py          # 双通道 JSON 客户端 + 并发进度落盘
│   ├── api_parser.py          # JSON → HTML 归一 → 复用既有解析器
│   ├── crawl_api.py           # 主采集入口 (并发/断点/分类过滤/重试)
│   ├── normalize_dirs.py      # 输出目录归一（英文目录 → 中文分类）
│   ├── migrate_legacy.py      # 旧渲染条目升级为 API 解析（保留结构化字段）
│   ├── clean_sections.py      # 清理站点导航噪声 + category 对齐
│   ├── dedupe_sections.py     # 清除正文整段重复
│   ├── build_graph.py         # 关联图谱（AC 自动机匹配提及 → 推荐）
│   ├── archive.py             # 一键归档：快照到备份盘 + MANIFEST + zip
│   ├── verify_data.py         # 覆盖率与正文质量校验
│   ├── downloader.py          # 旧链路下载器 (Playwright + httpx)
│   ├── parser.py              # 多层解析器 + 噪声过滤
│   ├── scheduler.py           # 旧链路调度器 (回调+断点续爬+线程安全)
│   ├── storage.py             # 存储层 (分类目录/JSON/纯文本/进度)
│   └── main.py                # 旧链路 CLI 入口
├── data/
│   ├── raw/                   # 原始 API 响应
│   ├── _legacy/               # 目录归一时归档的重复旧文件
│   └── output/
│       ├── index.json         #   全站索引（13,941 条）
│       ├── progress.json      #   爬取进度（已完成/失败）
│       ├── wiki.db            #   Web 前端检索索引（SQLite FTS5）
│       ├── 角色/ 武器/ 圣遗物/ …  # 按分类存放的条目 JSON（共 13,936 个）
│       └── text/              #   纯文本副本
└── logs/
    ├── app.log                # 全量日志
    ├── crawl.log              # 旧链路爬取日志
    └── api_crawl.log          # 主链路（JSON API）爬取日志
```

## 使用方式

### MCP 服务器（给其他 AI 项目用）

把资料库接入任何支持 MCP 的 AI 客户端（Claude Desktop / Cursor / DSH / 自研 agent）。

```powershell
# 先确保索引存在（数据更新后重建，约 11 秒）
.venv\Scripts\python.exe -m web.build_db

# 自检
.venv\Scripts\python.exe -m mcp_server.smoke_test
```

客户端配置（Claude Desktop 的 `claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "genshin-wiki": {
      "command": "E:\\aki\\yuanshen_wiki\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server.server"],
      "cwd": "E:\\aki\\yuanshen_wiki"
    }
  }
}
```

提供 9 个工具：`wiki_search`、`wiki_get_entry`、`wiki_get_text`、`wiki_get_section`、
`wiki_list_categories`、`wiki_browse`、`wiki_random`、`wiki_dataset_info`、`wiki_rebuild_index`；
资源 `wiki://index`、`wiki://entry/{content_id}`；提示词 `analyze_entry`。
详见 [mcp_server/README.md](mcp_server/README.md)。

#### 作为 DSH 插件常驻（已安装）

`plugins/dsh-wiki-mcp/` 是一个纯配置 bundle，把上面的 MCP 服务器注册进 Harness。
安装后本机每个会话都直接拥有 `mcp__genshin-wiki__*` 工具，无需再配客户端：

```
plugins/dsh-wiki-mcp/
├── package.json        # dsh.bundle.patch
├── cordis.patch.yml    # 插入 @deepseek-ai/dsh-mcp-client（stdio）
├── locale/{en,zh}.json # 插件管理器里的标题与说明
└── icon.svg
```

改了配置后用 Plugin Manager 重新安装该目录；卸载在插件管理里移除 `@local/genshin-wiki-mcp`。

### Web 前端（推荐）

浏览器界面，不依赖任何前端构建工具，服务端只用 Python 标准库。

```bash
# 首次使用，或数据有更新后重建索引（约 13 秒）
.venv\Scripts\python.exe -m web.build_db

# 启动服务并打开浏览器
.venv\Scripts\python.exe -m web.server --open
# → http://127.0.0.1:8321
```

| 功能 | 说明 |
|------|------|
| 全文检索 | SQLite FTS5 按字索引，任意长度关键词（含 2 字词）都能命中，毫秒级 |
| 快速搜索 | 顶栏搜索框（`Ctrl/Cmd + K`）实时下拉，回车进入完整结果页 |
| 分类浏览 | 左侧按「角色与装备 / 故事与文本 / 收集与家园 / 玩法与挑战」四组收纳 30 个分类，可折叠；顶部筛选框按名字即时过滤 |
| 快捷入口 | 侧栏顶部「助手」「数据」一键进入；分类可点星标置顶（默认置顶：角色 / 任务 / 观测考据），置顶项不进分组，避免重复 |
| 最近阅读 | 侧栏展示最近 4 条浏览记录，点一下直接回到该词条 |
| 阅读视图 | 默认「卡片」模式：一句话速览 + 关键数据卡 + 分节卡片（带 emoji）、台词引用块、材料 ×N 行、标签；长内容折叠，一键切「原文」看全量章节 |
| 生成笔记 | 卡片模式右上「生成笔记」，把当前词条交给资料库助手改写成小红书风格文案（标题 + 3 个看点 + 出处 id） |
| 内容关联 | 正文里的词条名自动变成链接；条目页给出「本文提及 / 被提及 / 相关阅读」；首页「推荐阅读」按引用热度与篇幅混合推荐 |
| 阅读设置 | 深浅色切换、正文字号调节，均本地记忆；支持 `?theme=dark` 深链接 |
| 快捷键 | `Ctrl+K`/`/` 搜索、`j`/`k` 上下篇、`Esc` 收起 |
| 离线可用 | 除图片走米哈游 CDN 外，其余全部本地；窄屏自适应移动端 |

界面截图见 `docs/screenshots/`。

### 发现（瀑布流信息流）

左侧「发现」，或首页「刷一刷」。参考小红书的信息流：瀑布流卡片墙 + 顶部频道。

- **频道 tab**：推荐 / 角色 / 武器 / 圣遗物 / 敌人 / 任务 / 书籍 / 观测考据 / NPC与商店
- **卡片**：封面图 + 两行标题 + 摘要 + 分类标签 + 🔥 热度，点开进词条
- **封面策略**：优先用户上传立绘（`wiki-user-upload`）→ 词条图标 → 道具类用物品图标 → 都没有就用带分类 emoji 的彩色文字卡，不会出现破图
- **排序**：「推荐」从高热度池（引用数 + 篇幅）里随机抽 24 条，点「换一批」刷新；频道内按热度降序
- **热度** = 关联图谱里的被引用数 + 字数/400，和条目页的「相关阅读」同源

### AI 梗概卡（DeepSeek 批量提炼）

把词条压成一张结构化卡片：**抓人标题 / 一句话亮点 / 3 个看点 / 5 个标签**，存到 `wiki.db` 的 `notes` 表。

```bash
python -m src.gen_notes -l 100 --workers 10        # 一批 100 条，10 并发
python -m src.gen_notes --category 角色 -l 20
python -m src.gen_notes --dry-run -l 1             # 只看提示词
```

网页端：数据管理 →「生成梗概（100 条）」，进度条与成功/失败计数实时可见。

- 生成过的词条**自动跳过**，可以反复点，直到全部覆盖
- 实测：100 条 55 秒、0 失败；并发 10 时约 2 条/秒
- 提示词要求「只用原文信息、不编造」，模型输出被截断时有 JSON 修复兜底
- 消费端：信息流卡片显示梗概标题 + 一句话（角标 `梗概`，下面小字保留原名）+ 条目页顶部显示「AI 梗概」卡

### 探索式笔记（模型自主取材）

和「按热度压缩」的梗概不同，这里是**模型自己逛资料库**：

```
搜关键词 → 读词条 / 查章节 / 顺关联 → 判断哪条最值得写 → 写成一条笔记
```

```bash
python -m src.explore_notes -n 20 --agents 4      # 4 个探索者并发，凑够 20 条
python -m src.explore_notes -n 3 --agents 1 --verbose   # 看完整取材过程
```

- 每个探索者带一个「兴趣种子」（冷门地名 / 语音反差 / 食谱来历 / 敌人图鉴细节 / 圣遗物叙事…），互不重复
- 产出存 `explore_notes` 表，可跨多个词条取材（一条笔记常常引用 4–10 个词条）
- 实测：20 条 / 4 并发 ≈ 100 秒，全部成功
- 入口：发现页「🔎 探索」标签页

### 封面生成（用现有素材改造）

```bash
python -m src.make_covers -l 20            # 给梗概卡做封面
python -m src.make_covers --explore -l 25  # 给探索笔记做封面
```

- 主体图沿用词条现有素材（用户上传立绘 / 物品图标 / 词条图标），不凭空生成
- 封面 = 分类色渐变底 + 柔光 + 主体图（透明图标加托片，实底图标切圆角，大图底部渐隐）+ 标题 + 一句话 + 标签
- 输出 `web/static/covers/{id}.png` 与 `explore-{id}.png`，尺寸 720×960
- 素材会缓存到 `data/output/_assets/`，重跑不再下载

### 数据管理（网页内更新）

左侧「数据管理」页签；条目页右上也有「更新此条」。不用开终端就能更新内容。

| 操作 | 说明 |
|---|---|
| 更新全部缺口 | 按 `index.json` 补齐尚未采集的条目 |
| 重试失败 | 只跑 `progress.json.failed` 里的条目 |
| 按分类更新 | 下拉选分类（如「角色」），整类刷新 |
| 按 id 更新 | 逗号分隔多个 `content_id`，适合修单条 |
| 重建检索索引 | 重建 `wiki.db`（约 10–13 秒，带进度日志） |
| 重建关联图谱 | 重算提及边与推荐边（约 10 秒） |

采集类任务完成后会**自动重建索引**，页面随即能看到新内容；进度条、成功/失败计数、实时日志都在任务面板里，同一时刻只允许一个任务。

实现要点：

- 任务跑在后台线程，前端每 800ms 轮询 `/api/manage/status`；
- 重建索引改为**原地重建表**（不再删库重建），否则 Windows 下其他进程持有 `wiki.db` 句柄会报 `WinError 32`；
- 建库进度按每 2000 条回调一次，前端能看到「读取词条 8000/14301」。

### 资料库助手（DeepSeek 对话）

左侧「资料库助手」页签，或条目页右上「问 AI」。模型先查本地资料库再作答，引用会带条目 id。

**默认零配置**：自动复用本机 DSH 的 DeepSeek 凭据，直连 `https://api.deepseek.com/anthropic`，模型 `deepseek-flash`。

```powershell
# 优先级：环境变量 > 页面「设置」> 本机 DSH 凭据（~/.dsh/.credentials.yaml）
$env:DEEPSEEK_API_KEY = "sk-..."     # 可选：覆盖
```

| 项 | 说明 |
|---|---|
| 凭据 | 页面「设置」写入 `data/llm_config.json`（不回传、GET 只返回掩码）；未填则用 DSH 凭据库里的 `refs.DEEPSEEK_API_KEY` |
| 协议 | base_url 含 `/anthropic` → Messages 协议（`x-api-key` + `/v1/messages`，模型 `deepseek-flash` / `deepseek-v4-pro`）；否则走 OpenAI 兼容 `/chat/completions` |
| 工具 | `wiki_search` / `wiki_get_entry` / `wiki_get_text` / `wiki_get_section` / `wiki_related` / `wiki_list_categories`，与 MCP 服务器同一套语义，进程内直接查 SQLite |
| 流式 | SSE 推送 `tool` / `delta` / `done` 事件，前端边收边渲染，工具调用以 chip 显示 |
| 总结对话 | 一键把当前对话压缩成「主题 / 结论 / 涉及词条 / 待查点」，可复制 |
| 成本控制 | 最多 4 轮工具调用，单条工具结果截断到 6000 字，`max_tokens` 4096；对话存 localStorage，只留最近 60 条 |

不想花额度时可以用本地假服务联调：

```powershell
.venv\Scripts\python.exe -m web.mock_llm      # 127.0.0.1:8399
# 然后在「设置」里把接口地址改成 http://127.0.0.1:8399
```

### GUI 查看器（Tkinter，旧版）

```bash
python viewer.py
```

三 Tab 界面：

| Tab | 功能 |
|-----|------|
| 📚 浏览 | 分类筛选 + 关键词搜索 + 爬取状态标记 + 详情树形导航 |
| 🕷 爬取 | 分类复选框 + 进度条 + 实时日志 + ▶开始/▣停止 |
| 📋 日志 | 爬取/浏览/全量日志切换 + 刷新 + 行数控制 + 打开日志目录 |

浏览 Tab 点击条目即可查看已爬取的完整数据（基础信息/天赋数值表/命之座/圣遗物部件/章节文本）。
爬取 Tab 选定分类后点击「开始」，支持断点续爬和随时停止。

### CLI

```bash
# 1) 建虚拟环境（不要装到全局 Python）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2) 主链路：JSON API 采集（推荐，约 40 条/秒）
.venv\Scripts\python.exe -m src.crawl_api                    # 续爬全部缺口
.venv\Scripts\python.exe -m src.crawl_api -c 敌人 教程        # 只爬指定分类
.venv\Scripts\python.exe -m src.crawl_api -l 50              # 抽样 50 条
.venv\Scripts\python.exe -m src.crawl_api --retry-failed     # 只重试失败条目
.venv\Scripts\python.exe -m src.crawl_api --force -c 角色     # 强制重爬

# 3) 数据维护
.venv\Scripts\python.exe -m src.normalize_dirs --dry-run     # 检查目录是否需要归一
.venv\Scripts\python.exe -m src.normalize_dirs               # 英文目录并入中文分类
.venv\Scripts\python.exe -m src.clean_sections               # 清理站点导航噪声、对齐分类字段
.venv\Scripts\python.exe -m src.dedupe_sections              # 清除正文整段重复
.venv\Scripts\python.exe -m src.build_graph                  # 重建关联图谱（提及/推荐，约 10 秒）
.venv\Scripts\python.exe -m src.verify_data                  # 覆盖率 + 各分类字数
.venv\Scripts\python.exe -m src.verify_data --thin 80        # 列出正文过短的条目

# 4) 归档备份
.venv\Scripts\python.exe -m src.archive                      # 快照到 F:\backup\yuanshen-wiki-<时间戳> 并打 zip
.venv\Scripts\python.exe -m src.archive --no-zip             # 只快照不压缩
.venv\Scripts\python.exe -m src.archive --target D:\bak      # 换备份盘
.venv\Scripts\python.exe -m src.archive --zip-only <目录>    # 只把已有归档打包

# 3) 旧链路（Playwright 渲染，需要额外 pip install playwright）
python -m src.main --stats
python -m src.main -c characters
python -m src.main -c all
python -m src.main -c characters -l 5 --force
python -m src.main -c characters --retry-failed
```

> 采集进度以 `data/output/progress.json` 为准，两条链路的已完成记录互相兼容；
> 失败条目会写进 `progress.json.failed`，用 `--retry-failed` 重试。

## 解析器输出

### 角色 (CharacterDetail)

```json
{
  "content_id": 111, "title": "诺艾尔",
  "rarity": 4, "element": "岩", "weapon_type": "双手剑",
  "constellation": "心护座", "birthday": "3月21日",
  "affiliation": "西风骑士团", "role": "岩元素护盾兼输出",
  "title_alias": "未授勋之花",
  "talents": [[...天赋数值表...]],
  "constellations": [{"name": "大扫除", "description": "..."}],
  "ascension_materials": [[...突破材料表...]],
  "cv_voice": {"初次见面": "...", "闲聊·其一": "..."},
  "sections": {...}, "raw_text": "..."
}
```

### 武器 (WeaponDetail)

```json
{
  "content_id": 507623, "title": "帷间夜曲",
  "rarity": 5, "weapon_type": "法器",
  "base_atk": 44, "sub_stat": "暴击伤害", "sub_stat_value": "19.2%",
  "effect_name": "十字路的旅歌",
  "effect_desc": "生命值上限提高(10%/12%...)。装备者触发月曜反应时...",
  "sections": {...}
}
```

### 圣遗物 (ArtifactDetail)

```json
{
  "content_id": 506178, "title": "穹境示现之夜",
  "rarity": 5,
  "set_2pc": "元素精通提高80点。",
  "set_4pc": "队伍中附近的角色触发月曜反应时...",
  "pieces": [
    {"name": "生之花", "description": "雕刻成花型的圣物..."},
    {"name": "死之羽", "description": "毡帽上佩戴的深邃羽饰..."},
    ...
  ],
  "sections": {...}
}
```

## 后续规划

### P2 - 搜索与浏览

- 查看器增强：深色模式、字体缩放、表格格式化
- 全文检索：SQLite FTS5 跨条目搜索
- 关系图谱：角色→武器→圣遗物→材料 关联可视化

### P2 - 数据质量

- 图片标注：提取 alt 融入正文
- 语音台词独立结构化

### P3 - 维护监控

- Wiki 变更检测（API 对比）
- Markdown 爬取报告
- 月度定时增量更新

## License

本项目仅用于个人学习与二创考据。数据版权归米哈游所有，请勿用于商业用途。
