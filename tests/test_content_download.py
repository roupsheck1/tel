"""آزمون‌های رگرسیون لایه دانلود — کوکی yt-dlp، استخراج خطای واقعی و پیام ترکیبی.

پس از باگ تولیدی (کاربر فقط دنبالهٔ بی‌مصرف خطای yt-dlp را می‌دید:
«...ssue template. Confirm you are on the latest version using yt-dlp -U»
در حالی که خطای واقعی «Instagram sent an empty media response» بود)،
این آزمون‌ها با شبیه‌سازی subprocess بررسی می‌کنند که:
- کوکی نشست ربات در قالب Netscape به yt-dlp پاس داده می‌شود و پس از اجرا حذف می‌شود
- خط «ERROR:» واقعی استخراج و به فارسی کوتاه می‌شود، نه دنبالهٔ بویلرپلیت
- پیام خطای نهایی دلایل «هر دو» موتور را نشان می‌دهد
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.instagram.client import InstagramClient
from app.instagram.content import InstagramContentDownloader, _ytdlp_reason


# ---------- کوکی‌ها ----------

class FakeCookie:
    def __init__(self, name, value, domain=".instagram.com", path="/",
                 secure=True, expires=1800000000):
        self.name = name
        self.value = value
        self.domain = domain
        self.path = path
        self.secure = secure
        self.expires = expires


class FakeJar(list):
    pass


class FakeSession:
    def __init__(self, cookies):
        self.cookies = cookies


class FakeCl:
    """شبیه‌سازی Client اینستاگراپی با نشست private/public."""

    def __init__(self, private_cookies=None, public_cookies=None):
        self.private = FakeSession(FakeJar(private_cookies or []))
        self.public = FakeSession(FakeJar(public_cookies or []))


class FakeIGClient(InstagramClient):
    """InstagramClient با لاگین بی‌اثر (بدون شبکه) و کلاینت آماده."""

    def __init__(self, cl=None):
        super().__init__("u", "p", Path("/tmp/nonexistent-session.json"))
        self.cl = cl or FakeCl([FakeCookie("sessionid", "SECRET123"),
                                FakeCookie("ds_user_id", "42"),
                                FakeCookie("csrftoken", "tok", expires=None)])
        self.login_calls = 0

    def login(self):
        self.login_calls += 1
        return True


def test_export_cookies_writes_netscape_format(tmp_path):
    client = FakeIGClient()
    out = tmp_path / "cookies.txt"
    assert client.export_cookies(out) is True
    text = out.read_text()
    assert text.startswith("# Netscape HTTP Cookie File")
    line = [l for l in text.splitlines() if "sessionid" in l][0]
    parts = line.split("\t")
    # قالب: domain, include_subdomains, path, secure, expires, name, value
    assert parts[0] == ".instagram.com"
    assert parts[1] == "TRUE"
    assert parts[2] == "/"
    assert parts[3] == "TRUE"
    assert parts[4] == "1800000000"
    assert parts[5] == "sessionid"
    assert parts[6] == "SECRET123"
    # کوکی بدون انقضا (csrftoken) با انقضای صفر (کوکی نشست) نوشته می‌شود
    csrf = [l for l in text.splitlines() if "csrftoken" in l][0]
    assert csrf.split("\t")[4] == "0"


def test_export_cookies_false_when_empty_session(tmp_path):
    client = FakeIGClient(FakeCl())
    assert client.export_cookies(tmp_path / "c.txt") is False


def test_export_cookies_false_when_not_logged_in(tmp_path, monkeypatch):
    client = InstagramClient("u", "p", Path("/tmp/nonexistent-session.json"))
    # لاگین واقعی در آزمون انجام نمی‌شود (شبیه‌سازی «ورود ممکن نیست»)
    from app.instagram.client import InstagramLoginError
    monkeypatch.setattr(client, "login", lambda: (_ for _ in ()).throw(InstagramLoginError("no login")))
    assert client.export_cookies(tmp_path / "c.txt") is False


# ---------- اجرای yt-dlp ----------

class FakeProc:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = b""


def _patch_subprocess(monkeypatch, procs, calls, side_effect=None):
    def fake_run(args, capture_output=True, timeout=None):
        calls.append(list(args))
        if side_effect:
            side_effect(args)
        return procs.pop(0)
    monkeypatch.setattr("app.instagram.content.subprocess.run", fake_run)


def test_ytdlp_receives_cookies_and_deletes_file(monkeypatch, tmp_path):
    client = FakeIGClient()
    dl = InstagramContentDownloader(client)
    calls: list[list[str]] = []

    def _make_file(args):
        out = args[args.index("-o") + 1]
        # شبیه‌سازی خروجی yt-dlp: %(id)s.%(ext)s
        import re as _re
        path = Path(_re.sub(r"%\(id\)s\.%\(ext\)s", "abc123.mp4", out))
        path.write_bytes(b"x" * 64)

    _patch_subprocess(monkeypatch, [FakeProc(0)], calls, _make_file)
    files = dl.download("https://www.instagram.com/reel/abc123/", "abc123",
                        tmp_path / "d", "reel")
    assert len(files) == 1 and files[0].name == "abc123.mp4"
    args = calls[0]
    assert "--cookies" in args
    cookie_path = Path(args[args.index("--cookies") + 1])
    assert cookie_path.parent == (tmp_path / "d")
    # پس از اجرا فایل کوکی باید حذف شده باشد (راز نشت نکند)
    assert not cookie_path.exists()


def _no_login_client(monkeypatch):
    """کلاینت بدون نشست که لاگینش همیشه شکست می‌خورد (بدون شبکه)."""
    from app.instagram.client import InstagramLoginError
    client = InstagramClient("u", "p", Path("/tmp/nonexistent-session.json"))
    monkeypatch.setattr(client, "login", lambda: (_ for _ in ()).throw(InstagramLoginError("no login")))
    return client


def test_ytdlp_runs_without_cookies_when_session_unavailable(monkeypatch, tmp_path):
    client = _no_login_client(monkeypatch)
    dl = InstagramContentDownloader(client)
    calls: list[list[str]] = []

    def _make_file(args):
        out = args[args.index("-o") + 1]
        Path(str(out).replace("%(id)s.%(ext)s", "abc123.mp4")).write_bytes(b"x" * 64)

    _patch_subprocess(monkeypatch, [FakeProc(0)], calls, _make_file)
    files = dl.download("https://www.instagram.com/reel/abc123/", "abc123",
                        tmp_path / "d", "reel")
    assert files
    assert "--cookies" not in calls[0]


def test_ytdlp_error_shows_real_reason_not_boilerplate(monkeypatch, tmp_path):
    """بازتولید مستقیم باگ کاربر: خطای واقعی در خط ERROR است، نه دنبالهٔ پیام."""
    client = _no_login_client(monkeypatch)
    dl = InstagramContentDownloader(client)
    stderr = (
        "[Instagram] Extracting URL: https://www.instagram.com/p/xyz/\n"
        "[Instagram] xyz: Downloading JSON metadata\n"
        "ERROR: [Instagram] xyz: Instagram sent an empty media response. Check if "
        "this post is accessible in your browser without being logged-in. If it is "
        "not, then use --cookies. Otherwise, please report this issue on "
        "https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the appropriate "
        "issue template. Confirm you are on the latest version using  yt-dlp -U\n"
    ).encode()
    # هر دو روش شکست می‌خورند: instagrapi هم (نشست ناموجود) خطا می‌دهد
    with pytest.raises(RuntimeError) as ei:
        dl.download("https://www.instagram.com/p/xyz/", "xyz", tmp_path / "d", "post")
    msg = str(ei.value)
    assert "پاسخ خالی" in msg                     # علت فارسی کوتاه
    assert "ssue template" not in msg             # بویلرپلیت به کاربر نمی‌رسد
    assert "instagrapi" in msg and "yt-dlp" in msg  # علت هر دو روش دیده می‌شود


def test_ytdlp_error_first_line_is_error_line():
    reason = _ytdlp_reason("noise line\nERROR: [Instagram] x: There is no video in this post\nmore noise")
    assert reason == "این پست ویدیو ندارد (yt-dlp فقط ویدیو دانلود می‌کند)"


def test_ytdlp_error_429_hint():
    reason = _ytdlp_reason("ERROR: [instagram:story] 123: Unable to download webpage: HTTP Error 429: Too Many Requests")
    assert "محدودیت موقت" in reason


# ---------- رفتار with_client (ورود مجدد فقط برای خطای نشست) ----------

def test_with_client_fast_fail_on_non_auth_error(monkeypatch):
    """خطای «پست وجود ندارد» نباید به ورود مجدد و خواب ۵ ثانیه‌ای بینجامد."""
    monkeypatch.setattr("app.instagram.client.time.sleep", lambda s: None)
    client = FakeIGClient()
    calls = {"fn": 0}

    def fn(cl):
        calls["fn"] += 1
        raise ValueError("media not found: post does not exist or is unavailable")

    with pytest.raises(Exception):
        client.with_client(fn)
    assert client.login_calls == 0   # بدون ورود مجدد
    assert calls["fn"] == 1          # بدون تلاش دوباره بی‌فایده


def test_with_client_relogin_on_auth_error(monkeypatch):
    """خطای نوع نشست: یک ورود مجدد و تلاش دوباره انجام می‌شود."""
    monkeypatch.setattr("app.instagram.client.time.sleep", lambda s: None)
    client = FakeIGClient()
    calls = {"fn": 0}

    def fn(cl):
        calls["fn"] += 1
        if calls["fn"] == 1:
            raise ValueError("Login Required")
        return "OK"

    assert client.with_client(fn) == "OK"
    assert client.login_calls == 1
    assert calls["fn"] == 2
