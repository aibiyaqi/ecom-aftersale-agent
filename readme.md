# 电商智能订单售后处理系统

基于 **FastAPI + Dify + SQLAlchemy** 的电商售后智能客服系统：Dify 负责意图理解与回复生成，后端负责订单数据、工单流转、对话留痕与兜底。

> 当前版本 **2.0.0**：在原有 POC 基础上补齐了**接口鉴权、工单幂等、单事务一致性、可观测性、测试与容器化**。
> 升级带来的接口变化见 [破坏性变更](#破坏性变更必须知道)。

---

## 目录

- [架构与分工](#架构与分工)
- [快速开始](#快速开始)
- [鉴权](#鉴权)
- [配置](#配置)
- [接口清单](#接口清单)
- [核心设计](#核心设计)
- [测试](#测试)
- [Docker 部署](#docker-部署)
- [破坏性变更（必须知道）](#破坏性变更必须知道)
- [生产化路线](#生产化路线)
- [已知限制](#已知限制)

---

## 架构与分工

```
客户浏览器 (chat.html)
      │  POST /api/send_msg（session_id + client_msg_id）
      ▼
FastAPI 后端 ──────────────────────────────────────────────┐
      │  ① 幂等检查（client_msg_id）                        │
      │  ② 取会话上下文 conversation_id                     │
      │  ③ 调用 Dify（chat / workflow 两种模式）             │
      │  ④ 解析结构化结果，失败则本地意图兜底                 │
      │  ⑤ 单事务落库（工单 + 对话记录）                      │
      ▼                                                     │
Dify 工作流 ── HTTP 节点（X-Service-Token）──► /api/get_order│
      │                                        /api/get_logistics
      │                                        /api/get_refund
      │                                        /api/create_ticket
      ▼                                                     │
SQLite / MySQL（t_order / t_chat_record / t_service_ticket）◄┘
```

**边界**：所有语义理解、意图分类、话术生成在 Dify；后端只做数据持久化、接口调度、业务流转、兜底与审计。

---

## 快速开始

```bash
# 1. 环境（Python 3.10+，推荐 3.12）
conda create -n dify-env python=3.12 -y && conda activate dify-env

# 2. 依赖
cd ecom_backend
pip install -r requirements.txt

# 3. 配置：复制模板并填入你的 Dify Key 与两个 Token
copy ..\.env.example ..\.env      # Windows
# cp ../.env.example ../.env      # macOS/Linux

# 4. 启动（必须在 ecom_backend 目录，或使用绝对路径的 uvicorn --app-dir）
uvicorn app.main:app --host 0.0.0.0 --port 5000 --reload
```

访问：

| 页面 | 地址 |
|---|---|
| 智能客服 | <http://localhost:5000/static/chat.html> |
| 管理后台 | <http://localhost:5000/static/admin.html>（输入 `ADMIN_TOKEN`） |
| 健康检查 | <http://localhost:5000/healthz> |
| OpenAPI 文档 | <http://localhost:5000/docs> |

启动时会自动完成：建表 → 旧库加列/补索引 → 补齐演示订单实付金额。**迁移是幂等的**，可重复启动。

演示订单：`OD2026001` ~ `OD2026005`，手机号 `138****1111` ~ `138****5555`（虚构数据，仅用于本地演示）。

---

## 鉴权

两套令牌，职责分离，互不通用：

| 范围 | 请求头 | 配置项 |
|---|---|---|
| 管理后台（`/api/chat_list` 等） | `X-Admin-Token` 或 `Authorization: Bearer <token>` | `ADMIN_TOKEN` |
| Dify 回调（4 个业务接口） | `X-Service-Token` 或 `Authorization: Bearer <token>` | `SERVICE_TOKEN` |
| 客户对话（`/api/send_msg`） | 公开，按 IP 限流 | `RATE_LIMIT_PER_MINUTE` |

```bash
curl -X POST http://localhost:5000/api/get_order \
  -H "Content-Type: application/json" \
  -H "X-Service-Token: $SERVICE_TOKEN" \
  -d '{"order_no":"OD2026001"}'

curl "http://localhost:5000/api/chat_list?page=1&size=20" \
  -H "X-Admin-Token: $ADMIN_TOKEN"
```

- 令牌未配置时接口返回 **503**（拒绝裸奔），只有显式设置 `ALLOW_INSECURE_NO_TOKEN=true` 才放行，且每次请求都会打警告日志。
- 令牌比较使用 `hmac.compare_digest`，避免时序侧信道。
- 管理后台页面把令牌存在 `sessionStorage`，关闭标签页即失效。

> **Dify 侧必须同步改**：4 个 HTTP 节点都要加请求头 `X-Service-Token: <你的 SERVICE_TOKEN>`，否则全部 401。

安全与隐私策略（密钥、演示数据、误提交处置）见 [SECURITY.md](SECURITY.md)；上传前自检：

```bash
python tools/preflight_secrets.py
```

---

## 配置

全部通过环境变量（项目根 `.env` 或容器 env），完整清单见 [.env.example](.env.example)。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite:///<ecom_backend>/orders.db` | 数据库连接，绝对路径，不随启动目录漂移 |
| `ADMIN_TOKEN` | 空 | 管理端令牌，空则拒绝访问 |
| `SERVICE_TOKEN` | 空 | Dify 回调令牌，空则拒绝访问 |
| `DIFY_APP_MODE` | `chat` | `chat`=advanced-chat；`workflow`=工作流（推荐） |
| `DIFY_API_URL` | 按模式推导 | 显式覆盖端点 |
| `DIFY_API_KEY` | 空 | Dify 应用密钥，空则直接走本地兜底 |
| `DIFY_TIMEOUT` | `60` | 秒；超时不重试（避免用户等待翻倍） |
| `DIFY_MAX_RETRIES` | `1` | 仅对 5xx / 连接错误重试 |
| `CORS_ORIGINS` | 本地两个地址 | 逗号分隔白名单 |
| `RATE_LIMIT_PER_MINUTE` | `30` | 单 IP 每分钟消息数，`0` 关闭 |
| `MAX_USER_TEXT_LENGTH` | `1000` | 用户消息长度上限 |
| `MAX_REQUEST_BYTES` | `65536` | 请求体上限，读取阶段拦截（413） |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `ALLOW_INSECURE_NO_TOKEN` | `false` | 仅本地调试用 |

---

## 接口清单

### 客户侧（公开）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/send_msg` | 发消息，返回回复/意图/工单号 |

请求：`{"user_text": "...", "session_id": "SID-...", "client_msg_id": "uuid"}`
- `session_id`：**必填**（缺失 422）。会话隔离与"上一单"回溯都依赖它，缺省会造成跨用户串单。
- `client_msg_id`：**幂等键**，相同值重复提交只处理一次、只落一条记录

### Dify 回调（需 `X-Service-Token`）

| 方法 | 路径 | 入参 | 说明 |
|---|---|---|---|
| POST | `/api/get_order` | `order_no` 或 `phone` | 订单详情（含实付金额） |
| POST | `/api/get_logistics` | 同上 | 物流信息，状态由发货状态推导，**幂等** |
| POST | `/api/get_refund` | 同上 | 退款状态与金额，金额来自 `paid_amount` |
| POST | `/api/create_ticket` | `order_no`（可选）+ `user_text` + `risk_reason` | 建单，**幂等**；有订单号按订单去重，无订单号按"会话 / 诉求内容+日期"去重 |

### 管理后台（需 `X-Admin-Token`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/chat_list` | 分页 + `intent` / `is_manual` / `session_id` / `order_no` / `keyword` 筛选 |
| GET | `/api/ticket_list` | 分页 + `status` / `assignee` / `order_no` / `keyword` 筛选 |
| PUT | `/api/ticket_status` | JSON body：`{"ticket_id","status","note"}`，写审计 |
| POST | `/api/ticket_claim` | 坐席认领：待处理→处理中，`X-Operator` 记录负责人；重复认领 409 |
| POST | `/api/ticket_assign` | 管理员指派：`{"ticket_id","assignee"}`，同样写审计 |
| GET | `/api/ticket_events` | 工单操作审计（认领/指派/状态变更：谁、何时、从什么改成什么） |
| GET | `/api/order_detail` | 订单详情（`order_no` 或 `phone`） |

### 统一错误结构

```json
{"code": 401, "msg": "管理后台未授权：缺少或错误的 X-Admin-Token", "request_id": "a1b2c3d4e5f6"}
```

`422` 额外返回 `errors: [{field, message}]`；`500` 只返回通用文案，堆栈只进日志。

---

## 核心设计

### 1. 工单幂等（防刷单）

`t_service_ticket.dedupe_key` = `sha256((订单号 | 会话) + 风险类型 + 状态)`，数据库唯一索引兜底：

- 同一订单重复申请退货 → 复用同一张工单
- Dify 故障且拿不到订单号时，**按会话**去重，一个会话只建一张 "系统自动转人工" 工单
- 并发请求用 `SAVEPOINT + IntegrityError` 回查，不会 500，也不会重复
- Dify 传来的自由文本 `risk_reason` 会归一化成受控枚举（退货退款 / 投诉 / 人工 / 系统异常），避免模板残留 `{{查询退款进度}}` 撑裂幂等键

### 2. 单事务 + 非阻塞

`/api/send_msg` 的数据库读写全部收进一个 `SessionLocal()`：

- 工单与对话记录同一次 `commit`，失败整体 `rollback`，不会出现"有工单没记录"
- 同步 SQLAlchemy 调用通过 `run_in_threadpool` 执行，不阻塞事件循环
- 建单走 `services/tickets.py` 单一入口，本地兜底、Dify 回调、自动转人工三条路径行为一致

### 3. 可观测性

- 每个请求有 `X-Request-ID`（可透传），日志格式含 request_id，响应头回传
- 访问日志记录方法、路径、状态码、耗时
- 未捕获异常统一 500 + request_id，堆栈只进服务端日志
- `/healthz` 检查数据库连通性，供容器探活

### 4. 本地意图兜底

Dify 超时 / 5xx / 空回复时，本地关键词分类器接管（查订单、查物流、退货退款、投诉）：

- 能查库直接答的，不建单、不打扰人工
- 本地也接不住的（如"在吗"），才自动转人工并建单（按会话去重）
- 带否定词（"不要退款了"）不会误建退款单
- Dify 有回复但缺 `intent` 时，本地规则补记意图（原实现该分支不可达）
- 只有带"订单不存在"等业务语义的 404 才当成业务结果；端点配错/应用删除按故障处理

### 5. 会话隔离与请求体限制

- `session_id` 必填：缺失直接 422，避免"上一单"退化成全局查询、不同用户共享匿名工单
- 请求体超过 `MAX_REQUEST_BYTES`（默认 64KB）在读取阶段返回 413，不会先把大包读进内存
- 限流按客户端 IP 计（反代部署时需配置可信代理，否则所有人共享一个桶）

---

## 测试

```bash
cd ecom_backend
pip install -r requirements-dev.txt
python -m pytest
```

覆盖 42 个用例：鉴权失败/越权/503、订单与手机号查询、物流确定性、退款金额、建单幂等、状态机与审计、认领/指派、Dify 故障兜底、消息幂等、限流、参数校验、静态页 XSS 约束、迁移幂等、启动目录无关性。

测试使用独立库 `ecom_backend/.pytest_tmp/test.db`，**不会动你的 `orders.db`**；Dify 调用被替换为假实现，不消耗额度。

---

## Docker 部署

```bash
# 1. 准备 .env（至少填 ADMIN_TOKEN / SERVICE_TOKEN / DIFY_API_KEY）
copy .env.example .env

# 2. 启动（默认 SQLite，数据在命名卷 ecom_data）
docker compose up -d --build

# 3. 需要 MySQL 时
docker compose --profile mysql up -d
# 并在 .env 里设置：
# MYSQL_ROOT_PASSWORD=... / MYSQL_PASSWORD=...
# DATABASE_URL=mysql+pymysql://ecom:<MYSQL_PASSWORD>@mysql:3306/ecom?charset=utf8mb4
# PyMySQL 已在 requirements.txt 中；MySQL 8 的 caching_sha2_password 还需取消 cryptography 注释
```

容器内 `/data/orders.db` 持久化；镜像以非 root 用户运行，自带 HEALTHCHECK。

### 生产部署（Caddy + 自动 HTTPS）

```bash
# .env 至少补 DOMAIN=aftersale.example.com
docker compose -f docker-compose.prod.yml up -d --build

# 备份数据库（建议部署前 + 每日 cron）
bash deploy/backup.sh
```

- `app` 不映射公网端口，只通过 `caddy` 反代；证书由 Caddy 自动签发。
- 资源限制 `512m / 1 CPU`、日志轮转 `10m×3`，可在 [docker-compose.prod.yml](docker-compose.prod.yml) 调整。
- 数据卷：`ecom_data`（业务库）、`caddy_data`（证书），升级容器不丢数据。
- 部署后把 Dify 环境变量 `BACKEND_BASE_URL` 指向 `https://<DOMAIN>`。

### 版本管理

仓库已推送到 GitHub（私有）：<https://github.com/aibiyaqi/ecom-aftersale-agent>

- `main`：52 个文件，无 `.env` / 数据库 / Dify 导出包
- `v2.0.0`：已发布 Release，镜像 `ghcr.io/aibiyaqi/ecom-aftersale-agent:v2.0.0`
- 提交作者：`aibiyaqi@users.noreply.github.com`

后续提交：

```bash
git add -A && git commit -m "feat: ..."
git push
# 发版：git tag v2.0.1 && git push origin v2.0.1
```

> 本机尚未安装 Git，仓库最初由容器内 git 创建；装上 Git 后可直接提交。
> 想换提交邮箱：`git commit --amend --reset-author`。
> 后续提交优先用 `scripts/git-init.ps1`（会先跑密钥自检），或直接 `git add/commit`。

上传前自检：`python tools/preflight_secrets.py`；隐私策略见 [SECURITY.md](SECURITY.md)。

### CI/CD

仓库：`github.com/aibiyaqi/ecom-aftersale-agent`（私有）

| 工作流 | 触发 | 内容 |
|---|---|---|
| [ci.yml](.github/workflows/ci.yml) | push main / PR | ruff、compile、pytest、DSL 校验、镜像构建、gitleaks |
| [release.yml](.github/workflows/release.yml) | 推送 `v*` tag | 构建并推送 GHCR 镜像、Trivy 扫描（高危阻断）、生成 Release |
| [deploy.yml](.github/workflows/deploy.yml) | 手动 | 人工批准 → SSH 到服务器执行 [deploy.sh](deploy/deploy.sh) |

**首次部署前准备**

1. 服务器安装 Docker + Compose，创建 `/opt/ecom`，把仓库文件放进去（或 `git clone`）。
2. 在服务器创建 `.env`（`DOMAIN`、两个令牌、Dify Key、可选 `DATABASE_URL`），权限 `600`。
3. `docker login ghcr.io`（用带 `read:packages` 的 PAT）。
4. GitHub 仓库 Settings → Environments 新建 `production`，添加 Required reviewers。
5. 配置 Secrets：`SSH_HOST`、`SSH_USER`、`SSH_KEY`（部署私钥）、可选 `SSH_PORT`。

**上线与回滚**

```bash
# 发布：打 tag，自动构建镜像
git tag v2.0.0 && git push origin v2.0.0

# 部署：Actions → Deploy → 输入 tag + confirm=deploy
# 服务器端等价命令：
bash deploy/deploy.sh v2.0.0
```

`deploy.sh` 会先备份、再拉镜像重启、等待健康检查；失败自动切回 `.current_tag` 记录的上一版。

**服务器目录约定**

```text
/opt/ecom/
├── .env                  # 600，不进仓库
├── docker-compose.prod.yml
├── deploy/
├── .current_tag          # 当前版本，自动维护
└── backups/              # SQLite 备份，保留 7 天
```

---

## 破坏性变更（必须知道）

从 1.x 升级到 2.0.0，以下接口行为已变：

1. **所有 `/api/*` 业务与管理接口都要带 Token**（原先完全公开）。Dify 的 4 个 HTTP 节点必须补 `X-Service-Token`。
2. `PUT /api/ticket_status` 从 **query 参数**改为 **JSON body**：
   ```json
   {"ticket_id": "TK...", "status": "处理中", "note": "已分配客服"}
   ```
   非法状态返回 **422**（原先可以写入任意字符串）。
3. `GET /api/order_detail` 未找到时返回 **HTTP 404**（原先 HTTP 200 + `{"code":404}`），成功仍返回 `{"code":200,"data":{...}}`。
4. `/api/get_order|get_logistics|get_refund` 入参改为 `order_no` **或** `phone`（至少一个），不再接受空 body。
5. 错误响应统一为 `{code, msg, request_id}`，不再有裸 `detail` 字段。
6. 物流状态不再随机、退款金额不再固定 3999.00，均来自订单数据；`t_order` 新增 `paid_amount` 列（迁移自动补齐演示数据）。

---

## 生产化路线

按优先级（对应上一轮差距分析）：

| 优先级 | 事项 | 现状 | 建议 |
|---|---|---|---|
| P0 | 鉴权与 PII | ✅ 已做（双令牌、管理端保护手机号明文） | 接入企业 SSO / 坐席账号体系 |
| P0 | 工单幂等 | ✅ 已做（唯一索引 + 会话级去重） | 多实例下同样有效 |
| P0 | 事务与并发 | ✅ 已做（单事务 + 线程池） | 后续可换 async 驱动 |
| P0 | Dify 契约 | ⚠️ 后端兼容 chat/workflow；DSL 已修 URL/令牌/超时/人工分支 | 开结构化输出，切 `DIFY_APP_MODE=workflow` |
| P1 | 可观测性 | ✅ 请求 ID / 访问日志 / 统一异常 | 接 Prometheus + 日志聚合 |
| P1 | 测试与部署 | ✅ 42 用例 + Dockerfile + compose | 接 CI 与镜像扫描 |
| P1 | MySQL | ✅ 连接串可切，索引按方言生成 | 复杂 DDL 变更仍建议 Alembic |
| P2 | Redis | ❌ 未引入（单实例用内存限流） | 多副本时用于限流、幂等锁、异步队列 |
| P2 | Milvus | ❌ 不引入 | 售后 FAQ 规模用 Dify 内置知识库足够，自建选 pgvector |
| P2 | MCP | ❌ 不引入 | 仅当同一套工具要给多客户端复用时再包一层 |

### Dify 工作流升级（v2）

[dify_app_dsl.yml](dify_app_dsl.yml) 已由 [tools/upgrade_dify_dsl.py](tools/upgrade_dify_dsl.py) 原地升级，原文件备份为 `dify_app_dsl.v1.backup.yml`。本次修复：

| 问题 | 修复 |
|---|---|
| 4 个 HTTP 节点写死公网隧道地址（ngrok 域名） | 改用环境变量 `{{#env.BACKEND_BASE_URL#}}` |
| 回调接口没有任何鉴权头 | 统一加 `X-Service-Token:{{#env.SERVICE_TOKEN#}}` |
| HTTP 节点 `max_*_timeout: 0`（永不超时） | 收敛为连接 5s / 读写 10s，重试 2 次 |
| 查订单/物流/退款请求体写死 `OD2026001` | 改为引用参数提取器的 `order_id`；建单改用代码节点生成的 JSON |
| `human-input` 节点（3 小时超时、无 action 出边、阻塞 API） | 删除该节点，建单后直接进入"已提交人工处理"回复 |
| `{{{#var#}}}}` 等非法模板占位符（20 处） | 归一化为 `{{#var#}}` |
| 模型名 `qwen3.7-plus`（通义无此模型） | 改为 `qwen-plus` |

**导入后必须做**：

1. 在 Dify「环境变量」里填入 `BACKEND_BASE_URL`（后端公网地址，不带结尾斜杠）和 `SERVICE_TOKEN`（与后端 `.env` 完全一致）。
2. 重新配置通义模型凭据（当前调用返回 `Incorrect model credentials`），并确认 `qwen-plus` 在你账号下可用，否则改成 `qwen-max`/`qwen-turbo`。
3. 知识检索节点使用的是 `qwen3-rerank`，若该模型不可用，请关闭 reranking 或换成可用模型。
4. 参数提取器指令里用的是 `{{conversation_history}}` / `{{query}}`，导入后确认能正常渲染（Dify 系统变量应写成 `{{#sys.query#}}` 形式）。
5. 导入 → 发布 → 用 `curl` 或页面验证 4 条分支。

升级脚本可重复执行：从备份恢复后运行 `python tools/upgrade_dify_dsl.py`，它会做 YAML 结构校验（节点可达性、重复键、边指向、HTTP 节点配置）。

> DSL 只做了静态校验，**没有在 Dify 实例里导入验证**——导入后的分支实测仍需要你在控制台完成。

### 排查：Dify 返回 400 `Incorrect model credentials`

已用现有 `DIFY_API_KEY` 实测确认（`python tools/verify_dify.py`）：

```text
[OK] 应用：电商智能订单售后处理系统（模式：advanced-chat）
[FAIL] HTTP 400 {"code":"invalid_param","message":"Run failed: [Tongyi] Incorrect model credentials provided, please check and try again."}
```

这是**工作区的模型供应商凭据问题**，不是后端代码问题（Key 本身有效，`/v1/info` 返回 200）。Dify 开放 API 不提供修改供应商凭据的接口，必须在控制台操作：

1. 登录 <https://cloud.dify.ai>，右上角头像 → **Settings / 设置** → **Model Provider / 模型供应商**。
2. 找到 **Tongyi（通义千问）**，检查 API Key；失效就去阿里云百炼控制台重新生成 DashScope Key 填入并保存。
3. 回到应用编排页，逐个 **LLM 节点**确认模型在下拉列表中真实存在（`qwen-plus` / `qwen-max` / `qwen-turbo`），不要手填不存在的模型名。
4. 发布应用后执行：

```bash
python tools/verify_dify.py
# 或指定消息
python tools/verify_dify.py --query "查物流 OD2026001"
```

退出码：`0` 正常、`2` 模型凭据/模型名问题、`3` Key 无效、`4` 其他错误。

> 如果 `/v1/info` 的应用名与 `/v1/parameters` 的开场白/站点标题对不上（例如站点标题是 `ITSM Copilot`），说明这个应用是从别的模板改的、或 Key 属于另一个应用。建议用 v2 DSL **新建一个应用**、单独生成 API Key 填入 `.env`，不要在旧应用上反复改。

---

## 已知限制

- **SQLite 单写者**：适合单实例/演示；多副本请切 MySQL（`DATABASE_URL` 一行）。
- **限流是单实例内存实现**：重启清零、多副本不共享，生产换 Redis。
- **坐席只有轻量接管**：有认领/指派与操作审计，但没有登录、在线状态、客户消息回推与工单转交历史（`X-Operator` 是自报的）。
- **售后无完整状态机**：退货→审核→退款→回寄仍靠单字段，未做审核流与金额校验。
- **PII**：手机号明文存储（管理端已鉴权），生产建议加密/脱敏 + 访问审计 + 保留期限。
- **迁移脚本是轻量实现**：建表/加列/建索引已区分 SQLite 与 MySQL 方言；更复杂的 DDL 变更建议改用 Alembic。
- **无订单号工单的归属**：既没有订单号也没有 `session_id` 时，按"诉求内容+日期"去重，两位客户同一天发同一句话会合并成一张单——在缺少用户身份的前提下这是有意的取舍。

---

## 项目结构

```
ecom_backend/
├── app/
│   ├── main.py               # 入口：lifespan 迁移/种子、中间件、异常、路由
│   ├── config.py             # 环境变量配置（唯一配置源）
│   ├── database.py           # 引擎/会话（SQLite PRAGMA 按方言隔离）
│   ├── models.py             # 4 张表 + 索引 + 幂等键
│   ├── migrations.py         # 轻量幂等迁移（加列/补索引/回填）
│   ├── seed.py               # 演示订单
│   ├── security.py           # 双令牌鉴权
│   ├── observability.py      # 请求 ID / 日志 / 统一异常
│   ├── ratelimit.py          # 单实例滑动窗口限流
│   ├── dify_client.py        # Dify 客户端（chat/workflow、重试、归一化）
│   ├── schemas.py            # 入参校验
│   ├── constants.py          # 状态/意图/风控枚举
│   ├── services/
│   │   ├── tickets.py        # 建单幂等、认领/指派、状态机、审计
│   │   └── orders.py         # 订单查询、物流推导、退款金额
│   ├── api/
│   │   ├── chat.py           # /api/send_msg（编排 + 兜底 + 单事务）
│   │   ├── dify_apis.py      # 4 个回调接口
│   │   └── admin.py          # 后台接口（含认领/指派）
│   └── static/               # chat.html / admin.html
├── tests/                    # pytest（42 用例）
├── Dockerfile / requirements*.txt / pytest.ini
```

顶层还有：

- `dify_app_dsl.yml`（v2 工作流）、`tools/upgrade_dify_dsl.py`（升级脚本）、`tools/verify_dify.py`（连通性诊断）、`tools/check_dify_dsl.py`（CI 校验）
- `docker-compose.yml`（开发）/ `docker-compose.prod.yml`（生产，Caddy + HTTPS）
- `deploy/Caddyfile`、`deploy/backup.sh`（SQLite 备份）、`deploy/deploy.sh`（服务器部署与回滚）
- `.github/workflows/`（ci / release / deploy）
- `SECURITY.md`（密钥与隐私策略）、`tools/preflight_secrets.py`（上传前自检）
- `scripts/git-init.ps1`（Git 安装后一键初始化仓库）、`CHANGELOG.md`、`pyproject.toml`

> 旧版 DSL 备份 `dify_app_dsl.v1.backup.yml` 只保留在本地，已被 `.gitignore` 排除。

---

## 变更记录

**2.0.0**
- 新增双令牌鉴权与 503 拒绝裸奔策略
- 新增工单幂等键 + 唯一索引 + 会话级去重 + 状态机审计
- 新增坐席认领/指派接口与前台入口，工单从"建了没人管"变成可接管
- 对话链路改为单事务 + 线程池，修复半成品数据与事件循环阻塞
- 新增 `client_msg_id` 接口幂等，修复双击重复落库
- 物流/退款去随机、去硬编码，支持手机号查询
- 新增请求 ID、访问日志、统一异常、健康检查
- 新增 42 个 pytest 用例、Dockerfile、compose、`.env.example`
- 修复后台存储型 XSS、工单状态任意写入、静态目录依赖启动目录等问题
- Dify 工作流 DSL 升级：环境变量、服务令牌、超时收敛、去掉写死订单号与阻塞式 human-input
- 修复跨会话串单：`session_id` 改为必填，消除全局"上一单"回退与匿名工单共享
- MySQL 真正可切：索引语句按方言生成、补齐 PyMySQL，修复 MySQL 下的迁移语法错误
- 新增请求体上限（413）与重复索引清理，补 Dify 端点/模式自洽与 404 业务判定
- 新增生产编排（Caddy 自动 HTTPS、资源/日志限制）与 SQLite 备份脚本
- 新增 SECURITY.md、上传前密钥自检、`.gitignore` 覆盖数据库/Dify 导出包
