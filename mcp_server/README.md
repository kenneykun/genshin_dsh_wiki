# 原神观测枢资料库 · MCP 服务器

把本项目的 13,936 条词条（约 1,980 万字）暴露成 MCP 工具，任何支持 MCP 的 AI 客户端都能直接检索、阅读、引用。

## 快速开始

```powershell
# 1) 先确保检索索引存在（数据更新后也要重建，约 11 秒）
.venv\Scripts\python.exe -m web.build_db

# 2) 自己跑一下确认工具都正常
.venv\Scripts\python.exe -m mcp_server.smoke_test
```

服务端本身由客户端拉起，不需要常驻。

## 客户端配置

### DSH 插件（已装好，开箱即用）

本项目已附带一个 DSH 插件包 `plugins/dsh-wiki-mcp/`，安装后本机所有 Harness 会话都能直接调用：

```
mcp__genshin-wiki__wiki_search      mcp__genshin-wiki__wiki_get_entry
mcp__genshin-wiki__wiki_get_text    mcp__genshin-wiki__wiki_get_section
mcp__genshin-wiki__wiki_browse      mcp__genshin-wiki__wiki_list_categories
mcp__genshin-wiki__wiki_random      mcp__genshin-wiki__wiki_dataset_info
mcp__genshin-wiki__wiki_rebuild_index
```

- 插件包是「纯配置 bundle」：`cordis.patch.yml` 插入 `@deepseek-ai/dsh-mcp-client`，
  用 stdio 拉起 `.venv\Scripts\python.exe -m mcp_server.server`。
- 改了命令、路径或参数后，用 Plugin Manager 重新安装该目录即可（`install_bundle`，target = 该目录绝对路径）。
- 卸载：Plugin Manager 里移除 `@local/genshin-wiki-mcp`。

### Claude Desktop

编辑 `%APPDATA%\Claude\claude_desktop_config.json`（没有就新建）：

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

改完重启 Claude Desktop。Windows 上如果拉不起来，把 `command` 换成 `cmd`，`args` 改成
`["/c", "E:\\aki\\yuanshen_wiki\\.venv\\Scripts\\python.exe", "-m", "mcp_server.server"]`。

### Cursor

项目根目录建 `.cursor/mcp.json`，内容同上（键名 `mcpServers`）。

### DSH / 其他 Agent 框架

凡是支持「stdio MCP server」的，都是同样的三元组：可执行文件、参数、工作目录。

```json
{
  "name": "genshin-wiki",
  "transport": "stdio",
  "command": "E:\\aki\\yuanshen_wiki\\.venv\\Scripts\\python.exe",
  "args": ["-m", "mcp_server.server"],
  "cwd": "E:\\aki\\yuanshen_wiki"
}
```

### HTTP 模式（给不能拉子进程的客户端 / 局域网)

```powershell
.venv\Scripts\python.exe -m mcp_server.server --http --port 8322
# streamable HTTP 端点: http://127.0.0.1:8322/mcp
```

## 工具一览

| 工具 | 用途 | 主要参数 |
|------|------|----------|
| `wiki_search` | 全文检索（标题+正文，中文按字索引，2 字词可命中） | `query`、`category?`、`limit=10` |
| `wiki_get_entry` | 按 id 读词条（Markdown：章节/表格/图片/结构化字段） | `id`、`max_chars=12000` |
| `wiki_get_text` | 分段读纯文本（长任务/书籍/考据） | `id`、`offset=0`、`max_chars=8000` |
| `wiki_get_section` | 只取某一章节 | `id`、`heading`（模糊匹配） |
| `wiki_related` | 关联网络：本文提及 / 被引用 / 延伸阅读 | `id`、`limit=8` |
| `wiki_list_categories` | 30 个分类及条目数 | — |
| `wiki_browse` | 按分类浏览条目（可标题过滤、按字数排序） | `category?`、`query?`、`page`、`page_size`、`sort` |
| `wiki_random` | 随机词条（发散考据用） | `category?` |
| `wiki_dataset_info` | 数据规模、采集进度、索引新鲜度 | — |
| `wiki_rebuild_index` | 重建检索索引 | — |

资源：`wiki://index`（分类索引）、`wiki://entry/{content_id}`（词条正文）
提示词：`analyze_entry`（取词条 + 考据分析指令）

## 典型调用顺序

```
用户：帮我查蒙德城的建筑风格
 → wiki_search(query="蒙德 建筑")            # 找到 id=1047
 → wiki_get_entry(id=1047)                   # 读结构化正文（超长会截断）
 → wiki_get_text(id=1047, offset=12000)      # 还有后续就分段续读
```

## 说明

- 数据只读；`wiki_rebuild_index` 是唯一会写盘的操作（重建 `data/output/wiki.db`）。
- 输出为中文 Markdown，长条目自动截断并提示续读方式，避免一次塞爆上下文。
- 索引缺失时工具会返回明确提示，不会静默失败。
- 数据本体仍在 `data/output/<分类>/<id>.json`，MCP 只是检索与读取层。
