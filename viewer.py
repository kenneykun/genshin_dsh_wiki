"""原神 Wiki 内容查看器 v3

Tab 浏览: 全站概览 + 分类筛选 + 搜索 + 爬取状态标记 + 详情页
Tab 爬取: 分类选择 + 进度条 + 实时日志 + 开始/暂停/停止
Tab 日志: 查看爬取/浏览日志文件
"""
import json
import time
import queue
import threading
import sys
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.logger import setup_logging, init_viewer_logger, read_recent_logs, get_log_path

setup_logging(console=False)

viewer_log = init_viewer_logger()

PROJECT_DIR = Path(__file__).parent
OUTPUT_DIR = PROJECT_DIR / "data" / "output"
INDEX_PATH = OUTPUT_DIR / "index.json"
PROGRESS_PATH = OUTPUT_DIR / "progress.json"

CATEGORY_NAMES = {}  # keys are already Chinese display names

FOLDER_MAP = {}  # no longer needed: folder == category name


class WikiViewer:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("原神 Wiki 内容查看器")
        self.root.geometry("1300x840")
        self.root.minsize(1000, 600)

        self.entries: list[dict] = []
        self.progress: dict = {"completed": {}, "failed": {}}
        self._loaded_detail: dict | None = None
        self._current_browse_cat: str = "all"
        self._current_browse_search: str = ""

        self._crawl_thread: threading.Thread | None = None
        self._crawl_stop_event = threading.Event()
        self._crawl_queue = queue.Queue()
        self._crawl_logs: list[str] = []
        self._crawl_state = "idle"

        self._setup_style()
        self._build_main_layout()
        self._load_data()
        self._show_browse()

        self.root.bind("<Control-b>", lambda e: self._show_browse())
        self.root.bind("<Control-f>", lambda e: self._focus_search())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        if self._crawl_state == "running":
            self._crawl_stop_event.set()
            self.root.after(500, self.root.destroy)
        else:
            self.root.destroy()

    def _setup_style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", rowheight=28, font=("Microsoft YaHei", 10))
        style.configure("Treeview.Heading", font=("Microsoft YaHei", 10, "bold"))
        style.configure("TButton", font=("Microsoft YaHei", 10))
        style.configure("TLabel", font=("Microsoft YaHei", 10))
        style.configure("Stats.TLabel", font=("Microsoft YaHei", 11, "bold"), foreground="#2c3e50")
        style.configure("Status.TLabel", font=("Microsoft YaHei", 9), foreground="gray")
        style.configure("Big.TLabel", font=("Microsoft YaHei", 14, "bold"))

    def _build_main_layout(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self.browse_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.browse_frame, text="  📚 浏览  ")

        self.crawl_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.crawl_frame, text="  🕷 爬取  ")
        self._build_crawl_tab()

        self.log_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.log_frame, text="  📋 日志  ")
        self._build_log_tab()

        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._crawl_poll_running = False

    # ==================== DATA ====================

    def _load_data(self):
        if INDEX_PATH.exists():
            try:
                with open(INDEX_PATH, encoding="utf-8") as f:
                    self.entries = json.load(f)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.entries = []
        if PROGRESS_PATH.exists():
            try:
                with open(PROGRESS_PATH, encoding="utf-8") as f:
                    self.progress = json.load(f)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.progress = {"completed": {}, "failed": {}}

    def _get_file_path(self, entry: dict) -> Path | None:
        cid = entry["content_id"]
        cat = entry.get("category", "misc")
        folder = cat
        path = OUTPUT_DIR / folder / f"{cid}.json"
        return path if path.exists() else None

    def _is_crawled(self, entry: dict) -> bool:
        return self._get_file_path(entry) is not None

    def _get_stats(self, entries: list[dict]) -> dict:
        crawled = sum(1 for e in entries if self._is_crawled(e))
        total_chars = 0
        for e in entries:
            path = self._get_file_path(e)
            if path:
                try:
                    d = json.load(open(path, encoding="utf-8"))
                    total_chars += len(d.get("raw_text", ""))
                except Exception:
                    pass
        return {
            "total": len(entries),
            "crawled": crawled,
            "remaining": len(entries) - crawled,
            "total_chars": total_chars,
        }

    # ==================== BROWSE TAB ====================

    def _show_browse(self, category_filter: str = "all", search_text: str = ""):
        self._loaded_detail = None
        self._current_browse_cat = category_filter
        self._current_browse_search = search_text
        for w in self.browse_frame.winfo_children():
            w.destroy()

        top_bar = ttk.Frame(self.browse_frame)
        top_bar.pack(fill=tk.X, padx=10, pady=(10, 0))

        ttk.Button(top_bar, text="⟲ 刷新数据", command=self._refresh_and_show_browse).pack(side=tk.LEFT, padx=(0, 20))

        search_frame = ttk.Frame(top_bar)
        search_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(search_frame, text="搜索:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar(value=search_text)
        self.search_entry = ttk.Entry(search_frame, textvariable=self.search_var, width=30)
        self.search_entry.pack(side=tk.LEFT, padx=5)
        self.search_entry.bind("<Return>", lambda e: self._apply_filter())
        ttk.Button(search_frame, text="搜索", command=self._apply_filter).pack(side=tk.LEFT, padx=2)

        stats = self._get_stats(self.entries)
        stats_text = (
            f"索引 {stats['total']:,} 条  |  已爬 {stats['crawled']} 条  "
            f"|  总字符 {stats['total_chars']:,}  |  待爬 {stats['remaining']} 条"
        )
        ttk.Label(top_bar, text=stats_text, style="Stats.TLabel").pack(side=tk.RIGHT)

        body = ttk.PanedWindow(self.browse_frame, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        left_frame = ttk.Frame(body, width=200)
        body.add(left_frame, weight=0)

        ttk.Label(left_frame, text="分类筛选", font=("Microsoft YaHei", 11, "bold")).pack(anchor=tk.W, pady=(0, 5))

        self.cat_tree = ttk.Treeview(left_frame, selectmode="browse", show="tree", height=20)
        self.cat_tree.pack(fill=tk.BOTH, expand=True)

        cat_counts = Counter(e.get("category", "misc") for e in self.entries)
        all_node = self.cat_tree.insert("", tk.END, text=f"全部 ({stats['total']})", open=True)
        self.cat_tree.item(all_node, tags=("all",))

        for cat in sorted(cat_counts.keys()):
            name = cat
            count = cat_counts[cat]
            crawled = sum(1 for e in self.entries if e.get("category") == cat and self._is_crawled(e))
            node = self.cat_tree.insert("", tk.END, text=f"{name} ({crawled}/{count})")
            self.cat_tree.item(node, tags=(cat,))

        self.cat_tree.tag_configure("all", font=("Microsoft YaHei", 10, "bold"))
        self.cat_tree.bind("<<TreeviewSelect>>", lambda e: self._on_category_select(cat_counts))

        right_frame = ttk.Frame(body)
        body.add(right_frame, weight=1)

        cols = ("ID", "标题", "分类", "状态", "字数")
        self.entry_list = ttk.Treeview(right_frame, columns=cols, show="headings", selectmode="browse")
        self.entry_list.heading("ID", text="ID")
        self.entry_list.heading("标题", text="标题")
        self.entry_list.heading("分类", text="分类")
        self.entry_list.heading("状态", text="状态")
        self.entry_list.heading("字数", text="字数")
        self.entry_list.column("ID", width=70, anchor=tk.CENTER)
        self.entry_list.column("分类", width=90, anchor=tk.CENTER)
        self.entry_list.column("状态", width=60, anchor=tk.CENTER)
        self.entry_list.column("字数", width=70, anchor=tk.CENTER)
        self.entry_list.column("标题", width=300)
        self.entry_list.pack(fill=tk.BOTH, expand=True)

        scroll = ttk.Scrollbar(self.entry_list, orient=tk.VERTICAL, command=self.entry_list.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.entry_list.configure(yscrollcommand=scroll.set)

        self.entry_list.bind("<Double-1>", lambda e: self._on_entry_double_click())
        self.entry_list.bind("<Return>", lambda e: self._on_entry_double_click())

        self._populate_entry_list(category_filter, search_text)

        if category_filter != "all":
            for item in self.cat_tree.get_children():
                tags = self.cat_tree.item(item, "tags")
                if tags and tags[0] == category_filter:
                    self.cat_tree.selection_set(item)
                    self.cat_tree.see(item)

    def _refresh_and_show_browse(self):
        self._load_data()
        self._show_browse(search_text=getattr(self, 'search_var', tk.StringVar()).get())

    def _apply_filter(self):
        viewer_log.info(f"搜索: \"{self.search_var.get()}\"")
        self._show_browse(search_text=self.search_var.get())

    def _on_category_select(self, cat_counts):
        sel = self.cat_tree.selection()
        if not sel:
            return
        tags = self.cat_tree.item(sel[0], "tags")
        cat = tags[0] if tags else "all"
        if cat == self._current_browse_cat and self._current_browse_search == "":
            return
        viewer_log.info(f"切换分类: {cat}")
        self._show_browse(category_filter=cat)

    def _focus_search(self):
        if hasattr(self, "search_entry"):
            self.search_entry.focus_set()
            self.search_entry.select_range(0, tk.END)

    def _populate_entry_list(self, category_filter: str = "all", search_text: str = ""):
        entries_to_show = self.entries
        if category_filter != "all":
            entries_to_show = [e for e in self.entries if e.get("category") == category_filter]
        if search_text:
            st = search_text.lower()
            entries_to_show = [e for e in entries_to_show if st in e.get("title", "").lower() or st in str(e.get("content_id", ""))]

        self.entry_list.delete(*self.entry_list.get_children())

        for e in entries_to_show:
            cid = e["content_id"]
            title = e.get("title", "")
            cat = e.get("category", "misc")
            cat_name = cat
            crawled = self._is_crawled(e)
            status = "✓" if crawled else ""
            chars_str = ""
            if crawled:
                path = self._get_file_path(e)
                if path:
                    try:
                        d = json.load(open(path, encoding="utf-8"))
                        chars_str = f"{len(d.get('raw_text', '')):,}"
                    except Exception:
                        pass
            iid = self.entry_list.insert("", tk.END, values=(cid, title, cat_name, status, chars_str))
            self.entry_list.item(iid, tags=("got",) if crawled else ("missing",))

        self.entry_list.tag_configure("got", foreground="#27ae60")
        self.entry_list.tag_configure("missing", foreground="#bdc3c7")

        stats = self._get_stats(entries_to_show)
        msg = f"显示 {stats['total']}  |  ✓{stats['crawled']}  |  待爬 {stats['remaining']}  |  {stats['total_chars']:,} 字"
        if hasattr(self, "_browse_status") and self._browse_status.winfo_exists():
            self._browse_status.configure(text=msg)
        else:
            self._browse_status = ttk.Label(self.browse_frame, text=msg, style="Status.TLabel")
            self._browse_status.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 5))

    def _on_entry_double_click(self):
        sel = self.entry_list.selection()
        if not sel:
            return
        item = self.entry_list.item(sel[0])
        cid = int(item["values"][0])
        cat = [e for e in self.entries if e["content_id"] == cid]
        cat_name = cat[0].get("category", "misc") if cat else "misc"
        self._show_detail(cid, cat_name)

    # ==================== DETAIL VIEW ====================

    def _show_detail(self, content_id: int, category: str = "misc"):
        self._loaded_detail = None
        for w in self.browse_frame.winfo_children():
            w.destroy()

        folder = category
        path = OUTPUT_DIR / folder / f"{content_id}.json"

        viewer_log.info(f"打开详情: [{content_id}] (分类={category})")

        if not path.exists():
            self._show_missing_detail(content_id, category)
            return

        try:
            with open(path, encoding="utf-8") as f:
                self._loaded_detail = json.load(f)
        except Exception:
            self._show_missing_detail(content_id, category)
            return

        self._build_detail_view()

    def _show_missing_detail(self, content_id: int, category: str = "misc"):
        entry = next((e for e in self.entries if e["content_id"] == content_id), None)
        title = entry.get("title", str(content_id)) if entry else str(content_id)
        cat_name = category

        top_bar = ttk.Frame(self.browse_frame)
        top_bar.pack(fill=tk.X, padx=10, pady=(10, 0))
        ttk.Button(top_bar, text="← 返回浏览 (Ctrl+B)", command=self._show_browse).pack(side=tk.LEFT)

        card = ttk.Frame(self.browse_frame)
        card.pack(expand=True, padx=60, pady=60)
        ttk.Label(card, text=f"{title}", font=("Microsoft YaHei", 18, "bold")).pack(pady=(0, 5))
        ttk.Label(card, text=f"ID: {content_id}  |  分类: {cat_name}", style="Status.TLabel").pack()
        ttk.Label(card, text="此条目尚未爬取", font=("Microsoft YaHei", 14), foreground="#e67e22").pack(pady=20)
        ttk.Label(card, text="切换到「爬取」Tab 即可开始爬取", style="Status.TLabel").pack()
        ttk.Button(card, text="前往爬取", command=lambda: self.notebook.select(1)).pack(pady=10)

    def _build_detail_view(self):
        d = self._loaded_detail
        if not d:
            return

        top_bar = ttk.Frame(self.browse_frame)
        top_bar.pack(fill=tk.X, padx=10, pady=(10, 5))
        ttk.Button(top_bar, text="← 返回浏览 (Ctrl+B)", command=self._show_browse).pack(side=tk.LEFT)

        title = d.get("title", "未知")
        cid = d.get("content_id", "")
        cat = d.get("category", "misc")
        cat_name = cat
        text_len = len(d.get("raw_text", ""))

        bar_frame = ttk.Frame(top_bar)
        bar_frame.pack(side=tk.RIGHT)

        info_parts = [f"{title} (ID: {cid})  |  {cat_name}  |  {text_len:,} 字"]
        rarity = d.get("rarity", 0)
        if rarity:
            info_parts.insert(0, "★" * rarity)
        element = d.get("element", "")
        if element:
            info_parts.append(f"元素: {element}")
        weapon_type = d.get("weapon_type", "")
        if weapon_type and weapon_type not in ("名称", "装备类型"):
            info_parts.append(weapon_type)

        ttk.Label(bar_frame, text="  ".join(info_parts), style="Stats.TLabel").pack(side=tk.RIGHT, padx=(20, 0))

        pane = ttk.PanedWindow(self.browse_frame, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        left = ttk.Frame(pane, width=280)
        pane.add(left, weight=0)

        self.detail_tree = ttk.Treeview(left, selectmode="browse")
        self.detail_tree.heading("#0", text="导航")
        self.detail_tree.column("#0", width=270, minwidth=120)
        self.detail_tree.pack(fill=tk.BOTH, expand=True)

        scroll_l = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.detail_tree.yview)
        scroll_l.pack(side=tk.RIGHT, fill=tk.Y)
        self.detail_tree.configure(yscrollcommand=scroll_l.set)
        self.detail_tree.bind("<<TreeviewSelect>>", self._on_detail_tree_select)

        right = ttk.Frame(pane)
        pane.add(right, weight=1)

        self.detail_meta = tk.Text(
            right, height=2, wrap=tk.WORD, state=tk.DISABLED,
            font=("Microsoft YaHei", 9), bg="#f0f0f0", relief=tk.FLAT, padx=10, pady=5,
        )
        self.detail_meta.pack(fill=tk.X)

        content_frame = ttk.Frame(right)
        content_frame.pack(fill=tk.BOTH, expand=True)

        self.detail_text = tk.Text(
            content_frame, wrap=tk.WORD, state=tk.DISABLED,
            font=("Microsoft YaHei", 11), padx=12, pady=10, spacing2=4, spacing3=6,
        )
        self.detail_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scroll_r = ttk.Scrollbar(content_frame, orient=tk.VERTICAL, command=self.detail_text.yview)
        scroll_r.pack(side=tk.RIGHT, fill=tk.Y)
        self.detail_text.configure(yscrollcommand=scroll_r.set)

        self.detail_text.tag_configure("heading", font=("Microsoft YaHei", 14, "bold"), foreground="#2c3e50")
        self.detail_text.tag_configure("subheading", font=("Microsoft YaHei", 12, "bold"), foreground="#8e44ad")

        self._populate_detail_tree()

    def _populate_detail_tree(self):
        self.detail_tree.delete(*self.detail_tree.get_children())
        d = self._loaded_detail

        meta_node = self.detail_tree.insert("", tk.END, text="── 基础信息 ──", open=True)
        meta_items = []
        rid = d.get("rarity", 0)
        if rid:
            meta_items.append(f"★ {'★' * (rid - 1) if rid > 1 else ''}")
        if d.get("element"):
            meta_items.append(f"元素: {d['element']}")
        if d.get("weapon_type") and d["weapon_type"] not in ("名称", "装备类型"):
            meta_items.append(f"武器: {d['weapon_type']}")
        if d.get("constellation"):
            meta_items.append(f"命之座: {d['constellation']}")
        if d.get("birthday") and "待更新" not in d.get("birthday", ""):
            meta_items.append(f"生日: {d['birthday']}")
        if d.get("affiliation") and "待更新" not in d.get("affiliation", ""):
            meta_items.append(f"所属: {d['affiliation']}")
        if d.get("role") and "待更新" not in d.get("role", ""):
            meta_items.append(f"定位: {d['role']}")
        if d.get("title_alias"):
            meta_items.append(f"称号: {d['title_alias']}")
        if d.get("base_atk"):
            meta_items.append(f"基础攻击力: {d['base_atk']}")
        if d.get("sub_stat") and d.get("sub_stat_value"):
            meta_items.append(f"副属性: {d['sub_stat']} {d['sub_stat_value']}")
        if d.get("set_2pc"):
            meta_items.append(f"2件套: {d['set_2pc'][:40]}")
        if d.get("set_4pc"):
            meta_items.append(f"4件套: {d['set_4pc'][:40]}")
        if d.get("effect_name"):
            meta_items.append(f"特效: {d['effect_name'][:30]}")
        if d.get("effect_desc"):
            meta_items.append(f"效果: {d['effect_desc'][:60]}...")
        for mi in meta_items:
            self.detail_tree.insert(meta_node, tk.END, text=mi)

        talents = d.get("talents")
        if talents:
            tn = self.detail_tree.insert("", tk.END, text="── 天赋 ──", open=False)
            for i, t in enumerate(talents):
                if t:
                    first_row = t[0] if t else []
                    label = first_row[0] if first_row else f"表{i}"
                    self.detail_tree.insert(tn, tk.END, text=f"表{i}: {label[:30]}")

        constellations = d.get("constellations")
        if constellations:
            cn = self.detail_tree.insert("", tk.END, text="── 命之座 ──", open=False)
            for c in constellations:
                name = c.get("name", "")
                desc = c.get("description", "")
                if desc:
                    self.detail_tree.insert(cn, tk.END, text=f"{name}: {desc[:40]}")

        pieces = d.get("pieces", [])
        if pieces:
            pn = self.detail_tree.insert("", tk.END, text="── 圣遗物部件 ──", open=True)
            for p in pieces:
                self.detail_tree.insert(pn, tk.END, text=f"{p.get('name','')}: {p.get('description','')[:40]}")

        sections = d.get("sections", {})
        if sections:
            sn = self.detail_tree.insert("", tk.END, text="── 章节 ──", open=True)
            sorted_sec = sorted(((k, v) for k, v in sections.items() if len(v) > 20),
                                key=lambda x: len(x[1]), reverse=True)
            for heading, content in sorted_sec:
                self.detail_tree.insert(sn, tk.END, text=f"{heading}  ({len(content)}字)")

        for child in self.detail_tree.get_children():
            grandchildren = self.detail_tree.get_children(child)
            for gc in grandchildren:
                self.detail_tree.see(gc)
                self.detail_tree.selection_set(gc)
                self._on_detail_tree_select()
                return

    def _on_detail_tree_select(self, event=None):
        sel = self.detail_tree.selection()
        if not sel:
            return
        item = self.detail_tree.item(sel[0])
        raw = item["text"]
        if raw.startswith("──"):
            return

        heading = raw.rsplit("  (", 1)[0]
        d = self._loaded_detail
        full_text = ""

        meta_keys = ("★", "元素", "武器", "命之座", "生日", "所属", "定位", "称号",
                      "基础", "副属性", "2件套", "4件套", "特效", "效果")

        if any(raw.startswith(k) for k in meta_keys):
            full_text = raw
        elif "天赋" in raw and "表" in raw:
            table_idx = None
            parts = raw.split(":")
            if parts and "表" in parts[0]:
                try:
                    table_idx = int(parts[0].replace("表", "").strip())
                except ValueError:
                    pass
            talents = d.get("talents", [])
            if table_idx is not None and talents and table_idx < len(talents):
                rows = talents[table_idx]
                if rows:
                    col_count = max(len(r) for r in rows)
                    col_widths = [max(len(r[i]) if i < len(r) else 0 for r in rows) + 4 for i in range(col_count)]
                    lines = []
                    for r in rows:
                        line = "".join(r[i].ljust(col_widths[i]) if i < len(r) else "".ljust(col_widths[i]) for i in range(col_count))
                        lines.append(line)
                    full_text = "\n".join(lines)
        elif "命之座" in raw:
            constellations = d.get("constellations", [])
            lines = []
            for c in constellations:
                name = c.get("name", "")
                desc = c.get("description", "")
                if desc:
                    lines.append(f"✦ {name}")
                    lines.append(f"  {desc}")
                    lines.append("")
            full_text = "\n".join(lines)
        elif "部件" in raw:
            pieces = d.get("pieces", [])
            lines = []
            for p in pieces:
                lines.append(f"✦ {p.get('name', '')}")
                lines.append(f"  {p.get('description', '')}")
                lines.append("")
            full_text = "\n".join(lines)
        else:
            sections = d.get("sections", {})
            full_text = sections.get(heading, "")

        self._display_detail_meta(heading, full_text)
        self._display_detail_content(heading, full_text)

    def _display_detail_meta(self, heading: str, text: str):
        self.detail_meta.configure(state=tk.NORMAL)
        self.detail_meta.delete("1.0", tk.END)
        d = self._loaded_detail
        cid = d.get("content_id", "")
        title = d.get("title", "")
        meta = f"  {title} (ID: {cid})  |  {heading}  |  {len(text)} 字"
        self.detail_meta.insert("1.0", meta)
        self.detail_meta.configure(state=tk.DISABLED)

    def _display_detail_content(self, heading: str, text: str):
        self.detail_text.configure(state=tk.NORMAL)
        self.detail_text.delete("1.0", tk.END)

        if not text or not text.strip():
            self.detail_text.insert(tk.END, "(暂无内容)")
            self.detail_text.configure(state=tk.DISABLED)
            return

        self.detail_text.insert(tk.END, f"{heading}\n", "heading")
        self.detail_text.insert(tk.END, "─" * 80 + "\n\n")

        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped:
                self.detail_text.insert(tk.END, "\n")
                continue
            if len(stripped) < 25 and (stripped.endswith("：") or any(
                kw in stripped for kw in ("介绍", "属性", "突破", "装备", "推荐", "展示", "信息", "天赋", "命之座", "描述", "故事")
            )):
                self.detail_text.insert(tk.END, f"\n{stripped}\n", "subheading")
            else:
                self.detail_text.insert(tk.END, f"  {stripped}\n")

        self.detail_text.see("1.0")
        self.detail_text.configure(state=tk.DISABLED)

    # ==================== CRAWL TAB ====================

    def _build_crawl_tab(self):
        toolbar = ttk.Frame(self.crawl_frame)
        toolbar.pack(fill=tk.X, padx=10, pady=(10, 5))

        self._crawl_btn_start = ttk.Button(toolbar, text="▶ 开始爬取", command=self._crawl_start)
        self._crawl_btn_start.pack(side=tk.LEFT, padx=2)

        self._crawl_btn_stop = ttk.Button(toolbar, text="▣ 停止", command=self._crawl_stop, state=tk.DISABLED)
        self._crawl_btn_stop.pack(side=tk.LEFT, padx=2)

        ttk.Button(toolbar, text="⟳ 刷新进度", command=self._refresh_crawl_tab).pack(side=tk.LEFT, padx=(10, 2))

        self._crawl_status_label = ttk.Label(toolbar, text="就绪", style="Status.TLabel")
        self._crawl_status_label.pack(side=tk.RIGHT)

        body = ttk.PanedWindow(self.crawl_frame, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        left = ttk.Frame(body, width=240)
        body.add(left, weight=0)

        ttk.Label(left, text="选择分类", font=("Microsoft YaHei", 11, "bold")).pack(anchor=tk.W, pady=(5, 5))

        sel_frame = ttk.Frame(left)
        sel_frame.pack(fill=tk.BOTH, expand=True)

        sel_canvas = tk.Canvas(sel_frame, highlightthickness=0)
        sel_scroll = ttk.Scrollbar(sel_frame, orient=tk.VERTICAL, command=sel_canvas.yview)
        sel_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        sel_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sel_canvas.configure(yscrollcommand=sel_scroll.set)

        self._crawl_cat_frame = ttk.Frame(sel_canvas)
        sel_canvas.create_window((0, 0), window=self._crawl_cat_frame, anchor=tk.NW)

        self._crawl_cat_frame.bind("<Configure>", lambda e: sel_canvas.configure(scrollregion=sel_canvas.bbox("all")))
        sel_canvas.bind("<Enter>", lambda e: sel_canvas.bind_all("<MouseWheel>", lambda ev: sel_canvas.yview_scroll(int(-1 * ev.delta / 120), "units")))

        ttk.Label(left, text="选项", font=("Microsoft YaHei", 10, "bold")).pack(anchor=tk.W, pady=(10, 0))
        self._crawl_force_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="强制重新爬取（覆盖已有数据）", variable=self._crawl_force_var).pack(anchor=tk.W, pady=2)
        self._crawl_retry_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="仅重试之前失败的条目", variable=self._crawl_retry_var).pack(anchor=tk.W, pady=2)
        self._crawl_priority_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(left, text="优先未爬分类（按覆盖率排序）", variable=self._crawl_priority_var).pack(anchor=tk.W, pady=2)

        right = ttk.Frame(body)
        body.add(right, weight=1)

        progress_frame = ttk.LabelFrame(right, text="爬取进度", padding=10)
        progress_frame.pack(fill=tk.X, padx=(5, 0))

        self._crawl_progress_bar = ttk.Progressbar(progress_frame, mode="determinate", length=100)
        self._crawl_progress_bar.pack(fill=tk.X, pady=(0, 5))

        stats_frame = ttk.Frame(progress_frame)
        stats_frame.pack(fill=tk.X)

        self._crawl_progress_text = tk.StringVar(value="0 / 0")
        ttk.Label(stats_frame, textvariable=self._crawl_progress_text, style="Big.TLabel").pack(side=tk.LEFT)

        self._crawl_speed_text = tk.StringVar(value="")
        ttk.Label(stats_frame, textvariable=self._crawl_speed_text, style="Status.TLabel").pack(side=tk.RIGHT)

        self._crawl_detail_text = tk.StringVar(value="成功: 0  |  失败: 0  |  预计剩余: --")
        ttk.Label(progress_frame, textvariable=self._crawl_detail_text, style="Status.TLabel").pack(anchor=tk.W)

        self._crawl_current_text = tk.StringVar(value="")
        ttk.Label(progress_frame, textvariable=self._crawl_current_text, style="Status.TLabel").pack(anchor=tk.W)

        log_frame = ttk.LabelFrame(right, text="爬取日志", padding=10)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=(5, 0), pady=(10, 0))

        self._crawl_log_text = tk.Text(
            log_frame, wrap=tk.WORD, state=tk.DISABLED,
            font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
            padx=8, pady=5, height=12,
        )
        self._crawl_log_text.pack(fill=tk.BOTH, expand=True)

        scroll_log = ttk.Scrollbar(self._crawl_log_text, orient=tk.VERTICAL, command=self._crawl_log_text.yview)
        scroll_log.pack(side=tk.RIGHT, fill=tk.Y)
        self._crawl_log_text.configure(yscrollcommand=scroll_log.set)

        self._crawl_log_text.tag_configure("error", foreground="#f44747")
        self._crawl_log_text.tag_configure("success", foreground="#6a9955")
        self._crawl_log_text.tag_configure("info", foreground="#9cdcfe")
        self._crawl_log_text.tag_configure("warning", foreground="#ce9178")

        self._refresh_crawl_tab()

    def _refresh_crawl_tab(self):
        self._load_data()
        for w in self._crawl_cat_frame.winfo_children():
            w.destroy()

        self._crawl_cat_vars = {}

        cat_counts = Counter(e.get("category", "misc") for e in self.entries)
        for i, cat in enumerate(sorted(cat_counts.keys())):
            name = cat
            count = cat_counts[cat]
            crawled = sum(1 for e in self.entries if e.get("category") == cat and self._is_crawled(e))
            remaining = count - crawled

            var = tk.BooleanVar(value=remaining > 0)
            self._crawl_cat_vars[cat] = var

            label = f"{name} ({crawled}/{count})"
            if remaining == 0:
                label += " ✓"

            cb = ttk.Checkbutton(self._crawl_cat_frame, text=label, variable=var)
            cb.pack(anchor=tk.W, pady=1, padx=5)
            if remaining == 0:
                cb.configure(state=tk.DISABLED)

        if self._crawl_state == "running":
            self._crawl_btn_start.configure(state=tk.DISABLED)
            self._crawl_btn_stop.configure(state=tk.NORMAL)
            self._crawl_status_label.configure(text="爬取中...", foreground="#e67e22")
        elif self._crawl_state == "idle":
            self._crawl_btn_start.configure(state=tk.NORMAL)
            self._crawl_btn_stop.configure(state=tk.DISABLED)
            self._crawl_status_label.configure(text="就绪", foreground="gray")
        elif self._crawl_state == "stopped":
            self._crawl_btn_start.configure(state=tk.NORMAL)
            self._crawl_btn_stop.configure(state=tk.DISABLED)
            self._crawl_status_label.configure(text="已停止", foreground="gray")

    def _build_log_tab(self):
        toolbar = ttk.Frame(self.log_frame)
        toolbar.pack(fill=tk.X, padx=10, pady=(10, 5))

        self._log_type_var = tk.StringVar(value="crawl")
        ttk.Radiobutton(toolbar, text="爬取日志", variable=self._log_type_var, value="crawl",
                        command=self._refresh_log_tab).pack(side=tk.LEFT, padx=2)
        ttk.Radiobutton(toolbar, text="浏览日志", variable=self._log_type_var, value="viewer",
                        command=self._refresh_log_tab).pack(side=tk.LEFT, padx=2)
        ttk.Radiobutton(toolbar, text="全量日志", variable=self._log_type_var, value="app",
                        command=self._refresh_log_tab).pack(side=tk.LEFT, padx=2)

        ttk.Button(toolbar, text="⟳ 刷新", command=self._refresh_log_tab).pack(side=tk.LEFT, padx=(20, 2))
        ttk.Button(toolbar, text="📂 打开日志目录", command=self._open_log_dir).pack(side=tk.LEFT, padx=2)

        self._log_lines_var = tk.StringVar(value="500")
        ttk.Label(toolbar, text="行数:").pack(side=tk.RIGHT, padx=(10, 0))
        ttk.Spinbox(toolbar, textvariable=self._log_lines_var, from_=50, to=5000, increment=100, width=6,
                    command=self._refresh_log_tab).pack(side=tk.RIGHT, padx=2)

        log_display_frame = ttk.Frame(self.log_frame)
        log_display_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self._log_view_text = tk.Text(
            log_display_frame, wrap=tk.NONE, state=tk.DISABLED,
            font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
            padx=8, pady=5,
        )
        self._log_view_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        v_scroll = ttk.Scrollbar(log_display_frame, orient=tk.VERTICAL, command=self._log_view_text.yview)
        v_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        h_scroll = ttk.Scrollbar(self.log_frame, orient=tk.HORIZONTAL, command=self._log_view_text.xview)
        h_scroll.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 10))
        self._log_view_text.configure(yscrollcommand=v_scroll.set, xscrollcommand=h_scroll.set)

        self._log_view_text.tag_configure("error", foreground="#f44747")
        self._log_view_text.tag_configure("success", foreground="#6a9955")
        self._log_view_text.tag_configure("info", foreground="#9cdcfe")
        self._log_view_text.tag_configure("warning", foreground="#ce9178")
        self._log_view_text.tag_configure("debug", foreground="#808080")

        self._refresh_log_tab()

    def _refresh_log_tab(self, event=None):
        log_type = self._log_type_var.get()
        try:
            lines = int(self._log_lines_var.get())
        except ValueError:
            lines = 500

        log_lines = read_recent_logs(log_type, lines)
        self._log_view_text.configure(state=tk.NORMAL)
        self._log_view_text.delete("1.0", tk.END)

        for line in log_lines:
            self._log_view_text.insert(tk.END, line + "\n")

        self._log_view_text.see(tk.END)
        self._log_view_text.configure(state=tk.DISABLED)

    def _open_log_dir(self):
        import os
        path = get_log_path()
        os.startfile(str(path.parent))

    def _on_tab_changed(self, event=None):
        idx = self.notebook.index("current")
        if idx == 1:
            self._refresh_crawl_tab()
            if not self._crawl_poll_running:
                self._crawl_poll_running = True
                self._poll_crawl_queue()
        elif idx == 2:
            self._refresh_log_tab()

    def _poll_crawl_queue(self):
        try:
            while True:
                msg = self._crawl_queue.get_nowait()
                msg_type = msg.get("type", "")

                if msg_type == "progress":
                    p = msg["data"]
                    done = p["done"]
                    total = p["total"]
                    failures = p["failures"]
                    elapsed = p["elapsed"]

                    self._crawl_progress_bar["maximum"] = max(total, 1)
                    self._crawl_progress_bar["value"] = done
                    self._crawl_progress_text.set(f"{done} / {total}")

                    if done > 0 and elapsed > 0:
                        speed = done / elapsed * 60
                        self._crawl_speed_text.set(f"{speed:.1f} 条/分")
                        if done < total:
                            eta_sec = (total - done) / (done / elapsed)
                            if eta_sec < 60:
                                eta_str = f"{eta_sec:.0f}秒"
                            elif eta_sec < 3600:
                                eta_str = f"{eta_sec / 60:.0f}分"
                            else:
                                eta_str = f"{eta_sec / 3600:.1f}时"
                        else:
                            eta_str = "--"
                    else:
                        self._crawl_speed_text.set("")
                        eta_str = "--"

                    self._crawl_detail_text.set(f"成功: {done - failures}  |  失败: {failures}  |  预计剩余: {eta_str}")

                    status = p.get("status", "")
                    if status == "crawling":
                        self._crawl_current_text.set(f"正在爬取: [{p['content_id']}] {p.get('title','')[:30]}")
                    elif status == "done":
                        self._crawl_current_text.set(f"✓ [{p['content_id']}] {p.get('title','')[:30]} ({p.get('text_len',0):,}字)")
                    elif status == "fail":
                        self._crawl_current_text.set(f"✗ [{p['content_id']}] {p.get('title','')[:30]}")

                elif msg_type == "log":
                    entry = msg["data"]
                    level = entry["level"]
                    text = entry["text"]
                    self._append_log(level, text)

                elif msg_type == "done":
                    self._crawl_finished(success=True)

                elif msg_type == "error":
                    self._append_log("error", msg["data"])
                    self._crawl_finished(success=False)

        except queue.Empty:
            pass

        if self._crawl_state == "running":
            self.root.after(200, self._poll_crawl_queue)
        else:
            self._crawl_poll_running = False

    def _append_log(self, level: str, text: str):
        self._crawl_log_text.configure(state=tk.NORMAL)
        ts = time.strftime("%H:%M:%S")
        tag = "info"
        if level == "error":
            tag = "error"
        elif level == "success":
            tag = "success"
        elif level == "warning":
            tag = "warning"
        prefix = {"error": "✗", "success": "✓", "warning": "⚠", "info": "·"}.get(level, "·")
        self._crawl_log_text.insert(tk.END, f"[{ts}] {prefix} {text}\n", tag)
        self._crawl_log_text.see(tk.END)
        self._crawl_log_text.configure(state=tk.DISABLED)

    def _crawl_start(self):
        selected_cats = [cat for cat, var in self._crawl_cat_vars.items() if var.get()]
        if not selected_cats:
            messagebox.showinfo("提示", "请至少选择一个分类")
            return

        force = self._crawl_force_var.get()
        retry = self._crawl_retry_var.get()

        self._crawl_state = "running"
        self._crawl_btn_start.configure(state=tk.DISABLED)
        self._crawl_btn_stop.configure(state=tk.NORMAL)
        self._crawl_status_label.configure(text="爬取中...", foreground="#e67e22")
        self._crawl_progress_bar["value"] = 0
        self._crawl_progress_text.set("0 / ?")
        self._crawl_detail_text.set("准备中...")
        self._crawl_current_text.set("")

        self._crawl_log_text.configure(state=tk.NORMAL)
        self._crawl_log_text.delete("1.0", tk.END)
        self._crawl_log_text.configure(state=tk.DISABLED)

        self._crawl_stop_event.clear()

        def _run():
            try:
                from src.scheduler import Scheduler

                def on_progress(data):
                    self._crawl_queue.put({"type": "progress", "data": data})

                def on_log(level, msg):
                    self._crawl_queue.put({"type": "log", "data": {"level": level, "text": msg}})

                scheduler = Scheduler(
                    categories=selected_cats,
                    force=force,
                    retry_failed=retry,
                    sort_by_coverage=self._crawl_priority_var.get(),
                    on_progress=on_progress,
                    on_log=on_log,
                    stop_event=self._crawl_stop_event,
                )
                scheduler.run()
                self._crawl_queue.put({"type": "done"})
            except Exception as e:
                self._crawl_queue.put({"type": "error", "data": str(e)})

        self._crawl_thread = threading.Thread(target=_run, daemon=True)
        self._crawl_thread.start()

        if not self._crawl_poll_running:
            self._crawl_poll_running = True
            self._poll_crawl_queue()

    def _crawl_stop(self):
        self._append_log("warning", "正在停止爬取...")
        self._crawl_stop_event.set()
        self._crawl_btn_stop.configure(state=tk.DISABLED)
        self._crawl_status_label.configure(text="停止中...", foreground="#e67e22")

    def _crawl_finished(self, success: bool = True):
        self._crawl_state = "idle"
        self._crawl_btn_start.configure(state=tk.NORMAL)
        self._crawl_btn_stop.configure(state=tk.DISABLED)
        self._crawl_status_label.configure(
            text="爬取完成" if success else "爬取出错",
            foreground="#27ae60" if success else "#f44747"
        )
        if self._crawl_thread:
            self._crawl_thread = None
        self._load_data()
        self._refresh_crawl_tab()

    # ==================== RUN ====================

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    WikiViewer().run()
