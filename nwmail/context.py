"""최근 메일이 속한 대화의 과거 이력 모으기.

최근 N일 안에 받은 메일이 있으면, 그 대화에 속한 과거 메일과 내가 보낸 답장을
기간 제한 없이 메일함 전체(휴지통·스팸·임시보관함 제외)에서 찾는다.

찾는 방법 (정확한 것부터)
  1. 헤더: 대화의 뿌리 ID 를 References 에 가진 메일, 내 메일에 대한 In-Reply-To,
     대화가 참조하는데 아직 없는 Message-ID. 새로 찾은 메일이 더 오래된 메일을
     가리키면 한 단계 더 따라간다.
  2. 제목: 답장인데(RE:/[RE] 등) 헤더로 과거 메일을 찾지 못한 대화만.
     엉뚱한 메일이 섞이지 않도록 받은/보낸메일함의 최근 90일에서, 회신 표식을 뗀
     제목이 완전히 같은 메일만 인정한다. 회신 표식이 없는 새 메일은 제목이 같아도
     별개의 대화로 본다 (매주 같은 제목으로 오는 보고 메일이 하나로 합쳐지지 않도록).

메일함 접근은 box 객체로 추상화했다 (NaverWorksImap 이 실제 구현, 테스트는 가짜 메일함).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .client import Mail
from .store import REPLY_PREFIX, Store, normalize_subject

SUBJECT_DAYS = 90       # 제목으로 찾을 때 거슬러 올라가는 기간
MAX_FETCH = 400         # 한 번에 가져올 과거 메일 상한 (비정상적으로 긴 대화 방지)
MAX_PASSES = 2          # 헤더를 따라 거슬러 올라가는 단계 수
SUBJECT_QUERY_CHARS = 40


class Mailbox(Protocol):
    def context_folders(self) -> list[str]: ...
    def sent_folder(self) -> str | None: ...
    def search_header(self, folder: str, pairs: list[tuple[str, str]]) -> list[str]: ...
    def search_subject(self, folder: str, subject: str, since_days: int) -> list[str]: ...
    def fetch(self, folder: str, uid: str) -> Mail | None: ...


@dataclass
class Seed:
    """과거 이력을 찾을 대화 하나."""
    thread_key: str
    root: str                                   # 대화 뿌리 Message-ID
    own_ids: set[str] = field(default_factory=set)   # 이미 가진 메일들의 Message-ID
    ref_ids: set[str] = field(default_factory=set)   # 이미 가진 메일들이 가리키는 과거 메일 ID
    subject: str = ""                           # normalize_subject 결과
    linked: bool = False                        # 답장 헤더를 가진 메일이 있는가
    is_reply: bool = False                      # 제목에 회신·전달 표식이 있는 메일이 있는가


@dataclass
class ContextResult:
    mails: list[Mail] = field(default_factory=list)
    by_header: int = 0
    by_subject: int = 0
    folders: int = 0
    capped: bool = False
    warnings: list[str] = field(default_factory=list)


def seeds_from_store(store: Store, thread_keys) -> list[Seed]:
    seeds = []
    for key in sorted(thread_keys):
        rows = store.conn.execute(
            "SELECT message_id, in_reply_to, refs, subject FROM mails"
            " WHERE thread_key = ? ORDER BY sent_at", (key,)).fetchall()
        if not rows:
            continue
        own, refs_all, first_ref, linked = set(), set(), "", False
        for r in rows:
            if r["message_id"]:
                own.add(r["message_id"])
            try:
                refs = json.loads(r["refs"]) if r["refs"] else []
            except (json.JSONDecodeError, TypeError):
                refs = []
            if refs and not first_ref:
                first_ref = refs[0]
            refs_all.update(refs)
            if r["in_reply_to"]:
                refs_all.add(r["in_reply_to"])
            linked |= bool(refs or r["in_reply_to"])
        seeds.append(Seed(
            thread_key=key,
            root=first_ref or (rows[0]["message_id"] or ""),
            own_ids=own,
            ref_ids=refs_all - own,
            subject=normalize_subject(rows[-1]["subject"] or ""),
            linked=linked,
            is_reply=any(REPLY_PREFIX.match(r["subject"] or "") for r in rows),
        ))
    return seeds


def _subject_query(subject: str) -> str:
    """IMAP 제목 검색어. 너무 길면 단어 경계에서 자른다 (부분 일치 검색이라 충분)."""
    if len(subject) <= SUBJECT_QUERY_CHARS:
        return subject
    cut = subject[:SUBJECT_QUERY_CHARS]
    return cut.rsplit(" ", 1)[0] if " " in cut else cut


def collect_context(box: Mailbox, seeds: list[Seed], known_keys: set[str],
                    known_ids: set[str], progress: Callable[[str], None] = lambda _: None,
                    subject_days: int = SUBJECT_DAYS, max_fetch: int = MAX_FETCH,
                    max_passes: int = MAX_PASSES) -> ContextResult:
    res = ContextResult()
    if not seeds:
        return res
    known_keys, known_ids = set(known_keys), set(known_ids)
    cache: dict[str, Mail | None] = {}

    def get(folder: str, uid: str) -> Mail | None:
        key = f"{folder}:{uid}"
        if key not in cache:
            if len(cache) >= max_fetch:
                res.capped = True
                return None
            try:
                cache[key] = box.fetch(folder, uid)
            except Exception as e:
                res.warnings.append(f"메일 가져오기 실패 ({key}): {e}")
                cache[key] = None
        return cache[key]

    def take(folder: str, uids: list[str], accept=None, hint: str = "") -> list[Mail]:
        added = []
        for uid in sorted(uids, key=lambda u: int(u) if u.isdigit() else 0, reverse=True):
            key = f"{folder}:{uid}"
            if key in known_keys:
                continue
            m = get(folder, uid)
            if m is None or (accept and not accept(m)):
                continue
            if m.message_id and m.message_id in known_ids:
                known_keys.add(key)
                continue
            m.folder = folder
            m.thread_hint = hint
            known_keys.add(key)
            if m.message_id:
                known_ids.add(m.message_id)
            res.mails.append(m)
            added.append(m)
        return added

    try:
        folders = box.context_folders()
    except Exception as e:
        res.warnings.append(f"메일함 목록을 읽지 못해 과거 이력을 건너뜁니다: {e}")
        return res
    res.folders = len(folders)

    # 1) 헤더로 따라가기
    roots = {s.root for s in seeds if s.root}
    ids = set().union(*(s.ref_ids for s in seeds))
    own = set().union(*(s.own_ids for s in seeds))
    searched: set[tuple[str, str]] = set()
    header_error = False
    for step in range(1, max_passes + 1):
        pairs = [p for p in (
            [("References", r) for r in sorted(roots)]
            + [("In-Reply-To", i) for i in sorted(own)]
            + [("Message-ID", i) for i in sorted(ids - known_ids)]
        ) if p not in searched]
        if not pairs:
            break
        searched.update(pairs)
        found: list[Mail] = []
        for folder in folders:
            try:
                uids = box.search_header(folder, pairs)
            except Exception as e:
                header_error = True
                res.warnings.append(f"헤더 검색 실패 ({folder}): {e}")
                continue
            found += take(folder, uids)
        res.by_header += len(found)
        progress(f"헤더 검색 {step}단계: 메일함 {len(folders)}개에서 {len(found)}통")
        if not found:
            break
        # 새로 찾은 메일이 가리키는 더 오래된 메일을 다음 단계에서 찾는다
        roots = {m.references[0] for m in found if m.references}
        ids = {i for m in found for i in (*m.references, m.in_reply_to) if i}
        own = {m.message_id for m in found if m.message_id}

    # 2) 제목으로 찾기: 답장인데 헤더가 없거나, 헤더로 과거 메일을 하나도 못 찾은 대화만
    fallback = [s for s in seeds if s.subject and s.is_reply and (
        not s.linked or (s.ref_ids and not (s.ref_ids & known_ids)))]
    if fallback:
        sent = None
        try:
            sent = box.sent_folder()
        except Exception:
            pass
        subject_folders = ["INBOX"] + ([sent] if sent else [])
        for s in fallback:
            for folder in subject_folders:
                try:
                    uids = box.search_subject(folder, _subject_query(s.subject), subject_days)
                except Exception as e:
                    res.warnings.append(f"제목 검색 실패 ({folder}): {e}")
                    subject_folders = []            # 서버가 지원하지 않으면 더 시도하지 않는다
                    break
                added = take(folder, uids, hint=s.thread_key,
                             accept=lambda m, want=s.subject: normalize_subject(m.subject) == want)
                res.by_subject += len(added)
        if res.by_subject:
            progress(f"제목 검색: 헤더로 못 찾은 답장 {len(fallback)}개에서 {res.by_subject}통")

    if header_error and not res.by_header:
        res.warnings.append("헤더 검색이 동작하지 않아 과거 이력이 제목 검색 결과에만 의존합니다.")
    if res.capped:
        res.warnings.append(f"과거 메일이 {max_fetch}통을 넘어 일부만 가져왔습니다.")
    return res
