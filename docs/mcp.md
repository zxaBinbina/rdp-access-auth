# 本机 MCP 接口与授权边界

传输为 JSON-RPC 2.0 / stdio。使用 `rdp-auth mcp --identity <path>` 启动。身份文件须为当前 Linux 用户所有、0600 权限、非符号链接。日志不写入 MCP stdout。

| 操作 | 接口 | 授权 |
| --- | --- | --- |
| 读取状态、配置摘要、向导、环境、部署进度 | get_status / read_configuration / get_setup_guide / get_environment / deployment_status | 有效身份 |
| 申请权限 | request_authorization | 有效身份；由用户批准 |
| 平台检查与部署 | inspect_platforms / prepare_deployment / apply_deployment | 一小时授权；每次远程写入重新检查 |
| 设置与偏好 | update_configuration / update_preferences | 一小时授权；配置需 revision |
| 同步连接器 | sync_connectors | 一小时授权；可能中断当前连接 |
| 服务控制 | control_services | 一小时授权；pause 跨重启保留，resume 才解除 |
| 日志与凭据维护 | read_logs / clear_access_bans / remove_passkey | 一小时授权 |
| 旧部署迁移 | migration_plan / migrate_legacy_deployment / migration_status | 一小时授权 |
| 管理密码、Agent 策略 | change_management_password / set_agent_policy | 针对精确请求的单次危险操作确认 |

错误返回 `isError=true`，文本包含错误说明和 code。428 表示需要批准；401 表示身份无效或窗口未解锁；409 表示配置版本或部署状态冲突。MCP 不提供用户登录、批准请求和读取明文管理凭据的工具。

普通授权以后台批准时间计算固定 3600 秒，不滚动续期。危险操作请求等待最长 15 分钟；批准后 120 秒内只能按原参数执行一次。后台重启会丢弃窗口会话和待批准请求；已批准的普通权限保留到原到期时间。修改管理密码会撤销全部窗口会话和 Agent 临时权限。

权限由 root 后台验证 Unix socket 的实际 UID/PID、窗口会话或 Agent 身份。密码使用 scrypt，Agent 密钥仅保存摘要；前端不能自报“已解锁”。这约束软件提供的管理接口，不能阻止已拥有 root 权限的进程直接修改系统，也不能将同一 Linux 用户下的不受信任程序隔离为独立账户。

请用户自己操作图形批准窗口或独立终端，不索取管理密码。平台令牌通过工具参数传入会出现在所用 Agent 平台的会话上下文中；也可让用户先在软件窗口输入平台密钥，再由 Agent 使用已保存的连接继续部署。
