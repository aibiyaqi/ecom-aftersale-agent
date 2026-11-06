# 安全与隐私

## 报告安全问题

请**不要**在公开 Issue 里贴密钥、订单号、手机号或完整请求日志。
请通过 GitHub 仓库的 **Security → Report a vulnerability**（Private vulnerability reporting）提交，或在 Issue 里只描述现象、把敏感内容单独发给维护者。

## 敏感信息处理原则

本仓库**永远不提交**以下内容（均已写入 `.gitignore`）：

| 类型 | 示例 | 说明 |
|---|---|---|
| 环境变量 | `.env`、`.env.prod`、`.env.*` | 只提交 `.env.example` 模板 |
| 密钥/证书 | Dify API Key、`ADMIN_TOKEN`、`SERVICE_TOKEN`、`*.pem`、`*.key` | 通过环境变量/CI Secrets 注入 |
| 业务数据库 | `orders.db`、`*.sqlite` | 含手机号与对话内容；演示数据由 `app/seed.py` 生成 |
| Dify 导出物 | `*.ifpkg`、`dify_app_dsl.v1.backup.yml` | 含工作流、数据集 ID、历史隧道地址 |
| 运行时产物 | `__pycache__`、`.pytest_tmp`、日志 | 无业务价值 |

## 演示数据声明

仓库内出现的订单号（`OD2026001` 等）、手机号、聊天内容**均为虚构**，仅用于本地演示与测试，不对应真实用户。
`app/seed.py` 中的手机号是功能必需的测试数据（用于"按手机号查订单"），不是真实号码。

## 密钥轮换

- Dify Key、`ADMIN_TOKEN`、`SERVICE_TOKEN` 一旦怀疑泄露，**立即在控制台重建**，再更新部署环境变量。
- 生产环境每个环境（dev/staging/prod）使用独立密钥，不复用。
- 服务器上的 `.env` 权限建议 `600`，属主为部署用户。

## 如果误提交了密钥

1. 立刻在对应平台**吊销/轮换**该密钥（这比改历史更重要）。
2. 从工作区移除文件，提交删除。
3. 需要彻底抹除历史时使用 `git filter-repo`（或 BFG），然后强制推送，并通知所有协作者重新克隆。
4. 检查 GitHub Secret scanning 告警，确认没有其它泄露点。

## 上传前自检

```bash
python tools/preflight_secrets.py        # 阻断密钥/数据库/私钥
python tools/preflight_secrets.py --strict
```

CI 中另用 gitleaks 扫描提交历史，两道防线互不替代。

## 生产部署安全清单

- [ ] `ADMIN_TOKEN` / `SERVICE_TOKEN` 为随机值，非示例值
- [ ] CORS 只放行真实前端域名，不使用 `*`
- [ ] 全站 HTTPS（反向代理自动签发证书）
- [ ] 后端管理接口不暴露在公网，或置于网关鉴权之后
- [ ] 数据库文件/卷有备份，且备份文件不进入代码仓库
- [ ] 日志不打印完整手机号、密钥、请求体
