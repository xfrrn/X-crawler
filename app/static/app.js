/* X-Crawler 管理面板 SPA（无构建，直接加载 vue.global.prod.js） */
const { createApp } = Vue;

const PAGES = [
  { key: "monitors", path: "#/monitors", label: "监控管理" },
  { key: "tweets", path: "#/tweets", label: "推文浏览" },
  { key: "stats", path: "#/stats", label: "统计看板" },
  { key: "accounts", path: "#/accounts", label: "采集账号" },
];

function currentRoute() {
  const h = location.hash.replace(/^#\/?/, "");
  return PAGES.some((p) => p.key === h) ? h : "monitors";
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

createApp({
  data() {
    return {
      pages: PAGES,
      route: currentRoute(),
      // 登录态
      authed: false,
      username: "",
      loggingIn: false,
      loginError: "",
      loginForm: { username: "", password: "" },
      // 监控管理
      monitors: [],
      monForm: { username: "", interval: 15 },
      monAdding: false,
      monError: "",
      // 推文浏览
      tweets: [],
      tweetMonId: null,
      tweetLimit: 20,
      tweetsLoading: false,
      // 统计看板 / 采集账号
      stats: {},
      acc: {},
      accBusy: false,
      accMsg: "",
      accMsgOk: false,
      pwForm: { username: "", password: "", email: "", email_password: "", proxy: "" },
      ckForm: { username: "", cookies: "" },
      tick: 0,
    };
  },
  methods: {
    /* 格式化工具挂到 methods，模板里才能按 _ctx.fmtXxx(...) 解析 */
    fmtTime,
    fmtMs,
    fmtDur,
    /* 视频时长毫秒 -> " 12s"（面板推文媒体用） */
    dur(ms) {
      if (!ms) return "";
      return " " + Math.round(ms / 1000) + "s";
    },
    /* 创建人标记：admin:xx -> 管理员 xx；apikey:xx -> API xx */
    creatorLabel(v) {
      if (!v) return "-";
      if (v.startsWith("admin:")) return "管理员 " + v.slice(6);
      if (v.startsWith("apikey:")) return "API " + v.slice(7);
      return v;
    },

    /* ---- 通用请求：401 视为会话失效，回登录视图 ---- */
    async api(path, opts = {}) {
      const res = await fetch(path, {
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        ...opts,
      });
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
      location.hash = "#/monitors";
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
    async removeMonitor(m) {
      if (!confirm(`确认删除监控 @${m.username}（#${m.id}）？历史推文保留。`)) return;
      try {
        await this.api(`/monitors/${m.id}`, { method: "DELETE" });
        await this.loadMonitors();
      } catch (e) {
        this.notify(e.message);
      }
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
    async removeAccount(a) {
      if (!confirm(`确认删除采集账号 ${a.username}？`)) return;
      try {
        await this.api(
          `/accounts/${encodeURIComponent(a.username)}`,
          { method: "DELETE" }
        );
        await this.loadAccounts();
      } catch (e) {
        this.notify(e.message);
      }
    },
    /* 可用性 = 已登录 且 未锁定（active） */
    avail(a) {
      return !!(a.logged_in && a.active);
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
          ? `账号 ${u} 添加成功，已登录（可用）`
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
        this.accMsg = `账号 ${u} cookies 导入成功（可用）`;
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
        if (r.logged_in) this.notify(`${a.username} 重新登录成功`);
        else this.notify(`${a.username} 登录失败：${r.error_msg || "未知原因"}`);
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
    },
    refreshForRoute() {
      const r = this.route;
      if (r === "monitors") this.loadMonitors();
      else if (r === "tweets") {
        if (this.tweetMonId == null && this.monitors.length) {
          this.tweetMonId = this.monitors[0].id;
        }
        this.loadTweets();
      } else if (r === "stats") this.loadStats();
      else if (r === "accounts") this.loadAccounts();
    },
    notify(msg) {
      if (this.authed) alert(msg);
    },
  },
  mounted() {
    this.initAuth();
    window.addEventListener("hashchange", () => {
      this.route = currentRoute();
      this.refreshForRoute();
    });
    // 每 5s 刷新当前页数据；推文/账号两个重页每 2 tick（10s）刷新一次
    setInterval(() => {
      if (!this.authed) return;
      this.tick++;
      const r = this.route;
      if ((r === "tweets" || r === "accounts") && this.tick % 2 !== 0) return;
      this.refreshForRoute();
    }, 5000);
  },
}).mount("#app");
