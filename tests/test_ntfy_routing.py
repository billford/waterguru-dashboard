"""Where alerts get published, and with what credentials.

NTFY_URL points at a self-hosted, access-controlled ntfy; unset, it falls back
to the public ntfy.sh so an existing .env keeps working untouched. The two
differ in a way that matters: a public topic is readable by anyone who guesses
the name, so it needs a random one, while a self-hosted topic is
access-controlled and needs credentials instead.

None of this was covered when the routing changed.
"""
import pytest

import alerts


class FakePost:
    """Captures what would have been sent, without sending it."""

    def __init__(self, status=200, body=None):
        self.calls = []
        self.status = status
        self.body = body or {"id": "abc123"}

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self

    def raise_for_status(self):
        if self.status >= 400:
            import requests
            raise requests.HTTPError(f"{self.status}")

    def json(self):
        return self.body


@pytest.fixture
def post(monkeypatch):
    fake = FakePost()
    monkeypatch.setattr(alerts.requests, "post", fake)
    for key in ("NTFY_URL", "NTFY_USER", "NTFY_PASS"):
        monkeypatch.delenv(key, raising=False)
    return fake


def test_without_a_url_it_falls_back_to_the_public_server(post):
    alerts._ntfy_push("some-topic", "t", "m")
    assert post.calls[0]["url"] == "https://ntfy.sh/some-topic"


def test_a_configured_url_is_used_instead(post, monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://pool.example.net:10000")
    alerts._ntfy_push("waterguru", "t", "m")
    assert post.calls[0]["url"] == "https://pool.example.net:10000/waterguru"


def test_a_trailing_slash_does_not_produce_a_double_slash(post, monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://pool.example.net/")
    alerts._ntfy_push("waterguru", "t", "m")
    assert post.calls[0]["url"] == "https://pool.example.net/waterguru"


def test_credentials_are_sent_when_both_are_present(post, monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://pool.example.net")
    monkeypatch.setenv("NTFY_USER", "waterguru-pub")
    monkeypatch.setenv("NTFY_PASS", "secret")
    alerts._ntfy_push("waterguru", "t", "m")
    assert post.calls[0]["auth"] == ("waterguru-pub", "secret")


def test_no_auth_is_sent_when_credentials_are_absent(post):
    alerts._ntfy_push("some-topic", "t", "m")
    assert post.calls[0]["auth"] is None


def test_a_half_configured_credential_pair_sends_none(post, monkeypatch):
    """Sending a username with no password would fail in a confusing way."""
    monkeypatch.setenv("NTFY_USER", "waterguru-pub")
    alerts._ntfy_push("some-topic", "t", "m")
    assert post.calls[0]["auth"] is None


def test_the_message_and_headers_survive_the_change_of_server(post, monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://pool.example.net")
    alerts._ntfy_push("waterguru", "Pool alert", "chlorine high", priority="high", tags="warning")
    call = post.calls[0]
    assert call["data"] == b"chlorine high"
    assert call["headers"]["Title"] == "Pool alert"
    assert call["headers"]["Priority"] == "high"


def test_a_rejected_push_reports_failure_rather_than_pretending(monkeypatch, capsys):
    """A self-hosted server refuses anonymous publishing; that must not look
    like a delivered alert."""
    monkeypatch.setattr(alerts.requests, "post", FakePost(status=403))
    monkeypatch.setenv("NTFY_URL", "https://pool.example.net")
    monkeypatch.delenv("NTFY_USER", raising=False)
    monkeypatch.delenv("NTFY_PASS", raising=False)

    assert alerts._ntfy_push("waterguru", "t", "m") is None
    assert "ntfy push failed" in capsys.readouterr().err


def test_a_successful_push_returns_the_server_message_id(post):
    assert alerts._ntfy_push("some-topic", "t", "m") == "abc123"
