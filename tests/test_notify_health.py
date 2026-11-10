from datetime import timedelta
from pathlib import Path

import pytest

from parking_checks.config import Config, ConfigError, load_env_file
from parking_checks.health import Outcome, run_check
from parking_checks.net import DataSourceError
from parking_checks.notify import Notifier
from tests.conftest import FakeChannel


def test_channel_failure_does_not_block_other(state, clock):
    bad, good = FakeChannel("email", fail=True), FakeChannel("telegram")
    n = Notifier(state, [bad, good], clock=clock)
    assert n.notify("k", "s", "b", "key1") == {"email": "failed", "telegram": "sent"}
    # Retry later: telegram is deduped, email tried again.
    bad.fail = False
    assert n.notify("k", "s", "b", "key1") == {"email": "sent", "telegram": "skipped"}
    assert len(good.sent) == 1 and len(bad.sent) == 1


def test_dry_run_prints_and_records_nothing(state, clock):
    out: list[str] = []
    ch = FakeChannel("email")
    n = Notifier(state, [ch], clock=clock, dry_run=True, printer=out.append)
    n.notify("k", "subject", "body", "key1")
    assert ch.sent == [] and not state.alert_sent("key1")
    assert "DRY RUN" in out[0] and "subject" in out[0]


def test_cannot_run_alert_after_two_failures_then_recovered(state, notifier, clock, channel):
    def boom():
        raise DataSourceError("city site down")

    def ok():
        return Outcome("ok", "fine")

    run_check("tow-plate", state, notifier, clock, boom)
    assert channel.sent == []
    clock.now += timedelta(hours=12)
    run_check("tow-plate", state, notifier, clock, boom)
    assert [s for s, _, _ in channel.sent] == ["tow-plate cannot run"]
    clock.now += timedelta(hours=12)
    run_check("tow-plate", state, notifier, clock, boom)
    assert len(channel.sent) == 1  # stays quiet
    clock.now += timedelta(hours=12)
    run_check("tow-plate", state, notifier, clock, ok)
    assert [s for s, _, _ in channel.sent] == ["tow-plate cannot run", "tow-plate recovered"]
    run_check("tow-plate", state, notifier, clock, ok)
    assert len(channel.sent) == 2


def test_single_failure_then_success_is_silent(state, notifier, clock, channel):
    run_check("x", state, notifier, clock, lambda: (_ for _ in ()).throw(RuntimeError("bug")))
    run_check("x", state, notifier, clock, lambda: Outcome("ok", ""))
    assert channel.sent == []
    assert [r.outcome for r in state.recent_runs("x")] == ["ok", "error"]


def test_env_file_and_validation(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text(
        "# comment\nPLATE=test123\nALERT_EMAILS=a@example.com, b@example.com\nSMTP_USER=a@example.com\n"
        'SMTP_PASSWORD="abcd efgh ijkl mnop"\nFRESHNESS_ON_CALENDAR=Wed *-*-* 07:00:00 America/New_York\n',
        encoding="utf-8",
    )
    env = load_env_file(p)
    cfg = Config.from_env(env)
    assert cfg.alert_emails == ["a@example.com", "b@example.com"]
    assert cfg.smtp_password == "abcdefghijklmnop"
    assert cfg.email_enabled and not cfg.telegram_enabled
    assert not Config.from_env({**env, "SMTP_PASSWORD": ""}).email_enabled
    assert not Config.from_env({"TELEGRAM_CHAT_IDS": "123"}).telegram_enabled
    with pytest.raises(ConfigError):
        Config.from_env({"CLEANING_LOOKAHEAD_DAYS": "0"})
    with pytest.raises(ConfigError):
        Config.from_env({"FRESHNESS_MAX_AGE_HOURS": "soon"})


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, str(data)

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class _Sess:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        return self.resp


def _relay_cfg():
    return Config.from_env(
        {
            "ALERT_EMAILS": "a@example.com",
            "EMAIL_WEBHOOK_URL": "https://script.google.com/macros/s/X/exec",
            "EMAIL_WEBHOOK_SECRET": "s3cret",
        }
    )


def test_apps_script_relay_preferred_and_posts_payload():
    from parking_checks.notify import AppsScriptEmailChannel, default_channels

    cfg = _relay_cfg()
    assert [type(c).__name__ for c in default_channels(cfg)] == ["AppsScriptEmailChannel"]
    sess = _Sess(_Resp(200, {"ok": True, "sent": 1}))
    AppsScriptEmailChannel(cfg, session=sess).send("Car towed", "details", "short")
    url, payload = sess.calls[0]
    assert url.endswith("/exec")
    assert payload == {
        "secret": "s3cret",
        "to": ["a@example.com"],
        "subject": "[parking] Car towed",
        "body": "details\n\n-- cambridge-ma-parking-checks\n",
    }


@pytest.mark.parametrize(
    "resp",
    [_Resp(200, {"ok": False, "error": "unauthorized"}), _Resp(200, ValueError("html")), _Resp(500, {})],
)
def test_apps_script_relay_failures_raise(resp):
    from parking_checks.notify import AppsScriptEmailChannel

    with pytest.raises(RuntimeError, match="email relay"):
        AppsScriptEmailChannel(_relay_cfg(), session=_Sess(resp)).send("s", "b", "s")


def test_relay_url_must_be_apps_script():
    with pytest.raises(ConfigError):
        Config.from_env({"EMAIL_WEBHOOK_URL": "https://example.com/hook", "EMAIL_WEBHOOK_SECRET": "x"})
