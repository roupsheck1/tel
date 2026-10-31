"""آزمون‌های دور جدید — چهار درخواست کاربر (۲۰۲۶-۱۰-۳۱):

۱) منوی «🛍 پلن‌ها و قیمت‌گذاری» باید «زیرمنو» را نشان دهد (نه پرش مستقیم
   به فهرست پلن‌ها) و دکمه «🗑 حذف پلن» هم داشته باشد.
۲) حذف پلن با سیاست درخواستی: مشترکان فعلی پلن را تا انقضا نگه می‌دارند؛
   پلن از خرید جدید پنهان می‌شود؛ بازگردانی هم ممکن است.
۳) پست «عکس + موزیک» باید به‌صورت «یک ویدیوی کامل» (عکس+موسیقی یکجا)
   تحویل داده شود — ساخت با ffmpeg از خود پاسخ API.
۴) بعد از تحویل پست، پیام لینک‌ها ارسال می‌شود:
   «بازکردن پست در اینستاگرام» + «بازکردن صفحه سازنده پست» (هایپرلینک؛
   لینک پست همان لینک اصلی خود پست است).
۵) کیبورد منوی کاربر باید با دکمه برگشت گوشی جمع شود (is_persistent=False).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.database import Database
from app.services import build_services
from app.telegram.handlers import admin as admin_handlers
from app.telegram.keyboards import admin as ak
from app.telegram.keyboards import user as uk

OWNER = 111
USER1 = 1001


# ---------------- فیک‌ها (الگوی test_back_buttons) ----------------
class FakeCbMessage:
    def __init__(self):
        self.edits: list = []

    async def edit_text(self, text, reply_markup=None, **_):
        self.edits.append((text, reply_markup))
        return True

    async def answer(self, text=None, reply_markup=None, **_):
        self.edits.append((text, reply_markup))
        return True


class FakeCb:
    def __init__(self, data: str, user_id: int = OWNER):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeCbMessage()
        self.answers: list = []

    async def answer(self, text=None, show_alert=False, **_):
        self.answers.append(text)
        return True


def buttons(markup) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


@pytest.fixture()
def services(tmp_path):
    db = Database(tmp_path / "t.db")
    svc = build_services(db, [OWNER])
    svc.users_repo.register(USER1, "user1", "User One")
    svc.settings.set("sales.mode", "paid")
    yield svc
    db.close()


# ============================ ۵) کیبورد جمع‌شو ============================
def test_user_keyboard_is_collapsible():
    """is_persistent=False → دکمه برگشت گوشی منو را جمع می‌کند (درخواست کاربر)."""
    assert uk.MAIN_KEYBOARD.is_persistent is False
    assert uk.MAIN_KEYBOARD.resize_keyboard is True
    assert uk.main_keyboard(False).is_persistent is False
    assert uk.main_keyboard(True).is_persistent is False
    # دکمه پنل فقط برای مدیر
    assert any(b.text == uk.ADMIN_PANEL_BUTTON for row in uk.main_keyboard(True).keyboard for b in row)
    assert not any(b.text == uk.ADMIN_PANEL_BUTTON for row in uk.main_keyboard(False).keyboard for b in row)


# ============================ ۱) ساختار منوی پلن‌ها ============================
def test_main_menu_plans_entry_opens_submenu_not_list():
    """«🛍 پلن‌ها و قیمت‌گذاری» باید به زیرمنو (a:subs:menu:) برود، نه فهرست."""
    entries = dict(buttons(ak.MAIN_MENU))
    assert entries["🛍 پلن‌ها و قیمت‌گذاری"] == "a:subs:menu:"


def test_subs_submenu_shows_all_items_including_delete():
    """زیرمنوی پلن‌ها: فهرست/جدید/حالت فروش/روش پرداخت/حذف + برگشت + منوی اصلی."""
    texts = [t for t, _ in buttons(ak.menu_of("subs"))]
    for needle in ("فهرست پلن‌ها", "پلن جدید", "حالت فروش", "روش‌های پرداخت", "حذف پلن",
                   "برگشت", "منوی اصلی"):
        assert any(needle in t for t in texts), f"«{needle}» در زیرمنو نیست: {texts}"


@pytest.mark.asyncio
async def test_subs_menu_renders_five_buttons(services):
    cb = FakeCb("a:subs:menu:")
    await admin_handlers.subs_menu(cb, services=services)
    text, markup = cb.message.edits[-1]
    texts = [t for t, _ in buttons(markup)]
    assert "حذف پلن" in " ".join(texts)
    assert any("برگشت" in t for t in texts)


# ============================ ۲) حذف پلن + سیاست ============================
def _basic_plan(services):
    return next(p for p in services.plans_repo.list() if p["name"] == "basic_month")


@pytest.mark.asyncio
async def test_delplan_lists_active_plans_with_counts(services):
    plan = _basic_plan(services)
    v = services.plans_repo.current_version(plan["id"])
    services.subs_repo.create(USER1, plan["id"], v["id"], 30 * 86400, "purchase")
    cb = FakeCb("a:subs:delplan:")
    await admin_handlers.subs_delplan(cb, services=services)
    text, markup = cb.message.edits[-1]
    assert "حذف پلن" in text
    assert "تا زمان انقضا" in text          # سیاست در متن منو
    assert "1 مشترک فعال" in text           # شمارش مشترکان
    cds = [cd for _, cd in buttons(markup)]
    assert f"a:subs:del2:{plan['id']}" in cds
    assert any("برگشت" in t for t, _ in buttons(markup))


@pytest.mark.asyncio
async def test_delete_confirm_screen_shows_policy(services):
    plan = _basic_plan(services)
    cb = FakeCb(f"a:subs:del2:{plan['id']}")
    await admin_handlers.subs_delplan_confirm(cb, services=services)
    text, markup = cb.message.edits[-1]
    assert "تا زمان انقضا" in text
    assert "نمایش داده نمی‌شود" in text
    cds = [cd for _, cd in buttons(markup)]
    assert f"a:subs:del3:{plan['id']}" in cds


@pytest.mark.asyncio
async def test_delete_plan_policy_keep_subscribers_hide_from_buyers(services):
    """سیاست کامل حذف: مشترک فعلی نگه داشته می‌شود؛ خرید جدید رد می‌شود."""
    plan = _basic_plan(services)
    pid = plan["id"]
    v = services.plans_repo.current_version(pid)
    services.subs_repo.create(USER1, pid, v["id"], 30 * 86400, "purchase")
    assert pid in {p["id"] for p in services.plan_service.saleable_plans()}

    cb = FakeCb(f"a:subs:del3:{pid}")
    await admin_handlers.subs_delplan_exec(cb, services=services)
    assert any("حذف شد" in (a or "") for a in cb.answers)

    # ۱) پلن از فهرست خرید پنهان شد
    assert pid not in {p["id"] for p in services.plan_service.saleable_plans()}
    assert services.plans_repo.get(pid)["is_archived"] == 1
    # ۲) مشترک فعلی پلن خود را تا انقضا دارد
    active = services.subs_repo.active_of(USER1)
    assert active and active["plan_id"] == pid
    # ۳) سفارش جدید روی پلن حذف‌شده رد می‌شود
    ok, msg = services.order_service.create_order(USER1, pid)
    assert ok is False and ("موجود نیست" in msg or "قابل خرید" in msg)
    # ۴) ثبت در audit log
    rows = services.db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1")
    assert rows and rows[0]["action"] == "delete_plan"


@pytest.mark.asyncio
async def test_deleted_plan_card_shows_restore(services):
    plan = _basic_plan(services)
    services.plans_repo.archive_plan(plan["id"])
    cb = FakeCb(f"a:subs:plan:{plan['id']}")
    await admin_handlers.subs_plan_card(cb, services=services)
    text, markup = cb.message.edits[-1]
    assert "حذف‌شده" in text
    cds = [cd for _, cd in buttons(markup)]
    assert f"a:subs:restore:{plan['id']}" in cds


@pytest.mark.asyncio
async def test_restore_plan_returns_it_to_sale(services):
    plan = _basic_plan(services)
    pid = plan["id"]
    services.plans_repo.archive_plan(pid)
    cb = FakeCb(f"a:subs:restore:{pid}")
    await admin_handlers.subs_restore(cb, services=services)
    assert any("بازگشت" in (a or "") for a in cb.answers)
    assert pid in {p["id"] for p in services.plan_service.saleable_plans()}
    assert services.plans_repo.get(pid)["is_archived"] == 0


@pytest.mark.asyncio
async def test_archive_from_plan_card_uses_same_policy(services):
    """دکمه «🗑 حذف پلن» روی کارت پلن هم همان سیاست را نشان می‌دهد."""
    plan = _basic_plan(services)
    cb = FakeCb(f"a:subs:archive:{plan['id']}")
    await admin_handlers.subs_archive(cb, services=services)
    text, markup = cb.message.edits[-1]
    assert "تا زمان انقضا" in text
    assert "نمایش داده نمی‌شود" in text


# ============================ ۳) عکس + موزیک → ویدیو ============================
def _ffmpeg(*args: str) -> None:
    proc = subprocess.run(["ffmpeg", "-y", *args], capture_output=True)
    assert proc.returncode == 0, proc.stderr[:300]


def _gen_test_image(path: Path, color: str = "red") -> None:
    _ffmpeg("-f", "lavfi", "-i", f"color=c={color}:s=320x240:d=1",
            "-frames:v", "1", str(path))


def _gen_test_audio(path: Path, dur: float = 2.0) -> None:
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}", str(path))


def _codec_types(path: Path) -> list[str]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=30,
    ).stdout.split()
    return out


def test_find_music_audio_known_shapes():
    from app.instagram.content import _find_music_audio
    # شکل رایج: music_metadata.audio_asset_info.progressive_download_url
    media = {"music_metadata": {"audio_asset_info": {
        "progressive_download_url": "https://cdn.example/track.m4a?sig=1"}}}
    assert _find_music_audio(media) == "https://cdn.example/track.m4a?sig=1"
    # شکل داخلی‌تر (clips_metadata)
    media2 = {"clips_metadata": {"music_metadata": {"audio_asset_info": {
        "progressive_download_url": "https://cdn.example/a.mp3"}}}}
    assert _find_music_audio(media2) == "https://cdn.example/a.mp3"
    # URL ویدیو/عکس در بافت غیر موزیک → گرفته نمی‌شود
    media3 = {"video_versions": [{"url": "https://cdn.example/v.mp4"}],
              "image_versions2": {"candidates": [{"url": "https://cdn.example/i.jpg"}]}}
    assert _find_music_audio(media3) is None


def test_render_music_video_creates_playable_mp4(tmp_path):
    from app.instagram.content import _render_music_video
    photo = tmp_path / "p.jpg"
    audio = tmp_path / "a.wav"
    _gen_test_image(photo)
    _gen_test_audio(audio, dur=2.0)
    out = _render_music_video(photo, audio, tmp_path, "mx")
    assert out is not None and out.exists() and out.suffix == ".mp4"
    codecs = _codec_types(out)
    assert "video" in codecs and "audio" in codecs   # عکس و موزیک با هم


def test_render_slideshow_for_music_carousel(tmp_path):
    from app.instagram.content import _render_slideshow_video
    photos = []
    for i, color in enumerate(("red", "blue")):
        p = tmp_path / f"p{i}.jpg"
        _gen_test_image(p, color)
        photos.append(p)
    audio = tmp_path / "a.wav"
    _gen_test_audio(audio, dur=3.0)
    out = _render_slideshow_video(photos, audio, tmp_path, "sl")
    assert out is not None and out.exists()
    assert "audio" in _codec_types(out)


def _music_photo_media() -> dict:
    """پاسخ API نمونه برای «عکس + موزیک» بدون video_versions."""
    return {
        "pk": 3400000000000000000, "code": "Cmusic1",
        "user": {"username": "artist.page"},
        "media_type": 1,
        "image_versions2": {"candidates": [
            {"url": "http://fake/img_full.jpg", "width": 1080, "height": 1080},
            {"url": "http://fake/img_small.jpg", "width": 320, "height": 320},
        ]},
        "music_metadata": {"audio_asset_info": {
            "progressive_download_url": "http://fake/track.m4a",
        }},
    }


def test_download_media_dict_music_photo_becomes_single_video(tmp_path, monkeypatch):
    """عکس + موزیک → دقیقاً «یک» ویدیوی یکپارچه (نه عکس بی‌موزیک)."""
    from app.instagram import content

    img = tmp_path / "src_img.jpg"
    aud = tmp_path / "src_audio.wav"
    _gen_test_image(img)
    _gen_test_audio(aud)
    served = {"http://fake/img_full.jpg": img, "http://fake/track.m4a": aud}

    def fake_fetch(url, dest_dir, base, default_ext):
        src = served[url]
        dst = Path(dest_dir) / f"{base}.{src.suffix.lstrip('.')}"
        dst.write_bytes(src.read_bytes())
        return dst

    monkeypatch.setattr(content, "_fetch_to_file", fake_fetch)
    meta: dict = {}
    files = content.download_from_media_dict(_music_photo_media(), tmp_path, meta_out=meta)
    assert len(files) == 1
    assert files[0].suffix == ".mp4"
    assert "video" in _codec_types(files[0]) and "audio" in _codec_types(files[0])
    # فایل‌های میانی (عکس/صدا) حذف شده‌اند؛ فقط ویدیو مانده
    assert not list(tmp_path.glob("artist.page_*img*"))
    assert not list(tmp_path.glob("artist.page_*audio*"))
    # فرادادهٔ سازنده برای پیام لینک‌ها
    assert meta["username"] == "artist.page"


def test_download_media_dict_music_failure_falls_back_to_photo(tmp_path, monkeypatch):
    """اگر ساخت ویدیو ممکن نشد، مثل قبل عکس ارسال می‌شود (رفتار امن)."""
    from app.instagram import content

    img = tmp_path / "src_img.jpg"
    _gen_test_image(img)

    def fake_fetch(url, dest_dir, base, default_ext):
        if "track" in url:
            raise RuntimeError("audio unreachable")
        dst = Path(dest_dir) / f"{base}.jpg"
        dst.write_bytes(img.read_bytes())
        return dst

    monkeypatch.setattr(content, "_fetch_to_file", fake_fetch)
    files = content.download_from_media_dict(_music_photo_media(), tmp_path)
    assert [f.suffix for f in files] == [".jpg"]


def test_video_versions_still_win_over_music_render(tmp_path, monkeypatch):
    """پستی که video_versions دارد → همان ویدیو؛ مسیر موزیک بی‌اثر است."""
    from app.instagram import content
    media = _music_photo_media()
    media["video_versions"] = [{"url": "http://fake/vid.mp4", "width": 720, "height": 1280}]
    vid = tmp_path / "src.mp4"
    _ffmpeg("-f", "lavfi", "-i", "color=c=green:s=320x240:d=1", str(vid))

    def fake_fetch(url, dest_dir, base, default_ext):
        dst = Path(dest_dir) / f"{base}.mp4"
        dst.write_bytes(vid.read_bytes())
        return dst

    monkeypatch.setattr(content, "_fetch_to_file", fake_fetch)
    files = content.download_from_media_dict(media, tmp_path)
    assert len(files) == 1 and files[0].suffix == ".mp4"
    assert not list(tmp_path.glob("*_music*"))    # رندر موزیک اجرا نشد


# ============================ ۴) پیام لینک‌های اینستاگرام ============================
def _bare_engine():
    from app.services.download_engine import DownloadEngine
    return DownloadEngine.__new__(DownloadEngine)   # متد خالص؛ بدون __init__


def test_links_message_contains_both_hyperlinks():
    req = {"media_url": "https://www.instagram.com/p/Cabc123/"}
    msg = _bare_engine()._links_message(req, {"username": "artist.page"})
    assert msg is not None
    assert 'href="https://www.instagram.com/p/Cabc123/"' in msg
    assert ">بازکردن پست در اینستاگرام</a>" in msg
    assert 'href="https://www.instagram.com/artist.page/"' in msg
    assert ">بازکردن صفحه سازنده پست</a>" in msg


def test_links_message_without_username_only_post_link():
    req = {"media_url": "https://www.instagram.com/reel/Dxyz9/"}
    msg = _bare_engine()._links_message(req, {})
    assert msg is not None
    assert "بازکردن پست در اینستاگرام" in msg
    assert "سازنده" not in msg


def test_links_message_without_url_is_none():
    assert _bare_engine()._links_message({"media_url": ""}, {}) is None


@pytest.mark.asyncio
async def test_engine_sends_links_message_after_files(tmp_path):
    """موتور بعد از فایل‌ها پیام لینک‌ها را می‌فرستد (زیر پست)."""
    from app.services.download_engine import DownloadEngine, EngineDeps

    db = Database(tmp_path / "eng.db")
    svc = build_services(db, [OWNER])   # حالت پیش‌فرض رایگان → محدودیت‌ها فعال
    svc.users_repo.register(USER1, "u1", "U")

    class Downloader:
        def __init__(self):
            self.meta = {"username": "artist.page"}

        def download(self, url, shortcode, dest_dir, kind="post"):
            dest_dir.mkdir(parents=True, exist_ok=True)
            f = dest_dir / "media.jpg"
            f.write_bytes(b"\xff\xd8" + b"x" * 64)
            return [f]

        def pop_meta(self, dest_dir):
            return self.meta

    class Sender:
        async def send(self, user_id, path):
            return True

    notified: list[tuple[int, str]] = []

    async def notify(user_id, text):
        notified.append((user_id, text))

    deps = EngineDeps(
        downloads=svc.downloads_repo, users=svc.users_repo, subs=svc.subs_repo,
        plans=svc.plans_repo, exceptions=svc.exceptions_repo, errors=svc.errors_repo,
        settings=svc.settings, downloader=Downloader(), sender=Sender(),
        notify=notify, temp_dir=tmp_path / "tmp",
        plan_config_of_version=lambda vid: None,
    )
    engine = DownloadEngine(deps)
    post_url = "https://www.instagram.com/p/Clinks1/"
    ok, rid = await engine.submit(USER1, post_url, "Clinks1")
    assert ok, rid
    row = next(r for r in svc.downloads_repo.queued() if r["id"] == rid)
    await engine._process(dict(row))

    assert svc.downloads_repo.get(rid)["status"] == "completed"
    texts = [t for _, t in notified]
    links_msg = next((t for t in texts if "بازکردن پست در اینستاگرام" in t), None)
    assert links_msg is not None, notified
    assert f'href="{post_url}"' in links_msg
    assert "https://www.instagram.com/artist.page/" in links_msg
    # ترتیب: اول لینک‌ها، بعد پیام موفقیت
    assert texts.index(links_msg) < next(
        i for i, t in enumerate(texts) if "آماده و ارسال شد" in t)


@pytest.mark.asyncio
async def test_engine_without_meta_still_works(tmp_path):
    """دانلودر بدون pop_meta (سازگاری رو به عقب): پیام لینک‌ها فقط با لینک پست."""
    from app.services.download_engine import DownloadEngine, EngineDeps

    db = Database(tmp_path / "eng2.db")
    svc = build_services(db, [OWNER])
    svc.users_repo.register(USER1, "u1", "U")

    class OldDownloader:  # مثل فیک‌های قدیمی — pop_meta ندارد
        def download(self, url, shortcode, dest_dir, kind="post"):
            dest_dir.mkdir(parents=True, exist_ok=True)
            f = dest_dir / "media.jpg"
            f.write_bytes(b"\xff\xd8" + b"x" * 64)
            return [f]

    class Sender:
        async def send(self, user_id, path):
            return True

    notified: list[str] = []

    async def notify(user_id, text):
        notified.append(text)

    deps = EngineDeps(
        downloads=svc.downloads_repo, users=svc.users_repo, subs=svc.subs_repo,
        plans=svc.plans_repo, exceptions=svc.exceptions_repo, errors=svc.errors_repo,
        settings=svc.settings, downloader=OldDownloader(), sender=Sender(),
        notify=notify, temp_dir=tmp_path / "tmp",
        plan_config_of_version=lambda vid: None,
    )
    engine = DownloadEngine(deps)
    post_url = "https://www.instagram.com/p/Cold1/"
    ok, rid = await engine.submit(USER1, post_url, "Cold1")
    assert ok
    row = next(r for r in svc.downloads_repo.queued() if r["id"] == rid)
    await engine._process(dict(row))
    assert svc.downloads_repo.get(rid)["status"] == "completed"
    links = [t for t in notified if "بازکردن پست" in t]
    assert links and f'href="{post_url}"' in links[0]
    assert "سازنده" not in links[0]     # بدون نام کاربری، لینک سازنده نیست
