/* 观测枢 · 本地资料库 —— 前端逻辑（原生 JS，无构建步骤） */
(() => {
  "use strict";

  // ---------------------------------------------------------------- 基础
  const $ = (sel, root = document) => root.querySelector(sel);
  const main = $("#main");
  const catList = $("#catList");
  const sideStats = $("#sideStats");
  const searchInput = $("#searchInput");
  const searchPanel = $("#searchPanel");
  const sidebar = $("#sidebar");
  const overlay = $("#overlay");
  const toastEl = $("#toast");

  const state = {
    stats: null,
    catMap: new Map(),
    route: null,
    searchIndex: -1,
    searchTimer: 0,
    categoryItems: [],
    categoryPage: 1,
    categoryTotal: 0,
    categoryFilter: "",
  };

  const fmt = (n) => (n ?? 0).toLocaleString("zh-CN");
  const esc = (s) => String(s ?? "");

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const child of [].concat(children)) {
      if (child == null || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function toast(message) {
    toastEl.textContent = message;
    toastEl.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (toastEl.hidden = true), 1800);
  }

  async function api(path) {
    const res = await fetch(path, { headers: { Accept: "application/json" } });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        detail = (await res.json()).error || detail;
      } catch { /* ignore */ }
      throw new Error(detail);
    }
    return res.json();
  }

  function skeleton() {
    return el("div", { class: "skeleton" }, [
      el("div", { class: "sk-line w40" }),
      el("div", { class: "sk-line w80" }),
      el("div", { class: "sk-line w60" }),
      el("div", { class: "sk-line w80" }),
      el("div", { class: "sk-line w40" }),
    ]);
  }

  function emptyBox(text, hint) {
    return el("div", { class: "empty" }, [
      el("div", { class: "big", text: "◈" }),
      el("div", { text }),
      hint ? el("div", { class: "page-sub", style: "margin-top:6px", text: hint }) : null,
    ]);
  }

  // ---------------------------------------------------------------- 主题 / 字号
  const THEME_KEY = "wiki.theme";
  const FONT_KEY = "wiki.fontSize";

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
  }

  function applyFont(size) {
    const value = Math.min(22, Math.max(14, size));
    document.documentElement.style.setProperty("--read-size", value + "px");
    localStorage.setItem(FONT_KEY, String(value));
    return value;
  }

  function initPrefs() {
    // 支持 ?theme=dark&font=18 深链接（方便截图与分享）
    const urlParams = new URLSearchParams(location.search);
    const urlTheme = urlParams.get("theme");
    const urlFont = Number(urlParams.get("font"));
    const savedTheme = urlTheme || localStorage.getItem(THEME_KEY);
    applyTheme(savedTheme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
    applyFont(urlFont || Number(localStorage.getItem(FONT_KEY)) || 17);
  }

  // ---------------------------------------------------------------- 侧栏
  // 分类分组：30 个分类摊平太长，按用途收成 4 组，默认展开最常用的两组
  const CATEGORY_GROUPS = [
    { key: "core", label: "角色与装备", items: ["角色", "武器", "圣遗物", "敌人", "角色逸闻"] },
    { key: "story", label: "故事与文本", items: ["任务", "书籍", "组织", "NPC与商店", "地图文本", "空月之歌", "观测考据", "冒险家协会"] },
    { key: "collect", label: "收集与家园", items: ["背包", "洞天", "食物", "动物", "成就", "名片", "头像", "装扮", "观景点", "原神OST", "活动"] },
    { key: "play", label: "玩法与挑战", items: ["教程", "卡牌图鉴", "秘境", "酒馆挑战", "深境螺旋", "幻想真境剧诗"] },
  ];
  const PIN_KEY = "wiki.pins";
  const GROUP_KEY = "wiki.groups";
  const DEFAULT_PINS = ["角色", "任务", "观测考据"];

  function readPins() {
    try {
      const saved = JSON.parse(localStorage.getItem(PIN_KEY) || "null");
      return Array.isArray(saved) ? saved : [...DEFAULT_PINS];
    } catch {
      return [...DEFAULT_PINS];
    }
  }

  function writePins(pins) {
    try {
      localStorage.setItem(PIN_KEY, JSON.stringify(pins));
    } catch { /* ignore */ }
  }

  function readGroups() {
    try {
      return JSON.parse(localStorage.getItem(GROUP_KEY) || "{}") || {};
    } catch {
      return {};
    }
  }

  function writeGroups(map) {
    try {
      localStorage.setItem(GROUP_KEY, JSON.stringify(map));
    } catch { /* ignore */ }
  }

  async function loadStats() {
    state.stats = await api("/api/stats");
    for (const cat of state.stats.categories) state.catMap.set(cat.name, cat);
    renderSidebar();
    renderSideStats();
  }

  function catRow(cat, opts = {}) {
    const pinned = opts.pinned;
    return el("li", { class: "cat-row" + (opts.matched ? " matched" : "") }, [
      el("a", { href: `#/c/${encodeURIComponent(cat)}`, "data-cat": cat, title: `${cat} · ${fmt(opts.count)} 条` }, [
        el("span", { class: "cat-name", text: cat }),
        el("span", { class: "cat-count", text: fmt(opts.count) }),
      ]),
      el("button", {
        class: "cat-pin" + (pinned ? " on" : ""),
        title: pinned ? "取消置顶" : "置顶",
        onclick: (e) => {
          e.preventDefault();
          e.stopPropagation();
          const pins = readPins();
          const next = pins.includes(cat) ? pins.filter((c) => c !== cat) : [...pins, cat];
          writePins(next);
          renderSidebar();
        },
      }, [pinned ? "★" : "☆"]),
    ]);
  }

  function renderSidebar() {
    if (!catList) return;
    const counts = new Map((state.stats ? state.stats.categories : []).map((c) => [c.name, c]));
    const total = state.stats ? state.stats.entries : 0;
    const query = (state.sideQuery || "").trim().toLowerCase();
    const pins = readPins();
    const groups = readGroups();

    const nodes = [];

    // 1) 快捷入口
    nodes.push(el("li", { class: "side-quick" }, [
      el("a", { href: "#/feed", "data-cat": "__feed", title: "发现 · 瀑布流" }, [el("span", { text: "🖼 发现" })]),
      el("a", { href: "#/chat", "data-cat": "__chat", title: "资料库助手" }, [el("span", { text: "🤖 助手" })]),
      el("a", { href: "#/manage", "data-cat": "__manage", title: "数据管理" }, [el("span", { text: "↻ 数据" })]),
    ]));

    // 2) 分类快速筛选
    const filter = el("input", {
      class: "side-filter",
      type: "search",
      placeholder: "筛选分类…",
      value: state.sideQuery || "",
      autocomplete: "off",
    });
    filter.addEventListener("input", (e) => {
      state.sideQuery = e.target.value;
      renderSidebar();
      const again = catList.querySelector(".side-filter");
      if (again) {
        again.focus();
        again.setSelectionRange(again.value.length, again.value.length);
      }
    });
    nodes.push(el("li", { class: "side-filter-row" }, [filter]));

    // 3) 全部条目
    nodes.push(el("li", {}, [
      el("a", { href: "#/", "data-cat": "", class: "side-all" }, [
        el("span", { class: "cat-name", text: "全部条目" }),
        el("span", { class: "cat-count", text: fmt(total) }),
      ]),
    ]));

    if (query) {
      // 搜索模式：平铺命中项
      const matched = [...counts.values()]
        .filter((c) => c.name.toLowerCase().includes(query))
        .sort((a, b) => b.count - a.count);
      nodes.push(el("li", { class: "side-sep" }));
      if (!matched.length) {
        nodes.push(el("li", { class: "side-empty", text: "没有匹配的分类" }));
      } else {
        nodes.push(el("li", { class: "side-group-label" }, [`匹配 ${matched.length}`]));
        matched.forEach((c) => nodes.push(catRow(c.name, { count: c.count, pinned: pins.includes(c.name), matched: true })));
      }
      catList.replaceChildren(...nodes);
      markActiveCat();
      return;
    }

    // 4) 置顶
    const pinCats = pins.filter((p) => counts.has(p));
    if (pinCats.length) {
      nodes.push(el("li", { class: "side-sep" }));
      nodes.push(el("li", { class: "side-group-label" }, ["置顶"]));
      pinCats.forEach((name) => nodes.push(catRow(name, { count: counts.get(name).count, pinned: true })));
    }

    // 5) 最近阅读
    const history = readHistory(4);
    if (history.length) {
      nodes.push(el("li", { class: "side-sep" }));
      nodes.push(el("li", { class: "side-group-label" }, ["最近阅读"]));
      history.forEach((item) => nodes.push(
        el("li", { class: "side-recent" }, [
          el("a", { href: `#/e/${item.id}`, title: item.title }, [
            el("span", { class: "side-recent-dot" }),
            el("span", { class: "side-recent-title", text: item.title || `#${item.id}` }),
          ]),
        ])
      ));
    }

    // 6) 分组分类（置顶项不重复出现在组里）
    nodes.push(el("li", { class: "side-sep" }));
    const pinnedSet = new Set(pinCats);
    const openMap = { core: true, story: true, collect: false, play: false, ...groups };
    for (const group of CATEGORY_GROUPS) {
      const items = group.items.filter((name) => counts.has(name) && !pinnedSet.has(name));
      if (!items.length) continue;
      const opened = openMap[group.key] !== false;
      const sum = items.reduce((acc, name) => acc + counts.get(name).count, 0);
      nodes.push(el("li", { class: "side-group" }, [
        el("button", {
          class: "side-group-head" + (opened ? " open" : ""),
          onclick: () => {
            const map = readGroups();
            map[group.key] = !opened;
            writeGroups(map);
            renderSidebar();
          },
        }, [
          el("span", { class: "side-group-caret", text: opened ? "▾" : "▸" }),
          el("span", { class: "side-group-name", text: group.label }),
          el("span", { class: "cat-count", text: fmt(sum) }),
        ]),
        opened
          ? el("ul", { class: "side-group-items" },
              items.map((name) => catRow(name, { count: counts.get(name).count, pinned: pins.includes(name) })))
          : null,
      ].filter(Boolean)));
    }

    catList.replaceChildren(...nodes);
    markActiveCat();
  }

  function markActiveCat() {
    const kind = state.route && state.route.kind;
    const current = kind === "category" ? state.route.cat
      : kind === "chat" ? "__chat"
      : kind === "manage" ? "__manage"
      : kind === "feed" ? "__feed" : "";
    for (const a of catList.querySelectorAll("a")) {
      a.classList.toggle("active", (a.dataset.cat || "") === current);
    }
  }

  function renderSideStats() {
    if (!state.stats) return;
    sideStats.replaceChildren(
      el("div", {}, ["条目 ", el("b", { text: fmt(state.stats.entries) })]),
      el("div", {}, ["正文 ", el("b", { text: fmt(state.stats.chars) }), " 字"]),
      el("div", {}, ["分类 ", el("b", { text: fmt(state.stats.categories.length) }), " 个"]),
      el("div", { style: "margin-top:6px;opacity:.75" }, ["本地 JSON · 无外部依赖"])
    );
  }

  // ---------------------------------------------------------------- 浏览历史 / 关联条目
  const HISTORY_KEY = "wiki.history";

  function pushHistory(entry) {
    try {
      const list = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
      const item = {
        id: entry.id ?? entry.content_id,
        title: entry.title,
        category: entry.category,
        ts: Date.now(),
      };
      const next = [item, ...list.filter((x) => x.id !== item.id)].slice(0, 30);
      localStorage.setItem(HISTORY_KEY, JSON.stringify(next));
    } catch { /* 隐私模式下忽略 */ }
  }

  function readHistory(limit = 6) {
    try {
      const list = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
      const seen = new Set();
      return list
        .filter((x) => x && x.id && !seen.has(x.id) && seen.add(x.id))
        .slice(0, limit);
    } catch {
      return [];
    }
  }

  const REASON_LABEL = {
    "co-mention": "共同提及",
    "co-cited": "被同时引用",
    "same-category": "同类条目",
  };

  function relatedPanel(links) {
    if (!links) return null;
    const { mentions = [], mentioned_by = [], related = [] } = links;
    if (!mentions.length && !mentioned_by.length && !related.length) return null;

    const chips = (items) =>
      el("div", { class: "rel-chips" }, items.map((it) =>
        el("a", { class: "rel-chip", href: `#/e/${it.id}`, title: `${it.title} · ${it.category}` }, [
          el("span", { class: "rel-chip-name", text: it.title }),
          el("span", { class: "rel-chip-count", text: it.n ? `${it.n}次` : it.category }),
        ])
      ));

    const groups = [];
    if (mentions.length) {
      groups.push(el("div", { class: "rel-group" }, [
        el("div", { class: "rel-title", text: `本文提及 ${mentions.length}` }),
        chips(mentions.slice(0, 18)),
      ]));
    }
    if (mentioned_by.length) {
      groups.push(el("div", { class: "rel-group" }, [
        el("div", { class: "rel-title", text: `被这些条目提及 ${mentioned_by.length}` }),
        chips(mentioned_by.slice(0, 12)),
      ]));
    }

    const cards = related.length
      ? el("div", { class: "rel-grid" }, related.map((it) =>
          el("a", { class: "rel-card", href: `#/e/${it.id}` }, [
            el("div", { class: "rel-card-title", text: it.title }),
            el("div", { class: "rel-card-meta" }, [
              el("span", { class: "badge", text: it.category }),
              el("span", { class: "badge", text: REASON_LABEL[it.reason] || it.reason || "相关" }),
            ]),
          ])
        ))
      : null;

    return el("section", { class: "related-panel" }, [
      el("h2", { text: "关联条目" }),
      ...groups,
      cards
        ? el("div", { class: "rel-group" }, [
            el("div", { class: "rel-title", text: "相关阅读" }),
            cards,
          ])
        : null,
    ].filter(Boolean));
  }

  /** 把正文里提到的条目名变成可点链接（每个标题最多 3 次，总量封顶 300） */
  function linkifyMentions(root, mentions) {
    if (!mentions || !mentions.length) return;
    const idOf = new Map(mentions.map((m) => [m.title, m.id]));
    const titles = [...idOf.keys()]
      .sort((a, b) => b.length - a.length)
      .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    if (!titles.length) return;
    const pattern = new RegExp(titles.join("|"), "g");

    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);

    const counts = new Map();
    let budget = 300;
    for (const node of nodes) {
      if (budget <= 0) break;
      const parent = node.parentElement;
      if (!parent || parent.closest("a, h2, .kv-k, .badge, .figure")) continue;
      const text = node.nodeValue;
      pattern.lastIndex = 0;
      if (!pattern.test(text)) continue;
      pattern.lastIndex = 0;
      const frag = document.createDocumentFragment();
      let last = 0;
      let match;
      while ((match = pattern.exec(text)) !== null && budget > 0) {
        const title = match[0];
        const id = idOf.get(title);
        if (!id || (counts.get(title) || 0) >= 3) continue;
        counts.set(title, (counts.get(title) || 0) + 1);
        budget -= 1;
        frag.append(document.createTextNode(text.slice(last, match.index)));
        frag.append(el("a", { class: "ilink", href: `#/e/${id}` }, [title]));
        last = match.index + title.length;
      }
      frag.append(document.createTextNode(text.slice(last)));
      node.replaceWith(frag);
    }
  }

  // ---------------------------------------------------------------- 路由
  function parseHash() {
    const raw = location.hash.replace(/^#/, "") || "/";
    const [path, qs] = raw.split("?");
    const params = new URLSearchParams(qs || "");
    const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
    if (!parts.length) return { kind: "home" };
    if (parts[0] === "c") return { kind: "category", cat: parts[1] || "" };
    if (parts[0] === "e") return { kind: "entry", id: Number(parts[1]) };
    if (parts[0] === "s") return { kind: "search", q: params.get("q") || "", cat: params.get("cat") || "" };
    if (parts[0] === "chat") return { kind: "chat", ask: params.get("ask") || "" };
    if (parts[0] === "manage") return { kind: "manage" };
    if (parts[0] === "feed") return { kind: "feed", feedCat: params.get("cat") || "" };
    return { kind: "home" };
  }

  function navigate(hash, replace = false) {
    if (replace) history.replaceState(null, "", hash);
    else location.hash = hash;
    if (replace) render();
  }

  async function render() {
    state.route = parseHash();
    stopManagePolling();
    markActiveCat();
    closeSidebar();
    const route = state.route;
    main.replaceChildren(skeleton());
    try {
      if (route.kind === "home") await renderHome();
      else if (route.kind === "category") await renderCategory(route.cat);
      else if (route.kind === "entry") await renderEntry(route.id);
      else if (route.kind === "search") await renderSearch(route.q, route.cat);
      else if (route.kind === "chat") await renderChat(route.ask);
      else if (route.kind === "manage") await renderManage();
      else if (route.kind === "feed") await renderFeed();
    } catch (err) {
      main.replaceChildren(emptyBox("加载失败", String(err.message || err)));
    }
  }

  // ---------------------------------------------------------------- 首页
  async function renderHome() {
    if (!state.stats) await loadStats();
    const s = state.stats;

    const hero = el("header", { class: "hero" }, [
      el("h1", { text: "提瓦特资料库" }),
      el("p", {
        text:
          "原神观测枢全站文本的本地镜像：角色、武器、圣遗物、敌人、书籍、任务、考据……" +
          "全部结构化存储，可离线全文检索。",
      }),
      el("div", { class: "hero-actions" }, [
        el("button", { class: "btn primary", onclick: () => randomEntry() }, ["🎲 随机条目"]),
        el("button", {
          class: "btn",
          onclick: () => {
            const cat = s.categories[Math.floor(Math.random() * s.categories.length)];
            navigate(`#/c/${encodeURIComponent(cat.name)}`);
          },
        }, ["浏览分类"]),
        el("button", { class: "btn", onclick: () => navigate("#/feed") }, ["刷一刷"]),
        el("button", { class: "btn", onclick: () => searchInput.focus() }, ["全文搜索"]),
      ]),
    ]);

    const stats = el("div", { class: "stat-grid" }, [
      statCard("收录条目", fmt(s.entries)),
      statCard("正文字数", fmt(s.chars)),
      statCard("分类", fmt(s.categories.length)),
      statCard("平均正文", fmt(Math.round(s.chars / Math.max(1, s.entries))) + " 字"),
    ]);

    const grid = el("div", { class: "cat-grid" }, s.categories.map((cat) =>
      el("a", { class: "cat-card", href: `#/c/${encodeURIComponent(cat.name)}` }, [
        el("div", { class: "name", text: cat.name }),
        el("div", { class: "meta", text: `${fmt(cat.count)} 条 · ${fmt(Math.round(cat.chars / 1000))}k 字` }),
      ])
    ));

    let featured = [];
    try {
      featured = (await api("/api/featured?limit=12")).items || [];
    } catch { /* 图谱未建时跳过 */ }
    const history = readHistory(6);

    const recCard = (it, extra) =>
      el("a", { class: "rel-card", href: `#/e/${it.id}` }, [
        el("div", { class: "rel-card-title", text: it.title }),
        el("div", { class: "rel-card-meta" }, [
          el("span", { class: "badge", text: it.category }),
          extra ? el("span", { class: "badge", text: extra }) : null,
        ].filter(Boolean)),
      ]);

    const blocks = [hero, stats];
    if (history.length) {
      blocks.push(
        el("div", { class: "section-title" }, ["继续阅读"]),
        el("div", { class: "rel-grid" }, history.map((h) => recCard(h)))
      );
    }
    if (featured.length) {
      blocks.push(
        el("div", { class: "section-title" }, ["推荐阅读"]),
        el("div", { class: "rel-grid" }, featured.map((it) =>
          recCard(it, it.indegree ? `被 ${it.indegree} 条引用` : "")
        ))
      );
    }
    blocks.push(el("div", { class: "section-title" }, ["按分类浏览"]), grid);

    main.replaceChildren(el("div", { class: "page" }, blocks));
  }

  function statCard(k, v) {
    return el("div", { class: "stat-card" }, [
      el("div", { class: "k", text: k }),
      el("div", { class: "v", text: v }),
    ]);
  }

  async function randomEntry() {
    try {
      const { id } = await api("/api/random");
      navigate(`#/e/${id}`);
    } catch {
      toast("没有可用条目");
    }
  }

  // ---------------------------------------------------------------- 分类页
  async function renderCategory(cat) {
    const data = await api(`/api/entries?category=${encodeURIComponent(cat)}&page=1&size=200`);
    state.categoryItems = data.items;
    state.categoryPage = 1;
    state.categoryTotal = data.total;
    state.categoryFilter = "";

    const toolbar = el("div", { class: "toolbar" }, [
      el("input", {
        type: "search",
        placeholder: `在「${cat}」内搜索…`,
        oninput: (e) => {
          state.categoryFilter = e.target.value.trim();
          renderCategoryList();
        },
      }),
      el("span", { class: "grow" }),
      el("button", { class: "btn", onclick: randomEntry }, ["🎲 随机"]),
    ]);

    const listHost = el("div", { id: "catListHost" });
    const moreHost = el("div", { class: "load-more", id: "moreHost" });

    main.replaceChildren(
      el("div", { class: "page page-narrow" }, [
        el("div", { class: "crumbs" }, [el("a", { href: "#/" }, ["首页"]), el("span", { class: "sep", text: "/" }), el("span", { text: "分类" })]),
        el("h1", { class: "page-title", text: cat || "全部条目" }),
        el("div", { class: "page-sub", text: `${fmt(data.total)} 条 · 按标题排序` }),
        toolbar,
        listHost,
        moreHost,
      ])
    );
    renderCategoryList();
    document.title = `${cat} · 观测枢本地资料库`;
  }

  function renderCategoryList() {
    const host = $("#catListHost");
    const moreHost = $("#moreHost");
    if (!host) return;

    const filter = state.categoryFilter.toLowerCase();
    const items = filter
      ? state.categoryItems.filter((it) => it.title.toLowerCase().includes(filter))
      : state.categoryItems;

    if (!items.length) {
      host.replaceChildren(emptyBox(filter ? "没有匹配的条目" : "这个分类还没有数据"));
      if (moreHost) moreHost.replaceChildren();
      return;
    }

    const list = el("div", { class: "list" }, items.map((it) =>
      el("div", { class: "list-row", onclick: () => navigate(`#/e/${it.id}`) }, [
        el("span", { class: "t", text: it.title }),
        el("span", { class: "n", text: `${fmt(it.chars)} 字` }),
        el("span", { class: "arrow", text: "›" }),
      ])
    ));
    host.replaceChildren(list);

    if (!filter && state.categoryItems.length < state.categoryTotal) {
      moreHost.replaceChildren(
        el("button", {
          class: "btn",
          onclick: async (e) => {
            e.target.disabled = true;
            e.target.textContent = "加载中…";
            try {
              const next = await api(
                `/api/entries?category=${encodeURIComponent(state.route.cat)}&page=${state.categoryPage + 1}&size=200`
              );
              state.categoryPage += 1;
              state.categoryItems = state.categoryItems.concat(next.items);
              renderCategoryList();
            } catch (err) {
              toast("加载失败");
              e.target.disabled = false;
              e.target.textContent = "加载更多";
            }
          },
        }, [`加载更多（剩余 ${fmt(state.categoryTotal - state.categoryItems.length)} 条）`])
      );
    } else if (moreHost) {
      moreHost.replaceChildren();
    }
  }

  // ---------------------------------------------------------------- 文章页
  // ---------------- 笔记式阅读（小红书风） ----------------
  const CATEGORY_EMOJI = {
    "角色": "🧝", "武器": "⚔️", "圣遗物": "💎", "敌人": "👹", "角色逸闻": "📰",
    "任务": "🗺️", "书籍": "📚", "组织": "🏛️", "NPC与商店": "🛒", "地图文本": "🧭",
    "空月之歌": "🌙", "观测考据": "🔍", "冒险家协会": "🧾",
    "背包": "🎒", "洞天": "🏡", "食物": "🍜", "动物": "🐾", "成就": "🏆",
    "名片": "🪪", "头像": "🙂", "装扮": "👗", "观景点": "⛰️", "原神OST": "🎵", "活动": "🎉",
    "教程": "📘", "卡牌图鉴": "🃏", "秘境": "🌀", "酒馆挑战": "🍺", "深境螺旋": "🗼", "幻想真境剧诗": "🎭",
  };

  function categoryEmoji(cat) {
    return CATEGORY_EMOJI[cat] || "✨";
  }

  const SECTION_EMOJI = [
    [/基础|属性|数值/, "📊"], [/天赋|技能/, "✨"], [/命之座|命座/, "🌟"],
    [/语音|台词|对话|CV|配音/, "💬"], [/材料|掉落|获取|消耗/, "📦"],
    [/故事|背景|设定|剧情/, "📖"], [/攻略|技巧|推荐|用法/, "🧭"],
    [/任务|流程|步骤/, "🗺️"], [/突破|养成|升级/, "⬆️"],
    [/图集|截图|立绘|图片/, "🖼️"], [/评价|口碑|强度/, "💭"],
    [/奖励|商店|价格|兑换/, "🎁"], [/同人|考据|解析/, "🔎"],
  ];

  function sectionEmoji(heading) {
    for (const [re, emoji] of SECTION_EMOJI) {
      if (re.test(heading)) return emoji;
    }
    return "📌";
  }

  function pickNoteSummary(sections, title) {
    const preferred = /介绍|简介|概述|背景|故事|描述|档案|逸闻/;
    for (const [heading, content] of Object.entries(sections)) {
      if (!preferred.test(heading)) continue;
      for (const line of String(content).split("\n")) {
        const text = line.trim();
        if (text.length < 24 || text === heading || text === title) continue;
        if (/^[「“"]/.test(text)) continue;
        if (/武器|圣遗物|配队|队伍/.test(text) && text.length < 120) continue;
        return text.length > 150 ? text.slice(0, 150) + "…" : text;
      }
    }
    return "";
  }

  const NOTE_STAT_LABELS = new Set([
    "元素", "武器类型", "命之座", "生日", "所属", "称号", "定位", "稀有度",
    "攻击方式", "职业", "位置", "功能", "性别", "神之眼",
  ]);

  /** 结构化字段缺失时，从「基础信息」这类章节里反解 key/value */
  function noteStatsFromSections(sections) {
    const found = {};
    const lines = [];
    for (const content of Object.values(sections || {})) {
      for (const line of String(content).split("\n")) {
        const text = line.trim();
        if (text) lines.push(text);
      }
    }
    for (let i = 0; i < lines.length - 1; i += 1) {
      const label = lines[i];
      const value = lines[i + 1];
      if (!NOTE_STAT_LABELS.has(label) || found[label]) continue;
      if (value.length > 40 || NOTE_STAT_LABELS.has(value)) continue;
      found[label] = value;
    }
    return found;
  }

  function buildNoteStats(d) {
    const items = [];
    const seenKeys = new Set();
    const push = (k, v) => {
      if (v == null || v === "" || v === 0 || seenKeys.has(k)) return;
      seenKeys.add(k);
      items.push({ k, v: String(v) });
    };

    push("元素", d.element);
    push("武器", d.weapon_type && d.weapon_type !== "名称" ? d.weapon_type : "");
    push("命之座", d.constellation);
    push("生日", d.birthday);
    push("所属", d.affiliation);
    push("定位", d.role);
    push("称号", d.title_alias);
    if (d.base_atk) push("基础攻击", d.base_atk);
    if (d.sub_stat) push(d.sub_stat, d.sub_stat_value);

    const derived = noteStatsFromSections(d.sections);
    const ELEMENTS = new Set(["冰", "火", "水", "雷", "风", "岩", "草", "物理"]);
    const okElement = (v) => Boolean(v) && ELEMENTS.has(v);
    const okBirthday = (v) => Boolean(v) && /^\d{1,2}月\d{1,2}日$/.test(v);
    const okShort = (v) => Boolean(v) && v.length <= 14 && !/[。！？]$/.test(v);

    if (okElement(derived["元素"]) || okElement(derived["神之眼"])) {
      push("元素", derived["元素"] || derived["神之眼"]);
    }
    if (okShort(derived["武器类型"])) push("武器", derived["武器类型"]);
    if (okShort(derived["命之座"])) push("命之座", derived["命之座"]);
    if (okBirthday(derived["生日"])) push("生日", derived["生日"]);
    if (okShort(derived["所属"])) push("所属", derived["所属"]);
    if (okShort(derived["定位"])) push("定位", derived["定位"]);
    if (okShort(derived["称号"])) push("称号", derived["称号"]);
    if (okShort(derived["职业"])) push("职业", derived["职业"]);
    if (okShort(derived["位置"])) push("位置", derived["位置"]);

    if (!items.length) return null;
    return el("div", { class: "note-stats" }, items.slice(0, 8).map((item) =>
      el("div", { class: "note-stat" }, [
        el("div", { class: "note-stat-k", text: item.k }),
        el("div", { class: "note-stat-v", text: item.v }),
      ])
    ));
  }

  const NOTE_QUANTITY = /^\*(\d[\d,]*)$/;

  function isShortLine(text) {
    return text.length <= 10 && !/[。，；：！？、]/.test(text);
  }

  function renderNoteLines(lines) {
    const nodes = [];
    let i = 0;
    while (i < lines.length) {
      const cur = lines[i];
      const next = lines[i + 1];

      // 数量行：坚牢黄玉碎屑 + *9 → 一行
      if (next && NOTE_QUANTITY.test(next)) {
        nodes.push(el("div", { class: "note-kv" }, [
          el("span", { class: "note-kv-k", text: cur }),
          el("span", { class: "note-kv-v", text: `×${NOTE_QUANTITY.exec(next)[1]}` }),
        ]));
        i += 2;
        continue;
      }

      // 字段行：生日 + 12月31日
      if (isFieldLabel(cur) && next && next.length <= 60 && !isFieldLabel(next)) {
        nodes.push(el("div", { class: "note-kv" }, [
          el("span", { class: "note-kv-k", text: cur }),
          el("span", { class: "note-kv-v", text: next }),
        ]));
        i += 2;
        continue;
      }

      // 连续短词（至少 4 个、都不是纯数字）→ 一排标签
      const chipLike = (t) => Boolean(t) && t.length <= 8 && !NOTE_QUANTITY.test(t)
        && !/^\d+(\.\d+)?%?$/.test(t);
      if (chipLike(cur) && chipLike(lines[i + 1] || "") && chipLike(lines[i + 2] || "")
          && chipLike(lines[i + 3] || "")) {
        const run = [];
        while (i < lines.length && chipLike(lines[i])) {
          run.push(lines[i]);
          i += 1;
        }
        nodes.push(el("div", { class: "note-chips" }, run.map((t) =>
          el("span", { class: "note-key", text: t }))));
        continue;
      }

      nodes.push(noteLineNode(cur));
      i += 1;
    }
    return nodes;
  }

  function noteLineNode(text) {
    if (/^[「“"]/.test(text) || (/「.+」/.test(text) && text.length <= 90)) {
      return el("blockquote", { class: "note-quote", text });
    }
    return el("p", { class: "note-p", text });
  }

  function buildNoteCard(heading, content, title, index) {
    const clean = heading.replace(/\s*\(\d+\)$/, "");
    const lines = String(content)
      .split("\n")
      .map((s) => s.trim())
      .filter((s) => s && s !== clean && s !== title);
    if (!lines.length) return null;

    const body = el("div", { class: "note-card-body" });
    renderNoteLines(lines).forEach((node) => body.append(node));

    const card = el("section", { class: "note-card", id: `note-sec-${index}` }, [
      el("h2", { class: "note-card-title" }, [
        el("span", { class: "note-card-emoji", text: sectionEmoji(clean) }),
        el("span", { text: clean || "正文" }),
      ]),
      body,
    ]);

    if (content.length > 600) {
      body.classList.add("clipped");
      const more = el("button", {
        class: "note-more",
        onclick: () => {
          const clipped = body.classList.toggle("clipped");
          more.textContent = clipped ? "展开全部" : "收起";
        },
      }, ["展开全部"]);
      card.append(more);
    }
    return card;
  }

  function buildNoteTags(d) {
    const tags = [];
    const add = (t) => {
      if (t && !tags.includes(t)) tags.push(t);
    };
    add(d.category);
    if (d.element) add(`${d.element}元素`);
    if (d.weapon_type && d.weapon_type !== "名称") add(d.weapon_type);
    if (d.rarity) add(`${d.rarity}星`);
    add(d.affiliation);
    for (const rel of (d._links && d._links.related) || []) {
      add(rel.category);
      if (tags.length >= 8) break;
    }
    if (!tags.length) return null;
    return el("div", { class: "note-tags" }, tags.slice(0, 8).map((t) =>
      el("span", { class: "note-tag", text: `#${t}` })));
  }

  function buildNoteBody(d, title) {
    const wrap = el("div", { class: "note-body" });
    const sections = d.sections || {};

    // AI 梗概卡（如果这个词条生成过）
    if (d._note && (d._note.hook || (d._note.highlights || []).length)) {
      const highlights = d._note.highlights || [];
      wrap.append(el("section", { class: "note-brief" }, [
        el("div", { class: "note-brief-head" }, [
          el("span", { class: "note-brief-tag", text: "AI 梗概" }),
          d._note.title ? el("span", { class: "note-brief-title", text: d._note.title }) : null,
        ].filter(Boolean)),
        d._note.hook ? el("p", { class: "note-brief-hook", text: d._note.hook }) : null,
        highlights.length
          ? el("ul", { class: "note-brief-list" }, highlights.map((h) => el("li", { text: h })))
          : null,
        (d._note.tags || []).length
          ? el("div", { class: "note-brief-tags" }, d._note.tags.map((t) => el("span", { text: `#${t}` })))
          : null,
      ].filter(Boolean)));
    }

    const summary = pickNoteSummary(sections, title);
    const stats = buildNoteStats(d);
    if (summary) {
      wrap.append(el("div", { class: "note-summary" }, [
        el("span", { class: "note-summary-label", text: "一句话" }),
        el("span", { text: summary }),
      ]));
    } else if (stats) {
      // 没有可用的散文段落时，用结构化字段拼一句速览
      const bits = [];
      if (d.affiliation) bits.push(d.affiliation);
      if (d.element) bits.push(`${d.element}元素`);
      if (d.weapon_type && d.weapon_type !== "名称") bits.push(d.weapon_type);
      if (d.rarity) bits.push(`${d.rarity}星`);
      if (d.constellation) bits.push(`命之座「${d.constellation}」`);
      if (bits.length) {
        wrap.append(el("div", { class: "note-summary" }, [
          el("span", { class: "note-summary-label", text: "速览" }),
          el("span", { text: bits.join(" · ") }),
        ]));
      }
    }
    if (stats) wrap.append(stats);

    // 章节速览
    const cards = [];
    let index = 0;
    for (const [heading, content] of Object.entries(sections)) {
      if (!content || !content.trim()) continue;
      if (/^(页签\d*|_intro)$/.test(heading)) continue;
      if (heading === title && content.trim() === title) continue;
      // 基础信息已经用顶部数据卡展示，不再重复一整屏
      if (stats && /^(基础信息|基本信息)$/.test(heading.replace(/\s*\(\d+\)$/, ""))) continue;
      index += 1;
      const card = buildNoteCard(heading, content, title, index);
      if (card) cards.push(card);
    }

    if (cards.length > 2) {
      const jumpItems = cards.slice(0, 10);
      wrap.append(el("div", { class: "note-jump" }, jumpItems.map((card, i) =>
        el("a", {
          href: `#${card.id}`,
          onclick: (e) => {
            e.preventDefault();
            document.getElementById(card.id)?.scrollIntoView({ behavior: "smooth", block: "start" });
          },
        }, [`${i + 1}. ${card.querySelector(".note-card-title span:last-child").textContent}`])
      )));
    }
    wrap.append(...cards);

    if ((d.tables || []).length) {
      const tableBox = el("div", { class: "note-card note-tables" }, [
        el("h2", { class: "note-card-title" }, [
          el("span", { class: "note-card-emoji", text: "📋" }),
          el("span", { text: `数据表 · ${d.tables.length}` }),
        ]),
      ]);
      const holder = el("div", { class: "note-card-body clipped" });
      d.tables.forEach((rows) => {
        const table = buildTable(rows);
        if (table) holder.append(table);
      });
      tableBox.append(holder, el("button", {
        class: "note-more",
        onclick: () => {
          const clipped = holder.classList.toggle("clipped");
          tableBox.querySelector(".note-more").textContent = clipped ? "展开全部" : "收起";
        },
      }, ["展开全部"]));
      wrap.append(tableBox);
    }

    if ((d.images || []).length) {
      wrap.append(el("section", { class: "note-card" }, [
        el("h2", { class: "note-card-title" }, [
          el("span", { class: "note-card-emoji", text: "🖼️" }),
          el("span", { text: `图集 · ${d.images.length}` }),
        ]),
        el("div", { class: "note-gallery" }, d.images.slice(0, 12).map((img) =>
          el("a", { href: img.src, target: "_blank", rel: "noreferrer" }, [
            el("img", { src: img.src, alt: img.alt || "", loading: "lazy", onerror: (e) => e.target.closest("a").remove() }),
          ])
        )),
      ]));
    }

    const tags = buildNoteTags(d);
    if (tags) wrap.append(tags);

    linkifyMentions(wrap, d._links && d._links.mentions);
    return wrap;
  }

  function buildEntryHead(d, title, id) {
    const mode = state.readMode;
    const toggle = el("div", { class: "mode-switch" }, [
      el("button", {
        class: mode === "card" ? "on" : "",
        onclick: () => {
          state.readMode = "card";
          localStorage.setItem("wiki.readMode", "card");
          renderEntry(id);
        },
      }, ["卡片"]),
      el("button", {
        class: mode === "classic" ? "on" : "",
        onclick: () => {
          state.readMode = "classic";
          localStorage.setItem("wiki.readMode", "classic");
          renderEntry(id);
        },
      }, ["原文"]),
    ]);

    return el("header", { class: "article-head" }, [
      el("div", { class: "crumbs" }, [
        el("a", { href: "#/" }, ["首页"]),
        el("span", { class: "sep", text: "/" }),
        el("a", { href: `#/c/${encodeURIComponent(d.category || "")}`, text: d.category || "未分类" }),
        el("span", { class: "sep", text: "/" }),
        el("span", { text: String(id) }),
      ]),
      mode === "card"
        ? el("div", { class: "note-eyebrow" }, [
            el("span", { class: "note-emoji", text: categoryEmoji(d.category) }),
            el("span", { text: d.category || "词条" }),
            d.rarity ? el("span", { class: "note-stars", text: "★".repeat(d.rarity) }) : null,
          ].filter(Boolean))
        : null,
      el("h1", { class: "article-title", text: title }),
      el("div", { class: "article-meta" }, metaBadges(d)),
      el("div", { class: "article-actions" }, [
        toggle,
        d.url
          ? el("a", { class: "btn small", href: d.url, target: "_blank", rel: "noreferrer" }, ["原站页面 ↗"])
          : null,
        el("button", {
          class: "btn small",
          onclick: () => navigate(`#/chat?ask=${encodeURIComponent(`把《${title}》写成小红书风格的资料笔记：标题 + 3 个看点 + 关键数据 + 原文出处（标 id）`)}`),
        }, ["生成笔记"]),
        el("button", {
          class: "btn small",
          onclick: () => updateCurrentEntry(id),
        }, ["更新此条"]),
        el("button", {
          class: "btn small",
          onclick: async () => {
            try {
              await navigator.clipboard.writeText(location.href);
              toast("链接已复制");
            } catch {
              toast(location.href);
            }
          },
        }, ["复制链接"]),
      ]),
    ]);
  }

  async function renderEntry(id) {
    const d = await api(`/api/entry/${id}`);
    const title = d.title || d._index_title || String(id);
    if (!state.readMode) state.readMode = localStorage.getItem("wiki.readMode") || "card";
    const head = buildEntryHead(d, title, id);
    const mode = state.readMode;

    const infobox = buildInfobox(d);
    const body = el("div", { class: "article-body" });
    const tocLinks = [];

    const sections = d.sections || {};
    let secIndex = 0;
    for (const [heading, content] of Object.entries(sections)) {
      if (!content || !content.trim()) continue;
      const cleanHeading = heading.replace(/\s*\(\d+\)$/, "");
      if (heading === title && content.trim() === title) continue;
      if (/^(页签\d*|_intro)$/.test(cleanHeading.trim())) continue;
      secIndex += 1;
      const anchor = `sec-${secIndex}`;
      const section = el("section", { id: anchor });
      section.append(el("h2", { text: cleanHeading || "正文" }));
      appendSectionContent(section, content, cleanHeading);
      body.append(section);
      tocLinks.push({ anchor, label: cleanHeading || "正文" });
    }

    for (const block of structuredBlocks(d)) {
      secIndex += 1;
      const anchor = `sec-${secIndex}`;
      const section = el("section", { id: anchor }, [el("h2", { text: block.title })]);
      for (const node of block.nodes) section.append(node);
      body.append(section);
      tocLinks.push({ anchor, label: block.title });
    }

    if ((d.tables || []).length) {
      secIndex += 1;
      const anchor = `sec-${secIndex}`;
      const section = el("section", { id: anchor }, [el("h2", { text: "数据表" })]);
      d.tables.forEach((rows) => {
        if (!Array.isArray(rows) || !rows.length) return;
        section.append(buildTable(rows));
      });
      body.append(section);
      tocLinks.push({ anchor, label: "数据表" });
    }

    if ((d.images || []).length) {
      secIndex += 1;
      const anchor = `sec-${secIndex}`;
      const grid = el("div", { class: "figure-grid" }, d.images.map((img) =>
        el("a", { class: "figure", href: img.src, target: "_blank", rel: "noreferrer", title: img.alt || "" }, [
          el("img", { src: img.src, alt: img.alt || "", loading: "lazy", onerror: (e) => e.target.closest("a").remove() }),
        ])
      ));
      const section = el("section", { id: anchor }, [el("h2", { text: `图集 · ${d.images.length}` }), grid]);
      body.append(section);
      tocLinks.push({ anchor, label: "图集" });
    }

    const pager = el("nav", { class: "pager" }, [
      d._prev
        ? el("a", { href: `#/e/${d._prev.id}` }, [
            el("div", { class: "dir", text: "上一篇" }),
            el("div", { class: "name", text: d._prev.title }),
          ])
        : el("span"),
      d._next
        ? el("a", { class: "right", href: `#/e/${d._next.id}` }, [
            el("div", { class: "dir", text: "下一篇" }),
            el("div", { class: "name", text: d._next.title }),
          ])
        : el("span"),
    ]);

    const toc = el("aside", { class: "toc" }, [
      el("div", { class: "toc-title", text: "目录" }),
      ...(() => {
        const seen = new Map();
        return tocLinks.map((t) => {
          const n = (seen.get(t.label) || 0) + 1;
          seen.set(t.label, n);
          const label = n > 1 ? `${t.label} ·${n}` : t.label;
          return el("a", { href: `#${t.anchor}`, "data-anchor": t.anchor, text: label });
        });
      })(),
    ]);

    linkifyMentions(body, d._links && d._links.mentions);
    const related = relatedPanel(d._links);

    const noteBody = mode === "card" ? buildNoteBody(d, title) : null;

    const article = mode === "card"
      ? el("article", { class: "article note" }, [head, noteBody, related, pager])
      : el("article", { class: "article" }, [head, infobox, body, related, pager]);
    pushHistory({ id, title, category: d.category });

    main.replaceChildren(
      el("div", { class: "page" + (mode === "card" ? " note-page" : "") }, [
        mode === "card"
          ? article
          : el("div", { class: "article-layout" }, [article, toc]),
      ])
    );

    document.title = `${title} · 观测枢本地资料库`;
    main.scrollTop = 0;
    window.scrollTo(0, 0);
    setupTocSpy(toc);
    highlightQuery();
  }

  // 站点模板里明确的字段名（白名单）。只有这些短行才和下一行组成「键：值」，
  // 否则短行会被误配（例如角色名后面紧跟「生日」）。
  const FIELD_LABELS = new Set([
    "生日", "所属", "神之眼", "武器类型", "命之座", "称号", "稀有度", "定位", "攻击方式",
    "暴击率", "生命值", "暴击伤害", "攻击力", "防御力", "元素精通", "元素充能效率",
    "基础攻击力", "装备类型", "获得方式", "获取方式", "突破材料", "等级限制", "特殊限制",
    "前置任务", "后续任务", "触发条件", "初始NPC", "结束NPC", "起始NPC", "性别", "职业",
    "位置", "地图说明", "用途", "物品描述", "一级分类", "二级分类", "摆放区域", "洞天仙力",
    "截图", "编者注", "所属区域", "上线时间", "版本", "相关任务", "相关套装", "成就集",
    "功能", "作曲", "编曲", "摆设类型", "类别", "品质", "价格", "库存", "显示名称",
  ]);
  const FIELD_LABELS_CI = new Set([...FIELD_LABELS].map((s) => s.toUpperCase()));
  const isFieldLabel = (text) => FIELD_LABELS.has(text) || FIELD_LABELS_CI.has(text.toUpperCase());

  function dedupeRepeatedBlock(lines) {
    // 站点模板会把同一段信息渲染两遍（PC/移动端），这里把整段重复折掉
    const n = lines.length;
    for (const parts of [2, 3]) {
      if (n % parts !== 0) continue;
      const size = n / parts;
      let same = true;
      for (let i = 0; i < size && same; i += 1) {
        if (lines[i] !== lines[size + i]) same = false;
      }
      if (same) return lines.slice(0, size);
    }
    return lines;
  }

  function appendSectionContent(section, content, heading) {
    const rawLines = content
      .split("\n")
      .map((s) => s.trim())
      .filter((s) => s && s !== heading);
    const lines = dedupeRepeatedBlock(rawLines);

    const isKeyLine = (text, next) =>
      isFieldLabel(text) &&
      next &&
      next.length >= 1 &&
      next.length <= 60 &&
      !isFieldLabel(next);

    // 站点的材料/属性表把数量拆成独立一行（"坚牢黄玉碎屑" + "*9"），合并成 "×9"
    const QUANTITY = /^\*(\d[\d,]*)$/;
    // 连续多条短语（掉落物、标签、标签式清单）排成标签墙，而不是逐行堆叠
    const isShortPhrase = (t) => t && t.length <= 14 && !/[。，；：！？、]/.test(t);

    let i = 0;
    while (i < lines.length) {
      const cur = lines[i];
      const next = lines[i + 1];
      const quantity = next ? QUANTITY.exec(next) : null;

      if (quantity) {
        section.append(
          el("div", { class: "kv-row" }, [
            el("span", { class: "kv-k", text: cur }),
            el("span", { class: "kv-v", text: `×${quantity[1]}` }),
          ])
        );
        i += 2;
        continue;
      }

      if (isKeyLine(cur, next)) {
        section.append(
          el("div", { class: "kv-row" }, [
            el("span", { class: "kv-k", text: cur }),
            el("span", { class: "kv-v", text: next }),
          ])
        );
        i += 2;
        continue;
      }

      // 连续短句 → 标签墙
      if (isShortPhrase(cur) && isShortPhrase(lines[i + 1]) && isShortPhrase(lines[i + 2])) {
        const run = [];
        while (i < lines.length && isShortPhrase(lines[i]) && !QUANTITY.test(lines[i])) {
          run.push(lines[i]);
          i += 1;
        }
        section.append(el("div", { class: "tag-wall" }, run.map((t) => el("span", { class: "tag", text: t }))));
        continue;
      }

      section.append(el("p", { text: cur }));
      i += 1;
    }
  }

  function metaBadges(d) {
    const out = [];
    if (d.rarity) out.push(el("span", { class: "badge gold" }, [el("span", { class: "stars", text: "★".repeat(d.rarity) })]));
    if (d.category) out.push(el("span", { class: "badge", text: d.category }));
    out.push(el("span", { class: "badge", text: `${fmt((d.raw_text || "").length)} 字` }));
    if ((d.sections ? Object.keys(d.sections).length : 0)) {
      out.push(el("span", { class: "badge", text: `${Object.keys(d.sections).length} 节` }));
    }
    if (d.api_kind) out.push(el("span", { class: "badge", text: d.api_kind === "new" ? "新模型" : d.api_kind === "old" ? "旧模型" : "渲染" }));
    const meta = d.api_meta || {};
    if (meta.author) out.push(el("span", { class: "badge", text: `作者 ${meta.author}` }));
    if (meta.mtime) out.push(el("span", { class: "badge", text: String(meta.mtime).slice(0, 10) }));
    return out;
  }

  function buildInfobox(d) {
    const cells = [];
    const push = (k, v, wide = false) => {
      if (v == null || v === "" || v === 0) return;
      cells.push(el("div", { class: "infobox-cell" + (wide ? " wide" : "") }, [
        el("div", { class: "k", text: k }),
        el("div", { class: "v", text: String(v) }),
      ]));
    };
    push("元素", d.element);
    push("武器类型", d.weapon_type && d.weapon_type !== "名称" ? d.weapon_type : "");
    push("命之座", d.constellation);
    push("生日", d.birthday);
    push("所属", d.affiliation);
    push("称号", d.title_alias);
    push("定位", d.role);
    if (d.base_atk) push("基础攻击力", d.base_atk);
    if (d.sub_stat) push(d.sub_stat, d.sub_stat_value);
    if (d.effect_name) push("武器特效", d.effect_name);
    if (d.effect_desc) push("特效说明", d.effect_desc, true);
    if (d.set_2pc) push("2 件套", d.set_2pc, true);
    if (d.set_4pc) push("4 件套", d.set_4pc, true);
    if (!cells.length) return null;
    return el("div", { class: "infobox" }, [
      el("div", { class: "infobox-head", text: "基本信息" }),
      el("div", { class: "infobox-grid" }, cells),
    ]);
  }

  function structuredBlocks(d) {
    const blocks = [];

    if ((d.pieces || []).length) {
      const nodes = d.pieces.map((piece) =>
        el("div", { style: "margin-bottom:14px" }, [
          el("div", { class: "chip", text: piece.name || "" }),
          el("p", { style: "margin:8px 0 0", text: piece.description || "" }),
        ])
      );
      blocks.push({ title: "圣遗物部件", nodes });
    }

    if ((d.constellations || []).length) {
      const nodes = d.constellations.map((c, i) =>
        el("div", { style: "margin-bottom:12px" }, [
          el("div", { style: "font-weight:600;font-family:var(--font-ui);font-size:14px" }, [`${i + 1}. ${c.name || ""}`]),
          el("p", { style: "margin:4px 0 0", text: c.description || "" }),
        ])
      );
      blocks.push({ title: "命之座", nodes });
    }

    if ((d.talents || []).length) {
      const nodes = d.talents.map((rows) => buildTable(rows)).filter(Boolean);
      if (nodes.length) blocks.push({ title: "天赋数据", nodes });
    }

    if ((d.ascension_materials || []).length) {
      const nodes = [buildTable(d.ascension_materials)].filter(Boolean);
      if (nodes.length) blocks.push({ title: "突破材料", nodes });
    }

    if (d.cv_voice && Object.keys(d.cv_voice).length) {
      const nodes = Object.entries(d.cv_voice).map(([k, v]) =>
        el("div", { style: "margin-bottom:10px" }, [
          el("div", { style: "font-family:var(--font-ui);font-size:12.5px;color:var(--text-faint)" }, [k || "语音"]),
          el("p", { style: "margin:2px 0 0", text: v }),
        ])
      );
      blocks.push({ title: "语音台词", nodes });
    }

    return blocks;
  }

  function buildTable(rows) {
    if (!Array.isArray(rows) || !rows.length) return null;
    const table = el("table", { class: "data-table" });
    const body = el("tbody");
    const headerRow = rows[0];
    const useHeader =
      rows.length > 1 &&
      headerRow.length > 1 &&
      headerRow.every((c) => typeof c === "string" && c.length > 0 && c.length <= 12);
    const start = useHeader ? 1 : 0;
    if (useHeader) {
      table.append(el("thead", {}, [el("tr", {}, headerRow.map((c) => el("th", { text: String(c) })))]));
    }
    for (let i = start; i < rows.length; i += 1) {
      const cells = rows[i];
      if (!Array.isArray(cells)) continue;
      body.append(el("tr", {}, cells.map((c) => el("td", { text: String(c ?? "") }))));
    }
    table.append(body);
    return el("div", { class: "table-scroll" }, [table]);
  }

  function setupTocSpy(toc) {
    const links = [...toc.querySelectorAll("a")];
    if (!links.length) return;
    const sections = links
      .map((a) => document.getElementById(a.dataset.anchor))
      .filter(Boolean);
    if (!sections.length) return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          for (const a of links) a.classList.toggle("active", a.dataset.anchor === entry.target.id);
          break;
        }
      },
      { root: main, rootMargin: "-10% 0px -70% 0px", threshold: 0 }
    );
    sections.forEach((s) => observer.observe(s));
    for (const a of links) {
      a.addEventListener("click", (e) => {
        e.preventDefault();
        document.getElementById(a.dataset.anchor)?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }
  }

  // ---------------------------------------------------------------- 资料库助手（DeepSeek）
  const CHAT_KEY = "wiki.chat";
  const chatState = { messages: [], streaming: false, config: null, logEl: null };

  function loadChat() {
    try {
      chatState.messages = JSON.parse(localStorage.getItem(CHAT_KEY) || "[]");
      if (!Array.isArray(chatState.messages)) chatState.messages = [];
    } catch {
      chatState.messages = [];
    }
  }

  function saveChat() {
    try {
      localStorage.setItem(CHAT_KEY, JSON.stringify(chatState.messages.slice(-60)));
    } catch { /* 隐私模式 */ }
  }

  function escapeHtml(text) {
    return String(text ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function inlineMd(text) {
    return text
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\bid[=＝](\d{2,7})/g, (m, id) => `<a class="id-link" href="#/e/${id}">${id}</a>`);
  }

  function renderRich(text) {
    const out = [];
    let list = [];
    const flush = () => {
      if (list.length) {
        out.push("<ul>" + list.map((item) => `<li>${item}</li>`).join("") + "</ul>");
        list = [];
      }
    };
    for (const raw of String(text || "").split("\n")) {
      const safe = escapeHtml(raw);
      const bullet = raw.match(/^\s*[-*]\s+(.*)$/);
      if (bullet) {
        list.push(inlineMd(escapeHtml(bullet[1])));
        continue;
      }
      flush();
      if (!raw.trim()) {
        out.push('<p class="gap"></p>');
      } else if (/^#{1,4}\s+/.test(raw)) {
        out.push(`<h4>${inlineMd(safe.replace(/^#{1,4}\s+/, ""))}</h4>`);
      } else if (/^\s*>\s?/.test(raw)) {
        out.push(`<blockquote>${inlineMd(safe.replace(/^\s*>\s?/, ""))}</blockquote>`);
      } else {
        out.push(`<p>${inlineMd(safe)}</p>`);
      }
    }
    flush();
    return out.join("");
  }

  function chatMessageNode(msg) {
    const isUser = msg.role === "user";
    const isSummary = msg.role === "summary";
    const body = el("div", { class: "chat-body" });
    if (isUser) body.append(el("p", { text: msg.text }));
    else body.innerHTML = renderRich(msg.text);

    const node = el("div", { class: `chat-msg ${isUser ? "user" : isSummary ? "summary" : "bot"}` }, [
      el("div", { class: "chat-avatar", text: isUser ? "你" : isSummary ? "摘" : "AI" }),
      el("div", { class: "chat-main" }, [
        el("div", { class: "chat-name", text: isUser ? "你" : isSummary ? "对话摘要" : "资料库助手" }),
        body,
        (msg.tools || []).length
          ? el("div", { class: "chat-tools-run" }, msg.tools.map((t) =>
              el("span", { class: "tool-chip" }, [`🔧 ${t.name} · ${t.summary}`])))
          : null,
        msg.error ? el("div", { class: "chat-error", text: msg.error }) : null,
      ].filter(Boolean)),
    ]);
    if (isSummary) {
      node.append(el("button", {
        class: "icon-btn chat-copy",
        title: "复制摘要",
        onclick: () => navigator.clipboard?.writeText(msg.text || "").then(() => toast("摘要已复制")),
      }, ["复制"]));
    }
    return node;
  }

  function renderChatLog() {
    const log = chatState.logEl;
    if (!log) return;
    log.replaceChildren(
      ...(chatState.messages.length ? chatState.messages.map(chatMessageNode) : [chatEmptyHint()])
    );
    log.scrollTop = log.scrollHeight;
  }

  function chatEmptyHint() {
    const suggestions = [
      "钟离的语音台词里提到过哪些神？",
      "整理一下西风骑士团的成员与职务",
      "《空月之歌》第九幕讲了什么？",
      "稻妻的雷电将军有哪些相关词条？",
    ];
    return el("div", { class: "chat-empty" }, [
      el("div", { class: "chat-empty-title", text: "问点什么，我会先查资料库再回答" }),
      el("div", { class: "chat-empty-sub", text: "检索用的是本地 1.4 万条词条（与 MCP 同一套数据），回答会标注条目 id" }),
      el("div", { class: "chat-sugs" }, suggestions.map((s) =>
        el("button", {
          class: "chip suggestion",
          onclick: () => {
            const input = $("#chatInput");
            if (input) {
              input.value = s;
              input.focus();
            }
          },
        }, [s])
      )),
    ]);
  }

  function chatHeader() {
    const cfg = chatState.config || {};
    const modelSelect = el("select", { class: "chat-select", id: "chatModel" },
      (cfg.models || [{ id: "deepseek-chat", label: "deepseek-chat" }]).map((m) =>
        el("option", { value: m.id, selected: m.id === (cfg.model || "deepseek-chat") ? "selected" : null }, [m.label])
      )
    );
    modelSelect.addEventListener("change", async () => {
      try {
        chatState.config = await postJson("/api/chat/config", { model: modelSelect.value });
        toast("模型已切换");
      } catch (err) {
        toast("切换失败：" + err.message);
      }
    });

    const settings = el("div", { class: "chat-settings", hidden: true }, [
      el("div", { class: "chat-settings-row" }, [
        el("label", { text: "DeepSeek API Key" }),
        el("input", {
          id: "chatKey",
          type: "password",
          placeholder: cfg.has_key
            ? `已配置（来源：${cfg.key_source || "本机"}，留空不变）`
            : "sk-…（留空则使用本机 DSH 凭据）",
          autocomplete: "off",
        }),
      ]),
      el("div", { class: "chat-settings-row" }, [
        el("label", { text: "模型" }),
        el("input", { id: "chatModelInput", type: "text", value: cfg.model || "deepseek-flash" }),
      ]),
      el("div", { class: "chat-settings-row" }, [
        el("label", { text: "接口地址" }),
        el("input", { id: "chatBase", type: "text", value: cfg.base_url || "https://api.deepseek.com" }),
      ]),
      el("div", { class: "chat-settings-actions" }, [
        el("button", {
          class: "btn primary",
          onclick: async () => {
            const key = $("#chatKey").value.trim();
            const base = $("#chatBase").value.trim();
            const model = $("#chatModelInput")?.value.trim();
            try {
              chatState.config = await postJson("/api/chat/config", {
                api_key: key || undefined,
                base_url: base || undefined,
                model: model || undefined,
              });
              settings.hidden = true;
              renderChatView();
              toast(chatState.config.has_key ? `已保存（${chatState.config.model}）` : "已保存（还没有 key）");
            } catch (err) {
              toast("保存失败：" + err.message);
            }
          },
        }, ["保存"]),
        cfg.has_key
          ? el("button", {
              class: "btn",
              onclick: async () => {
                chatState.config = await postJson("/api/chat/config", { clear_key: true });
                settings.hidden = true;
                renderChatView();
                toast("已清除本地 key");
              },
            }, ["清除 key"])
          : null,
        el("span", { class: "chat-settings-hint", text: "key 只写在本机 data/llm_config.json" }),
      ].filter(Boolean)),
    ]);

    return el("div", { class: "chat-head" }, [
      el("div", { class: "chat-head-left" }, [
        el("h1", { class: "page-title", text: "资料库助手" }),
        el("div", { class: "page-sub", text: "DeepSeek + 本地检索：先查 1.4 万条词条，再作答" }),
      ]),
      el("div", { class: "chat-head-actions" }, [
        modelSelect,
        el("button", { class: "icon-btn", title: "API 设置", onclick: () => { settings.hidden = !settings.hidden; } }, ["设置"]),
        el("button", { class: "icon-btn", title: "把当前对话压缩成要点", onclick: () => summarizeChat() }, ["总结对话"]),
        el("button", { class: "icon-btn", title: "清空对话", onclick: () => { chatState.messages = []; saveChat(); renderChatLog(); } }, ["新对话"]),
      ]),
      settings,
    ]);
  }

  function renderChatView() {
    const log = el("div", { class: "chat-log", id: "chatLog" });
    const input = el("textarea", {
      id: "chatInput",
      class: "chat-textarea",
      rows: 2,
      placeholder: "问角色、剧情、台词、材料…（Enter 发送 / Shift+Enter 换行）",
    });
    const form = el("form", {
      class: "chat-form",
      onsubmit: (e) => {
        e.preventDefault();
        const text = input.value.trim();
        if (!text) return;
        input.value = "";
        sendChat(text);
      },
    }, [input, el("button", { class: "btn primary chat-send", type: "submit" }, ["发送"])]);

    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        form.requestSubmit();
      }
    });

    main.replaceChildren(el("div", { class: "page chat-page" }, [chatHeader(), log, form]));
    chatState.logEl = log;
    renderChatLog();
    setTimeout(() => input.focus(), 30);
    document.title = "资料库助手 · 观测枢本地资料库";
  }

  async function renderChat(seedQuestion) {
    loadChat();
    if (!chatState.config) {
      try {
        chatState.config = await api("/api/chat/config");
      } catch {
        chatState.config = null;
      }
    }
    renderChatView();
    if (seedQuestion) {
      const input = $("#chatInput");
      if (input) {
        input.value = seedQuestion;
        input.focus();
      }
    }
  }

  async function postJson(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }

  async function sendChat(text) {
    if (chatState.streaming) return;
    if (!chatState.config || !chatState.config.has_key) {
      toast("先在「设置」里填入 DeepSeek API Key");
      document.querySelector(".chat-head-actions .icon-btn")?.click();
      return;
    }
    chatState.streaming = true;
    const assistant = { role: "assistant", text: "", tools: [], status: "", startedAt: Date.now() };
    const history = chatState.messages
      .filter((m) => m.role === "user" || (m.role === "assistant" && m.text))
      .map((m) => ({ role: m.role, content: m.text }))
      .slice(-12);
    chatState.messages.push({ role: "user", text });
    chatState.messages.push(assistant);
    saveChat();
    renderChatLog();
    setChatBusy(true);
    tickChatStatus(assistant);

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: [...history, { role: "user", content: text }] }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || res.statusText);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buffer.indexOf("\n\n")) >= 0) {
          const chunk = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          for (const line of chunk.split("\n")) {
            if (!line.startsWith("data:")) continue;
            let event;
            try {
              event = JSON.parse(line.slice(5).trim());
            } catch {
              continue;
            }
            if (event.type === "delta") {
              assistant.text += event.text || "";
              updateStreamingBubble(assistant);
            } else if (event.type === "status") {
              assistant.status = event.text || "";
              updateStreamingBubble(assistant);
            } else if (event.type === "tool") {
              assistant.tools.push({ name: event.name, summary: event.summary });
              updateStreamingBubble(assistant);
            } else if (event.type === "error") {
              assistant.error = event.message;
              updateStreamingBubble(assistant);
            }
          }
        }
      }
    } catch (err) {
      assistant.error = String(err.message || err);
    } finally {
      chatState.streaming = false;
      clearInterval(chatStatusTimer);
      setChatBusy(false);
      saveChat();
      renderChatLog();
    }
  }

  function updateStreamingBubble(assistant) {
    const log = chatState.logEl;
    if (!log) return;
    const nodes = log.querySelectorAll(".chat-msg.bot");
    const last = nodes[nodes.length - 1];
    if (!last) {
      renderChatLog();
      return;
    }
    const main = last.querySelector(".chat-main");
    const body = last.querySelector(".chat-body");
    if (body) body.innerHTML = renderRich(assistant.text) || (chatState.streaming ? '<p class="typing">▌</p>' : "");

    // 进度状态行（思考 / 执行工具）
    let status = main.querySelector(".chat-status");
    if (chatState.streaming && assistant.status) {
      if (!status) {
        status = el("div", { class: "chat-status" });
        main.insertBefore(status, body);
      }
      status.textContent = assistant.status;
    } else if (status) {
      status.remove();
    }

    // 工具调用 chip
    if (assistant.tools.length) {
      let run = main.querySelector(".chat-tools-run");
      if (!run) {
        run = el("div", { class: "chat-tools-run" });
        main.append(run);
      }
      run.replaceChildren(
        ...assistant.tools.map((t) => el("span", { class: "tool-chip" }, [`🔧 ${t.name} · ${t.summary}`]))
      );
    }

    let errorBox = main.querySelector(".chat-error");
    if (assistant.error) {
      if (!errorBox) {
        errorBox = el("div", { class: "chat-error" });
        main.append(errorBox);
      }
      errorBox.textContent = assistant.error;
    } else if (errorBox) {
      errorBox.remove();
    }

    log.scrollTop = log.scrollHeight;
  }

  function setChatBusy(busy) {
    const btn = document.querySelector(".chat-send");
    if (btn) {
      btn.disabled = busy;
      btn.textContent = busy ? "思考中…" : "发送";
    }
  }

  // 进度状态行上的秒表，让等待过程可见
  let chatStatusTimer = null;

  function tickChatStatus(assistant) {
    clearInterval(chatStatusTimer);
    if (!assistant) return;
    chatStatusTimer = setInterval(() => {
      const node = chatState.logEl?.querySelector(".chat-status");
      if (!node || !assistant.status) return;
      const seconds = Math.round((Date.now() - (assistant.startedAt || Date.now())) / 1000);
      node.textContent = `${assistant.status}（${seconds}s）`;
    }, 500);
  }

  async function summarizeChat() {
    const messages = chatState.messages
      .filter((m) => (m.role === "user" || m.role === "assistant") && m.text)
      .map((m) => ({ role: m.role, content: m.text }));
    if (!messages.length) {
      toast("先聊几句再总结");
      return;
    }
    if (!chatState.config || !chatState.config.has_key) {
      toast("先在「设置」里填入 API Key");
      return;
    }
    toast("正在总结…");
    try {
      const data = await postJson("/api/chat/summary", { messages });
      chatState.messages.push({ role: "summary", text: data.summary || "（空）" });
      saveChat();
      renderChatLog();
    } catch (err) {
      toast("总结失败：" + err.message);
    }
  }

  // ---------------------------------------------------------------- 发现（瀑布流）
  const feedState = { category: "", items: [], offset: 0, total: 0, loading: false, mode: "hot" };

  const FEED_TABS = ["", "__explore", "角色", "武器", "圣遗物", "敌人", "任务", "书籍", "观测考据", "NPC与商店"];
  const FEED_COVERS = ["#f2e6d2", "#e3eef2", "#eee6f2", "#e8f2e6", "#f2e9e2", "#e6ecf5"];

  function feedCard(item) {
    const emoji = categoryEmoji(item.category);
    const note = item.note;
    const cardTitle = note && note.title ? note.title : item.title;
    const cardExcerpt = note && note.hook ? note.hook : item.excerpt;
    const cover = item.cover
      ? el("img", {
          class: "feed-cover-img",
          src: item.cover,
          alt: item.title,
          loading: "lazy",
          onerror: (e) => {
            const holder = e.target.parentElement;
            holder.replaceChildren(el("span", { class: "feed-cover-emoji", text: emoji }),
              el("span", { class: "feed-cover-title", text: item.title.slice(0, 12) }));
            holder.classList.add("placeholder");
          },
        })
      : el("div", { class: "feed-cover placeholder", style: `background:${FEED_COVERS[item.id % FEED_COVERS.length]}` }, [
          el("span", { class: "feed-cover-emoji", text: emoji }),
          el("span", { class: "feed-cover-title", text: item.title.slice(0, 12) }),
        ]);

    return el("a", { class: "feed-card", href: `#/e/${item.id}` }, [
      el("div", { class: "feed-cover" }, [
        cover,
        note ? el("span", { class: "feed-badge", text: "梗概" }) : null,
      ].filter(Boolean)),
      el("div", { class: "feed-body" }, [
        el("div", { class: "feed-title", text: cardTitle }),
        note ? el("div", { class: "feed-origin", text: item.title }) : null,
        cardExcerpt ? el("div", { class: "feed-excerpt", text: cardExcerpt }) : null,
        el("div", { class: "feed-meta" }, [
          el("span", { class: "feed-cat" }, [`${emoji} ${item.category}`]),
          el("span", { class: "feed-heat", text: `🔥 ${fmt(item.heat ? Math.round(item.heat) : 0)}` }),
        ].filter(Boolean)),
      ].filter(Boolean)),
    ]);
  }

  function exploreCard(item) {
    const cover = item.cover
      ? el("img", { class: "feed-cover-img", src: item.cover, alt: item.title, loading: "lazy" })
      : el("div", { class: "feed-cover placeholder", style: `background:${FEED_COVERS[item.id % FEED_COVERS.length]}` }, [
          el("span", { class: "feed-cover-emoji", text: "🔎" }),
          el("span", { class: "feed-cover-title", text: item.title.slice(0, 12) }),
        ]);
    return el("a", { class: "feed-card explore-card", href: item.primary_id ? `#/e/${item.primary_id}` : "#/feed" }, [
      el("div", { class: "feed-cover" }, [
        cover,
        el("span", { class: "feed-badge explore", text: "探索" }),
      ]),
      el("div", { class: "feed-body" }, [
        el("div", { class: "feed-title", text: item.title }),
        item.hook ? el("div", { class: "feed-excerpt", text: item.hook }) : null,
        (item.tags || []).length
          ? el("div", { class: "explore-tags" }, item.tags.slice(0, 3).map((tag) => el("span", { text: `#${tag}` })))
          : null,
        el("div", { class: "feed-meta" }, [
          el("span", { class: "feed-cat", text: item.seed ? `种子 · ${item.seed}` : "自主取材" }),
          el("span", { class: "feed-heat", text: `${(item.sources || []).length} 条来源` }),
        ]),
      ].filter(Boolean)),
    ]);
  }

  async function loadFeed(reset = false) {
    if (feedState.loading) return;
    feedState.loading = true;
    const host = $("#feedGrid");
    if (reset) {
      feedState.items = [];
      feedState.offset = 0;
      if (host) host.replaceChildren();
    }
    const more = $("#feedMore");
    if (more) more.textContent = "加载中…";
    try {
      if (feedState.category === "__explore") {
        const data = await api("/api/explore?limit=40");
        feedState.items = data.items;
        feedState.total = data.items.length;
        if (host) host.append(...data.items.map(exploreCard));
        if (more) more.textContent = data.items.length ? "已显示全部探索笔记" : "还没有探索笔记";
        return;
      }
      const cat = feedState.category ? `&category=${encodeURIComponent(feedState.category)}` : "";
      const data = await api(`/api/feed?size=24&offset=${feedState.offset}${cat}`);
      feedState.items = feedState.items.concat(data.items);
      feedState.offset = feedState.items.length;
      feedState.total = data.total;
      if (host) host.append(...data.items.map(feedCard));
    } finally {
      feedState.loading = false;
      if (more && feedState.category !== "__explore") {
        more.textContent = feedState.items.length >= feedState.total ? "没有更多了" : "加载更多";
      }
    }
  }

  async function renderFeed() {
    feedState.category = state.route && state.route.feedCat ? state.route.feedCat : "";
    const tabs = el("div", { class: "feed-tabs" }, FEED_TABS.map((name) =>
      el("a", {
        href: `#/feed${name ? `?cat=${encodeURIComponent(name)}` : ""}`,
        class: name === feedState.category ? "on" : "",
      }, [name === "__explore" ? "🔎 探索" : name ? `${categoryEmoji(name)} ${name}` : "✨ 推荐"])
    ));

    const grid = el("div", { class: "feed-grid", id: "feedGrid" });
    const more = el("button", { class: "btn", id: "feedMore", onclick: () => loadFeed(false) }, ["加载更多"]);

    main.replaceChildren(
      el("div", { class: "page feed-page" }, [
        el("div", { class: "crumbs" }, [
          el("a", { href: "#/" }, ["首页"]),
          el("span", { class: "sep", text: "/" }),
          el("span", { text: "发现" }),
        ]),
        el("div", { class: "feed-head" }, [
          el("h1", {
            class: "page-title",
            text: feedState.category === "__explore" ? "探索" : (feedState.category || "发现"),
          }),
          el("div", { class: "feed-head-actions" }, [
            el("button", { class: "btn", onclick: () => loadFeed(true) }, ["换一批"]),
          ]),
        ]),
        tabs,
        grid,
        el("div", { class: "load-more" }, [more]),
      ])
    );
    document.title = "发现 · 观测枢本地资料库";
    await loadFeed(true);
  }

  // ---------------------------------------------------------------- 数据管理
  const manageState = { timer: null, lastJob: null, data: null };

  function stopManagePolling() {
    if (manageState.timer) {
      clearInterval(manageState.timer);
      manageState.timer = null;
    }
  }

  async function renderManage() {
    stopManagePolling();
    const data = await api("/api/manage/overview");
    manageState.data = data;

    const card = (k, v, hint) =>
      el("div", { class: "stat-card" }, [
        el("div", { class: "k", text: k }),
        el("div", { class: "v", text: v }),
        hint ? el("div", { class: "stat-hint", text: hint }) : null,
      ].filter(Boolean));

    const catSelect = el("select", { class: "chat-select", id: "manageCat" },
      data.categories.map((c) => el("option", { value: c }, [c])));
    const idInput = el("input", {
      class: "manage-input",
      id: "manageIds",
      placeholder: "按 id 更新，逗号分隔，如 507993, 1290",
    });

    const actionBtn = (label, action, params = {}, primary = false) =>
      el("button", {
        class: "btn" + (primary ? " primary" : ""),
        disabled: data.running ? "disabled" : null,
        onclick: () => runManageJob(action, params),
      }, [label]);

    const categoryBtn = el("button", {
      class: "btn",
      disabled: data.running ? "disabled" : null,
      onclick: () => runManageJob("crawl_category", { category: catSelect.value }),
    }, ["更新该分类"]);

    const idsBtn = el("button", {
      class: "btn",
      disabled: data.running ? "disabled" : null,
      onclick: () => {
        const ids = idInput.value.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean);
        if (!ids.length) {
          toast("先填 id");
          return;
        }
        runManageJob("crawl_ids", { ids });
      },
    }, ["更新这批 id"]);

    const actions = el("div", { class: "manage-actions" }, [
      actionBtn(`更新全部缺口（${fmt(data.missing)} 条）`, "crawl_incomplete", {}, true),
      actionBtn(`重试失败（${fmt(data.failed)} 条）`, "retry_failed"),
      el("div", { class: "manage-inline" }, [catSelect, categoryBtn]),
      el("div", { class: "manage-inline" }, [idInput, idsBtn]),
      actionBtn("重建检索索引", "rebuild_index"),
      actionBtn("重建关联图谱", "rebuild_graph"),
      actionBtn("生成梗概（100 条）", "gen_notes", { limit: 100 }, true),
    ]);

    const recent = el("div", { class: "list" }, data.recent.length
      ? data.recent.map((item) =>
          el("div", { class: "list-row", onclick: () => navigate(`#/e/${item.id}`) }, [
            el("span", { class: "t", text: `#${item.id}` }),
            el("span", { class: "n", text: item.category }),
            el("span", { class: "n", text: item.updated }),
            el("span", { class: "arrow", text: "›" }),
          ]))
      : [el("div", { class: "list-row" }, [el("span", { class: "t", text: "暂无记录" })])]);

    const failedList = el("div", { class: "list" }, data.failed_items.length
      ? data.failed_items.map(([cid, info]) =>
          el("div", { class: "list-row" }, [
            el("span", { class: "t", text: `#${cid}` }),
            el("span", { class: "n", text: String(info.error || "").slice(0, 40) }),
          ]))
      : [el("div", { class: "list-row" }, [el("span", { class: "t", text: "没有失败条目" })])]);

    main.replaceChildren(
      el("div", { class: "page page-narrow" }, [
        el("div", { class: "crumbs" }, [
          el("a", { href: "#/" }, ["首页"]),
          el("span", { class: "sep", text: "/" }),
          el("span", { text: "数据管理" }),
        ]),
        el("h1", { class: "page-title", text: "数据管理" }),
        el("div", { class: "page-sub", text: "更新词条、重建索引与图谱，不用开终端" }),
        data.db_stale
          ? el("div", { class: "manage-alert", text: "⚠️ 检索索引比 index.json 旧，建议点「重建检索索引」" })
          : null,
        el("div", { class: "stat-grid", style: "margin-top:18px" }, [
          card("词条", fmt(data.entries)),
          card("已完成", fmt(data.completed)),
          card("缺口", fmt(data.missing)),
          card("失败", fmt(data.failed)),
          card("索引更新", data.db_updated || "—", `index.json：${data.index_updated || "—"}`),
        ].filter(Boolean)),
        el("div", { class: "section-title" }, ["更新操作"]),
        actions,
        el("div", { class: "manage-job", id: "manageJob", hidden: true }),
        el("div", { class: "section-title" }, ["最近更新"]),
        recent,
        el("div", { class: "section-title" }, ["失败条目"]),
        failedList,
      ].filter(Boolean))
    );

    document.title = "数据管理 · 观测枢本地资料库";
    if (data.running) pollManageJob();
  }

  async function runManageJob(action, params) {
    try {
      await postJson("/api/manage/run", { action, params: params || {} });
      toast("已开始，进度见下方");
      pollManageJob();
    } catch (err) {
      toast("启动失败：" + err.message);
    }
  }

  function pollManageJob() {
    stopManagePolling();
    const panel = $("#manageJob");
    if (panel) panel.hidden = false;
    manageState.timer = setInterval(async () => {
      let job = null;
      try {
        job = (await api("/api/manage/status")).job;
      } catch {
        return;
      }
      if (!job) return;
      manageState.lastJob = job;
      renderJobPanel(job);
      if (job.status !== "running") {
        stopManagePolling();
        toast(job.status === "done" ? "完成" : "失败：" + (job.message || ""));
        if (location.hash.startsWith("#/manage")) setTimeout(() => renderManage(), 500);
      }
    }, 800);
  }

  function renderJobPanel(job) {
    const panel = $("#manageJob");
    if (!panel) return;
    panel.hidden = false;
    const percent = job.total ? Math.min(100, Math.round((job.done / job.total) * 100)) : 0;
    const headText = job.status === "running"
      ? `执行中：${job.action}`
      : job.status === "done" ? `已完成：${job.action}` : `失败：${job.action}`;
    const log = el("pre", { class: "manage-log", text: (job.log || []).slice(-40).join("\n") });
    panel.replaceChildren(
      el("div", { class: "manage-job-head" }, [
        el("span", { class: "chat-status" + (job.status === "running" ? "" : " done"), text: headText }),
        el("span", { class: "muted", text: `${job.elapsed || 0}s` }),
      ]),
      el("div", { class: "manage-bar" }, [
        el("div", {
          class: "manage-bar-fill" + (job.total ? "" : " indeterminate"),
          style: job.total ? `width:${percent}%` : "width:35%",
        }),
      ]),
      el("div", { class: "manage-counts", text: job.total
        ? `进度 ${job.done}/${job.total}（成功 ${job.ok}，失败 ${job.fail}）`
        : (job.message || "处理中…") }),
      log
    );
    log.scrollTop = log.scrollHeight;
  }

  async function updateCurrentEntry(id) {
    try {
      await postJson("/api/manage/run", { action: "crawl_ids", params: { ids: [id] } });
      toast(`正在更新 #${id}，完成后自动刷新`);
      const poll = setInterval(async () => {
        let job = null;
        try {
          job = (await api("/api/manage/status")).job;
        } catch {
          return;
        }
        if (!job || job.status === "running") return;
        clearInterval(poll);
        if (job.status === "done") {
          toast("已更新，重新加载");
          render();
        } else {
          toast("更新失败：" + (job.message || ""));
        }
      }, 800);
    } catch (err) {
      toast("启动失败：" + err.message);
    }
  }

  // ---------------------------------------------------------------- 搜索
  async function renderSearch(q, cat) {
    if (!q.trim()) {
      main.replaceChildren(emptyBox("输入关键词开始搜索", "支持标题与正文，按字索引，任意长度都能命中"));
      return;
    }
    const url = `/api/search?q=${encodeURIComponent(q)}${cat ? `&category=${encodeURIComponent(cat)}` : ""}&limit=60`;
    const data = await api(url);

    const list = data.items.length
      ? el("div", {}, data.items.map((it) =>
          el("a", { class: "result-item", href: `#/e/${it.id}` }, [
            el("div", { class: "result-title", text: it.title }),
            el("div", { class: "result-meta" }, [
              el("span", { class: "badge", text: it.category }),
              el("span", { class: "badge", text: `${fmt(it.chars)} 字` }),
              el("span", { class: "badge", text: it.where === "title" ? "标题命中" : "正文命中" }),
            ]),
            it.snippet ? el("div", { class: "result-snippet", text: it.snippet }) : null,
          ])
        ))
      : emptyBox("没有找到相关内容", "换个关键词试试");

    main.replaceChildren(
      el("div", { class: "page page-narrow" }, [
        el("div", { class: "crumbs" }, [el("a", { href: "#/" }, ["首页"]), el("span", { class: "sep", text: "/" }), el("span", { text: "搜索" })]),
        el("h1", { class: "page-title", text: `“${q}”` }),
        el("div", { class: "page-sub", text: `${data.count} 条结果${cat ? ` · 限「${cat}」` : ""}` }),
        el("div", { style: "margin-top:18px" }, [list]),
      ])
    );
    document.title = `${q} · 搜索`;
    highlightIn(main, q);
  }

  function escapeRegExp(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function highlightIn(root, query) {
    const terms = query.split(/\s+/).filter(Boolean);
    if (!terms.length) return;
    const pattern = new RegExp(terms.map(escapeRegExp).join("|"), "gi");
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const targets = [];
    let node;
    while ((node = walker.nextNode())) {
      if (node.parentElement?.closest("mark")) continue;
      if (pattern.test(node.nodeValue)) targets.push(node);
      pattern.lastIndex = 0;
      if (targets.length > 400) break;
    }
    for (const target of targets) {
      const frag = document.createDocumentFragment();
      let last = 0;
      const text = target.nodeValue;
      pattern.lastIndex = 0;
      let m;
      while ((m = pattern.exec(text))) {
        frag.append(document.createTextNode(text.slice(last, m.index)));
        frag.append(el("mark", { text: m[0] }));
        last = m.index + m[0].length;
      }
      frag.append(document.createTextNode(text.slice(last)));
      target.replaceWith(frag);
    }
  }

  function highlightQuery() {
    // 从搜索页跳转到条目时高亮（?q= 参数在 state 里）
    const q = sessionStorage.getItem("wiki.lastQuery");
    if (q) highlightIn(main, q);
  }

  // ---------------------------------------------------------------- 搜索下拉
  function openSearchPanel() {
    searchPanel.hidden = false;
  }

  function closeSearchPanel() {
    searchPanel.hidden = true;
    state.searchIndex = -1;
  }

  async function runQuickSearch(q) {
    if (!q.trim()) {
      closeSearchPanel();
      return;
    }
    try {
      const data = await api(`/api/search?q=${encodeURIComponent(q)}&limit=8`);
      renderSearchPanel(q, data);
    } catch {
      searchPanel.hidden = false;
      searchPanel.replaceChildren(el("div", { class: "sp-empty", text: "搜索失败" }));
    }
  }

  function renderSearchPanel(q, data) {
    openSearchPanel();
    state.searchIndex = -1;
    const head = el("div", { class: "sp-head" }, [
      el("span", { text: `${data.count} 条结果` }),
      el("span", { text: "Enter 查看全部" }),
    ]);
    const nodes = [head];
    if (!data.items.length) {
      nodes.push(el("div", { class: "sp-empty", text: "没有找到相关内容" }));
    } else {
      data.items.forEach((it) => {
        nodes.push(
          el("button", {
            class: "sp-item",
            onclick: () => {
              sessionStorage.setItem("wiki.lastQuery", q);
              closeSearchPanel();
              navigate(`#/e/${it.id}`);
            },
          }, [
            el("div", { class: "sp-title" }, [
              el("span", { text: it.title }),
              el("span", { class: "badge", text: it.category }),
            ]),
            it.snippet ? el("div", { class: "sp-snippet", text: it.snippet }) : null,
          ])
        );
      });
    }
    searchPanel.replaceChildren(...nodes);
    highlightIn(searchPanel, q);
  }

  function moveSearchIndex(delta) {
    const items = [...searchPanel.querySelectorAll(".sp-item")];
    if (!items.length) return;
    state.searchIndex = (state.searchIndex + delta + items.length) % items.length;
    items.forEach((node, i) => node.classList.toggle("active", i === state.searchIndex));
    items[state.searchIndex].scrollIntoView({ block: "nearest" });
  }

  function goSearchPage() {
    const q = searchInput.value.trim();
    if (!q) return;
    sessionStorage.setItem("wiki.lastQuery", q);
    closeSearchPanel();
    navigate(`#/s?q=${encodeURIComponent(q)}`);
  }

  // ---------------------------------------------------------------- 侧栏开合
  function openSidebar() {
    sidebar.classList.add("open");
    overlay.hidden = false;
  }

  function closeSidebar() {
    sidebar.classList.remove("open");
    overlay.hidden = true;
  }

  // ---------------------------------------------------------------- 事件绑定
  function bindEvents() {
    window.addEventListener("hashchange", () => {
      render();
    });

    searchInput.addEventListener("input", (e) => {
      const q = e.target.value;
      clearTimeout(state.searchTimer);
      state.searchTimer = setTimeout(() => runQuickSearch(q), 160);
    });

    searchInput.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        moveSearchIndex(1);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        moveSearchIndex(-1);
      } else if (e.key === "Enter") {
        e.preventDefault();
        const items = [...searchPanel.querySelectorAll(".sp-item")];
        if (!searchPanel.hidden && state.searchIndex >= 0 && items[state.searchIndex]) {
          items[state.searchIndex].click();
        } else {
          goSearchPage();
        }
      } else if (e.key === "Escape") {
        searchInput.value = "";
        closeSearchPanel();
        searchInput.blur();
      }
    });

    searchInput.addEventListener("focus", () => {
      if (searchInput.value.trim()) runQuickSearch(searchInput.value);
    });

    document.addEventListener("click", (e) => {
      if (!e.target.closest(".search-wrap")) closeSearchPanel();
    });

    $("#themeBtn").addEventListener("click", () => {
      applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
    });

    $("#fontPlus").addEventListener("click", () => {
      applyFont(Number(localStorage.getItem(FONT_KEY)) + 1);
      toast("正文字号 " + localStorage.getItem(FONT_KEY) + "px");
    });

    $("#fontMinus").addEventListener("click", () => {
      applyFont(Number(localStorage.getItem(FONT_KEY)) - 1);
      toast("正文字号 " + localStorage.getItem(FONT_KEY) + "px");
    });

    $("#menuBtn").addEventListener("click", openSidebar);
    $("#sideClose").addEventListener("click", closeSidebar);
    overlay.addEventListener("click", closeSidebar);

    document.addEventListener("keydown", (e) => {
      const inField = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        searchInput.focus();
        searchInput.select();
        return;
      }
      if (inField) return;
      if (e.key === "/") {
        e.preventDefault();
        searchInput.focus();
      } else if (e.key === "j" || e.key === "k") {
        const cur = state.route?.kind === "entry" ? Number(state.route.id) : null;
        if (cur == null) return;
        // 用页面上的上下篇链接实现
        const links = [...document.querySelectorAll(".pager a")];
        const link = e.key === "j" ? links[links.length - 1] : links[0];
        if (link) link.click();
      } else if (e.key === "Escape") {
        closeSearchPanel();
        closeSidebar();
      }
    });

    catList.addEventListener("click", (e) => {
      if (e.target.closest("a")) closeSidebar();
    });
  }

  // ---------------------------------------------------------------- 启动
  async function boot() {
    initPrefs();
    bindEvents();
    try {
      await loadStats();
    } catch (err) {
      main.replaceChildren(
        emptyBox("索引还没建好", "先运行 python -m web.build_db，然后刷新页面")
      );
      return;
    }
    await render();
  }

  boot();
})();
