# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目是什么

dola-pool 把一组 dola.com（字节 Seedance 海外版）和 doubao.com（豆包国内版）的账号组成号池，对外提供 OpenAI 兼容的异步视频 / 生图 API，同时兼容火山方舟（Ark）任务协议，可接入 new-api 等网关。上游没有官方 API，全部依赖网页协议逆向；协议知识与踩坑记录集中在 `docs/DEVELOPMENT.md`（交接文档）和各模块 docstring 里，改协议相关代码前先读。

代码、注释、文档、日志全部是中文——新代码保持这个风格。仓库没有 lint / format 配置，沿用现有风格即可。

目录约定：核心代码在 `dola_pool/` 包里（FastAPI 服务、号池调度、出片路径、协议、存储、配置），模块顶层互 import、100+ 测试直接 import 包内模块；专题文档在 `docs/`；调试探针和登录工具在 `tools/`（从仓库根目录运行，如 `python tools/ui_explore.py`）。

## 常用命令

```bash
# 环境（测试与 CI 用 Python 3.12，与 Docker 镜像同版本）
pip install -r requirements-dev.txt        # 运行依赖 + pytest/httpx
python -m patchright install chromium      # 仅浏览器兜底路径需要

# 启动服务（管理面板 http://127.0.0.1:8000/）
uvicorn dola_pool.server:app --host 127.0.0.1 --port 8000
bash start-local.sh                        # Windows: start-local.bat，本机一键启动

# 测试（CI 同款命令；测试环境需要 Python 3.12 + Node.js + ffmpeg）
python -m pytest -q tests
python -m pytest -q tests/test_pool.py           # 单个文件
python -m pytest -q tests/test_pool.py -k <name> # 单个用例

# Docker
docker compose up -d                       # 数据持久化在 ./data

# 面板文案改动后必做（中文是源语言，web/i18n/vi.json 是「中文原文→译文」映射）
python tools/i18n_tool.py sync                   # 新文案补进所有词典，删掉已不存在的
python tools/i18n_tool.py missing vi             # 列出缺译
python tools/i18n_tool.py check                  # 占位符不一致时退出码 1
python -m pytest -q tests/test_i18n.py tests/test_i18n_dom.py
```

CI（`.github/workflows/`）：push/PR 跑 pytest；打 `v*` tag 构建 Docker 镜像发到 ghcr.io。当前版本看 `dola_pool/server.py` 的 `APP_VERSION` 与 `CHANGELOG.md` 首条。

## 架构

### 请求主链路

```
客户端 → server.py（FastAPI 单体，~5000 行）
          ├─ /v1/videos、/v1/images/generations、/v1/images/edits、Ark /api/v3/contents/generations/tasks（客户接口，Bearer key）
          ├─ /api/admin/*（管理接口，X-Admin-Key 鉴权）
          ├─ _run_task()：挑号 → 生成路径 → 下载转存 downloads/ → /videos/<file> 静态服务
          └─ GET / → web/index.html 管理面板（纯静态单文件，改完刷新即可，无需重启；改 Python 后端才需重启）
```

### 按模型名分派站点与出片路径

- **dola 号**（`seedance-2.0-fast` / `seedance-2.5`）：默认纯 HTTP `dola_pool/pure_api_gen.py`（requests + `dola_pool/pure_signer.py` 调 Node 跑 bdms 签名，**运行时依赖 Node**）；失败回退浏览器路径 `dola_pool/browser.py` + `dola_pool/video_worker_ui.py`（patchright 反检测浏览器 + 可解滑块）。`DOLA_PURE_API` / `DOLA_PURE_FALLBACK_BROWSER` 控制。
- **豆包号**（`doubao-seedance-*` 视频、`doubao-seedream-*` 生图）：`dola_pool/doubao_gen.py` / `dola_pool/image_gen.py`。对话流程与 dola 不同（参数确认、付费确认、会员识别不降级、官方无水印下载），单独一条路径。
- **关键设计**：dola 与豆包网页接口是同一套协议，`dola_pool/protocol/doubao_site.py` 把 `dola_pool/protocol/dola_pure_api.py` **再加载一份独立模块实例**并改写站点常量（域名 / aid / bot_id / 上传机房 / 签名页面），两站在同进程并发互不串。改 `dola_pure_api.py` 协议时两边都要顾及。

### 号池调度（browser_pool.py）

- 扫描 `accounts/<账号>/` 目录自动发现账号，无需重启。
- 记账保守：出片成功或上游明确额度报错才计数；拿不到结果不扣。
- **拿到上游 conversation_id 后绝不换号重提**（会重复出片、重复扣额度），只能继续轮询或判超时失败；只有明确额度 / 风控错误才换号。
- 全池额度用完直接 429；每号同时只处理一个任务；`DOLA_TASK_DEADLINE`（默认 600s）内持续尝试。
- 服务重启后恢复带 conversation_id 的 queued/processing 任务：只轮询，不重发 prompt。

### 持久化（全部 SQLite，无外部服务依赖）

| 库 | 谁在用 | 内容 |
|---|---|---|
| tasks.db | store.py | 任务表（含 conversation_id、deadline_at 等）、面板创建的 API Key |
| pool_usage.db | browser_pool.py / user_store.py / proxy_store.py | 配额计数、accounts_meta（调度开关/冷却/会员标记）、管理员、代理池 |

### 其他要点

- **配置**：全部走环境变量，集中在 `dola_pool/config.py`（前缀 `DOLA_` / `DOUBAO_`，完整表见 API.md 附录 B）。启动时读 `.env.local`（已 gitignore），真实环境变量优先。
- **dola 追问**：上游反问（比例 / 时长 / 是否开始）时，`dola_pool/upstream_reply.py` / `dola_pool/dola_responder.py` 按任务参数自动答两次，仍不行交给可选 AI 代答，都不行才结束（此时未生成、不扣点）。
- **超分**：`dola_pool/upscale.py`，按请求 `resolution`（1080p/2K/4K）在出片后超分。
- **测试不碰真实账号与外网**，断言基于协议结构的 mock/monkeypatch；新增调度 / 协议行为要配对应用例（现有 100+ 个 `tests/test_*.py` 按模块命名）。

## 文档索引

改哪块先读哪篇：`docs/USAGE.md`（使用 / 运营 / 面板逐页说明）、`docs/API.md`（接口、错误码、管理接口、环境变量全表）、`docs/DEVELOPMENT.md`（协议逆向历史与已知坑）、`docs/PROXY.md`（代理与防风控）、`docs/COOKIE.md`（导入账号）、`docs/UPSCALE.md`、`docs/ISOLATION.md`（Cookie 号浏览器隔离）、`docs/I18N.md`（面板多语言机制）、`CHANGELOG.md`。

## 红线

- `accounts/`、`*.db`、`dola.env`、`cookies.txt`、`downloads/`、`.env.local` 均已 gitignore——**绝不提交真实凭据**（Cookie、代理密码、API Key）。
- 面板插入用户数据（账号名、代理名、备注、提示词、上游原话）必须走 `noTr(x)`，否则文本恰好命中 i18n 词典会被误替换（`tests/test_i18n_dom.py` 检查插入点）。
- `extensions/dola30/`、`bdcaptcha.js`、`dola_pool/protocol/js/bdms-sdk.js`、`web/fonts/` 是第三方资产，不在 MIT 授权范围内。
