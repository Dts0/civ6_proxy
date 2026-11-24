# 故障排查

按“中转连接 → 房间发现 → 游戏会话”的顺序检查。能看到 Civ 6 房间，说明发现阶段大概率已成功，应把重点转向 `62056` 会话、防火墙和游戏内容一致性。

| 现象 | 重点检查 |
| --- | --- |
| 客户端无法启动 | 是否保留完整发布目录、是否同意 UAC、杀毒软件是否隔离 WinDivert 文件 |
| Docker 构建在 `load metadata` 超时 | VPS 无法访问 Docker Hub；配置云厂商镜像加速、覆盖 `PYTHON_IMAGE`，或直接使用 Python 运行 |
| 中转服务器没有响应 | VPS 地址和端口、云安全组、Linux 防火墙、服务进程是否运行 |
| 房间码创建成功但成员无法加入 | 所有人是否填写同一中转地址、房间码是否完整、房间是否已满 |
| 工具成员在线但 Civ 6 看不到房间 | 确认房主已创建“局域网”房间、游戏进程已被检测、发现包发送计数增长，并按下文检查每台玩家机的入站规则 |
| 能看到房间但无法加入 | UDP `62056`、Windows 防火墙、安全软件、游戏版本/DLC/模组 |
| 进入后频繁掉线 | RTT、VPS 丢包和带宽、运营商 UDP 质量、成员是否反复离线 |
| 提示虚拟网段冲突 | 关闭冲突 VPN，或调整占用 `10.250.250.0/24`、`172.31.250.0/24`、`192.168.250.0/24` 的路由 |

## Windows 检查

确认客户端处于管理员权限，并且发布目录包含：

```text
Civ6RelayClient.exe
_internal\pydivert\windivert_dll\WinDivert64.dll
_internal\pydivert\windivert_dll\WinDivert64.sys
```

Windows 第一次询问网络访问权限时，允许 Civ 6 和 Civ6RelayClient。创建或加入房间时，客户端会用本机选中的完整虚拟网段创建或更新本地规则 `Civ6Relay-Game-Inbound`。断开后规则保留，下次连接时更新。

如果成员在线且 RTT 正常，却看不到房间，请在每台玩家机上查看运行日志。出现“配置 Windows 防火墙失败”时客户端仍会继续连接。用管理员 PowerShell 检查生效的规则，比较 `RemoteAddress` 与该客户端状态栏的虚拟网段，并确认 `LocalPort` 包含 `62056` 和 `62900-62999`。

```powershell
$rule = Get-NetFirewallRule -PolicyStore ActiveStore -Name 'Civ6Relay-Game-Inbound'
$rule | Format-Table Enabled, Direction, Action, Profile
$rule | Get-NetFirewallAddressFilter
$rule | Get-NetFirewallPortFilter
```

使用旧版客户端或需要手动配置时，可在管理员 PowerShell 中运行以下命令。将示例网段替换为本机状态栏显示的值。

```powershell
$subnet = '10.250.250.0/24'
$params = @{
    Name = 'Civ6Relay-Game-Inbound'
    Direction = 'Inbound'
    Action = 'Allow'
    Enabled = 'True'
    Profile = 'Any'
    Protocol = 'UDP'
    LocalPort = '62056', '62900-62999'
    RemoteAddress = $subnet
    PolicyStore = 'PersistentStore'
}
if (Get-NetFirewallRule -PolicyStore PersistentStore -Name $params.Name -ErrorAction SilentlyContinue) {
    Set-NetFirewallRule @params
} else {
    New-NetFirewallRule @params -DisplayName 'Civ6Relay Inbound'
}
```

如果 GUI 的 RTT 持续为 `-` 或状态变成“中转响应超时”，先排查 VPS，不要继续调游戏设置。

## 服务端检查

Docker：

```bash
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs --tail=100
```

systemd：

```bash
systemctl status civ6relay
journalctl -u civ6relay -n 100 --no-pager
sudo ss -ulnp | grep 42056
```

## 收集日志

客户端右侧“导出”按钮会保存当前运行日志。反馈问题时同时说明：

- 房主和加入者的 Windows 版本。
- Civ 6 使用 DX11 还是 DX12。
- 玩家数量及问题发生阶段。
- GUI 显示的 RTT、收发计数和成员状态。
- 服务端同一时间段的生命周期日志。

日志不包含游戏负载和会话令牌，但可能包含中转地址、昵称和房间码，公开发送前请自行检查。
