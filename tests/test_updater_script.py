"""真实运行 deploy/updater.sh（源码部署的一键更新程序）：临时 git 仓库 + 替身 systemctl / health。

覆盖：git 方式更新成功、新版本起不来自动回滚、有本地改动拒绝、非 git 部署用源码包更新且不碰 dola.env。
需要 Linux 的 bash / git / tar / curl；Windows 上跳过（CI 是 Linux）。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name == "nt" or not shutil.which("bash") or not shutil.which("git"),
                                reason="needs a POSIX shell with git")


def sh(cmd, cwd=None, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, shell=True, check=True, capture_output=True, text=True)


def make_origin(tmp):
    origin = tmp / "origin"
    origin.mkdir()
    sh("git init -q && git config user.email t@t && git config user.name t", cwd=origin)
    (origin / "requirements.txt").write_text("fastapi\n")
    (origin / "deploy").mkdir()
    shutil.copy(ROOT / "deploy" / "updater.sh", origin / "deploy" / "updater.sh")
    for version in ("1.0.0", "1.1.0"):
        (origin / "server.py").write_text(f'APP_VERSION = "{version}"\n')
        sh(f"git add -A && git commit -qm v{version} && git tag v{version}", cwd=origin)
    return origin


def make_env(tmp, app, origin, **extra):
    stub = tmp / "stub"
    stub.mkdir(exist_ok=True)
    health = tmp / "health.json"
    # 替身 systemctl：restart 时按 server.py 的版本写 health；FAIL_VERSION 版本「起不来」
    (stub / "systemctl").write_text(f"""#!/bin/bash
echo "$@" >> {tmp}/systemctl.log
v=$(grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+' {app}/server.py)
if [ -n "${{FAIL_VERSION:-}}" ] && [ "$v" = "$FAIL_VERSION" ]; then v=broken; fi
printf '{{"version":"%s"}}' "$v" > {health}
""")
    (stub / "systemctl").chmod(0o755)
    (tmp / "update").mkdir(exist_ok=True)
    (tmp / "update" / "request.json").write_text(json.dumps({"id": "req1", "version": "1.1.0"}))
    (tmp / "update" / "ready").write_text("{}")
    env = {**os.environ, "DOLA_UPDATER_CONF": "/nonexistent", "APP_DIR": str(app), "SERVICE": "fake",
           "SVC_USER": subprocess.run("id -un", shell=True, capture_output=True, text=True).stdout.strip(),
           "UPDATE_DIR": str(tmp / "update"), "HEALTH_URL": f"file://{health}", "GIT_URL": str(origin),
           "BACKUP_DIR": str(tmp / "backups"), "SYSTEMCTL": str(stub / "systemctl"), "VERIFY_SECONDS": "3",
           "PY": sys.executable, **extra}
    return env


def run_updater(env):
    return subprocess.run(["bash", str(ROOT / "deploy" / "updater.sh")], env=env, capture_output=True, text=True)


def agent_status(tmp):
    return json.loads((tmp / "update" / "agent-status.json").read_text())


def version_of(app):
    return (app / "server.py").read_text().split('"')[1]


def git_app(tmp, origin):
    app = tmp / "app"
    sh(f"git clone -q {origin} {app} && git -C {app} checkout -q -B main v1.0.0")
    (app / "tasks.db").write_bytes(b"")
    (app / "dola.env").write_text("DOLA_SECRET=keep\n")
    return app


def test_git_update_succeeds_and_clears_the_request(tmp_path):
    origin = make_origin(tmp_path)
    app = git_app(tmp_path, origin)
    result = run_updater(make_env(tmp_path, app, origin))
    status = agent_status(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert status["state"] == "done" and status["id"] == "req1" and "git" in status["message"]
    assert version_of(app) == "1.1.0" and (app / "dola.env").read_text() == "DOLA_SECRET=keep\n"
    assert not (tmp_path / "update" / "request.json").exists() and not (tmp_path / "update" / "ready").exists()
    backups = list((tmp_path / "backups").iterdir())
    assert len(backups) == 1 and (backups[0] / "code.tar").exists() and (backups[0] / "tasks.db").exists()
    assert "restart fake" in (tmp_path / "systemctl.log").read_text()


def test_new_version_that_does_not_come_up_is_rolled_back(tmp_path):
    origin = make_origin(tmp_path)
    app = git_app(tmp_path, origin)
    result = run_updater(make_env(tmp_path, app, origin, FAIL_VERSION="1.1.0"))
    status = agent_status(tmp_path)
    assert result.returncode == 1
    assert status["state"] == "rolled_back" and "1.0.0" in status["message"]
    assert version_of(app) == "1.0.0"
    assert (tmp_path / "systemctl.log").read_text().count("restart fake") == 2
    assert not (tmp_path / "update" / "request.json").exists()


def test_local_changes_block_the_update_without_touching_files(tmp_path):
    origin = make_origin(tmp_path)
    app = git_app(tmp_path, origin)
    (app / "requirements.txt").write_text("fastapi\nhotfix\n")
    result = run_updater(make_env(tmp_path, app, origin))
    status = agent_status(tmp_path)
    assert result.returncode == 1 and status["state"] == "failed"
    assert "未提交的改动" in status["message"] and "未改动任何文件" in status["message"]
    assert version_of(app) == "1.0.0" and not (tmp_path / "systemctl.log").exists()


def test_non_git_install_updates_from_the_source_tarball(tmp_path):
    origin = make_origin(tmp_path)
    app = tmp_path / "app"
    sh(f"mkdir -p {app} && git -C {origin} archive v1.0.0 | tar -x -C {app}")
    (app / "dola.env").write_text("DOLA_SECRET=keep\n")
    sh(f"git -C {origin} archive --prefix=dola-pool-1.1.0/ v1.1.0 | gzip > {tmp_path}/v1.1.0.tgz")
    env = make_env(tmp_path, app, origin, TARBALL_URL=f"file://{tmp_path}/%s.tgz")
    result = run_updater(env)
    status = agent_status(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert status["state"] == "done" and "tarball" in status["message"]
    assert version_of(app) == "1.1.0" and (app / "dola.env").read_text() == "DOLA_SECRET=keep\n"


def test_tarball_update_removes_modules_the_new_version_dropped(tmp_path):
    origin = make_origin(tmp_path)
    app = tmp_path / "app"
    sh(f"mkdir -p {app} && git -C {origin} archive v1.0.0 | tar -x -C {app}")
    (app / "dola.env").write_text("DOLA_SECRET=keep\n")
    (app / "legacy_module.py").write_text("X = 1\n")
    (app / "protocol").mkdir()
    (app / "protocol" / "old.py").write_text("Y = 2\n")
    sh(f"git -C {origin} archive --prefix=dola-pool-1.1.0/ v1.1.0 | gzip > {tmp_path}/v1.1.0.tgz")
    env = make_env(tmp_path, app, origin, TARBALL_URL=f"file://{tmp_path}/%s.tgz")
    result = run_updater(env)
    status = agent_status(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert status["state"] == "done" and version_of(app) == "1.1.0"
    assert not (app / "legacy_module.py").exists() and not (app / "protocol").exists()


def test_nothing_to_do_without_a_ready_request(tmp_path):
    origin = make_origin(tmp_path)
    app = git_app(tmp_path, origin)
    env = make_env(tmp_path, app, origin)
    (tmp_path / "update" / "ready").unlink()
    result = run_updater(env)
    assert result.returncode == 0 and version_of(app) == "1.0.0"
    assert not (tmp_path / "update" / "agent-status.json").exists()


def fake_python(stub, name, version):
    """一个只认 `-c`、`-`（stdin 脚本）和 `-m venv` 的假解释器，模拟「新解释器」重建 venv。"""
    path = stub / name
    path.write_text(f"""#!/bin/bash
if [ "$1" = "-c" ]; then echo "{version}"; exit 0; fi
if [ "$1" = "-" ]; then cat >/dev/null; exit 0; fi
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  mkdir -p "$3/bin"
  printf '#!/bin/bash\\nexit 0\\n' > "$3/bin/pip"
  printf '#!/bin/bash\\nif [ "$1" = "-c" ]; then echo "{version}"; else exit 0; fi\\n' > "$3/bin/python"
  chmod +x "$3/bin/python" "$3/bin/pip"
  exit 0
fi
echo "{version}"
""")
    path.chmod(0o755)
    return path


def _py_stub(path, version, new_enough):
    """假解释器：-c 打版本号；从 stdin 读脚本（py_new_enough 的用法）时按 new_enough 退出。"""
    path.write_text(f"""#!/bin/bash
if [ "$1" = "-c" ]; then echo "{version}"; exit 0; fi
if [ "$1" = "-" ]; then cat >/dev/null; exit {0 if new_enough else 1}; fi
echo "{version}"
""")
    path.chmod(0o755)


def old_venv(app):
    """模拟升级前的 3.10 虚拟环境。"""
    venv = app / ".venv"
    (venv / "bin").mkdir(parents=True)
    _py_stub(venv / "bin" / "python", "3.10", new_enough=False)
    (venv / "bin" / "pip").write_text("#!/bin/bash\nexit 0\n")
    (venv / "bin" / "pip").chmod(0o755)
    (venv / "marker-old").write_text("old")
    return venv


def test_update_keeps_a_venv_that_is_already_new_enough(tmp_path):
    origin = make_origin(tmp_path)
    app = git_app(tmp_path, origin)
    venv = old_venv(app)
    _py_stub(venv / "bin" / "python", "3.12", new_enough=True)
    env = make_env(tmp_path, app, origin)
    result = run_updater(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert version_of(app) == "1.1.0"
    assert (venv / "marker-old").exists()                       # 没重建 venv


def test_update_rebuilds_a_python310_venv_on_the_new_interpreter(tmp_path):
    origin = make_origin(tmp_path)
    app = git_app(tmp_path, origin)
    venv = old_venv(app)
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    (stub / "fake_pip").write_text("#!/bin/bash\nexit 0\n")
    (stub / "fake_pip").chmod(0o755)
    fake_python(stub, "python3.12", "3.12")
    env = make_env(tmp_path, app, origin, PATH=f"{stub}:{os.environ['PATH']}")
    result = run_updater(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert version_of(app) == "1.1.0"
    assert not (venv / "marker-old").exists()                   # 旧 venv 被换掉
    assert "3.12" in (venv / "bin" / "python").read_text()
    assert agent_status(tmp_path)["state"] == "done"


def test_update_still_succeeds_when_no_newer_python_is_available(tmp_path):
    """找不到 3.11+ 时不能把更新判失败：代码里有兼容层，3.10 照常跑。"""
    origin = make_origin(tmp_path)
    app = git_app(tmp_path, origin)
    venv = old_venv(app)
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    # 让 PATH 里所有 python3.1x 都指向一个「3.10」的假解释器：模拟机器上只有 3.10
    for name in ("python3.11", "python3.12", "python3.13", "python3"):
        (stub / name).symlink_to(venv / "bin" / "python")
    env = make_env(tmp_path, app, origin, PATH=f"{stub}:{os.environ['PATH']}",
                   UV_BIN=str(tmp_path / "no-uv"), DOLA_PYTHON_DIR=str(tmp_path / "no-py"))
    result = run_updater(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert version_of(app) == "1.1.0"
    assert (venv / "marker-old").exists()                       # 原 venv 原样保留
    assert agent_status(tmp_path)["state"] == "done"


def test_rerun_rebuilds_the_runtime_without_fetching_code(tmp_path):
    """面板「升级运行环境」：版本不变（FROM == TO），不下载代码，只重建 venv 后重启。"""
    origin = make_origin(tmp_path)
    app = git_app(tmp_path, origin)
    venv = old_venv(app)
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    (stub / "fake_pip").write_text("#!/bin/bash\nexit 0\n")
    (stub / "fake_pip").chmod(0o755)
    fake_python(stub, "python3.12", "3.12")
    env = make_env(tmp_path, app, origin, PATH=f"{stub}:{os.environ['PATH']}")
    (tmp_path / "update" / "request.json").write_text(json.dumps({"id": "req1", "version": "1.0.0", "rerun": True}))
    result = run_updater(env)
    status = agent_status(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert status["state"] == "done" and "运行环境" in status["message"] and "3.12" in status["message"]
    assert version_of(app) == "1.0.0"                             # 代码版本没动
    assert not (venv / "marker-old").exists()                     # venv 换到了 3.12
    assert "3.12" in (venv / "bin" / "python").read_text()
    assert (tmp_path / "systemctl.log").read_text().count("restart fake") == 1
