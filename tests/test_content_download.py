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


# ---------- دانلود از API خام (فیکس عکس/کاروسل — بازتولید باگ واقعی) ----------

from app.instagram.content import download_from_media_dict  # noqa: E402

# شکل دقیق پاسخ واقعی پست کاربر (آیتم کاروسل عکس bahman_wp):
# media_type=8 + carousel_media خالی + like_count=null → پیاده‌سازی قدیمی
# album_download «بدون خطا و بدون فایل» برمی‌گشت (سکوت کامل) و yt-dlp هم
# ذاتاً عکس را نمی‌تواند بگیرد → «No video formats found» کاربر.
USER_BUG_MEDIA = {
    "media_type": 8,
    "product_type": "carousel_item",
    "pk": "4017169569784828890",
    "like_count": None,      # همان چیزی که pydantic کتابخانه را می‌شکست
    "comment_count": None,
    "carousel_media": [],    # کاروسلِ خالی → قبلاً سکوت کامل
    "video_versions": [],
    "image_versions2": {"candidates": [
        {"url": "https://cdn.example.net/v/photo_small.jpg?oe=1", "width": 320, "height": 400},
        {"url": "https://cdn.example.net/v/photo_big.jpg?oe=1", "width": 1440, "height": 1800},
    ]},
    "user": {"username": "bahman_wp"},
}


class FakeResp:
    def __init__(self, data=b"x" * 100):
        self.headers = {"Content-Length": str(len(data))}
        self._data = data

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=1 << 20):
        yield self._data


def _patch_get(monkeypatch, data=b"x" * 100):
    seen: list[str] = []
    monkeypatch.setattr(
        "app.instagram.content.requests.get",
        lambda url, stream=True, timeout=None, headers=None: (seen.append(url), FakeResp(data))[1],
    )
    return seen


def test_raw_download_carousel_item_photo(tmp_path, monkeypatch):
    """باگ اصلی کاربر: آیتم کاروسل (media_type=8، carousel خالی) باید عکسش دانلود شود."""
    seen = _patch_get(monkeypatch, b"\xff\xd8JFIFDATA" * 20)
    files = download_from_media_dict(USER_BUG_MEDIA, tmp_path)
    assert len(files) == 1
    assert files[0].exists() and files[0].stat().st_size > 0
    # بهترین کیفیت (بزرگ‌ترین ابعاد) انتخاب شده است
    assert "photo_big.jpg" in seen[0]
    assert files[0].name.startswith("bahman_wp_4017169569784828890")
    assert files[0].suffix == ".jpg"


def test_raw_download_mixed_carousel(tmp_path, monkeypatch):
    """کاروسل ترکیبی: عکس + ویدیو → دو فایل با پسوند درست و بهترین کیفیت."""
    seen = _patch_get(monkeypatch)
    media = {
        "pk": "998877",
        "user": {"username": "some.user"},
        "carousel_media": [
            {"pk": "1", "media_type": 1, "video_versions": [],
             "image_versions2": {"candidates": [
                 {"url": "https://cdn.example.net/x1.jpg", "width": 1080, "height": 1080}]}},
            {"pk": "2", "media_type": 2,
             "video_versions": [
                 {"url": "https://cdn.example.net/v2_720.mp4", "width": 720, "height": 1280},
                 {"url": "https://cdn.example.net/v2_1080.mp4", "width": 1080, "height": 1920}],
             "image_versions2": {"candidates": [
                 {"url": "https://cdn.example.net/x2.jpg", "width": 480, "height": 854}]}},
        ],
    }
    files = download_from_media_dict(media, tmp_path)
    assert len(files) == 2
    names = sorted(f.name for f in files)
    assert names == ["some.user_998877_1.jpg", "some.user_998877_2.mp4"]
    # ویدیو: بهترین کیفیت (1080) انتخاب شده، نه 720 و نه تصویر بندانگشتی
    assert any("v2_1080.mp4" in u for u in seen)


def test_raw_download_video_post_prefers_video(tmp_path, monkeypatch):
    """پست ویدیویی: video_versions مقدم بر عکس است و mp4 ذخیره می‌شود."""
    seen = _patch_get(monkeypatch, b"\x00\x00\x00\x18ftypmp42" * 8)
    media = {
        "pk": "777",
        "user": {"username": "videouser"},
        "video_versions": [{"url": "https://cdn.example.net/reel.mp4", "width": 1080, "height": 1920}],
        "image_versions2": {"candidates": [
            {"url": "https://cdn.example.net/cover.jpg", "width": 480, "height": 854}]},
    }
    files = download_from_media_dict(media, tmp_path)
    assert len(files) == 1 and files[0].suffix == ".mp4"
    assert all("reel.mp4" in u for u in seen)


def test_raw_download_raises_when_nothing_downloadable(tmp_path):
    """بدون هیچ video/image versions → خطای روشن، نه سکوت."""
    with pytest.raises(RuntimeError) as ei:
        download_from_media_dict({"pk": "5", "user": {"username": "u"}}, tmp_path)
    assert "فایل قابل دریافت" in str(ei.value)


def test_raw_download_partial_file_not_left_behind(tmp_path, monkeypatch):
    """شکست وسط دانلود نباید فایل .part یا ناقص باقی بگذارد."""
    class BrokenResp(FakeResp):
        def iter_content(self, chunk_size=1 << 20):
            yield b"short"

    monkeypatch.setattr(
        "app.instagram.content.requests.get",
        lambda url, stream=True, timeout=None, headers=None: BrokenResp(b"much-longer-expected"),
    )
    media = {"pk": "9", "user": {"username": "u"},
             "image_versions2": {"candidates": [
                 {"url": "https://cdn.example.net/p.jpg", "width": 800, "height": 800}]}}
    with pytest.raises(RuntimeError):
        download_from_media_dict(media, tmp_path)
    assert list(tmp_path.iterdir()) == []  # هیچ فایل ناقصی نمانده


# ---------- پسوند واقعی از محتوای فایل (heic→jpg) ----------

from app.instagram.content import _collect_downloaded, _sniff_ext  # noqa: E402


def test_sniff_ext_magic_bytes():
    assert _sniff_ext(b"\xff\xd8\xff\xe0xx", "jpg") == "jpg"
    assert _sniff_ext(b"\x89PNG\r\n\x1a\n", "jpg") == "png"
    assert _sniff_ext(b"RIFFxxxxWEBP", "jpg") == "webp"
    assert _sniff_ext(b"\x00\x00\x00\x18ftypmp42xxx", "mp4") == "mp4"
    assert _sniff_ext(b"\x00\x00\x00\x18ftypheicxxx", "jpg") == "heic"
    assert _sniff_ext(b"whatever", "mp4") == "mp4"   # ناشناخته → پیش‌فرض زمینه


def test_heic_url_with_jpeg_content_saved_as_jpg(tmp_path, monkeypatch):
    """باگ واقعی پست کاربر: همهٔ ۱۴ کاندیدای عکس URLهای «.heic» دارند اما
    CDN محتوای JPEG می‌دهد؛ فایل باید با پسوند واقعی jpg ذخیره شود تا
    هم find_media ببیند و هم تلگرام به‌عنوان عکس بپذیرد."""
    seen = _patch_get(monkeypatch, b"\xff\xd8\xff\xe0JFIFDATA" * 20)
    media = {  # شکل واقعی پاسخ (کاندیداهای heic فقط)
        "pk": "4017169569784828890",
        "user": {"username": "bahman_wp"},
        "video_versions": [],
        "image_versions2": {"candidates": [
            {"url": "https://cdn.example.net/v/photo_n.heic?oe=1", "width": 1440, "height": 1800},
            {"url": "https://cdn.example.net/v/photo_small_n.heic?oe=1", "width": 320, "height": 400},
        ]},
    }
    files = download_from_media_dict(media, tmp_path)
    assert len(files) == 1
    assert files[0].suffix == ".jpg", "محتوای JPEG باید با پسوند jpg ذخیره شود"
    assert "photo_n.heic" in seen[0]
    assert files[0].name == "bahman_wp_4017169569784828890.jpg"


def test_real_heic_content_kept_as_heic(tmp_path, monkeypatch):
    """اگر محتوا واقعاً HEIC بود (نه JPEG)، نام heic حفظ می‌شود تا به‌عنوان
    سند (document) قابل ارسال بماند، نه اینکه به‌اشتباه jpg نام‌گذاری شود."""
    _patch_get(monkeypatch, b"\x00\x00\x00\x18ftypheicHEICDATA" * 10)
    media = {"pk": "5", "user": {"username": "u"},
             "image_versions2": {"candidates": [
                 {"url": "https://cdn.example.net/x_n.heic", "width": 800, "height": 800}]}}
    files = download_from_media_dict(media, tmp_path)
    assert files[0].suffix == ".heic"


def test_collect_downloaded_renames_library_saved_heic(tmp_path):
    """story_download کتابخانه فایل را از URL (heic) نام‌گذاری می‌کند؛
    _collect_downloaded باید پسوند را با محتوای واقعی (JPEG) درست کند."""
    f = tmp_path / "story_123_n.heic"
    f.write_bytes(b"\xff\xd8\xff\xe0JFIF" * 30)
    (tmp_path / "cookies.txt").write_text("x")
    (tmp_path / "partial.part").write_bytes(b"x")
    files = _collect_downloaded(tmp_path)
    assert [p.name for p in files] == ["story_123_n.jpg"]
    assert (tmp_path / "story_123_n.jpg").exists()
    assert not (tmp_path / "story_123_n.heic").exists()


# ---------- مسیر instagrapi جدید (private_request خام) ----------

class RawCl:
    """کلاینت ساختگی با مسیر API خام (private_request)."""

    def __init__(self, media_dict):
        self.media_dict = media_dict
        self.private_calls: list[str] = []
        self.media_info_calls = 0
        self.album_calls = 0

    def media_pk_from_url(self, url):
        return 4017169569784828890

    def private_request(self, endpoint, **kw):
        self.private_calls.append(endpoint)
        return {"items": [self.media_dict]}

    # متدهای قدیمی شکننده — نباید در مسیر جدید صدا زده شوند
    def media_info(self, pk):
        self.media_info_calls += 1
        raise AssertionError("media_info قدیمی نباید صدا زده شود")

    def album_download(self, pk, folder):
        self.album_calls += 1
        raise AssertionError("album_download قدیمی نباید صدا زده شود")


def test_via_instagrapi_uses_raw_endpoint_and_downloads_photo(monkeypatch, tmp_path):
    """پست عکسیِ کاربر از مسیر API خام دانلود می‌شود، دور از pydantic شکننده."""
    _patch_get(monkeypatch, b"\xff\xd8JFIF" * 30)
    cl = RawCl(USER_BUG_MEDIA)
    client = FakeIGClient(cl)
    dl = InstagramContentDownloader(client)
    files = dl.download("https://www.instagram.com/reel/De_2nCZiJPa/",
                        "De_2nCZiJPa", tmp_path / "d", "reel")
    assert len(files) == 1 and files[0].suffix == ".jpg"
    assert cl.private_calls == ["media/4017169569784828890/info/"]
    assert cl.media_info_calls == 0 and cl.album_calls == 0


def test_via_instagrapi_no_more_silent_empty(monkeypatch, tmp_path):
    """اگر instagrapi بدون فایل برگردد، علتش گزارش می‌شود (نه سکوت)."""
    cl = RawCl({"pk": "5", "user": {"username": "u"}, "like_count": None})
    client = FakeIGClient(cl)
    dl = InstagramContentDownloader(client)
    # yt-dlp هم شکست می‌خورد (بدون شبکه واقعی، subprocess ساختگی لازم نیست؛
    # چون download فقط تا رسیدن به خطای instagrapi می‌رسد و سپس yt-dlp را
    # با نشست بی‌کوکی اجرا می‌کند — در محیط آزمون __main__ yt_dlp موجود است
    # اما بدون شبکه شکست می‌خورد؛ برای قطعیت، subprocess شبیه‌سازی می‌شود):
    _patch_subprocess(monkeypatch, [FakeProc(1, b"ERROR: [Instagram] x: No video formats found!\n")], [])
    with pytest.raises(RuntimeError) as ei:
        dl.download("https://www.instagram.com/p/xyz/", "xyz", tmp_path / "d", "post")
    msg = str(ei.value)
    # علت هر دو روش دیده می‌شود؛ دیگر سکوتِ روش اول وجود ندارد
    assert "instagrapi" in msg
    assert "فایل قابل دریافت" in msg          # علت واقعی روش اول
    assert "این محتوا ویدیو نیست" in msg       # نگاشت فارسی خطای yt-dlp


def test_ytdlp_hint_no_video_formats_found():
    reason = _ytdlp_reason(
        "ERROR: [Instagram] De_2nCZiJPa: No video formats found!; "
        "please report this issue on  https://github.com/yt-dlp/yt-dlp/issues?q=...")
    assert reason == "این محتوا ویدیو نیست (عکس‌ها از مسیر اصلی دریافت می‌شوند)"


def test_via_instagrapi_story_missing_file_raises(monkeypatch, tmp_path):
    """استوری: اگر فایلی ذخیره نشد باید خطا داده شود، نه بازگشت خالی ساکت."""
    monkeypatch.setattr("app.instagram.client.time.sleep", lambda s: None)

    class StoryCl(RawCl):
        def story_download(self, story_pk, filename="", folder=""):
            return Path(folder) / "nothing.mp4"  # فایل واقعی نمی‌نویسد

    client = FakeIGClient(StoryCl(USER_BUG_MEDIA))
    dl = InstagramContentDownloader(client)
    _patch_subprocess(monkeypatch, [FakeProc(1, b"ERROR: [instagram:story] 1: no formats\n")], [])
    with pytest.raises(RuntimeError) as ei:
        dl.download("https://www.instagram.com/stories/u/123/", "123_456", tmp_path / "d", "story")
    assert "استوری دریافت شد اما فایلی ذخیره نشد" in str(ei.value)
