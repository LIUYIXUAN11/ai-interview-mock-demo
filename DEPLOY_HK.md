# 香港轻量服务器部署（国内可直连、常驻不休眠）

适合：链接要发给国内招聘方，不想依赖国际信用卡 / Render / Railway。
成本：服务器约 ¥24–40/月 + 域名约 ¥60–90/年；不需要 ICP 备案（香港节点）。

## 1. 买服务器

阿里云 / 腾讯云「轻量应用服务器」，选：

- 地域：**香港**
- 系统：**Ubuntu 24.04**
- 配置：**2 核 2G**（1 核 1G 也能跑，但构建会慢）
- 带宽：3M 以上
- 安全组/防火墙：放行 **80、443**

## 2. 买域名并解析

买一个 `.com` 或便宜后缀，添加 A 记录：

```
interview.你的域名.com  →  服务器公网 IP
```

等 5–10 分钟生效。没有域名也能用 `http://IP:8501`，但没 HTTPS，不适合放简历。

## 3. 服务器上执行

```bash
# 装 Docker
sudo apt update
sudo apt install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker

# 拉代码
git clone https://github.com/LIUYIXUAN11/ai-interview-mock-demo.git
cd ai-interview-mock-demo

# 配置
cp .env.example .env
nano .env
# 填：DEEPSEEK_API_KEY、DOMAIN

# 启动（首次会构建镜像，约 1–3 分钟）
sudo docker compose up -d --build
```

如果提示 `docker compose` 不存在，把命令换成 `sudo docker-compose up -d --build`。

## 4. 验证

浏览器打开：

```
https://interview.你的域名.com
```

Caddy 会自动申请 Let's Encrypt 证书。第一次打开可能等几秒。

手机 4G、关掉梯子再打开一次，确认国内可访问。

## 5. 常用命令

```bash
# 看日志
sudo docker compose logs -f app
sudo docker compose logs -f caddy

# 更新代码后重启
git pull
sudo docker compose up -d --build

# 停止
sudo docker compose down
```

## 6. 数据与隐私

- `records/` 挂在宿主机上，容器重启不丢
- 里面是访客的简历和对话，属于个人信息：建议每周清理 7 天前的会话

```bash
find records/_scopes -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf {} +
```

- `.env` 里有 API Key，权限设为 600：`chmod 600 .env`

## 7. 备选：没有域名

先只跑 app，用 IP + 端口访问：

```bash
sudo docker run -d --name interview \
  -p 8501:8501 \
  --env-file .env \
  -v $(pwd)/records:/app/records \
  --restart unless-stopped \
  $(sudo docker build -q .)
```

然后访问 `http://服务器IP:8501`。安全组放行 8501。这个方案没有 HTTPS，只适合临时验收。
