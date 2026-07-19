# Linux 中转服部署

## 前提

- 一台拥有公网 IPv4 的 Linux VPS。
- 云安全组和主机防火墙允许入站 UDP `42056`。
- UDP 端口可以修改，但服务端和所有客户端必须一致。

中转服务只需要 UDP，不需要开放 TCP 端口、数据库或域名。房间状态仅保存在内存中，服务重启会清空全部房间。

## Docker Compose

在工程根目录运行：

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs -f
```

更新代码后重新构建：

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

停止服务：

```bash
docker compose -f deploy/docker-compose.yml down
```

## Python 与 systemd

安装到 `/opt/civ6relay`：

```bash
sudo useradd --system --home /opt/civ6relay --shell /usr/sbin/nologin civ6relay
sudo mkdir -p /opt/civ6relay
sudo cp -a . /opt/civ6relay/
sudo chown -R civ6relay:civ6relay /opt/civ6relay
sudo -u civ6relay python3 -m venv /opt/civ6relay/.venv
sudo -u civ6relay /opt/civ6relay/.venv/bin/pip install /opt/civ6relay
sudo cp deploy/civ6relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now civ6relay
sudo systemctl status civ6relay
```

查看日志：

```bash
journalctl -u civ6relay -f
```

## 防火墙示例

UFW：

```bash
sudo ufw allow 42056/udp
sudo ufw status
```

firewalld：

```bash
sudo firewall-cmd --permanent --add-port=42056/udp
sudo firewall-cmd --reload
```

还需要在云厂商控制台的安全组中放行同一个 UDP 端口。只配置主机防火墙通常不够。

## 验证

确认服务正在监听：

```bash
sudo ss -ulnp | grep 42056
```

UDP 没有类似 TCP 的简单连通握手。最直接的端到端验证方式是让 Windows 客户端创建房间；成功返回 10 位房间码即表示客户端到中转服的双向 UDP 已经可用。

## 运行参数

```text
python -m civ6relay.server --listen 0.0.0.0:42056 --log-level INFO
```

日志只记录房间和成员生命周期，不记录游戏包内容或会话令牌。

