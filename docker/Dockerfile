# 模块: docker/Dockerfile
# 职责: 生产镜像——三段式构建（前端产物 → Python 依赖 → 运行时），内置 Chromium 供浏览器子 Agent 兜底
# 来源: 自研；Chromium 与中文字体是 browser-use 渲染中文动态页的必要条件
#
# 说明:
#   - 三段式是为了让运行时镜像不带 node / 编译器，只留 venv 与 Chromium。
#   - uv 镜像用 latest，若要复现构建请改成具体 tag（例如 ghcr.io/astral-sh/uv:0.9.7）。
#   - 健康检查端口与 CMD 里的 --port 必须一致；改端口时两处都要改。

# ── 1. 前端：产出 web/dist，供 FastAPI 静态托管 ──
FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json web/.npmrc ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ── 2. Python 依赖：装进 /app/.venv，整目录拷进运行时 ──
FROM python:3.12-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR /app
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never
# 先只装第三方依赖，改动源码时不必重装
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --extra browser
COPY src/ ./src/
RUN uv sync --frozen --extra browser

# ── 3. 运行时 ──
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NEWSBOT_DATA_DIR=/app/data \
    PATH="/app/.venv/bin:$PATH" \
    CHROME_PATH=/usr/bin/chromium

# chromium: 浏览器子 Agent 的可执行文件
# fonts-noto-cjk: 缺了中文页面会渲染成方框
# tini: 作为 PID 1 转发信号，保证 docker stop 能优雅关闭
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-noto-cjk tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src/ ./src/
COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY config.example.yaml ./config.example.yaml
COPY --from=web /web/dist ./web/dist

# 非 root 运行；Chromium 在容器内需要 --no-sandbox（由代码侧的启动参数负责）
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

EXPOSE 8765
VOLUME ["/app/data"]

# 用标准库 urllib 探测，避免为健康检查再装 curl
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u, sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8765/api/health', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "newsbot", "api", "--host", "0.0.0.0", "--port", "8765"]
