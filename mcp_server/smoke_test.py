"""MCP 服务器冒烟测试：用官方客户端把工具/资源/提示词各跑一遍

    .venv\\Scripts\\python.exe -m mcp_server.smoke_test
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)


def pick(obj, *names, default=None):
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


async def main() -> int:
    params = StdioServerParameters(
        command=str(PYTHON), args=["-m", "mcp_server.server"], cwd=str(PROJECT_ROOT)
    )
    failures = 0

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            server = pick(info, "server_info", "serverInfo")
            print(f"[OK] 已连接 {pick(server, 'name')} {pick(server, 'version')}")

            tools = [t.name for t in pick(await session.list_tools(), "tools")]
            print(f"[OK] 工具 {len(tools)} 个: {', '.join(tools)}")

            resources = [str(pick(r, "uri")) for r in pick(await session.list_resources(), "resources")]
            templates = [pick(t, "uriTemplate", "uri_template")
                         for t in pick(await session.list_resource_templates(), "resource_templates", "resourceTemplates")]
            prompts = [p.name for p in pick(await session.list_prompts(), "prompts")]
            print(f"[OK] 资源 {resources} / 模板 {templates} / 提示词 {prompts}")

            async def call(name: str, args: dict, expect: str = "") -> None:
                nonlocal failures
                result = await session.call_tool(name, args)
                content = pick(result, "content", default=[]) or []
                text = "".join(pick(c, "text", default="") for c in content)
                ok = bool(text) and (expect in text if expect else True)
                flag = "[OK]" if ok else "[FAIL]"
                if not ok:
                    failures += 1
                print(f"{flag} {name}({args}) → {len(text)} 字" + ("" if ok else f"  预览: {text[:120]!r}"))

            await call("wiki_dataset_info", {}, "条目")
            await call("wiki_search", {"query": "蒙德", "limit": 3}, "id=")
            await call("wiki_list_categories", {}, "分类")
            await call("wiki_get_entry", {"id": 111, "max_chars": 800}, "诺艾尔")
            await call("wiki_get_text", {"id": 508722, "max_chars": 300}, "next_offset")
            await call("wiki_get_section", {"id": 111, "heading": "基础属性"}, "基础属性")
            await call("wiki_browse", {"category": "敌人", "page_size": 3}, "id=")
            await call("wiki_related", {"id": 1290, "limit": 5}, "关联网络")
            await call("wiki_random", {"category": "书籍"})

            rr = await session.read_resource("wiki://index")
            text = pick(pick(rr, "contents")[0], "text", default="")
            print(f"{'[OK]' if text else '[FAIL]'} resource wiki://index → {len(text)} 字")
            if not text:
                failures += 1

            prompt = await session.get_prompt("analyze_entry", {"id": "111"})
            messages = pick(prompt, "messages")
            print(f"{'[OK]' if messages else '[FAIL]'} prompt analyze_entry → {len(messages)} 条消息")
            if not messages:
                failures += 1

    print("\n全部通过" if failures == 0 else f"\n{failures} 项失败")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
