"""
模块: tests.integration.test_deploy
职责: 部署产物契约测试——compose 与 Dockerfile 自洽、健康检查探的是真实路由、不把密钥烘焙进镜像
依赖: api.server, core.config
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from newsbot.api.server import create_app
from newsbot.core.config import Config, Secrets

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "docker" / "Dockerfile"
COMPOSE = ROOT / "docker" / "compose.yaml"
DOCKERIGNORE = ROOT / ".dockerignore"

# 健康检查里的探测地址与容器 CMD 的监听端口都得对上
HEALTHCHECK_RE = re.compile(r"http://127\.0\.0\.1:(\d+)(/[\w/.\-]+)")
CMD_PORT_RE = re.compile(r'"--port",\s*"(\d+)"')
DATA_DIR_RE = re.compile(r'NEWSBOT_DATA_DIR[=:]\s*"?([^\s"]+)"?')
SECRET_NAMES = ("LLM_API_KEY", "ADMIN_PASSWORD", "TAVILY_API_KEY", "WEIXIN_TOKEN", "QQ_CLIENT_SECRET")


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _service(compose: dict) -> dict:
    services = compose["services"]
    assert len(services) == 1, "只应有一个服务，多服务需同步更新本测试"
    return next(iter(services.values()))


def test_compose_build_points_at_existing_dockerfile(compose: dict) -> None:
    # Compose 规范：context 相对项目目录（compose 文件所在目录）解析,
    # dockerfile 相对 build context 解析。所以两者都用同一套规则复核。
    build = _service(compose)["build"]
    context = (COMPOSE.parent / build.get("context", ".")).resolve()
    assert context == ROOT, "构建上下文必须是仓库根目录（镜像里要拷 src、web、pyproject）"
    dockerfile = (context / build["dockerfile"]).resolve()
    assert dockerfile.is_file(), f"compose 指向的 Dockerfile 不存在: {dockerfile}"
    assert dockerfile == DOCKERFILE


def test_compose_mounts_data_volume_at_data_dir(compose: dict, dockerfile_text: str) -> None:
    service = _service(compose)
    targets = [item.split(":")[-1] for item in service["volumes"]]
    assert "/app/data" in targets, "data 目录必须挂卷，否则重建容器会丢数据库"

    compose_dir = DATA_DIR_RE.search(" ".join(f"{k}: {v}" for k, v in service["environment"].items()))
    docker_dir = DATA_DIR_RE.search(dockerfile_text)
    assert compose_dir and docker_dir, "Dockerfile 与 compose 都要显式指定 NEWSBOT_DATA_DIR"
    assert compose_dir.group(1) == docker_dir.group(1) == targets[-1]


def test_healthcheck_probes_a_real_route(dockerfile_text: str) -> None:
    match = HEALTHCHECK_RE.search(dockerfile_text)
    assert match, "Dockerfile 缺少 HTTP 健康检查"
    path = match.group(2)

    config = Config(secrets=Secrets(_env_file=None, llm_api_key="sk-test", admin_password="pw-for-test"))
    api = create_app(config)
    routes = {getattr(route, "path", "") for route in api.routes}
    assert path in routes, f"健康检查探的是 {path}，但它不在已注册路由里"


def test_healthcheck_port_matches_container_cmd(dockerfile_text: str) -> None:
    ports = {int(value) for value in CMD_PORT_RE.findall(dockerfile_text)}
    assert ports, "CMD 必须显式指定 --port"
    assert int(HEALTHCHECK_RE.search(dockerfile_text).group(1)) in ports
    assert f"EXPOSE {ports.pop()}" in dockerfile_text


def test_web_stage_builds_and_is_copied_in(dockerfile_text: str) -> None:
    assert "FROM node:" in dockerfile_text, "需要独立的 node 阶段构建前端"
    assert "npm run build" in dockerfile_text
    assert "/web/dist" in dockerfile_text, "运行时必须拷入前端产物，否则管理后台没有界面"


def test_runtime_ships_chromium(dockerfile_text: str) -> None:
    runtime = dockerfile_text.split("AS runtime", 1)[-1]
    assert "chromium" in runtime, "浏览器子 Agent 需要真实 Chromium"
    assert "fonts-noto-cjk" in runtime, "缺中文字体会把中文页面渲染成方框"
    assert "CHROME_PATH" in runtime


def test_no_secrets_baked_into_images(dockerfile_text: str, compose: dict) -> None:
    text = dockerfile_text + "\n" + COMPOSE.read_text(encoding="utf-8")
    for name in SECRET_NAMES:
        assert not re.search(rf"(?i)\b{name}\b\s*[:=]\s*\S", text), f"{name} 被写进了部署产物，密钥只能走 .env"
    assert "env_file" in _service(compose), "compose 必须从 .env 读取密钥"


def test_dockerignore_keeps_secrets_and_data_out() -> None:
    lines = [line.strip() for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines() if line.strip()]
    ignored = {line.rstrip("/") for line in lines if not line.startswith("#")}
    for needed in (".env", "data", ".git", "web/node_modules", "__pycache__"):
        assert needed in ignored, f"构建上下文应排除 {needed}"
