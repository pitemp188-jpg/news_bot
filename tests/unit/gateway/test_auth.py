"""
模块: tests.unit.gateway.test_auth
职责: 校验白名单放行、配对码生成与批准、持久化与过期
依赖: gateway.auth
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from newsbot.gateway.auth import CODE_TTL, Authorizer, parse_allowed, pending_to_dict


def test_parse_allowed_splits_and_trims() -> None:
    assert parse_allowed(" u1 , u2 ,, ") == {"u1", "u2"}
    assert parse_allowed("") == set()


def test_empty_allowlist_denies_everyone() -> None:
    auth = Authorizer()
    assert auth.is_allowed("qqbot", "u1") is False


def test_configured_user_is_allowed() -> None:
    auth = Authorizer({"qqbot": {"u1"}})
    assert auth.is_allowed("qqbot", "u1") is True
    assert auth.is_allowed("qqbot", "u2") is False
    assert auth.is_allowed("weixin", "u1") is False


def test_wildcard_allows_everyone() -> None:
    auth = Authorizer({"weixin": {"*"}})
    assert auth.is_allowed("weixin", "anyone") is True


def test_request_generates_six_digit_code() -> None:
    auth = Authorizer()
    request = auth.request("qqbot", "u1")
    assert len(request.code) == 6 and request.code.isdigit()
    assert auth.pending() == [request]


def test_request_reuses_unexpired_code() -> None:
    auth = Authorizer()
    first = auth.request("qqbot", "u1")
    assert auth.request("qqbot", "u1").code == first.code


def test_request_issues_new_code_after_expiry() -> None:
    auth = Authorizer()
    first = auth.request("qqbot", "u1")
    auth._pending[("qqbot", "u1")].created_at = time.time() - CODE_TTL - 1
    assert auth.request("qqbot", "u1").code != first.code


def test_expired_requests_are_purged() -> None:
    auth = Authorizer()
    request = auth.request("qqbot", "u1")
    auth._pending[("qqbot", "u1")].created_at = time.time() - CODE_TTL - 1
    assert auth.pending() == []
    assert request.is_expired() is True


def test_approve_adds_to_allowlist() -> None:
    auth = Authorizer()
    auth.request("qqbot", "u1")
    assert auth.approve("qqbot", "u1") is True
    assert auth.is_allowed("qqbot", "u1") is True
    assert auth.pending() == []


def test_approve_unknown_returns_false() -> None:
    assert Authorizer().approve("qqbot", "stranger") is False


def test_deny_removes_pending() -> None:
    auth = Authorizer()
    auth.request("qqbot", "u1")
    assert auth.deny("qqbot", "u1") is True
    assert auth.pending() == []
    assert auth.deny("qqbot", "u1") is False


def test_add_is_idempotent() -> None:
    auth = Authorizer()
    auth.add("qqbot", "u1")
    auth.add("qqbot", "u1")
    assert auth.snapshot() == {"qqbot": ["u1"]}


def test_persistence_round_trip(tmp_path: Path) -> None:
    auth = Authorizer(root=tmp_path)
    auth.add("qqbot", "u1")
    auth.add("weixin", "u2")

    reloaded = Authorizer(root=tmp_path)
    assert reloaded.is_allowed("qqbot", "u1") is True
    assert reloaded.is_allowed("weixin", "u2") is True
    assert reloaded.is_allowed("qqbot", "u9") is False


def test_config_allowlist_merges_with_stored(tmp_path: Path) -> None:
    Authorizer({"qqbot": {"cfg-user"}}, root=tmp_path).add("qqbot", "file-user")
    merged = Authorizer({"qqbot": {"cfg-user"}}, root=tmp_path)
    assert merged.is_allowed("qqbot", "cfg-user") is True
    assert merged.is_allowed("qqbot", "file-user") is True


def test_broken_file_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "pairing.json").write_text("{坏的", encoding="utf-8")
    assert Authorizer(root=tmp_path).snapshot() == {}


def test_malformed_stored_shape_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "pairing.json").write_text(json.dumps({"allowed": ["nope"]}), encoding="utf-8")
    assert Authorizer(root=tmp_path).snapshot() == {}


def test_pending_to_dict_is_serializable() -> None:
    entry = pending_to_dict(Authorizer().request("qqbot", "u1"))
    assert entry["platform"] == "qqbot" and "created_at" in entry
