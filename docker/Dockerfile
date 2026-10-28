# ZDM 服务端（Docker 版）：无桌面 GUI，提供网页版 + REST/JSON-RPC API。
# 与桌面端同一份 zdm 包与钉版本的随包工具链（yt-dlp / deno / N_m3u8DL-RE），
# ffmpeg 用 Debian 官方包。构建上下文 = 仓库根：docker build -f docker/Dockerfile .

FROM python:3.12-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY zdm/zdm /app/zdm
COPY zdm/zdm.py /app/zdm.py
COPY zdm/web /app/web
COPY zdm/fetch_bin.py /app/fetch_bin.py

# 随包工具与桌面端同版本；--version 自检失败就让构建当场红。
ARG YTDLP_VERSION=2026.08.19
RUN mkdir -p /app/bin \
 && curl -fL --retry 3 -o /app/bin/yt-dlp \
      "https://github.com/yt-dlp/yt-dlp/releases/download/${YTDLP_VERSION}/yt-dlp_linux" \
 && chmod +x /app/bin/yt-dlp \
 && python3 /app/fetch_bin.py --dest /app/bin --os linux --arch x86_64 --tool all \
 && /app/bin/yt-dlp --version \
 && /app/bin/deno --version \
 && /app/bin/N_m3u8DL-RE --version \
 && rm /app/fetch_bin.py

# /config: 任务状态、令牌、设置（XDG_CONFIG_HOME）；/downloads: 落盘目录
VOLUME ["/config", "/downloads"]
ENV XDG_CONFIG_HOME=/config \
    ZDM_DOWNLOAD_DIR=/downloads \
    ZDM_HOST=0.0.0.0 \
    ZDM_ALLOW_LAN=1
EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:9000/api/state',timeout=4)" || exit 1

CMD ["python3", "/app/zdm.py", "serve"]
