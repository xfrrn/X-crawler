# X-Crawler

X、抖音、快手和小红书账号采集服务：管理真实采集目标与源历史，同时为 AutoUp Cloud 提供共享目标订阅和增量 change feed。现有管理面板、REST 与 SSE 接口保持兼容。

## 技术栈

- FastAPI + uvicorn（API 层）
- twscrape（采集层，本地 clone，`./twscrape`）
- SQLite（aiosqlite 存取）+ Redis-free 内存事件总线
- Python 3.13 / uv

## 克隆

两个采集引擎都以 git submodule 引入（twscrape 钉在 v0.20.0，MediaCrawler 钉在其当前上游），
克隆时加 `--recurse-submodules` 一步到位：

```bash
git clone --recurse-submodules <本仓库地址>
# 若已克隆但忘了带参数：
git submodule update --init --recursive
```

克隆后首次 `uv sync`（装主项目依赖，含 twscrape），再启动服务即可；MediaCrawler 的初始化
（`uv sync` + 应用补丁 + 装 Playwright 浏览器）会在**服务首次启动时自动完成**，无需手动进它目录操作。

## 快速开始

```bash
# 1. 安装依赖（含 path 依赖 twscrape）
uv sync

# 2. 配置
cp .env.example .env   # 按需修改：API_KEYS、SCRAPER_MODE 等

# 3. 启动服务
uv run uvicorn app.main:app --reload --port 8000
```

### 无 X 账号时（mock 模式）

把 `.env` 里 `SCRAPER_MODE=mock`，无需任何 X 账号即可验证全链路：

```bash
# 添加监控（返回 monitor id）
curl -X POST http://localhost:8000/monitors \
  -H "Authorization: Bearer dev-key-1" -H "Content-Type: application/json" \
  -d '{"username": "testuser", "interval_seconds": 3}'

# 查看监控
curl http://localhost:8000/monitors -H "Authorization: Bearer dev-key-1"

# 拉取推文
curl "http://localhost:8000/tweets?username=testuser&limit=10" -H "Authorization: Bearer dev-key-1"

# SSE 实时推送（另开一个终端，能看到新推文实时到达）
curl -N "http://localhost:8000/stream?monitor_id=1" -H "Authorization: Bearer dev-key-1"

# 停止监控
curl -X DELETE http://localhost:8000/monitors/1 -H "Authorization: Bearer dev-key-1"
```

## Web 管理面板

浏览器打开 `http://localhost:8000/` 即可进入后台面板（无需 Node/前端构建，纯静态 SPA）。

- 独立后台登录：`ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录后下发服务端 session cookie，**不在浏览器存 API Key**
- 四个页面：监控管理（增删/恢复）、推文浏览（**图片/视频/GIF 直接预览**，点击看原图）、统计看板、采集账号管理（面板内直接添加账号并自动登录，实时显示可用性，可重新登录/删除）
- 面板登录后即可调用全部数据接口；外部调用方仍用 `Authorization: Bearer <API_KEY>`，两种鉴权互不影响

首次使用先在 `.env` 设置后台密码并改掉默认值：

```bash
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<你的强密码>
ADMIN_SESSION_SECRET=<随机长字符串>
```

### 配置真实采集账号（twscrape 模式）

采集账号（用于登录 X 抓数据的账号，与被监控账号不同）除了命令行，也可以在**管理面板 → 采集账号**页直接添加（密码登录 / cookies 导入两种方式），添加后自动登录并立即显示是否可用；不可用时点「重新登录」重试。命令行方式：

```bash
# 密码登录（X 风控严，建议配合代理 + 养号）
uv run python -m app.cli add --username U --password P --email E --email_password EP --proxy http://user:pass@host:port

# 或导入浏览器已登录的 cookies（更稳）
uv run python -m app.cli add-cookies --username U --cookies "auth_token=...; ct0=..."

# 查看账号状态
uv run python -m app.cli accounts
```

账号凭据存在 `data/accounts.db`，服务启动时自动加载。然后把 `.env` 改成 `SCRAPER_MODE=twscrape` 重启即可抓真实数据。

### 代理（爬 X 必备）

国内网络直连 X 会被墙，必须在 `.env` 配全局代理（所有采集账号都会走它，密码登录和 cookies 导入的都覆盖）：

```
TWS_PROXY=http://127.0.0.1:7890     # 或 socks5://127.0.0.1:1080
```

优先级：单账号 `--proxy`（仅密码登录可配）> `TWS_PROXY` 全局 > 无代理。

## 多平台监控（抖音 / 快手 / 小红书）

X 之外，本服务也能以**子进程**方式驱动外部 [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) 监控三大中文内容平台创作者的**最新动态**。每条动态入库去重 + 更新，像 X 推文一样供面板/REST 主动拉取，不推送。

### 前置条件

1. MediaCrawler 以 **git submodule** 自带（`./mediacrawler`，克隆时 `--recurse-submodules` 拉全）。
   **服务首次启动会自动初始化**它：`uv sync` 建 venv → 应用补丁 → 安装 Playwright Chromium，
   之后每次启动走快路径毫秒级跳过。`.env` 里 `MC_REPO_PATH` 默认 `./mediacrawler`。
2. 登录方式二选一（**推荐 CDP**，见下节）：
   - **CDP**：连一个已登录目标平台的 Chrome，最稳、反检测最好。
   - **cookie**：浏览器登录平台后，把 Cookie 填进对应的 `MC_COOKIES_XHS` / `MC_COOKIES_DY` / `MC_COOKIES_KS`（小红书填 `web_session=...` 即可），作为 CDP 连不上时的回退。

#### CDP 浏览器启动方法（推荐）

本版 MediaCrawler **默认开启 CDP 模式**（`ENABLE_CDP_MODE=True` 硬编码，连本机 9222 端口）。
抓取时它优先复用 CDP Chrome 里已登录的账号（`--lt` 和 `MC_COOKIES_*` 会被忽略）；
CDP 连不上会等约 60 秒再回退标准模式（此时才用 cookies）。

**第一步：启动带远程调试的 Chrome（每次抓取前保持运行）**

```cmd
:: 先完全退出所有 Chrome，再执行（务必带 --user-data-dir 独立目录）：
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --no-proxy-server ^
  --remote-debugging-port=9222 ^
  --user-data-dir=C:\code\MyGit\X-Crawler\browser_data\cdp
```

> ⚠️ 若普通 Chrome 已在运行，新启的带 flag 实例只会变成普通窗口、**不会开 9222**——必须
> 先全退出再用独立 `--user-data-dir` 启动。登录态存在该 profile，重启 Chrome 依然在。
>
> 🔌 `--no-proxy-server`：这个浏览器只访问抖音/快手/小红书等国内平台，直接连接、不走
> 系统代理（不依赖 Clash 是否运行）。若你的网络必须靠代理才能访问这些站，去掉此参数即可。

**第二步**：在**这个窗口**里登录抖音 / 快手 / 小红书（三平台一起登录也没问题）。

**第三步**：`.env` 保持 `MC_HEADLESS=false`、`MC_COOKIES_*` 可留空（想双保险就也填上）。
然后照常启动服务，面板「平台监控」添加博主 →「立即抓取」，抓取即走 CDP。

> 💡 **本机配了 Clash 类系统代理也别怕**：MediaCrawler 发现 CDP 调试地址时走 httpx，
> 系统代理会把 `localhost` 一起劫持导致连不上（返回 502）。服务已自动给 MediaCrawler
> 子进程注入 `NO_PROXY=localhost,127.0.0.1` 绕过，无需手动处理。

> 想省去手动开浏览器：把 `mediacrawler/config/base_config.py` 的 `CDP_CONNECT_EXISTING` 改为
> `False`，MediaCrawler 会自动检测 Chrome 路径并自己拉起——但登录态每次要重新来，不推荐。

> ⚠️ **MediaCrawler 补丁**：抖音/快手两个 client 原代码**不遵守** `--crawler_max_notes_count` 上限（会无上限翻页）。本仓库在 `patches/mediacrawler/` 下存放已补丁的完整文件（对
> `media_platform/douyin/client.py` 的 `get_all_user_aweme_posts` 和 `media_platform/kuaishou/client.py`
> 的 `get_all_videos_by_creator` 镜像小红书 `len(result) < config.CRAWLER_MAX_NOTES_COUNT` 模式），
> **启动时自动应用**（整文件复制 + sha256 比对幂等）。升级/重置 `mediacrawler` submodule 后，
> 若补丁与新版失配，需重新从 `C:\code\OtherGit\MediaCrawler`（已打补丁版）更新 `patches/` 下两个文件。

### 采集规则

- **每个博主每轮只抓最新 15 条**（`MC_MAX_POSTS_PER_CREATOR`，三平台统一），不做全量爬。
- 三平台各自一个轮询循环，间隔默认 1800s（`MC_POLL_INTERVAL_*`），启动错峰 0/10/20 分钟；同一时刻只跑一个子进程（全局串行，避免 CDP 9222 端口冲突与共享 sqlite 写冲突）。
- **去重 + 更新**：入库按 `(platform, content_id)` 去重；已存在的动态只刷新内容/点赞数等数据，仅新动态才写入并触发 SSE `platform_post` 事件。

### 面板 / API

- 面板新增两个页：**平台监控**（添加监控、列表、每平台「立即抓取」）与**平台内容**（按平台/博主浏览动态，图片/视频预览，每 10s 自动刷新）。
- 小红书 `creator_id` 填**带 xsec_token 的完整主页 URL**；抖音/快手可填主页 URL 或裸 ID。
- ⚠️ MediaCrawler "教学版"落库前会**匿名化**博主身份：库里只有 `creator_hash = sha256(原始id)[:16]`，没有真实昵称/头像。因此归属靠"复算 hash 匹配"完成，**添加监控时 `label`（展示名）必填**，否则列表里无法辨认是谁。
- 相关接口见下表 `/platform/*` 与 SSE 事件 `platform_post`。

## API 概览

所有接口都需要请求头 `Authorization: Bearer <API_KEYS 中的任意一个>`；后台面板登录后可用 session cookie 免带 Key。

### AutoUp 集成接口

集成接口不改变现有管理面板 API。AutoUp 平台标识为 `x / douyin / kuaishou / xiaohongshu`，服务内部映射为 `x / dy / ks / xhs`：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| PUT | `/integrations/autoup/subscriptions/{competitorId}` | 以 `{platform,target,displayName,enabled?}` 幂等创建订阅并返回不透明 `sourceTargetId` |
| PATCH | `/integrations/autoup/subscriptions/{competitorId}` | 同步当前 workspace 订阅的展示名或启停状态 |
| DELETE | `/integrations/autoup/subscriptions/{competitorId}` | 幂等删除订阅；最后一个有效订阅删除后才停底层目标，历史保留 |
| GET | `/integrations/autoup/targets/{sourceTargetId}/changes?cursor=&limit=200` | 空游标分页返回已有历史，之后只返回新增或映射字段变化的数据 |

X 用户名忽略 `@` 和大小写；中文平台从官方主页路径提取稳定创作者 ID。同目标只有一份采集历史，不同 AutoUp workspace 通过各自 competitor UUID 订阅。每个新目标抓最近 15 条；中文平台创建后异步唤醒对应调度器，调用方不等待浏览器采集。

小红书含 `xsec_token` 的完整定位链接只保存在本服务的 monitor 表，change feed 和响应不会返回该 Token。API Key 创建人只保存 `apikey_sha256:<12位指纹>`；启动时会把旧版 `apikey:<明文>` 标记原位改为 `[redacted]`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/monitors` | 添加监控，body `{username, interval_seconds?}`；自动记录创建人（后台=`admin:xx`，API=`apikey_sha256:指纹`） |
| GET | `/monitors` | 监控列表（含状态） |
| DELETE | `/monitors/{id}` | 停止并删除监控 |
| POST | `/monitors/{id}/resume` | 恢复被自动暂停的监控 |
| GET | `/monitors/{id}/tweets?limit=&since_id=&before_id=` | 按监控拉推文 |
| GET | `/tweets?username=&monitor_id=&limit=&since_id=&before_id=` | 通用查询 |
| GET | `/stream?monitor_id=` | SSE 实时推送（先回放最近 N 条，再推实时；事件 `tweet` / `platform_post` / 心跳 `ping`） |
| GET | `/accounts` | 采集账号健康（可用性/登录态/请求量/错误） |
| POST | `/accounts` | 添加采集账号（密码登录，添加后自动登录并返回状态，仅后台 session） |
| POST | `/accounts/cookies` | cookies 导入添加采集账号（含 `auth_token` 和 `ct0`，仅后台 session） |
| POST | `/accounts/{username}/relogin` | 强制重新登录采集账号并返回状态（仅后台 session） |
| DELETE | `/accounts/{username}` | 删除采集账号（仅后台 session 可用） |
| GET | `/platform/monitors` | 平台监控列表（`?platform=xhs|dy|ks` 过滤） |
| POST | `/platform/monitors` | 添加平台监控，body `{platform, creator_id, label}`；`label` 必填（昵称已脱敏） |
| GET | `/platform/monitors/{id}` | 平台监控详情 |
| DELETE | `/platform/monitors/{id}` | 停止平台监控（软删，历史动态保留） |
| POST | `/platform/monitors/{id}/resume` | 恢复平台监控 |
| GET | `/platform/posts?platform=&monitor_id=&limit=&before_id=` | 平台动态查询（图片/视频/点赞数等） |
| GET | `/platform/stats` | 平台监控计数 + 各平台运行态（轮询次数/新增/最近延迟） |
| POST | `/platform/run/{platform}` | 手动立即抓取某平台（验证用）；无运行中监控→400，正在抓→400 |
| GET | `/stats` | 聚合统计（每监控运行态、抓取量、账号健康） |
| GET | `/health` | 存活探针（公开，无需 API Key） |
| POST | `/admin/login` | 后台登录，body `{username, password}`，下发 session cookie |
| POST | `/admin/logout` | 登出 |
| GET | `/admin/me` | 当前登录态（SPA 启动探测用） |

### 自适应轮询

每个监控独立轮询，内置容错：

- **错峰**：每次轮询间隔加 0~`JITTER_FACTOR`（默认 30%）随机偏移，避免所有监控同时打接口
- **退避**：失败时轮询间隔按 `base × 2^连续失败次数` 指数增长，封顶 `MAX_POLL_INTERVAL`；成功恢复后回到基础间隔
- **自动暂停**：连续失败达到 `PAUSE_AFTER_ERRORS` 次自动置为 `active=false`，防止死磕已失效账号；用 `POST /monitors/{id}/resume` 恢复
- 运行态（轮询次数/抓取量/最近延迟/当前间隔/连续错误）通过 `GET /stats` 查看

## 配置项（.env）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATA_DIR` | `./data` | 数据目录 |
| `API_KEYS` | `dev-key-1` | API Key，逗号分隔多个 |
| `DEFAULT_POLL_INTERVAL` | `15` | 默认轮询间隔（秒） |
| `SCRAPER_MODE` | `twscrape` | `twscrape` 真实抓取 / `mock` 演示 |
| `TWSCRAPE_ACCOUNTS_DB` | `./data/accounts.db` | 采集账号库文件名 |
| `MAX_POLL_INTERVAL` | `300` | 失败退避上限（秒） |
| `PAUSE_AFTER_ERRORS` | `10` | 连续失败自动暂停阈值 |
| `JITTER_FACTOR` | `0.3` | 错峰随机系数 |
| `SSE_REPLAY_SIZE` | `50` | SSE 事件回放条数 |
| `MOCK_FAIL_START` | `0` | mock 自第 N 次轮询起持续失败（0=永不） |
| `ADMIN_USERNAME` | `admin` | 后台面板登录用户名 |
| `ADMIN_PASSWORD` | （空） | 后台面板登录密码，未设置则无法登录 |
| `ADMIN_SESSION_SECRET` | `change-me...` | 登录 cookie 签名密钥，务必改成随机串 |
| `TWS_PROXY` | （空） | 爬 X 全局代理，如 `http://127.0.0.1:7890`（所有采集账号生效） |
| `MC_ENABLED` | `true` | 是否启用多平台（抖音/快手/小红书）监控 |
| `MC_REPO_PATH` | `./mediacrawler` | MediaCrawler submodule 目录（首次启动自动初始化） |
| `MC_POLL_INTERVAL_XHS` | `1800` | 小红书轮询间隔（秒） |
| `MC_POLL_INTERVAL_DY` | `1800` | 抖音轮询间隔（秒） |
| `MC_POLL_INTERVAL_KS` | `1800` | 快手轮询间隔（秒） |
| `MC_LOGIN_TYPE` | `cookie` | 登录方式：`cookie` / `qrcode` / `phone` |
| `MC_COOKIES_XHS` | （空） | 小红书 Cookie（填 `web_session=...` 即可） |
| `MC_COOKIES_DY` | （空） | 抖音 Cookie 整串 |
| `MC_COOKIES_KS` | （空） | 快手 Cookie 整串 |
| `MC_MAX_POSTS_PER_CREATOR` | `15` | 每博主每轮抓取的最新动态条数上限（三平台统一） |
| `MC_HEADLESS` | `false` | 子进程浏览器无头模式 |
| `MC_SUBPROCESS_TIMEOUT` | `900` | 单次抓取子进程硬超时（秒） |

## 数据去重

`tweets` 表以 tweet id 为主键，重复抓取会幂等更新内容、媒体和指标；`platform_posts` 以 `(platform, content_id)` 去重。只有映射数据真正变化时 `updated_at` 才推进，因此 AutoUp change feed 不会因原始传输噪声重复产生变化；SSE 仍只为新记录发送事件，实时流为尽力投递。

## 测试

```bash
uv run python -m unittest discover -s tests
```

测试使用标准库 `unittest`，覆盖目标规范化、游标、共享订阅、数据级更新时间和 API Key 指纹，不引入额外测试框架。

## 已知限制

- 目标只保留服务已采集到的历史；每次实际平台抓取仍限制最近 15 条，不做平台全量回溯。
- 中文平台依赖本地 MediaCrawler 与浏览器登录态；禁用或目录缺失时可继续读取既有历史，但异步唤醒不会启动采集。
- change feed 是 Cloud 轮询接口，不提供 AutoUp 专用 SSE/WebSocket。
