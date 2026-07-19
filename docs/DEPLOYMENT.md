# Linux 中转服部署

## 前提

- 一台拥有公网 IPv4 的 Linux VPS。
- 云安全组和主机防火墙允许入站 UDP `42056`。
- UDP 端口可以修改，但服务端和所有客户端必须一致。

中转服务只需要 UDP，不需要开放 TCP 端口、数据库或域名。房间状态仅保存在内存中，服务重启会清空全部房间。

## Docker Compose

### 构建过程

Compose 使用 `deploy/docker-compose.yml`，其中构建上下文 `..` 指向工程根目录。Docker 随后按根目录的 `Dockerfile` 执行：

1. 拉取默认基础镜像 `python:3.12-slim`。
2. 把 `pyproject.toml`、README 和 `civ6relay/` 复制进镜像。
3. 在镜像中执行 `pip install .`。
4. 容器启动时执行 `python -m civ6relay.server`，监听 UDP `42056`。

第一次构建必须能访问基础镜像所在的容器镜像仓库。之后只有基础镜像或构建缓存缺失时才需要重新拉取。

在工程根目录运行：

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs -f
```

### Docker Hub 连接超时

如果错误停在下面这一步：

```text
load metadata for docker.io/library/python:3.12-slim
failed to fetch anonymous token
https://auth.docker.io/token ... i/o timeout
```

说明 Docker 尚未执行本项目的复制或安装步骤，VPS 无法访问 Docker Hub 的鉴权或镜像服务。

首选方案是在云厂商控制台取得该账号/地域对应的 Docker 镜像加速地址，将其配置到 `/etc/docker/daemon.json`：

```json
{
  "registry-mirrors": [
    "https://你的可信镜像加速地址"
  ]
}
```

重启 Docker 并先单独验证基础镜像：

```bash
sudo systemctl daemon-reload
sudo systemctl restart docker
docker info | sed -n '/Registry Mirrors/,+5p'
docker pull python:3.12-slim
docker compose -f deploy/docker-compose.yml up -d --build
```

也可以不修改 Docker 全局设置，临时指定云厂商或可信仓库提供的完整镜像路径：

```bash
export PYTHON_IMAGE='你的可信仓库/library/python:3.12-slim'
docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml up -d
```

项目只把该变量用于 `Dockerfile` 的 `FROM`。不要使用来源不明的公共镜像；中转服务会接触房间内的游戏 UDP 数据。

更新代码后重新构建：

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

停止服务：

```bash
docker compose -f deploy/docker-compose.yml down
```

## Python 与 systemd

中转服务本身只使用 Python 标准库。如果暂时无法拉取任何 Docker 基础镜像，直接使用 VPS 自带的 Python 是最短路径。

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
