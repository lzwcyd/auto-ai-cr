from __future__ import annotations

import auto_ai_cr.updater as updater


def test_version_comparison_handles_patch_numbers_and_prefixes():
    assert updater.is_newer_version("v0.5.10", "0.5.9")
    assert not updater.is_newer_version("v0.5.9", "0.5.9")
    assert not updater.is_newer_version("0.5.9", "0.5.10")
    assert updater.is_newer_version("0.6.0-rc1", "0.5.99")


def test_auto_update_skips_source_runs_by_default(monkeypatch):
    monkeypatch.delenv("AUTO_AI_CR_UPDATE_IN_SOURCE", raising=False)
    monkeypatch.setattr(updater.sys, "frozen", False, raising=False)

    result = updater.ensure_latest_before_review()

    assert result.skipped is True
    assert result.checked is False


def test_auto_update_installs_when_latest_is_newer(monkeypatch):
    installed = []
    stages = []
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater, "latest_version", lambda: "99.0.0")
    monkeypatch.setattr(updater, "installed_version", lambda: None)
    monkeypatch.setattr(updater, "install_latest", lambda: installed.append(True))

    result = updater.ensure_latest_before_review(stages.append)

    assert result.updated is True
    assert result.latest_version == "99.0.0"
    assert installed == [True]
    assert stages == ["检查 auto-ai-cr 更新", "发现新版本 99.0.0，正在更新"]


def test_auto_update_skips_install_when_another_process_already_updated(monkeypatch):
    installed = []
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater, "latest_version", lambda: "99.0.0")
    monkeypatch.setattr(updater, "installed_version", lambda: "99.0.0")
    monkeypatch.setattr(updater, "install_latest", lambda: installed.append(True))

    result = updater.ensure_latest_before_review()

    assert result.skipped is True
    assert result.message == "已由其它进程更新到 99.0.0，继续 CR"
    assert installed == []


def test_auto_update_failure_continues_review(monkeypatch):
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)

    def fail_latest():
        raise RuntimeError("network down")

    monkeypatch.setattr(updater, "latest_version", fail_latest)

    result = updater.ensure_latest_before_review()

    assert result.error == "network down"
    assert "继续 CR" in result.message


def test_install_latest_does_not_restart_daemon_during_review(monkeypatch, tmp_path):
    captured_env = {}
    script = tmp_path / "install.sh"

    monkeypatch.setattr(updater.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(updater.tempfile, "TemporaryDirectory", lambda prefix: _TempDir(tmp_path))
    monkeypatch.setattr(updater, "_download", lambda url, output: output.write_text("echo ok\n", encoding="utf-8"))

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, env, **kwargs):
        captured_env.update(env)
        assert command == ["bash", str(script)]
        return Completed()

    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    updater.install_latest()

    assert captured_env["AUTO_AI_CR_RESTART_DAEMON"] == "0"


def test_update_lock_removes_stale_lock(monkeypatch, tmp_path):
    lock = tmp_path / "update.lock"
    info = lock / "owner.json"
    lock.mkdir()
    info.write_text('{"createdAt": 1}', encoding="utf-8")
    monkeypatch.setattr(updater, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(updater, "UPDATE_LOCK", lock)
    monkeypatch.setattr(updater, "UPDATE_LOCK_INFO", info)
    monkeypatch.setattr(updater.time, "time", lambda: 10_000)

    with updater._update_lock(timeout_seconds=0.1, stale_seconds=1):
        assert lock.exists()
        assert info.exists()

    assert not lock.exists()


class _TempDir:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return str(self.path)

    def __exit__(self, exc_type, exc, tb):
        return None
