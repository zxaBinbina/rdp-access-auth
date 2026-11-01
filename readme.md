# RDP Access Auth 2.0 开发版

通过 SakuraFrp 和 Cloudflare Tunnel，为 Linux 远程桌面提供访问认证。2.0 使用独立 Qt WebView 窗口管理软件，提供系统托盘、首次使用向导和需要用户授权的本机 MCP 接口。远程访问者仍通过浏览器完成认证。

当前版本为 **2.0.0-dev.1**。RPM/DEB 使用 `2.0.0~dev.1` 表示开发版，排序低于正式版 `2.0.0`。`dist/` 中已有的 `2.0.0` 安装包是之前的构建，不包含本次演示功能。

## 无需配置，先体验界面

使用窗口顶部的“界面演示”打开独立演示窗口。也可以在应用启动器右键菜单选择“界面演示（无需配置）”，或运行：

```sh
./rdp-auth demo           # 主界面演示
./rdp-auth demo --wizard  # 首次配置向导演示
```

无需提供真实凭据或部署服务。每次打开或点击“重新生成数据”，都会随机填入域名、密码、隧道、设备和 Agent 等示例数据；也可以清空输入后继续向导。主界面与正常模式一样需要解锁后修改设置，密码确认框会自动填入本次演示的管理密码，错误密码无法解锁。演示控制栏可切换主界面和独立的配置向导。主界面侧栏提供管理功能；公网认证页预览仅在演示模式显示。演示窗口注册标有“演示模式”的系统托盘，支持主题切换及模拟操作。

演示数据只保存在当前进程的内存中，不读取或修改真实配置、不调用平台 API、不控制系统服务、不导出身份文件，也不会成为正式配置的默认值。关闭窗口会收起界面并锁定设置，保留托盘；选择“关闭演示”或托盘中的“退出演示”才会退出并丢弃数据。演示窗口可以与实际管理窗口同时打开。

## 从零安装

从 GitHub Actions 的 **Native packages** 运行记录下载与系统匹配的 Artifact，解压后安装其中的包。Fedora 44 使用 RPM；Ubuntu 24.04 使用 `py312` DEB；Debian 13 使用 `py313` DEB。不同 Python ABI 的包不能混装。

```sh
# Fedora 44
sudo dnf install ./rdp-access-auth-2.0.0~dev.1-1.fc44.x86_64.rpm

# Ubuntu 24.04
sudo apt install ./rdp-access-auth_2.0.0~dev.1_py312_amd64.deb

# Debian 13
sudo apt install ./rdp-access-auth_2.0.0~dev.1_py313_amd64.deb
```

安装包带有 Python 运行依赖；系统包管理器负责安装 Qt WebEngine、systemd 和权限认证组件。安装本身不创建远程资源、不收集密钥、不启动认证与隧道服务。

1. 从应用启动器打开 **RDP Access Auth**。尚未配置时进入独立的首次配置向导，点击“开始配置”。完成后进入主界面，后续启动不再显示向导。
2. 先选择配色和明暗模式，预览后继续设置独立的管理密码。系统会通过 Polkit 请求一次 Linux 管理员认证，建立受保护的本机后台并保存外观设置。
3. 选择部署方式。推荐“自动配置”；也可按手动教程准备隧道，或者选择“交给 Agent”。
4. 准备 SakuraFrp 账户及访问密钥、Cloudflare 托管域名。自动配置需要 Cloudflare API Token：Cloudflare Tunnel 编辑、DNS 编辑、区域读取；自动创建 Turnstile 时另需 Turnstile 编辑。
5. 选择节点、账户、域名和认证子域名，确认本机 RDP 端口。软件不安装或启用 RDP 服务，请先在系统设置中启用它。
6. 设置固定访问密码，可配置 Turnstile。软件下载并校验连接器和临时密码词库，展示部署计划。
7. 确认后创建或更新平台资源，安装本机服务。失败时保留资源 ID，可按原计划重试。
8. 使用另一网络打开认证域名，以固定密码、三词临时密码或 Passkey 完成认证，再使用原远程桌面客户端和系统账户连接。

临时密码成功使用后轮换。通行密钥注册默认关闭，可在桌面“访问凭据”中开启“允许添加通行密钥”，保存后立即生效；开启后仍需使用固定密码认证才能绑定。关闭注册不影响已有通行密钥登录。公网凭据页仅允许移除本次认证使用的通行密钥；其他密钥由桌面管理端移除。浏览器和 RDP 客户端必须使用同一公网 IPv4 出口。本机健康检查不能替代真实账号、HTTPS 路由和异网 RDP 验收。

## 窗口、托盘和自启动

关闭窗口只收起界面并重新锁定设置。托盘右键可打开设置、暂停／继续、重启和退出。隐藏托盘不停止服务；可从应用启动器再次打开窗口。

解锁后连续 10 分钟没有鼠标、键盘或滚动操作，设置会自动锁定并显示提示。后台状态刷新不会延长解锁时间。尝试操作已锁定的设置时，界面会提示先从右上角解锁。

点击“退出”会先说明影响，确认后停止认证、frpc 和 Cloudflare Tunnel，连接会断开。弹窗中的“下次开机不自启动”默认不勾选：不勾选时恢复开机启动，勾选则切为手动启动。**暂停跨重启保持，始终需要手动继续**，不受该复选框影响。设置中也可选择开机、桌面登录或手动启动。

管理状态无需解锁；修改前输入管理密码，解锁仅限当前窗口会话。两套配色为芙芙蓝与胡桃红，明暗可跟随系统或固定。公网认证页可独立保存外观偏好。

## Agent 与 MCP

在“Agent 管理”中登记身份并导出 MCP 配置。身份文件仅本人可读，配置示例：

```json
{
  "mcpServers": {
    "rdp-access-auth": {
      "command": "/usr/bin/rdp-auth",
      "args": ["mcp", "--identity", "/你的身份文件路径.json"]
    }
  }
}
```

只支持本机 stdio；经 SSH 在本机运行的 Agent 也可以使用。没有 TCP 管理接口。Agent 调用 `request_authorization` 后，由用户在窗口批准，或者在自己的独立终端运行：

```sh
rdp-auth approve <申请编号>
```

批准绑定已登记身份，固定有效一小时，调用不会续期。普通配置、密钥和服务操作在有效期内自动放行；管理密码、Agent 身份和策略的修改必须单独确认，确认绑定具体请求且只可使用一次。用户可随时撤销授权。Agent 不应索取管理密码、代替用户批准或绕过 MCP 修改系统文件。

部署接口依次为 `get_status`、`get_setup_guide`、`get_environment`、`inspect_platforms`、`prepare_deployment`、`deployment_status`、`apply_deployment`。设置修改使用 revision 防止覆盖；连接参数变化后调用 `sync_connectors`。查看 [接口与权限说明](docs/mcp.md)。

## KDE 兼容修复

检测到匹配的 KRDP 时启用私有修复库，不覆盖系统库。当前提供 Fedora 44 x86_64 的 KRDP `6.7.5-1.fc44` 修复，并核对原始库 SHA-256。其他版本保留官方实现，显示无匹配修复。已有用户级修复优先；新修复在 KDE 远程桌面下次启动时生效。

补丁处理帧确认、挂起恢复、输入坐标及图像尺寸等问题。源码、补丁、上游许可证在 `third_party/krdp`；不会接管 KDE 安装、登录账户或桌面设置。

## 维护和卸载

运行维护页提供脱敏日志、解封、凭据维护和旧部署迁移。修改认证配置后需重启；改域名、隧道或准入端口后使用“同步隧道并重启”。手动部署的 Cloudflare 路由仍由用户在平台同步。

- 管理凭据、授权记录和部署状态：`/etc/rdp-access-auth-control`。
- 认证配置与连接器凭据：`/etc/rdp-access-auth`，文件权限为 0600。
- 认证数据库：`/var/lib/rdp-access-auth`。
- 三个运行服务：`rdp-access-auth.service`、`rdp-access-auth-frpc.service`、`cloudflared-rdp-access.service`。

卸载通过 `sudo dnf remove rdp-access-auth` 或 `sudo apt remove rdp-access-auth`。软件包卸载会停止相关服务，保留配置、数据库、下载组件和远程平台资源，供重新安装恢复。KDE RDP 保留。删除远程隧道和清空密钥属于另行执行的清理操作。

旧版完整代码与历史保留在 [`legacy/1.x`](https://github.com/zxaBinbina/rdp-access-auth/tree/legacy/1.x)，`main` 用于 2.0 开发。

## 开发与验证

```sh
python3 -m venv .build/package-venv
.build/package-venv/bin/pip install -r requirements-package.txt
.build/package-venv/bin/python -m unittest discover -q
python3 tools/build-krdp-fix.py
.build/package-venv/bin/python tools/build_packages.py rpm
```

KDE 修复需要 Fedora 的 Qt/KF6、FreeRDP、KPipeWire 和 PAM 开发依赖。其他发行版的安装包及安装测试使用 [Native packages Workflow](.github/workflows/packages.yml)，不在开发机下载其他系统容器。工作流分别构建 Fedora 44、Ubuntu 24.04 和 Debian 13，验证匹配的 Python ABI，并执行安装／卸载和认证运行验证。Ubuntu 还运行真实 Qt WebView 隔离测试。

`tools/check_desktop_ui.py` 使用临时控制后台和模拟服务，驱动真实 WebView；调试端口和关闭 Chromium sandbox 的参数仅用于该测试，不用于生产启动。公网页面预览使用 `rdp-auth preview`，认证后端测试覆盖 Passkey、临时密码、防暴力破解、Turnstile、可信 IP 与本机准入。

[完整需求与后续计划](docs/2.0-requirements.md) · [1.x 源码维护参考](docs/1.x-maintenance.md)
