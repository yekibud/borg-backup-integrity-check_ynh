from borg_backup_integrity_check.redaction import REDACTED, Redactor, secret_fingerprint
from borg_backup_integrity_check.units import format_bytes, format_pct, parse_size, pct_change


def test_format_bytes_decimal_like_borg():
    assert format_bytes(0) == "0 B"
    assert format_bytes(999) == "999 B"
    assert format_bytes(1000) == "1.0 kB"
    assert format_bytes(286_400_000_000) == "286.4 GB"
    assert format_bytes(None) == "n/a"


def test_pct_change():
    assert pct_change(110, 100) == 10.0
    assert pct_change(50, 100) == -50.0
    assert pct_change(5, 0) is None
    assert pct_change(0, 0) == 0.0
    assert pct_change(None, 3) is None
    assert format_pct(56.44) == "+56.4%"
    assert format_pct(-42.0) == "-42.0%"
    assert format_pct(None) == "n/a"


def test_parse_size():
    assert parse_size("50M") == 50 * 1024**2
    assert parse_size("2G") == 2 * 1024**3
    assert parse_size("123") == 123


def test_redactor_replaces_known_secrets_and_patterns():
    r = Redactor(["hunter2-passphrase", "dop_v1_" + "a" * 64])
    text = (
        "BORG_PASSPHRASE=hunter2-passphrase token: dop_v1_"
        + "a" * 64
        + " Authorization: Bearer abcdefghijklmnop ssh://u:pw@host/repo"
    )
    out = r.redact(text)
    assert "hunter2-passphrase" not in out
    assert "dop_v1_" not in out
    assert "abcdefghijklmnop" not in out
    assert ":pw@" not in out
    assert out.count(REDACTED) >= 4


def test_redactor_short_secrets_ignored_and_none_safe():
    r = Redactor(["ab", ""])
    assert r.redact("ab is fine") == "ab is fine"
    assert r.redact(None) == ""


def test_secret_fingerprint_stable_and_short():
    assert secret_fingerprint("x") == secret_fingerprint("x")
    assert len(secret_fingerprint("x")) == 8
