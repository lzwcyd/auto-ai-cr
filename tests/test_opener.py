import shlex

from auto_ai_cr.opener import open_report


def test_open_report_replaces_report_placeholder(tmp_path, monkeypatch):
    calls = []

    def fake_popen(command, **kwargs):
        calls.append(command)

    monkeypatch.setattr("auto_ai_cr.opener.subprocess.Popen", fake_popen)
    report = tmp_path / "review report.md"

    open_report(report, "code {report}")

    assert calls == [f"code '{report}'"]


def test_open_report_appends_report_when_command_has_no_placeholder(tmp_path, monkeypatch):
    calls = []

    def fake_popen(command, **kwargs):
        calls.append(command)

    monkeypatch.setattr("auto_ai_cr.opener.subprocess.Popen", fake_popen)
    report = tmp_path / "review.md"

    open_report(report, "code")

    assert calls == [f"code {shlex.quote(str(report))}"]
