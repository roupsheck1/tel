"""آزمون‌های جریان اتصال تلگرام↔اینستاگرام (صندوق‌های دایرکت).

مهم‌ترین رگرسیون‌های این بخش:
- پیام کاربرِ متصل‌نشده در صندوق «مخفی/اسپم» باید دیده و کد اتصال
  برایش ارسال شود (قبلاً این صندوق اصلاً خوانده نمی‌شد → «کد نمی‌آمد»).
- ترد pending بدون users باید از inviter یا فرستندهٔ واقعی پیام تشخیص شود.
- ترد غیرقابل‌تشخیص نباید «دیده‌شده» علامت بخورد (تا بعد از رفع، دوباره
  بررسی شود).
- نخستین اسکن (baseline) پیام‌های تازه را از دست نمی‌دهد.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from app.database import Database
from app.instagram.inbox import InstagramInbox
from app.services import Services, build_engine, build_services


# ---------------------------------------------------------------- فیک‌ها
@dataclass
class FakeUser:
    pk: str = "1"
    username: str = "tester"


@dataclass
class FakeMessage:
    id: str
    user_id: str
    item_type: str = "text"
    text: str = "سلام"
    timestamp: float = field(default_factory=time.time)


@dataclass
class FakeThread:
    id: str
    messages: list
    users: list = field(default_factory=list)
    inviter: object = None


class FakeInstagramClient:
    """کلاینت جعلی: سه صندوق + ارسال دایرکت + user_info."""

    def __init__(self):
        self.cl = SimpleNamespace(user_id="999")
        self.inbox: list[FakeThread] = []
        self.pending: list[FakeThread] = []
        self.spam: list[FakeThread] = []
        self.sent: list[tuple] = []          # (text, kwargs)
        self.fail_thread_send = False
        self.usernames: dict[int, str] = {}  # uid → username

    def with_client(self, fn):
        cl = self.cl
        cl.direct_threads = lambda amount=20: list(self.inbox)
        cl.direct_requests = lambda amount=20: list(self.pending)
        cl.direct_pending_inbox = lambda amount=20: list(self.pending)
        cl.direct_spam_inbox = lambda amount=20: list(self.spam)

        def _send(text, user_ids=None, thread_ids=None, **kw):
            if thread_ids and self.fail_thread_send:
                raise RuntimeError("fake: thread send blocked")
            self.sent.append((text, {"user_ids": user_ids, "thread_ids": thread_ids}))
            return SimpleNamespace(pk="m1")

        cl.direct_send = _send
        cl.direct_request_approve = lambda tid: True
        cl.user_info = lambda uid: SimpleNamespace(username=self.usernames.get(uid))
        return fn(cl)

    @property
    def is_ready(self):
        return True


class NullLoop:
    """loop_holder جعلی — تسک‌ها بلافاصله اجرا نمی‌شوند."""

    def call_soon_threadsafe(self, fn):
        pass


@pytest.fixture()
def rig(tmp_path):
    """ساخت مجموعهٔ کامل: db + services + engine + inbox با فیک‌ها."""
    db = Database(tmp_path / "t.db")
    services = build_services(db, [1])
    engine = build_engine(services, downloader=SimpleNamespace(download=None),
                          sender=SimpleNamespace(send=None), notify=None,
                          temp_dir=tmp_path)
    client = FakeInstagramClient()
    inbox = InstagramInbox(client, services.links_repo, engine, NullLoop())
    yield SimpleNamespace(db=db, services=services, engine=engine,
                          client=client, inbox=inbox)
    db.close()


def _user_msg(mid: str, uid: str, text: str = "سلام", age: float = 0):
    m = FakeMessage(id=mid, user_id=uid, text=text)
    m.timestamp = time.time() - age
    return m


# ---------------------------------------------------------------- آزمون‌ها
def test_spam_box_message_gets_code(rig):
    """پیام در صندوق مخفی/اسپم: باید کد اتصال ساخته و ارسال شود (باگ اصلی)."""
    t = FakeThread(id="7001", messages=[_user_msg("m1", "111")],
                   users=[], inviter=None)
    rig.client.usernames[111] = "newbie_tester"
    rig.client.spam.append(t)
    # پیام فرستندهٔ واقعی (نه ربات) → تشخیص با user_info
    processed = rig.inbox.scan_once()
    assert processed == 1
    assert len(rig.client.sent) == 1
    assert "کد اتصال شما" in rig.client.sent[0][0]
    row = rig.db.query_one("SELECT * FROM link_codes WHERE ig_username='newbie_tester'")
    assert row is not None


def test_pending_thread_without_users_uses_inviter(rig):
    """ترد pending با users خالی: inviter ملاک است."""
    t = FakeThread(id="7002", messages=[_user_msg("m2", "222")],
                   users=[], inviter=FakeUser(pk="222", username="inviter_user"))
    rig.client.pending.append(t)
    processed = rig.inbox.scan_once()
    assert processed == 1
    assert "کد اتصال شما" in rig.client.sent[0][0]
    row = rig.db.query_one("SELECT * FROM link_codes WHERE ig_username='inviter_user'")
    assert row is not None


def test_unresolvable_thread_not_marked_seen(rig):
    """بدون users/inviter و بدون user_info: ترد رد می‌شود ولی دیده‌شده نمی‌خورد."""
    t = FakeThread(id="7003", messages=[_user_msg("m3", "333")],
                   users=[], inviter=None)
    rig.client.usernames = {}  # user_info هم هیچ برنمی‌گرداند
    rig.client.pending.append(t)
    rig.inbox.first_scan_done = True
    processed = rig.inbox.scan_once()
    assert processed == 0
    assert rig.client.sent == []
    assert rig.db.query_one("SELECT * FROM ig_seen WHERE thread_id='7003'") is None


def test_first_scan_processes_recent_but_not_old(rig):
    """baseline: پیام تازه پردازش می‌شود؛ پیام قدیمی فقط خط‌گذاری."""
    fresh = FakeThread(id="7004", messages=[_user_msg("m4", "444", age=300)],
                       users=[FakeUser(pk="444", username="fresh_user")])
    old = FakeThread(id="7005", messages=[_user_msg("m5", "555", age=7 * 3600)],
                     users=[FakeUser(pk="555", username="old_user")])
    rig.client.inbox.extend([fresh, old])
    assert rig.inbox.first_scan_done is False
    processed = rig.inbox.scan_once()
    # فقط پیام تازه کد می‌گیرد؛ پیام ۷ ساعته فقط خط‌گذاری می‌شود
    assert processed == 1
    assert len(rig.client.sent) == 1
    assert rig.db.query_one("SELECT 1 FROM link_codes WHERE ig_username='fresh_user'")
    assert rig.db.query_one("SELECT 1 FROM link_codes WHERE ig_username='old_user'") is None


def test_group_thread_skipped_and_marked(rig):
    """گفتگوی گروهی: رد می‌شود و نشانگر جلو می‌رود (ارزیابی مجدد نمی‌شود)."""
    t = FakeThread(id="7006", messages=[_user_msg("m6", "666")],
                   users=[FakeUser(pk="666", username="a"), FakeUser(pk="777", username="b")])
    rig.client.inbox.append(t)
    rig.inbox.first_scan_done = True
    processed = rig.inbox.scan_once()
    assert processed == 0
    assert rig.client.sent == []
    assert rig.db.query_one("SELECT * FROM ig_seen WHERE thread_id='7006'") is not None


def test_send_dm_falls_back_to_user_ids(rig):
    """ارسال با thread_ids خطا می‌خورد → ارسال با user_ids جایش می‌نشیند."""
    rig.client.fail_thread_send = True
    ok = rig.inbox._send_dm("7007", "سلام", user_pk="888")
    assert ok is True
    assert rig.client.sent[0][1]["user_ids"] == [888]


def test_fast_poll_window(rig):
    """پول سریع: پس از arm، فاصلهٔ اسکن در محدودهٔ سریع است و بعد از انقضا عادی."""
    rig.inbox.fast_poll_min, rig.inbox.fast_poll_max = 6, 9
    rig.inbox.poll_min, rig.inbox.poll_max = 30, 60
    assert rig.inbox.fast_poll_active is False
    rig.inbox.arm_fast_poll(seconds=60)
    assert rig.inbox.fast_poll_active is True
    assert 6 <= rig.inbox._sleep_interval() <= 9
    # پس از پایان پنجره → بازهٔ عادی
    rig.inbox._fast_until = time.time() - 1
    assert 30 <= rig.inbox._sleep_interval() <= 60


def test_scan_continues_when_one_box_fails(rig):
    """خطای یک صندوق نباید بقیهٔ صندوق‌ها را متوقف کند."""
    good = FakeThread(id="7008", messages=[_user_msg("m8", "555")],
                      users=[FakeUser(pk="555", username="box_user")])
    rig.client.inbox.append(good)
    orig = rig.client.with_client
    calls = {"n": 0}

    def flaky(fn):
        calls["n"] += 1
        # ترتیب فراخوانی‌ها: 1=user_id، 2=inbox، 3=pending، 4=spam
        if calls["n"] == 3:  # pending در وسط خطا می‌دهد
            raise RuntimeError("fake pending failure")
        return orig(fn)

    rig.client.with_client = flaky
    processed = rig.inbox.scan_once()
    assert processed == 1  # inbox و spam همچنان پردازش شدند
    assert rig.client.sent, "کد باید ارسال می‌شد"


def test_code_reused_for_same_username(rig):
    """پیام دوباره از همان کاربر → همان کد معتبر قبلی دوباره ارسال می‌شود."""
    t = FakeThread(id="7009", messages=[_user_msg("m9", "123")],
                   users=[FakeUser(pk="123", username="repeat_user")])
    rig.client.pending.append(t)
    rig.inbox.first_scan_done = True
    rig.inbox.scan_once()
    first = rig.db.query_one("SELECT code FROM link_codes WHERE ig_username='repeat_user'")["code"]
    # پیام دوم
    t2 = FakeThread(id="7009", messages=[
        FakeMessage(id="m10", user_id="123"), FakeMessage(id="m9", user_id="123")],
        users=[FakeUser(pk="123", username="repeat_user")])
    rig.client.pending[0] = t2
    rig.inbox.scan_once()
    second = rig.db.query_one("SELECT code FROM link_codes WHERE ig_username='repeat_user'")["code"]
    assert first == second
    assert len(rig.client.sent) == 2


def test_debug_report_contains_boxes(rig):
    """گزارش دیاگ: سه صندوق و کدهای اخیر را فهرست می‌کند."""
    t = FakeThread(id="7010", messages=[_user_msg("m11", "321")],
                   users=[FakeUser(pk="321", username="diag_user")])
    rig.client.spam.append(t)
    rig.inbox.scan_once()
    report = rig.inbox.debug_report()
    assert "دیاگ اینستاگرام" in report
    assert "مخفی/اسپم=1" in report
    assert "diag_user" in report
    assert "کدهای اتصال اخیر" in report
    # کد کامل نباید در گزارش بیاید (ماسک است)
    code = rig.db.query_one("SELECT code FROM link_codes WHERE ig_username='diag_user'")["code"]
    assert code not in report


# ---------------------------------------------- آزمون هندلر /connect (فراخوانی مستقیم)
def test_connect_command_arms_fast_poll_and_shows_page_link(tmp_path):
    """/connect: پول سریع فعال می‌شود و لینک پیج ربات در متن می‌آید."""
    import asyncio
    from types import SimpleNamespace
    from aiogram.types import Chat, Message, User
    from datetime import datetime

    from app.telegram.handlers.user import cmd_connect

    db2 = Database(tmp_path / "t2.db")
    svc = build_services(db2, [1])
    svc.settings.set("bot.ig_username", "momplyf_43")
    armed = []
    svc.ig_fastpoll = lambda: armed.append(True)  # type: ignore[method-assign]

    sent: list[str] = []

    class ReplyMessage(Message):
        async def reply(self, text, **kwargs):
            sent.append(text)

    msg = ReplyMessage(message_id=11, date=datetime.now(),
                       chat=Chat(id=4242, type="private"),
                       from_user=User(id=4242, is_bot=False, first_name="T"),
                       text="/connect")
    asyncio.run(cmd_connect(msg, svc))
    assert armed, "پول سریع باید فعال شود"
    assert sent, "پاسخ باید ارسال شود"
    assert "https://instagram.com/momplyf_43" in sent[0]
    assert "کد ۶ رقمی" in sent[0]
    db2.close()


def test_connect_button_text_triggers_arm(tmp_path):
    """دکمه «🔗 اتصال اینستاگرام»: همان مسیر /connect را می‌رود (فیلتر روتر)."""
    import asyncio
    from aiogram.types import Chat, Message, User
    from datetime import datetime
    from app.telegram.handlers.user import router

    db2 = Database(tmp_path / "t3.db")
    svc = build_services(db2, [1])
    armed = []
    svc.ig_fastpoll = lambda: armed.append(True)  # type: ignore[method-assign]

    sent: list[str] = []

    class ReplyMessage(Message):
        async def reply(self, text, **kwargs):
            sent.append(text)

    msg = ReplyMessage(message_id=12, date=datetime.now(),
                       chat=Chat(id=4243, type="private"),
                       from_user=User(id=4243, is_bot=False, first_name="T"),
                       text="🔗 اتصال اینستاگرام")
    # پیدا کردن هندلری که فیلترش همین متن دکمه را می‌پذیرد
    async def find_handler():
        for h in router.message.handlers:
            try:
                res = await h.check(msg)
            except Exception:
                continue  # فیلتر Command به bot نیاز دارد — رد می‌شود
            if res:
                return h
        return None

    handler = asyncio.run(find_handler())
    assert handler is not None, "فیلتر دکمه اتصال روی روتر کاربر نیست"
    assert handler.callback.__name__ == "cmd_connect"
    asyncio.run(handler.callback(msg, svc))
    assert armed, "با دکمه هم پول سریع فعال شود"
    assert sent and "کد ۶ رقمی" in sent[0]
    db2.close()
