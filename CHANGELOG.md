# Changelog

## [2.0.0] - 2026-11-06

### Added
- 双令牌鉴权（管理端 `X-Admin-Token`、Dify 回调 `X-Service-Token`），未配置令牌默认拒绝
- 工单幂等键 + 唯一索引 + 会话级去重，杜绝重复建单
- 坐席认领 / 指派接口与后台入口，工单状态变更审计
- `client_msg_id` 接口幂等，双击/重试不重复落库
- 请求 ID、访问日志、统一异常响应、`/healthz` 健康检查
- 请求体上限（`MAX_REQUEST_BYTES`，超限 413）
- 生产编排：`docker-compose.prod.yml` + Caddy 自动 HTTPS + 备份脚本
- CI/CD：lint、pytest、DSL 校验、镜像构建、GHCR 发布、手动批准部署与回滚
- `SECURITY.md` 与上传前密钥自检脚本

### Changed
- 对话链路改为单事务 + 线程池，修复半成品数据与事件循环阻塞
- 物流状态、退款金额改为由订单数据推导，不再随机/硬编码
- `/api/send_msg` 的 `session_id` 改为**必填**
- `PUT /api/ticket_status` 改为 JSON body 并校验状态枚举
- 错误响应统一为 `{code, msg, request_id}`

### Fixed
- 后台存储型 XSS（全部改为 `textContent`）
- 跨会话串单与共享匿名工单
- MySQL 下 `CREATE INDEX IF NOT EXISTS` 语法错误
- Dify 404 被误判为"订单不存在"
- 静态目录/数据库依赖启动目录的问题
- 重复索引（phone / intent / order_no / ticket_id）

### Security
- 旧版 DSL 备份、Dify 导出包、业务数据库、`.env` 全部排除出版本库
- compose 中 MySQL 密码改为必须由环境注入
