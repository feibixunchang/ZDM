# ZDM 容器版（NAS / 服务器）

无桌面 GUI 的 ZDM 服务端：网页版界面 + REST API + aria2 兼容 JSON-RPC。
镜像由 CI 自动构建推送：**`ghcr.io/feibixunchang/zdm-server`**（amd64 + arm64 双架构）。

## 一键部署

```bash
mkdir zdm && cd zdm
curl -o docker-compose.yml https://raw.githubusercontent.com/feibixunchang/ZDM/main/docker/docker-compose.yml
docker compose up -d
# 浏览器打开 http://<NAS_IP>:9000 即可用（网页版自动带令牌）
```

私有镜像需先登录 GitHub：`docker login ghcr.io -u <你的GitHub用户名>`（密码用 PAT）。

## 卷与端口

| 路径 | 用途 |
|---|---|
| `/config` | 任务状态 state.json、设置、**api-token**（`docker compose exec zdm cat /config/zdm/api-token`） |
| `/downloads` | 下载落盘目录（换成 NAS 存储卷即可把文件直接下到共享目录） |
| `9000` | Web UI / API / SSE / JSON-RPC |

## 环境变量

| 变量 | 默认（镜像内置） | 说明 |
|---|---|---|
| `ZDM_HOST` | `0.0.0.0` | 绑定地址 |
| `ZDM_ALLOW_LAN` | `1` | 放行局域网 Host（写操作仍逐条验令牌） |
| `ZDM_DOWNLOAD_DIR` | `/downloads` | 下载目录 |
| `XDG_CONFIG_HOME` | `/config` | 配置目录 |

## 其它客户端接入

- 网页版：直接开 `http://<NAS_IP>:9000`
- 桌面端「兼容 aria2 JSON-RPC」：把 AriaNg/qBittorrent 一类客户端指向 `http://<NAS_IP>:9000/jsonrpc`，密钥填 api-token
- 手机浏览器同样可用（网页版已做移动端适配）

## 飞牛 fnOS

`fnpack` 项目位于 `fnos/`（构建产物 `.fpk` 可在飞牛应用中心手动安装）。
