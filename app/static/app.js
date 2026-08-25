/* X-Crawler 管理面板 SPA（无构建，直接加载 vue.global.prod.js） */
const { createApp } = Vue;

/* 管理信息按任务分组，避免把采集引擎的内部划分暴露给日常使用者。 */
const GROUPS = [
  {
    name: "工作台",
    pages: [
      {
        key: "overview",
        path: "#/overview",
        label: "总览",
        icon: "总",
        title: "采集总览",
        description: "查看全部渠道、目标和登录会话的当前状态",
      },
    ],
  },
  {
    name: "采集目标",
    pages: [
      { key: "monitors", path: "#/monitors", label: "X 账号", icon: "X", title: "X 采集目标", description: "添加账号并管理轮询状态", },
      { key: "pmonitors", path: "#/pmonitors", label: "平台目标", icon: "平", title: "平台采集目标", description: "管理小红书、抖音、快手和微信公众号", },
    ],
  },
  {
    name: "内容库",
    pages: [
      { key: "tweets", path: "#/tweets", label: "X 内容", icon: "推", title: "X 内容库", description: "浏览已采集的推文与媒体", },
      { key: "pposts", path: "#/pposts", label: "平台内容", icon: "库", title: "平台内容库", description: "按平台和采集目标浏览最新内容", },
    ],
  },
  {
    name: "系统",
    pages: [
      { key: "accounts", path: "#/accounts", label: "登录与会话", icon: "钥", title: "登录与会话", description: "管理 X 采集账号和微信公众号后台会话", },
      { key: "stats", path: "#/stats", label: "运行状态", icon: "态", title: "运行状态", description: "检查轮询、退避、错误和服务运行时间", },
    ],
  },
];
const PAGES = GROUPS.flatMap((g) => g.pages);

function currentRoute() {
  const h = location.hash.replace(/^#\/?/, "");
  return PAGES.some((p) => p.key === h) ? h : "overview";
}

/* 格式化工具 */
function fmtTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString("zh-CN", { hour12: false });
}
function fmtMs(ms) {
  if (ms == null) return "-";
  if (ms < 1000) return Math.round(ms) + "ms";
  return (ms / 1000).toFixed(1) + "s";
}
function fmtDur(s) {
  if (s == null) return "-";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return h + "h " + m + "m";
  if (m > 0) return m + "m " + sec + "s";
  return sec + "s";
}
/* 大数缩写：12345 → "1.2万"，1200 → "1.2k"（保留 1 位小数，去尾零） */
function r1(x) {
  return String(Math.round(x * 10) / 10).replace(/\.0$/, "");
}
function fmtNum(n) {
  const v = Number(n);
  if (!isFinite(v)) return n ?? "0";
  const abs = Math.abs(v);
  if (abs >= 1e8) return r1(v / 1e8) + "亿";
  if (abs >= 1e4) return r1(v / 1e4) + "万";
  if (abs >= 1e3) return r1(v / 1e3) + "k";
  return String(v);
}
/* 超长 ID 截断：7414000156015742245 → "7414…2245"（title 里给全） */
function shortId(id, head = 4, tail = 4) {
  const s = String(id == null ? "" : id);
  return s.length > head + tail + 1 ? s.slice(0, head) + "…" + s.slice(-tail) : s;
}

createApp({
  data() {
    return {
      groups: GROUPS,
      route: currentRoute(),
      // 登录态
      authed: false,
      username: "",
      serviceReachable: false,
      loggingIn: false,
      loginError: "",
      loginForm: { username: "", password: "" },
      // 监控管理
      monitors: [],
      monForm: { username: "", interval: 43200 },
      monAdding: false,
      monError: "",
      // 推文浏览
      tweets: [],
      tweetMonId: null,
      tweetLimit: 20,
      tweetsLoading: false,
      // 平台监控
      platforms: [
        { key: "xhs", name: "小红书", mark: "红" },
        { key: "dy", name: "抖音", mark: "抖" },
        { key: "ks", name: "快手", mark: "快" },
        { key: "wx", name: "微信公众号", mark: "微" },
      ],
      pmonitors: [],
      pMonitorFilter: "all",
      pmForm: { platform: "xhs", creator_id: "", label: "" },
      pmAdding: false,
      pmError: "",
      pMsg: "",
      pMsgOk: false,
      pStats: {},
      wechatSession: { status: "missing", qrReady: false },
      wechatBusy: false,
      wechatQrUrl: "",
      // 「立即抓取」默认平台（平台监控列表卡片头的下拉）
      runPlat: "xhs",
      // 平台内容
      pposts: [],
      pPlat: "xhs",
      pMonId: null,
      pLimit: 20,
      pLoading: false,
      // 统计看板 / 采集账号
      stats: {},
      acc: {},
      accBusy: false,
      accMsg: "",
      accMsgOk: false,
      pwForm: { username: "", password: "", email: "", email_password: "", proxy: "" },
      ckForm: { username: "", cookies: "" },
      toast: { message: "", kind: "" },
      toastTimer: null,
      confirmMessage: "",
      confirmAction: null,
      tick: 0,
    };
  },
  computed: {
    activePage() {
      const page = PAGES.find((item) => item.key === this.route) || PAGES[0];
      const group = GROUPS.find((item) => item.pages.includes(page));
      return { ...page, group: group ? group.name : "工作台" };
    },
    visiblePlatformMonitors() {
      return this.pMonitorFilter === "all"
        ? this.pmonitors
        : this.pmonitors.filter((item) => item.platform === this.pMonitorFilter);
    },
    dashboard() {
      const platformStats = Object.values(this.pStats.per_platform || {});
      const runtime = this.stats.monitors_detail || [];
      const platformRuntime = this.pStats.runtime || [];
      const accountIssues = (this.acc.accounts || []).filter(
        (item) => !item.logged_in || (item.error_msg && item.error_msg !== "None")
      ).length;
      const xIssues = this.monitors.filter((item) => item.last_error).length;
      const xSchedulerIssues = runtime.filter(
        (item) => item.active && !item.task_alive
      ).length;
      const failedPlatforms = new Set(
        this.pmonitors.filter((item) => item.last_error).map((item) => item.platform)
      );
      platformRuntime.forEach((item) => {
        if (item.last_error) failedPlatforms.add(item.platform);
      });
      const platformIssues = failedPlatforms.size;
      const schedulerIssues = platformRuntime.filter(
        (item) =>
          (this.pStats.per_platform?.[item.platform]?.active || 0) > 0 &&
          (!item.scheduled || this.platformInitError(item.platform))
      ).length;
      const wechatIssues = ["expired", "error"].includes(this.wechatSession.status) ? 1 : 0;
      return {
        targetsTotal:
          (this.stats.monitors_total || 0) +
          platformStats.reduce((sum, item) => sum + (item.monitors || 0), 0),
        targetsActive:
          (this.stats.monitors_active || 0) +
          platformStats.reduce((sum, item) => sum + (item.active || 0), 0),
        contentsTotal:
          (this.stats.tweets_total || 0) +
          platformStats.reduce((sum, item) => sum + (item.posts || 0), 0),
        issues:
          xIssues +
          xSchedulerIssues +
          platformIssues +
          accountIssues +
          schedulerIssues +
          wechatIssues,
        totalPolls: runtime.reduce((sum, item) => sum + (item.total_polls || 0), 0),
        totalNew: runtime.reduce((sum, item) => sum + (item.total_new || 0), 0),
        runtimeErrors: runtime.reduce(
          (sum, item) => sum + (item.consecutive_errors || 0),
          0
        ),
      };
    },
    serviceState() {
      if (!this.serviceReachable) return { text: "服务连接异常", state: "warning" };
      const platformStopped = (this.pStats.runtime || []).some(
        (item) =>
          (this.pStats.per_platform?.[item.platform]?.active || 0) > 0 &&
          (!item.scheduled || this.platformInitError(item.platform))
      );
      return this.stats.status === "degraded" || platformStopped
        ? { text: "部分调度异常", state: "warning" }
        : { text: "服务已连接", state: "ok" };
    },
  },
  methods: {
    /* 格式化工具挂到 methods，模板里才能按 _ctx.fmtXxx(...) 解析 */
    fmtTime,
    fmtMs,
    fmtDur,
    fmtNum,
    shortId,
    go(route) {
      location.hash = "#/" + route;
    },
    openDialog(name) {
      const dialog = this.$refs[name];
      if (dialog && !dialog.open) dialog.showModal();
    },
    closeDialog(name) {
      const dialog = this.$refs[name];
      if (dialog?.open) dialog.close();
    },
    askConfirm(message, action) {
      this.confirmMessage = message;
      this.confirmAction = action;
      this.openDialog("confirmDialog");
    },
    cancelConfirm() {
      this.confirmAction = null;
      this.closeDialog("confirmDialog");
    },
    runConfirmed() {
      const action = this.confirmAction;
      this.cancelConfirm();
      if (action) action();
    },
    /* 视频时长毫秒 -> " 12s"（面板推文媒体用） */
    dur(ms) {
      if (!ms) return "";
      return " " + Math.round(ms / 1000) + "s";
    },
    /* ---- 通用请求：401 视为会话失效，回登录视图 ---- */
    async api(path, opts = {}) {
      let res;
      try {
        res = await fetch(path, {
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          ...opts,
        });
        this.serviceReachable = true;
      } catch (e) {
        this.serviceReachable = false;
        throw new Error("无法连接 X-Crawler 服务");
      }
      if (!res.ok) {
        let msg = res.status + " " + res.statusText;
        try {
          const j = await res.json();
          if (j.detail) msg = j.detail;
        } catch (e) {}
        if (res.status === 401 && this.authed) {
          this.authed = false; // 会话过期/被清除，回登录
        }
        throw new Error(msg);
      }
      return res.json();
    },

    /* ---- 登录 / 登出 ---- */
    async initAuth() {
      try {
        const me = await this.api("/admin/me");
        this.authed = true;
        this.username = me.username;
        this.route = currentRoute();
        this.refreshAll();
      } catch (e) {
        // 未登录：停留在登录视图
      }
    },
    async doLogin() {
      this.loggingIn = true;
      this.loginError = "";
      try {
        const r = await this.api("/admin/login", {
          method: "POST",
          body: JSON.stringify(this.loginForm),
        });
        this.username = r.username;
        this.authed = true;
        this.route = currentRoute();
        this.refreshAll();
      } catch (e) {
        this.loginError = e.message;
      } finally {
        this.loggingIn = false;
      }
    },
    async doLogout() {
      try {
        await this.api("/admin/logout", { method: "POST" });
      } catch (e) {}
      this.authed = false;
      this.loginForm = { username: "", password: "" };
      location.hash = "#/overview";
    },

    /* ---- 监控管理 ---- */
    async loadMonitors() {
      try {
        this.monitors = await this.api("/monitors");
      } catch (e) {
        this.notify(e.message);
      }
    },
    async addMonitor() {
      const username = this.monForm.username.trim();
      if (!username) return;
      this.monAdding = true;
      this.monError = "";
      try {
        await this.api("/monitors", {
          method: "POST",
          body: JSON.stringify({
            username,
            interval_seconds: this.monForm.interval,
          }),
        });
        this.monForm.username = "";
        this.closeDialog("monitorDialog");
        this.notify("X 采集目标已添加", "success");
        await this.loadMonitors();
      } catch (e) {
        this.monError = e.message;
      } finally {
        this.monAdding = false;
      }
    },
    async resumeMonitor(m) {
      try {
        await this.api(`/monitors/${m.id}/resume`, { method: "POST" });
        await this.loadMonitors();
      } catch (e) {
        this.notify(e.message);
      }
    },
    removeMonitor(m) {
      this.askConfirm(`停止 @${m.username} 的采集？历史内容仍会保留。`, async () => {
        try {
          await this.api(`/monitors/${m.id}`, { method: "DELETE" });
          this.notify("采集目标已停止", "success");
          await this.loadMonitors();
        } catch (e) {
          this.notify(e.message);
        }
      });
    },
    openTweets(m) {
      this.tweetMonId = m.id;
      this.go("tweets");
    },

    /* ---- 推文浏览 ---- */
    async loadTweets() {
      if (this.tweetMonId == null) return;
      this.tweetsLoading = true;
      try {
        const list = await this.api(
          `/monitors/${this.tweetMonId}/tweets?limit=${this.tweetLimit || 20}`
        );
        this.tweets = list.map((t) => ({ ...t, _exp: false }));
      } catch (e) {
        this.notify(e.message);
      } finally {
        this.tweetsLoading = false;
      }
    },

    /* ---- 平台监控 ---- */
    platName(p) {
      const f = this.platforms.find((x) => x.key === p);
      return f ? f.name : p;
    },
    platformMark(p) {
      const platform = this.platforms.find((item) => item.key === p);
      return platform ? platform.mark : "?";
    },
    pmOfPlat(p) {
      return this.pmonitors.filter((m) => m.platform === p);
    },
    pmLabel(mid) {
      const m = this.pmonitors.find((x) => x.id === mid);
      return m ? m.label : "#" + mid;
    },
    targetState(m) {
      if (!m.active)
        return m.last_error
          ? { cls: "error", label: "失败后暂停" }
          : { cls: "off", label: "已停止" };
      if (m.last_error) return { cls: "error", label: "采集异常" };
      if (!m.last_success_at) return { cls: "off", label: "等待首次采集" };
      return { cls: "ok", label: "已启用" };
    },
    xTargetState(m) {
      const runtime = (this.stats.monitors_detail || []).find((item) => item.id === m.id);
      if (m.active && runtime && !runtime.task_alive)
        return { cls: "error", label: "调度异常" };
      return this.targetState(m);
    },
    platformTargetState(m) {
      const runtime = (this.pStats.runtime || []).find((item) => item.platform === m.platform);
      if (m.active && this.platformInitError(m.platform))
        return { cls: "error", label: "初始化失败" };
      if (m.active && runtime && !runtime.scheduled)
        return { cls: "error", label: "调度不可用" };
      if (!m.active)
        return m.last_error
          ? { cls: "error", label: "抓取失败后暂停" }
          : { cls: "off", label: "已停止" };
      if (m.last_error) return { cls: "error", label: "目标抓取异常" };
      if (!m.last_success_at) return { cls: "off", label: "等待首次抓取" };
      return { cls: "ok", label: "已启用" };
    },
    platformInitError(plat) {
      const components = this.pStats.components || {};
      if (plat === "wx" && components.playwright_ready === false)
        return "Playwright Chromium 初始化失败";
      if (plat !== "wx" && components.mediacrawler_ready === false)
        return "MediaCrawler 初始化失败";
      return "";
    },
    /* 平台 runtime 是目标执行结果的汇总，目标自身状态以 last_error 为准。 */
    platCrawlStatus(plat) {
      const rt = (this.pStats.runtime || []).find((r) => r.platform === plat);
      if (this.platformInitError(plat))
        return { text: "初始化失败", cls: "error", rt };
      if (rt && !rt.scheduled)
        return { text: "调度不可用", cls: "error", rt };
      if (rt && rt.running) return { text: "正在抓取", cls: "busy", rt };
      if (rt && rt.total_runs > 0) {
        return rt.last_error
          ? { text: "平台部分失败", cls: "error", rt }
          : { text: "平台抓取成功", cls: "ok", rt };
      }
      return { text: "等待首次调度", cls: "wait", rt };
    },
    platRunTip(plat) {
      const st = this.platCrawlStatus(plat);
      const rt = st.rt;
      if (this.platformInitError(plat)) return this.platformInitError(plat);
      if (!rt) return "平台调度状态尚未加载";
      if (!rt.scheduled) return rt.unavailable_reason || "平台调度不可用";
      if (!rt.total_runs) return "尚未抓取，等待定时轮询或手动「立即抓取」";
      const parts = [`已跑 ${rt.total_runs} 次`];
      if (rt.last_run_ms != null) parts.push(`上次耗时 ${this.fmtMs(rt.last_run_ms)}`);
      if (rt.total_new > 0) parts.push(`共新增 ${rt.total_new} 条`);
      if (rt.last_error) parts.push(rt.last_error);
      return parts.join(" · ");
    },
    async loadPlatformMonitors() {
      try {
        this.pmonitors = await this.api("/platform/monitors");
      } catch (e) {
        this.notify(e.message);
      }
    },
    async loadPlatformStats() {
      try {
        this.pStats = await this.api("/platform/stats");
      } catch (e) {
        this.notify(e.message);
      }
    },
    async loadWechatSession() {
      try {
        this.wechatSession = await this.api("/admin/wechat/session");
        this.wechatQrUrl = this.wechatSession.qrReady
          ? `/admin/wechat/login/qr?_=${Date.now()}`
          : "";
      } catch (e) {
        this.notify(e.message);
      }
    },
    wechatStatusLabel() {
      return {
        missing: "未登录",
        starting: "正在生成二维码",
        waiting_scan: "等待扫码",
        ready: "会话已保存",
        expired: "登录态已失效",
        error: "登录失败",
      }[this.wechatSession.status] || this.wechatSession.status;
    },
    async startWechatLogin() {
      this.wechatBusy = true;
      try {
        this.wechatSession = await this.api("/admin/wechat/login", { method: "POST" });
        while (this.authed && this.wechatSession.status === "starting") {
          await new Promise((resolve) => window.setTimeout(resolve, 500));
          await this.loadWechatSession();
        }
      } catch (e) {
        this.notify(e.message);
      } finally {
        this.wechatBusy = false;
      }
    },
    async addPlatformMonitor() {
      const creator_id = this.pmForm.creator_id.trim();
      const label = this.pmForm.label.trim();
      if (!creator_id || !label) {
        this.pmError = "创作者链接/ID/公众号名称和展示名都要填";
        return;
      }
      this.pmAdding = true;
      this.pmError = "";
      try {
        await this.api("/platform/monitors", {
          method: "POST",
          body: JSON.stringify({
            platform: this.pmForm.platform,
            creator_id,
            label,
          }),
        });
        this.pmForm.creator_id = "";
        this.pmForm.label = "";
        this.closeDialog("platformMonitorDialog");
        this.notify("平台采集目标已添加", "success");
        await this.loadPlatformMonitors();
        await this.loadPlatformStats();
      } catch (e) {
        this.pmError = e.message;
      } finally {
        this.pmAdding = false;
      }
    },
    async resumePlatformMonitor(m) {
      try {
        await this.api(`/platform/monitors/${m.id}/resume`, { method: "POST" });
        await this.loadPlatformMonitors();
      } catch (e) {
        this.notify(e.message);
      }
    },
    removePlatformMonitor(m) {
      this.askConfirm(`停止 ${this.platName(m.platform)}“${m.label}”的采集？历史内容仍会保留。`, async () => {
        try {
          await this.api(`/platform/monitors/${m.id}`, { method: "DELETE" });
          this.notify("平台采集目标已停止", "success");
          await this.loadPlatformMonitors();
          await this.loadPlatformStats();
        } catch (e) {
          this.notify(e.message);
        }
      });
    },
    async triggerPlatformRun(p) {
      this.pMsg = "";
      try {
        const r = await this.api(`/platform/run/${p}`, { method: "POST" });
        this.pMsg = `${this.platName(p)} 抓取已启动（${r.monitors.length} 个监控），稍后在「平台内容」刷新查看`;
        this.pMsgOk = true;
        await this.loadPlatformMonitors();
      } catch (e) {
        this.pMsg = e.message;
        this.pMsgOk = false;
      }
    },
    async loadPlatformPosts() {
      if (this.pMonId == null) return;
      this.pLoading = true;
      try {
        const list = await this.api(
          `/platform/posts?monitor_id=${this.pMonId}&limit=${this.pLimit || 20}`
        );
        this.pposts = list.map((p) => ({ ...p, _exp: false }));
      } catch (e) {
        this.notify(e.message);
      } finally {
        this.pLoading = false;
      }
    },
    onPlatformChange() {
      const list = this.pmOfPlat(this.pPlat);
      this.pMonId = list.length ? list[0].id : null;
      this.loadPlatformPosts();
    },
    openPlatformPosts(m) {
      this.pPlat = m.platform;
      this.pMonId = m.id;
      this.go("pposts");
    },

    /* ---- 统计 / 账号 ---- */
    async loadStats() {
      try {
        this.stats = await this.api("/stats");
      } catch (e) {
        this.notify(e.message);
      }
    },
    async loadAccounts() {
      try {
        this.acc = await this.api("/accounts");
      } catch (e) {
        this.notify(e.message);
      }
    },
    removeAccount(a) {
      this.askConfirm(`删除 X 采集账号 ${a.username}？该账号的本机会话将不可恢复。`, async () => {
        try {
          await this.api(
            `/accounts/${encodeURIComponent(a.username)}`,
            { method: "DELETE" }
          );
          this.notify("X 采集账号已删除", "success");
          await this.loadAccounts();
        } catch (e) {
          this.notify(e.message);
        }
      });
    },
    /* 账号 active 只代表允许调度；登录材料和采集错误单独判断。 */
    accState(a) {
      if (a.error_msg && a.error_msg !== "None")
        return { cls: "error", label: "会话异常" };
      if (!a.logged_in) return { cls: "error", label: "未登录" };
      if (!a.active) return { cls: "off", label: "不可调度" };
      return { cls: "ok", label: "已启用" };
    },
    async addAccountByPassword() {
      const u = this.pwForm.username.trim();
      if (!u || !this.pwForm.password) return;
      this.accBusy = true;
      this.accMsg = "";
      try {
        const r = await this.api("/accounts", {
          method: "POST",
          body: JSON.stringify({
            username: u,
            password: this.pwForm.password,
            email: this.pwForm.email,
            email_password: this.pwForm.email_password,
            proxy: this.pwForm.proxy || null,
          }),
        });
        this.pwForm = { username: "", password: "", email: "", email_password: "", proxy: "" };
        this.accMsg = r.logged_in
          ? `账号 ${u} 添加成功，已登录并启用`
          : `账号 ${u} 已添加，但登录失败：${r.error_msg || "未知原因"}`;
        this.accMsgOk = r.logged_in;
        await this.loadAccounts();
      } catch (e) {
        this.accMsg = e.message;
        this.accMsgOk = false;
      } finally {
        this.accBusy = false;
      }
    },
    async addAccountByCookies() {
      const u = this.ckForm.username.trim();
      if (!u || !this.ckForm.cookies.trim()) return;
      this.accBusy = true;
      this.accMsg = "";
      try {
        await this.api("/accounts/cookies", {
          method: "POST",
          body: JSON.stringify({ username: u, cookies: this.ckForm.cookies }),
        });
        this.ckForm = { username: "", cookies: "" };
        this.accMsg = `账号 ${u} Cookies 已导入，首次采集请求会验证会话有效性`;
        this.accMsgOk = true;
        await this.loadAccounts();
      } catch (e) {
        this.accMsg = e.message;
        this.accMsgOk = false;
      } finally {
        this.accBusy = false;
      }
    },
    async reloginAccount(a) {
      try {
        const r = await this.api(
          `/accounts/${encodeURIComponent(a.username)}/relogin`,
          { method: "POST" }
        );
        if (r.logged_in) this.notify(`${a.username} 重新登录成功`, "success");
        else this.notify(`${a.username} 登录失败：${r.error_msg || "未知原因"}`);
        await this.loadAccounts();
      } catch (e) {
        this.notify(e.message);
      }
    },
    async setAccountActive(a, active) {
      try {
        await this.api(
          `/accounts/${encodeURIComponent(a.username)}/${active ? "resume" : "pause"}`,
          { method: "POST" }
        );
        this.notify(`${a.username} 已${active ? "启用" : "停止调度"}`, "success");
        await this.loadAccounts();
      } catch (e) {
        this.notify(e.message);
      }
    },

    /* ---- 刷新调度 ---- */
    refreshAll() {
      this.loadMonitors();
      this.loadStats();
      this.loadAccounts();
      this.loadPlatformMonitors();
      this.loadPlatformStats();
      this.loadWechatSession();
    },
    refreshForRoute() {
      const r = this.route;
      if (r === "overview") this.refreshAll();
      else if (r === "monitors") {
        this.loadMonitors();
        this.loadStats();
      }
      else if (r === "tweets") {
        if (this.tweetMonId == null && this.monitors.length) {
          this.tweetMonId = this.monitors[0].id;
        }
        this.loadTweets();
      } else if (r === "pmonitors") {
        this.loadPlatformMonitors();
        this.loadPlatformStats();
        this.loadWechatSession();
      } else if (r === "pposts") {
        if (this.pMonId == null) {
          const list = this.pmOfPlat(this.pPlat);
          if (list.length) this.pMonId = list[0].id;
        }
        this.loadPlatformPosts();
      } else if (r === "stats") this.loadStats();
      else if (r === "accounts") {
        this.loadAccounts();
        this.loadWechatSession();
      }
    },
    refreshCurrent() {
      this.refreshForRoute();
    },
    notify(message, kind = "error") {
      if (!this.authed) return;
      window.clearTimeout(this.toastTimer);
      this.toast = { message, kind };
      this.toastTimer = window.setTimeout(() => {
        this.toast = { message: "", kind: "" };
      }, 3200);
    },
  },
  mounted() {
    this.initAuth();
    window.addEventListener("hashchange", () => {
      this.route = currentRoute();
      document.title = `${this.activePage.title} · X-Crawler`;
      this.refreshForRoute();
    });
    // 每 5s 刷新当前页数据；聚合总览和内容/账号重页每 10s 刷新一次。
    setInterval(() => {
      if (!this.authed) return;
      this.tick++;
      const r = this.route;
      if (
        (r === "overview" || r === "tweets" || r === "accounts" || r === "pposts") &&
        this.tick % 2 !== 0
      )
        return;
      this.refreshForRoute();
    }, 5000);
  },
}).mount("#app");
