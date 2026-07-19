# Civ 6 公网 UDP 中继

Civ6Relay 让 2–8 名玩家通过一台公网 Linux VPS 使用 Civilization VI 的“局域网”模式联机。每名玩家运行 Windows 客户端；客户端通过 WinDivert 捕获房间发现和游戏 UDP 包，中转服负责按房间与玩家转发。

> 当前状态：协议、中转服务、Windows 客户端、发布打包和自动化测试已经完成。正式使用前仍需在不同 NAT 的真实 Windows 电脑上完成 Civ 6 双机及多人验收。

## 功能

- 转发 `62900–62999` 房间发现广播并保留原始 LAN 广播。
- 透明转发 UDP `62056` 游戏会话，不与 Civ 6 抢占端口。
- 支持 2–8 人、房间码、成员列表、RTT 和流量统计。
- 支持 HMAC 身份验证、重放保护、NAT 地址更新和大包分片。
- 提供 Windows x64 GUI、Linux Docker Compose 和 systemd 部署文件。

## 五分钟开始

### 1. 部署中转服

在拥有公网 IPv4 的 Linux VPS 上开放 UDP `42056`，然后从工程根目录运行：

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

没有 Docker 时可直接运行：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install .
python -m civ6relay.server --listen 0.0.0.0:42056
```

### 2. 启动 Windows 客户端

解压整个 `Civ6RelayClient-windows-x64.zip`，不要只复制 exe。以管理员身份运行：

```text
Civ6RelayClient\Civ6RelayClient.exe
```

所有玩家填写同一个 `VPS公网IP:42056` 和各自昵称。房主点击“创建房间”，其他玩家输入房主发来的 10 位房间码并点击“加入房间”。

### 3. 在 Civ 6 中联机

1. 确认工具成员列表里所有玩家均为“在线”。
2. 房主进入“多人游戏 → 局域网”并创建游戏。
3. 其他玩家打开或刷新“局域网”列表，选择房间加入。
4. 进入大厅后确认版本、DLC、模组完全一致，再开始游戏。

## 文档

- [玩家使用说明](docs/USAGE.md)
- [Linux 中转服部署](docs/DEPLOYMENT.md)
- [故障排查](docs/TROUBLESHOOTING.md)
- [协议与安全边界](docs/PROTOCOL.md)
- [第三方许可证](THIRD_PARTY_NOTICES.md)

## 开发

Windows 源码运行：

```powershell
python -m pip install -e ".[client]"
python -m civ6relay
```

开发检查依赖：

```powershell
python -m pip install -e ".[client,dev]"
```

构建 Windows 发布包：

```powershell
.\build_client.ps1
```

运行检查：

```powershell
python -m unittest discover -s tests -v
python -m ruff check civ6relay tests run_client.py
python -m compileall -q civ6relay tests
```

现有 `civ6_proxy.py` 是早期固定端口 demo，新实现位于 `civ6relay/`，两者互不依赖。

研究参考：[injciv6](https://github.com/xaxys/injciv6)、[Civ6 L3 Discovery Helper](https://github.com/Frwere/civ6-l3-discovery-helper) 和 [WinDivert 文档](https://reqrypt.org/windivert-doc.html)。
