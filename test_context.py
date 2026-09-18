"""과거 이력 수집 검증: 최근 7일 안에 온 메일이 속한 대화 전체를 모으는가.

가짜 메일함으로 실제 상황을 재현한다.
  - 과거 메일이 받은메일함·보낸메일함·다른 메일함에 흩어져 있음
  - 휴지통에 있는 답장은 제외
  - References 가 잘린 메일 (한 단계 더 거슬러 올라가야 함)
  - 답장 헤더가 없는 답장 (제목으로 찾기)
  - 같은 제목으로 매주 오는 새 메일 (합치면 안 됨)
  - 서버가 헤더 검색을 지원하지 않는 경우
"""
from __future__ import annotations

import dataclasses
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from nwmail.client import Mail
from nwmail.context import collect_context, seeds_from_store
from nwmail.imap_client import NaverWorksImap, build_or_query, imap_date, quote_arg
from nwmail.store import Store, window_start

ME = "gdhong@lumi.example"
TODAY = date(2026, 9, 14)
NOW = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)      # 한국 시각 12:00
SENT = "&vPSwuLpUx3zVaA-"                                    # 보낸메일함
PROJ = "&07jHdA-A"                                          # 다른 메일함
ok = total = 0


def check(label: str, cond, detail: str = "") -> None:
    global ok, total
    total += 1
    ok += bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))


def hdr(t: str) -> None:
    print(f"\n{t}\n" + "-" * 68)


def mail(uid, subject, sender, mid, when, to=(ME,), refs=(), irt="", cc=()) -> Mail:
    return Mail(mail_id=str(uid), subject=subject, from_name="", from_email=sender,
                received_time=when, status="Read", body=f"{subject} 본문", to=list(to),
                cc=list(cc), message_id=mid, references=list(refs), in_reply_to=irt)


class FakeMailbox:
    """NaverWorksImap 과 같은 메서드를 가진 가짜 메일함."""

    def __init__(self, folders: dict[str, list[Mail]], header_ok: bool = True):
        self.data = folders
        self.header_ok = header_ok
        self.header_calls = 0

    def context_folders(self) -> list[str]:
        return [f for f in self.data if f != "Trash"]

    def sent_folder(self) -> str:
        return SENT

    def search_header(self, folder, pairs):
        self.header_calls += 1
        if not self.header_ok:
            raise RuntimeError("BAD Unsupported search key HEADER")
        hits = []
        for m in self.data.get(folder, []):
            fields = {"Message-ID": m.message_id, "References": " ".join(m.references),
                      "In-Reply-To": m.in_reply_to}
            if any(v and v in fields.get(n, "") for n, v in pairs):
                hits.append(m.mail_id)
        return hits

    def search_subject(self, folder, subject, since_days):
        since = TODAY - timedelta(days=since_days)
        return [m.mail_id for m in self.data.get(folder, [])
                if subject.lower() in m.subject.lower()
                and datetime.fromisoformat(m.received_time).date() >= since]

    def fetch(self, folder, uid):
        for m in self.data.get(folder, []):
            if m.mail_id == str(uid):
                return dataclasses.replace(m, folder=folder)
        return None


# ── 메일함 구성 ──────────────────────────────────────────────────────────────
# 대화 T: 과거 메일이 받은메일함·보낸메일함·다른 메일함에 흩어져 있음
R = mail(10, "[루미] 매거진 기획안", "pm@partner.co.kr", "<r@x>", "2026-08-10T10:00:00+09:00")
M1 = mail(3, "RE: [루미] 매거진 기획안", ME, "<m1@x>", "2026-08-20T10:00:00+09:00",
          to=("pm@partner.co.kr",), refs=["<r@x>"], irt="<r@x>")
R2 = mail(5, "RE: RE: [루미] 매거진 기획안", "dev@partner.co.kr", "<r2@x>",
          "2026-09-01T10:00:00+09:00", refs=["<r@x>", "<m1@x>"], irt="<m1@x>")
S = mail(7, "RE: [루미] 매거진 기획안", "x@partner.co.kr", "<s@x>",
         "2026-08-25T10:00:00+09:00", refs=["<r@x>"], irt="<r@x>")            # 휴지통
N = mail(50, "RE: RE: RE: [루미] 매거진 기획안", "pm@partner.co.kr", "<n@x>",
         "2026-09-12T10:00:00+09:00", refs=["<r@x>", "<m1@x>", "<r2@x>"], irt="<r2@x>")

# 대화 X: 최신 메일의 References 가 잘려 있어 두 단계를 거슬러 올라가야 함
X1 = mail(20, "[누리] 신청 페이지", "a@nuri.example", "<x1@x>", "2026-07-01T10:00:00+09:00")
X2 = mail(21, "RE: [누리] 신청 페이지", "b@nuri.example", "<x2@x>", "2026-07-05T10:00:00+09:00",
          refs=["<x1@x>"], irt="<x1@x>")
X3 = mail(60, "RE: RE: [누리] 신청 페이지", "a@nuri.example", "<x3@x>", "2026-09-13T10:00:00+09:00",
          to=("team@lumi.example",), refs=["<x2@x>"], irt="<x2@x>")

# 대화 H: 답장인데 헤더가 없음 → 제목으로 찾기 (90일 이내, 제목 완전 일치만)
H_OLD = mail(30, "월간 정산 보고", "fin@lumi.example", "<h1@x>", "2026-08-20T10:00:00+09:00")
H_VARIANT = mail(31, "월간 정산 보고 (수정)", "fin@lumi.example", "<h9@x>",
                 "2026-08-25T10:00:00+09:00")
H_ANCIENT = mail(2, "월간 정산 보고", "fin@lumi.example", "<h0@x>", "2026-03-01T10:00:00+09:00")
H_NEW = mail(70, "RE: 월간 정산 보고", "boss@lumi.example", "<h2@x>", "2026-09-13T10:00:00+09:00")

# 대화 W: 매주 같은 제목의 '새' 메일 → 지난주 메일과 합치면 안 됨
W_OLD = mail(40, "주간 보고", "team@lumi.example", "<w1@x>", "2026-09-01T10:00:00+09:00")
W_NEW = mail(80, "주간 보고", "team@lumi.example", "<w2@x>", "2026-09-13T10:00:00+09:00")


def mailbox(header_ok: bool = True) -> FakeMailbox:
    return FakeMailbox({
        "INBOX": [R, X1, X2, H_OLD, H_VARIANT, H_ANCIENT, W_OLD, N, X3, H_NEW, W_NEW],
        SENT: [M1],
        PROJ: [R2],
        "Trash": [S],
    }, header_ok=header_ok)


WINDOW = [N, X3, H_NEW, W_NEW]                      # 최근 7일 안에 받은 메일


def sync_like(st: Store, box: FakeMailbox):
    """sync.py 와 같은 순서: 최근 메일 저장 → 새 대화의 과거 이력 수집 → 저장."""
    res = st.save_mails([dataclasses.replace(m, folder="INBOX") for m in WINDOW], me=ME)
    active = {t["thread_key"] for t in st.threads(7, now=NOW)}
    seeds = seeds_from_store(st, res.thread_keys & active)
    hist = collect_context(box, seeds, st.known_keys(), st.known_message_ids())
    saved = st.save_mails(hist.mails, me=ME)
    return hist, saved


def thread_of(st: Store, subject_part: str) -> dict | None:
    return next((t for t in st.threads(7, now=NOW) if subject_part in t["subject"]), None)


def main() -> int:
    hdr("[1] IMAP 검색 명령 구성")
    check("OR 는 앞에 겹쳐 쌓음", build_or_query(["A", "B", "C"]) == "OR OR A B C")
    check("조건 1개면 OR 없음", build_or_query(["A"]) == "A")
    check("따옴표·역슬래시 이스케이프", quote_arg('<a"b\\c@x>') == '"<a\\"b\\\\c@x>"')
    check("SINCE 날짜는 로케일과 무관한 영문 월", imap_date(date(2026, 9, 7)) == "07-Sep-2026")

    hdr("[2] 이력을 찾을 메일함 고르기")
    imap = NaverWorksImap("u", "p")
    imap._entries = [
        {"wire": "INBOX", "name": "INBOX", "flags": set()},
        {"wire": "&x2DMBA-", "name": "휴지통", "flags": {"\\trash"}},
        {"wire": SENT, "name": "보낸메일함", "flags": {"\\sent"}},
        {"wire": "&wqTTOLpUx3zVaA-", "name": "스팸메일함", "flags": set()},
        {"wire": "&x4TC3Lz0rQDVaA-", "name": "임시보관함", "flags": {"\\drafts"}},
        {"wire": "Parent", "name": "Parent", "flags": {"\\noselect"}},
        {"wire": PROJ, "name": "프로젝트A", "flags": set()},
    ]
    picked = imap.context_folders()
    check("받은·보낸메일함이 먼저, 다른 메일함 포함", picked == ["INBOX", SENT, PROJ], str(picked))
    check("보낸메일함은 \\Sent 표식으로 찾음", imap.sent_folder() == SENT)

    with tempfile.TemporaryDirectory() as tmp:
        hdr("[3] 대화 전체 수집 — 흩어진 메일함, 휴지통 제외")
        with Store(Path(tmp) / "c.db") as st:
            box = mailbox()
            hist, saved = sync_like(st, box)
            t = thread_of(st, "매거진 기획안")
            subjects = [m["uid"] for m in t["mails"]] if t else []
            check("최근 메일 1통 + 과거 3통 = 4통", t and t["mail_count"] == 4, str(subjects))
            check("최근 7일 메일은 1통으로 구분", t and t["recent_count"] == 1)
            check("다른 메일함(프로젝트A)의 과거 메일 포함", f"{PROJ}:5" in subjects)
            check("내가 보낸 답장(보낸메일함) 포함",
                  any(m["uid"] == f"{SENT}:3" and m["is_mine"] for m in t["mails"]) if t else False)
            check("휴지통의 답장은 제외", "Trash:7" not in subjects)
            check("시간순 정렬 (가장 오래된 메일이 먼저)",
                  subjects[:1] == ["INBOX:10"] and subjects[-1:] == ["INBOX:50"], str(subjects))

            hdr("[4] References 가 잘린 메일 — 두 단계 거슬러 올라감")
            x = thread_of(st, "신청 페이지")
            check("잘린 체인도 끝까지 수집 (3통)", x and x["mail_count"] == 3,
                  str([m["uid"] for m in x["mails"]]) if x else "")

            hdr("[5] 헤더 없는 답장 — 제목으로 찾기")
            h = thread_of(st, "월간 정산 보고")
            h_uids = [m["uid"] for m in h["mails"]] if h else []
            check("같은 제목의 이전 메일을 붙임", "INBOX:30" in h_uids, str(h_uids))
            check("90일보다 오래된 메일은 제외", "INBOX:2" not in h_uids)
            check("제목이 조금이라도 다르면 제외 ((수정) 버전)", "INBOX:31" not in h_uids)

            hdr("[6] 같은 제목의 새 메일은 합치지 않음")
            w = thread_of(st, "주간 보고")
            check("지난주 '주간 보고'를 가져오지 않음", w and w["mail_count"] == 1)

            hdr("[7] 누구 차례인지 — 과거 이력 반영")
            check("나에게 온 최근 메일에 아직 답 안 함 → 미응답", t and t["awaiting_my_reply"])
            check("과거에 한 번 답했어도 그 뒤 새 메일이 오면 미응답 유지",
                  t and t["my_last_at"] and not t["replied_by_me"])
            reply = mail(4, "RE: RE: RE: RE: [루미] 매거진 기획안", ME, "<m2@x>",
                         "2026-09-13T09:00:00+09:00", to=("pm@partner.co.kr",),
                         refs=["<r@x>", "<n@x>"], irt="<n@x>")
            st.save_mails([dataclasses.replace(reply, folder=SENT)], me=ME)
            t2 = thread_of(st, "매거진 기획안")
            check("내가 답한 뒤에는 미응답에서 빠짐",
                  t2 and not t2["awaiting_my_reply"] and t2["replied_by_me"])
            check("나에게 직접 오지 않은 대화는 미응답 아님", x and not x["awaiting_my_reply"])

            hdr("[8] 다시 실행해도 중복 없음")
            before = st.stats(7, now=NOW)["total"]
            again = collect_context(box, seeds_from_store(st, {t2["thread_key"]}),
                                    st.known_keys(), st.known_message_ids())
            st.save_mails(again.mails, me=ME)
            check("이미 가진 메일은 다시 가져오지 않음",
                  not again.mails and st.stats(7, now=NOW)["total"] == before,
                  f"새로 가져온 메일 {len(again.mails)}통")

        hdr("[9] 서버가 헤더 검색을 지원하지 않을 때 — 제목으로 대체")
        with Store(Path(tmp) / "nohdr.db") as st:
            box = mailbox(header_ok=False)
            hist, _ = sync_like(st, box)
            check("경고를 남김", any("헤더 검색" in w for w in hist.warnings), hist.warnings[-1:])
            x = thread_of(st, "신청 페이지")
            check("잘린 체인도 제목으로 대화에 붙음 (3통)", x and x["mail_count"] == 3,
                  str([m["uid"] for m in x["mails"]]) if x else "")
            t = thread_of(st, "매거진 기획안")
            uids = [m["uid"] for m in t["mails"]] if t else []
            check("받은·보낸메일함의 같은 대화는 제목으로 찾음",
                  "INBOX:10" in uids and f"{SENT}:3" in uids, str(uids))
            check("제목 검색은 받은·보낸메일함만 (다른 메일함은 헤더 검색 필요)",
                  f"{PROJ}:5" not in uids)

    hdr("[10] 7일 창 경계 (한국 날짜 기준, IMAP SINCE 와 동일)")
    start = window_start(7, NOW)
    check("9/14 기준 창 시작 = 9/7 00:00 KST",
          start == datetime(2026, 9, 7, tzinfo=timezone(timedelta(hours=9))), start.isoformat())

    print(f"\n{'=' * 68}\n결과: {ok}/{total} PASS\n{'=' * 68}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
