# X-Crawler

推特账号实时监控服务：通过 API 注册要监控的推特账号，高频轮询抓取新推文，对外提供 REST 拉取 + SSE 实时推送。

## 技术栈

- FastAPI + uvicorn（API 层）
- twscrape（采集层，本地 clone，`./twscrape`）
- SQLite（aiosqlite 存取）+ Redis-free 内存事件总线
- Python 3.13 / uv

## 克隆

twscrape 以 git submodule 引入（钉在 v0.20.0），克隆时加 `--recurse-submodules` 一步到位：

```bash
git clone --recurse-submodules <本仓库地址>
# 若已克隆但忘了带参数：
git submodule update --init
```

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
- 四个页面：监控管理（增删/恢复）、推文浏览、统计看板、采集账号管理（含删除账号）
- 面板登录后即可调用全部数据接口；外部调用方仍用 `Authorization: Bearer <API_KEY>`，两种鉴权互不影响

首次使用先在 `.env` 设置后台密码并改掉默认值：

```bash
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<你的强密码>
ADMIN_SESSION_SECRET=<随机长字符串>
```

### 配置真实采集账号（twscrape 模式）

先配好采集账号（用于登录 X 抓数据的账号，与被监控账号不同）：

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

## API 概览

所有接口都需要请求头 `Authorization: Bearer <API_KEYS 中的任意一个>`；后台面板登录后可用 session cookie 免带 Key。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/monitors` | 添加监控，body `{username, interval_seconds?}`；自动记录创建人（后台=`admin:xx`，API=`apikey:xx`） |
| GET | `/monitors` | 监控列表（含状态） |
| DELETE | `/monitors/{id}` | 停止并删除监控 |
| POST | `/monitors/{id}/resume` | 恢复被自动暂停的监控 |
| GET | `/monitors/{id}/tweets?limit=&since_id=&before_id=` | 按监控拉推文 |
| GET | `/tweets?username=&monitor_id=&limit=&since_id=&before_id=` | 通用查询 |
| GET | `/stream?monitor_id=` | SSE 实时推送（先回放最近 N 条，再推实时；事件 `tweet` / 心跳 `ping`） |
| GET | `/accounts` | 采集账号健康（登录态/请求量/错误） |
| DELETE | `/accounts/{username}` | 删除采集账号（仅后台 session 可用） |
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

## 数据去重

`tweets` 表以 tweet id 为主键，`INSERT OR IGNORE` 保证幂等；SSE 实时流为尽力投递（消费者落后会丢最旧事件），历史数据靠 REST 查询兜底。
