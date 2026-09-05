# freebuff-proxy

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)

> 将 **freebuff/codebuff** 的免费模型额度暴露成 **OpenAI / Anthropic 兼容 API**。
> 推荐 **Docker Compose 本地构建**（或 Node/VPS 直跑），适配任意 OpenAI SDK / 客户端（ChatGPT-Next-Web、LobeChat、one-api、page-assist 等）。

> ⚠️ **部署方式重要提示**：Freebuff 官方已检测到 Cloudflare Worker 部署（识别 `cf-worker` / `cf-ray` 等边缘标记），**在 CF 上部署会显著增加账号封禁风险**。本项目**不推荐 Cloudflare 部署**，推荐 Docker 容器 / 自建 VPS（见「[🐳 Docker 部署](#-docker-部署推荐)」）。

---

## 目录

1. [特性](#-特性)
2. [仓库结构](#-仓库结构)
3. [快速开始（Docker 推荐）](#-快速开始docker-推荐)
4. [本地 Node 直跑](#-本地-node-直跑)
5. [环境变量](#-环境变量)
6. [凭据文件格式](#-凭据文件格式)
7. [获取 FREEBUFF_TOKEN](#-获取-freebuff_token)
8. [健康检查](#-健康检查)
9. [API 调用示例](#-api-调用示例)
10. [模型说明](#-模型说明)
11. [多账号与账号管理面板](#-多账号与账号管理面板)
12. [注册服务 register_service（可选）](#-注册服务-register_service可选)
13. [免责声明](#-免责声明)
14. [License](#-license)

---

## ✨ 特性

- ⭐ **完整访问模式模型**：非 CF 部署通常可获得 Freebuff 完整访问模式；`deepseek/deepseek-v4-flash` 与 `mimo/mimo-v2.5` 属于官方非 Premium 特殊模型
- 🔁 **多账号自动切换**：撞额度（429/空响应）自动冷却并切换到下一账号，逗号分隔即可
- 💡 **优先复用活跃 session**：session 约 1 小时有效，创建才扣额度；同一模型 session 存活期间钉在同一账号，用满再换，最大化额度利用率
- 📢 **广告与 streak 流程兼容**：创建新 session 前按官方客户端流程请求广告并调用 `GET /api/v1/freebuff/streak`，失败静默跳过
- 🧩 **OpenAI 兼容**：`GET /v1/models`、`POST /v1/chat/completions`、`/v1/responses`（流式/非流式）
- 📨 **Anthropic Messages API**：`POST /v1/messages`、`/messages`、`count_tokens` 路由
- ❤️ **健康检查**：`GET /healthz`（免鉴权）
- 🖥️ **账号管理面板**（可选）：`/accounts` 页面 + `/admin/api/*`，查看/添加/移除账号、管理 banned 名单
- 📦 **无外部镜像依赖**：Docker 镜像由本仓库 `Dockerfile` 本地构建，不拉取第三方 worker.js

---

## 📁 仓库结构

```
├── worker.js                  # 核心 Worker（协议代理 / 账号调度 / session 生命周期）
├── server.js                  # Node HTTP 服务入口（Docker/VPS 直跑用）
├── admin.js                   # 账号管理路由（/accounts 页面 + /admin/api/*，可选）
├── api/index.js               # Vercel Function 适配入口
├── static/accounts.html       # 账号管理面板前端
├── freebuff_tools/            # authToken 提取 / 账号管理 CLI
├── oc/                        # CF 临时邮箱与 GitHub 协议注册链路（注册服务依赖）
├── register_service/          # 账号注册服务（可选，FastAPI :8899）
├── Dockerfile / docker-compose.yml
├── freebuff-models.json       # 模型映射快照（可自动更新）
└── MODELS.md                  # 模型列表文档
```

---

## 🐳 快速开始（Docker 推荐）

> 适合本地/NAS/VPS 长期运行：不暴露 CF 边缘标记，账号封禁风险显著低于 CF 部署。
> 本仓库直接 `docker compose` 本地构建镜像，**无需先 clone 到部署机以外**、无需外部镜像仓库。

### 方式一：clone 后 compose 构建（推荐长期运行）

```bash
# 1. clone 本仓库
git clone https://github.com/VentourF/freebuff-proxy.git
cd freebuff-proxy

# 2. 准备凭据（多账号聚合格式，见「凭据文件格式」）
mkdir -p credentials
#   用提取工具生成：
#     python3 freebuff_tools/extract_freebuff.py login
#   或手动创建 credentials/freebuff_credentials.json：
#     {"accounts": {"<账号id>": {"email": "...", "authToken": "...", "name": "..."}}}

# 3. 准备 .env（参考 docker-compose.yml 中的变量）
cat > .env <<'EOF'
FREEBUFF_API_KEY=your-api-key
RELAY_KEY=
EOF

# 4. 构建并启动（本仓库 Dockerfile，无需外部镜像）
docker compose up -d --build
```

### 方式二：Windows 一键脚本

仓库提供 `start-docker.ps1`：自动探测宿主机代理（系统代理 / 常见端口），
转换为 `host.docker.internal:端口` 注入容器后拉起 compose：

```powershell
powershell -ExecutionPolicy Bypass -File start-docker.ps1
```

### 验证

```bash
curl http://localhost:8877/healthz
# {"status":"ok","version":"1.8.10.2","time":"..."}
```

> 容器将宿主机 `./credentials` 以**读写**方式挂载到 `/app/credentials`
> （管理面板"移除账号"与 banned 持久化需写回）；`./static` 也挂载为可写，
> 便于直接改页面。

---

## 🖥️ 本地 Node 直跑

不依赖 Docker，直接在当前目录运行（要求 Node ≥ 20）：

```bash
# 1. 准备凭据（同上 credentials/freebuff_credentials.json）
# 2. 启动（Windows 可改用 start.ps1，自动探测代理）
PORT=8787 HOST=0.0.0.0 FREEBUFF_API_KEY=your-api-key node server.js
```

- Windows 一键脚本：`start.ps1`（本机反代，自动探测代理）。
- 访问地址：`http://localhost:8787`（Docker 内为 `:8787`，映射到宿主机 `:8877`）。

---

## ⚙️ 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `FREEBUFF_TOKEN` | 条件 | freebuff authToken（多账号用英文逗号分隔）；容器/VPS 由 `server.js` 从 `credentials/` 自动读取并拼接，可留空 |
| `FREEBUFF_API_KEY` | 否 | 本 API 访问 key，缺省 `freebuff-default-key` |
| `PORT` / `HOST` | 否 | 监听端口/地址，默认 `8787` / `0.0.0.0` |
| `FREEBUFF_DEBUG` | 否 | `true` 开启请求级调试日志 |
| `RELAY_KEY` | 否 | 中继密钥（当上游走带鉴权的中继时使用） |
| `CODEBUFF_API` | 否 | 上游地址，默认空 = 直连 `https://www.codebuff.com` |
| `REGISTER_SERVICE_URL` | 否 | 注册服务地址，默认 `http://host.docker.internal:8899`（仅管理面板需要） |
| `NODE_USE_ENV_PROXY` | 否 | `1` 时 Node fetch 走 `HTTPS_PROXY`（free 模型要求美区出口） |
| `HTTPS_PROXY` / `HTTP_PROXY` | 否 | 上游代理地址（美区出口，如 `http://127.0.0.1:7897`） |
| `NO_PROXY` | 否 | 默认 `localhost,127.0.0.1,host.docker.internal` |

---

## 🔑 凭据文件格式

容器/VPS 启动时 `server.js` 会扫描 `credentials/` 下所有 `*.json`
（排除 `banned_tokens.json`），收集其中的 `authToken` 拼成账号池。

支持两种格式，可混合放多个文件：

**多账号聚合格式（提取工具默认输出）：**

```json
{
  "accounts": {
    "<账号id>": {
      "email": "you@example.com",
      "name": "account-name",
      "authToken": "<authToken>",
      "registeredAt": 1788497161765
    }
  }
}
```

**单账号顶层格式：**

```json
{
  "authToken": "<authToken>"
}
```

被封禁/被移除的 token 由 Worker 写入 `credentials/banned_tokens.json`，
调度层永久跳过，重启不丢失。

---

## 🔐 获取 FREEBUFF_TOKEN

freebuff 登录凭证（authToken）通过官方 CLI 同款**授权码轮询**获取。
仓库自带提取工具 `freebuff_tools/extract_freebuff.py`：

```bash
cd freebuff_tools
python3 extract_freebuff.py login   # 打印授权 URL → 浏览器授权 → 自动轮询
python3 extract_freebuff.py show    # 显示全部账号：邮箱 + token + 存活状态
python3 extract_freebuff.py export  # 汇总全部账号 token，一行一个
python3 extract_freebuff.py quota   # 查用量
python3 extract_freebuff.py session # 开/查 session
python3 extract_freebuff.py chat "你好"  # 发一条测试消息
```

> 可选：配置 `TG_BOT_TOKEN` / `TG_CHAT_ID` 后，授权链接与 token 会推送到 Telegram，
> 终端不打印敏感信息。本地 `login` 按账号追加保存到
> `freebuff_tools/freebuff_credentials.json`（已被 `.gitignore` 忽略，不会提交）。
> 结构参考 `freebuff_tools/freebuff_credentials.example.json`。

---

## ❤️ 健康检查

```bash
curl http://localhost:8877/healthz
# {"status":"ok","version":"1.8.10.2","time":"..."}
```

- 免鉴权，适合接入 UptimeRobot 等监控探活。
- `version` 字段即当前实际运行的 worker 版本号，用于确认线上是否已更新。

---

## 💬 API 调用示例

```bash
# 模型列表
curl http://localhost:8877/v1/models \
  -H "Authorization: Bearer <FREEBUFF_API_KEY>"

# OpenAI chat 非流式
curl http://localhost:8877/v1/chat/completions \
  -H "Authorization: Bearer <FREEBUFF_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek/deepseek-v4-flash","messages":[{"role":"user","content":"你好"}]}'

# OpenAI chat 流式
curl -N http://localhost:8877/v1/chat/completions \
  -H "Authorization: Bearer <FREEBUFF_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek/deepseek-v4-flash","messages":[{"role":"user","content":"你好"}],"stream":true}'

# Anthropic Messages
curl http://localhost:8877/v1/messages \
  -H "Authorization: Bearer <FREEBUFF_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek/deepseek-v4-flash","max_tokens":1024,"messages":[{"role":"user","content":"你好"}]}'
```

客户端 Base URL 填 `http://localhost:8877/v1`，API Key 填 `FREEBUFF_API_KEY` 的值即可。

---

## 📋 模型说明

模型映射来自 Freebuff Desktop 官方常量（`CodebuffAI/freebuff` 仓库），
并支持运行时刷新：Worker 会从官方 GitHub 源拉取最新模型映射，失败时回退到
`freebuff-models.json`（本仓库内快照）。完整模型表见 [MODELS.md](MODELS.md)。

| API 模型名 | 说明 |
|---|---|
| `deepseek/deepseek-v4-flash` | 官方非 Premium 特殊模型，主力推荐 |
| `mimo/mimo-v2.5` | 官方非 Premium 特殊模型，均衡性能 |
| `minimax/minimax-m3` 等 | 普通模型，按每日基础 session 额度理解 |
| `z-ai/glm-5.2` 等 | 需官方资格（referral/streak），独立额度池 |

> 额度说明：扣额度按「创建 session」计（一次 session 约 1 小时有效，期间多轮对话不重复扣）。
> 普通模型按**每日 6 次基础 session / 太平洋日**理解（北京时间约 15:00 重置）；
> 非 Premium 特殊模型的可用性与额度以 freebuff 上游返回为准，官方规则可能调整。
> 请勿将本项目宣传为"无限量"。

---

## 👥 多账号与账号管理面板

### 多账号

- `FREEBUFF_TOKEN`（或 `credentials/` 文件）用英文逗号/多文件拼接多个账号。
- **账号选择策略**：优先复用已有活跃 session 缓存的账号（不扣额度）；
  无活跃缓存时轮询下一个；撞额度（429/空响应）自动冷却。
- 冷却状态保存在 Worker 内存，冷启动后重置；多实例间不共享。

### 管理面板（可选）

容器/服务启动后访问：

- `http://localhost:8877/accounts` —— 管理页面（账号状态/增删/注册入口）
- `GET /admin/api/accounts` —— 账号列表（15s 轮询，自动清理 banned 账号）
- `POST /admin/api/accounts/add` —— 手动添加账号
- `POST /admin/api/accounts/remove` —— 移除账号（token 写入 banned 名单）
- `GET /admin/api/banned` —— 查看 banned 名单
- `GET /admin/api/health` —— 账号池汇总 + 注册服务连通性

> 管理面板"自动注册/CF 邮箱"类功能依赖本地运行的可选注册服务
> （见下一节）；不启用时其余账号管理功能不受影响。

---

## 📨 注册服务 register_service（可选）

> 仅当你需要**自动批量注册 freebuff 账号**时才需要此服务；只用手头账号无需部署。

- 功能：CF 临时邮箱自动创建 → GitHub 协议注册 → Playwright 浏览器走
  freebuff OAuth 授权 → CLI 链路轮询拿 authToken，写入凭据文件。
- 依赖：仓库内 `oc/`（协议链路 + CF 临时邮箱客户端）、CloakBrowser/Camoufox（反检测浏览器）、curl_cffi。
- 启动（宿主机本地，FastAPI 端口 8899）：

```bash
# 方式一：Windows 一键（自动探测代理）
powershell -ExecutionPolicy Bypass -File start-register.ps1

# 方式二：直接运行（需先 pip install -r register_service/requirements.txt）
python -u register_service/register_service.py
```

- 环境变量：
  - `OC_DIR`：协议链路目录，默认取仓库内 `oc/`（与 `register_service/` 同级）
  - `REG_PROXY` / `REG_PROXIES`：代理/代理池；`REG_HEADLESS=1` 无头（链接授权）或 `0` 有头弹窗
- 容器化：仓库提供 `register_service/Dockerfile`（需挂载宿主机 `~/.cloakbrowser` 复用 CloakBrowser 二进制）。

> ⚠️ 注册服务涉及的自动注册行为请遵守目标平台条款，风险自担，详见下方免责声明。

---

## ⚠️ 免责声明

本项目仅供**技术交流与学习研究**使用。

- 本项目通过逆向 freebuff 桌面版/API 协议实现代理，**违反 freebuff 官方服务条款（ToS）**。
- 使用本项目存在**账号被封禁（banned）的风险**，且封禁为终态、不可恢复，请知悉并自行承担后果。
- 请勿用于商业用途或大规模滥用，请尊重 freebuff 服务提供方的运营。
- 使用者需自行遵守所在地法律法规及 freebuff 官方条款，作者不对任何账号损失或纠纷负责。

---

## 📄 License

本项目采用 [AGPL-3.0 License](LICENSE)。本项目参考并改写了
[freebuff2api](https://github.com/XxxXTeam/freebuff2api) 的部分代码与结构
（原项目为 AGPL-3.0），因此本项目同样以 AGPL-3.0 开源；
使用时请保留原版权声明，欢迎自由使用、修改与分享。
