"""프로젝트 식별 규칙.

프로젝트는 고객사 > 세부 프로젝트 > 업무 세 단계로 묶는다.
  오로라 > AX9 프라이빗 테니스클럽 > 알림톡 문구 변경 요청
  오로라 > 오로라 한결카드 PLCC > 카카오톡 안내 알림톡 발송 요청

실제 메일을 분석해 만든 규칙 (위에서부터 먼저 맞는 것):
  1. 제목 키워드 ("AX9 프라이빗 테니스" -> AX9 프라이빗 테니스클럽, "PLCC" -> 한결카드 PLCC)
  2. 업무 정리(LLM)가 고른 세부 프로젝트
  3. 제목 대괄호 태그. 누리·루미는 회사라서 프로젝트가 아니라, 그 회사가 맡은 일로 보낸다
     ([루미] -> 오로라 스토어, [NURI] = [누리마케팅] -> 오로라 운영(기타))
     말머리는 프로젝트가 아니다 ([RE], [문의], [개발], [공유], [검토요청])
  4. 발신 도메인 (aurora.example -> 오로라 운영(기타))
규칙은 projects.json 에 있으므로 코드 수정 없이 바꿀 수 있다.

프로젝트 안에서는 같은 메일명(업무명)끼리 한 묶음으로 보여준다. 업무명은 제목에서
회신 표식·앞머리 태그·날짜/고객명 꼬리·프로젝트 이름을 걷어낸 것이다.
  "RE: [RE][누리마케팅] 신청페이지 수정 관련 이미지 전달의 건_20260903"
  -> "신청페이지 수정 관련 이미지 전달"
  "[NURI] 시그니처 신청 기한 연장 요청의 건_홍○동 고객"  (고객만 다른 메일끼리 묶임)
  -> "시그니처 신청 기한 연장 요청"
  "[누리마케팅] AX9  프라이빗 테니스 클럽 레슨_알림톡 문구 변경 요청의 건"
  -> "알림톡 문구 변경 요청"   (AX9 프라이빗 테니스클럽 안에서)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache

from .config import ROOT
from .store import REPLY_PREFIX

CONFIG_PATH = ROOT / "projects.json"
TAG_RE = re.compile(r"\[\s*([^\]]{1,30}?)\s*\]")

DEFAULT_ROLES = ["개발", "디자인", "수정", "기타"]
DEFAULT_EXTRACT_SKIP = ["일일 통계 데이터"]
DEFAULT_FIX_TAGS = ["개발", "디자인", "데이터"]
FALLBACK_ROLE = "기타"
FIX_ROLE = "수정"

LEADING_TAG = re.compile(r"^\s*\[[^\]]{1,40}\]\s*")
# 제목 끝에 붙는, 같은 업무인데 메일마다 달라지는 꼬리
TITLE_TAIL = re.compile(
    r"(?:"
    r"\s*_\s*\d{6,8}"                                        # _20260903
    r"|\s*_\s*[^_]{1,20}?\s*(?:고객님|고객|님)"                 # _홍○동 고객
    r"|\s*\(\s*~?\s*\d{1,2}/\d{1,2}(?:[^()]|\([^()]*\))*\)"   # (9/8 런칭), (~8/7(금) 10:00)
    r"|\s*의\s*건|\s+건"                                      # ~의 건, ~ 건
    r"|\s*[.。]+"                                            # 끝 마침표
    r")\s*$"
)
# 프로젝트 이름을 떼고 남은 앞머리 구분자 ("오로라 포럼 - 가을 음악회" -> "가을 음악회")
LEAD_SEP = re.compile(r"^[\s\-–—:·_/,.]+")
NAME_END = r"(?=$|[\s\-–—:·_/,.()\[\]])"      # 키워드 뒤가 이어진 낱말이면 떼지 않는다 (스토어몰)


def squash(text: str) -> str:
    """키워드 비교용: 띄어쓰기를 없애고 소문자로 ('AX9  프라이빗 테니스' = 'ax9프라이빗테니스')."""
    return re.sub(r"\s+", "", text or "").lower()


def _loose(keyword: str) -> str:
    """제목에서 키워드를 찾는 정규식. 글자 사이 띄어쓰기는 있어도 없어도 된다."""
    return r"\s*".join(re.escape(c) for c in keyword if not c.isspace())


@dataclass
class ProjectRules:
    non_project_tags: set[str] = field(default_factory=set)
    aliases: dict[str, str] = field(default_factory=dict)   # 표기(소문자) -> 대표이름
    domain_hints: dict[str, str] = field(default_factory=dict)
    roles: list[str] = field(default_factory=lambda: list(DEFAULT_ROLES))
    fix_tags: list[str] = field(default_factory=lambda: list(DEFAULT_FIX_TAGS))
    org_domains: dict[str, str] = field(default_factory=dict)  # 메일 도메인 -> 소속
    extract_skip: list[str] = field(default_factory=lambda: list(DEFAULT_EXTRACT_SKIP))
    clients: list[str] = field(default_factory=list)              # 고객사 (설정 순서)
    client_of: dict[str, str] = field(default_factory=dict)       # 세부 프로젝트 -> 고객사
    defaults: dict[str, str] = field(default_factory=dict)        # 고객사 -> 기본 프로젝트(기타)
    keywords: dict[str, list[str]] = field(default_factory=dict)  # 세부 프로젝트 -> 제목 키워드
    org_projects: dict[str, str] = field(default_factory=dict)    # 회사(누리·루미) -> 맡은 프로젝트

    def is_project_tag(self, tag: str) -> bool:
        return tag.strip().lower() not in self.non_project_tags

    def canonicalize(self, name: str) -> str:
        """표기 흔들림을 대표 이름으로 정규화. 모르는 이름은 그대로 둔다."""
        return self.aliases.get(name.strip().lower(), name.strip())

    def is_default(self, project: str) -> bool:
        """고객사의 기본 프로젝트(기타)인가. 키워드로 못 정한 대화가 모이는 곳."""
        return bool(project) and self.defaults.get(self.client_of.get(project, "")) == project

    def match_keyword(self, text: str) -> str:
        """텍스트에 키워드가 들어간 첫 세부 프로젝트 (설정 순서). 없으면 ''."""
        s = squash(text)
        for project, words in self.keywords.items():
            if any(squash(w) in s for w in words):
                return project
        return ""

    def resolve(self, name: str, projects_only: bool = False) -> str:
        """이름(태그 · 모델이 적은 프로젝트 · 도메인 힌트)을 세부 프로젝트로. 모르면 ''.

        projects_only 면 기본 프로젝트(기타)가 아닌 세부 프로젝트로 확정되는 이름만 받는다.
        고객사(오로라)·회사(누리) 이름은 너무 넓어서 태그 같은 다른 근거를 먼저 본다.
        """
        raw = (name or "").strip()
        if not raw:
            return ""
        project = next((p for p in self.client_of if squash(p) == squash(raw)), "") \
            or self.match_keyword(raw)
        if not project and not projects_only:
            canon = self.canonicalize(raw)
            if canon in self.org_projects:
                project = next((p for p in self.client_of
                                if squash(p) == squash(self.org_projects[canon])), "")
            else:
                client = next((c for c in self.clients if squash(c) == squash(canon)), "")
                project = self.defaults.get(client, "")
        return "" if projects_only and self.is_default(project) else project


@lru_cache(maxsize=1)
def load_rules(path: str | None = None) -> ProjectRules:
    p = ROOT / path if path else CONFIG_PATH
    if not p.exists():
        return ProjectRules()
    raw = json.loads(p.read_text(encoding="utf-8"))

    aliases: dict[str, str] = {}
    for canon, variants in (raw.get("aliases") or {}).items():
        aliases[canon.strip().lower()] = canon
        for v in variants:
            aliases[v.strip().lower()] = canon

    clients: list[str] = []
    client_of: dict[str, str] = {}
    defaults: dict[str, str] = {}
    keywords: dict[str, list[str]] = {}
    for client, spec in (raw.get("clients") or {}).items():
        spec = spec or {}
        clients.append(client)
        defaults[client] = (spec.get("default") or client).strip()
        for project, words in (spec.get("projects") or {}).items():
            client_of.setdefault(project, client)
            keywords[project] = [w for w in words or [] if w.strip()]
        client_of.setdefault(defaults[client], client)
        keywords.setdefault(defaults[client], [])

    roles = list(raw.get("roles") or DEFAULT_ROLES)
    if FALLBACK_ROLE not in roles:
        roles.append(FALLBACK_ROLE)
    return ProjectRules(
        non_project_tags={t.strip().lower() for t in raw.get("non_project_tags", [])},
        aliases=aliases,
        domain_hints={k.lower(): v for k, v in (raw.get("domain_hints") or {}).items()},
        roles=roles,
        fix_tags=list(raw.get("fix_tags") or DEFAULT_FIX_TAGS),
        org_domains={k.lower(): v for k, v in (raw.get("org_domains") or {}).items()},
        extract_skip=[s for s in (raw.get("extract_skip", DEFAULT_EXTRACT_SKIP) or []) if s.strip()],
        clients=clients, client_of=client_of, defaults=defaults, keywords=keywords,
        org_projects={k: v for k, v in (raw.get("org_projects") or {}).items() if v},
    )


def tags_in(subject: str) -> list[str]:
    return [t for t in TAG_RE.findall(subject or "") if t.strip()]


def classify(subject: str, from_email: str = "", model_project: str = "",
             rules: ProjectRules | None = None) -> tuple[str, str, str]:
    """(고객사, 세부 프로젝트, 근거). 못 정하면 ('', '', 'unknown').

    근거는 keyword / model / tag / domain / unknown. 설정에 없는 태그나 모델이 지은 이름은
    고객사 없이 그 이름을 프로젝트로 쓴다 (예: ['26 전사 워크숍]).
    """
    r = rules or load_rules()
    model = (model_project or "").strip()

    def placed(project: str, why: str) -> tuple[str, str, str]:
        return r.client_of.get(project, ""), project, why

    if hit := r.match_keyword(subject):
        return placed(hit, "keyword")
    if hit := r.resolve(model, projects_only=True):
        return placed(hit, "model")
    tags = [r.canonicalize(t) for t in tags_in(subject) if r.is_project_tag(t)]
    for tag in tags:
        if hit := r.resolve(tag):
            return placed(hit, "tag")
    if hit := r.resolve(model):
        return placed(hit, "model")
    if tags:
        return "", tags[0], "tag"
    if model:
        return "", r.canonicalize(model), "model"
    hint = r.domain_hints.get((from_email or "").split("@")[-1].lower(), "")
    if hit := r.resolve(hint):
        return placed(hit, "domain")
    return ("", hint, "domain") if hint else ("", "", "unknown")


def skip_extract(subject: str, rules: ProjectRules | None = None) -> bool:
    """정기 보고·알림처럼 할 일이 거의 안 나오는데 매일 붙어 크게 자라는 대화.

    대시보드에는 보이되 추출(Claude 호출)에서는 뺀다. 제목에 설정 문구가 들어가면 해당.
    """
    s = (subject or "").lower()
    return any(k.lower() in s for k in (rules or load_rules()).extract_skip)


def known_projects(rules: ProjectRules | None = None) -> list[str]:
    """LLM 에게 보여줄 세부 프로젝트 목록 (추론 시 표기 통일용)."""
    return list((rules or load_rules()).client_of)


# ── 업무명 (같은 메일명 묶기) ───────────────────────────────────────────
def _clean_title(text: str) -> str:
    s, prev = text, None
    while prev != s:
        prev = s
        s = REPLY_PREFIX.sub("", s)
        s = LEADING_TAG.sub("", s)
        s = TITLE_TAIL.sub("", s).strip()
    return s


def _strip_project(title: str, names: list[str]) -> str:
    """업무명 앞머리의 프로젝트 이름을 뗀다. 카드 제목이 이미 프로젝트 이름이라서.

    "AX9 프라이빗 테니스 클럽 레슨_알림톡 문구 변경 요청" -> "알림톡 문구 변경 요청"
    "오로라 한결카드 (오로라 PLCC 3.0) 카카오톡 안내 알림톡" -> "카카오톡 안내 알림톡"
    """
    loose = [_loose(n) for n in sorted(names, key=len, reverse=True) if n.strip()]
    head, sep, tail = title.partition("_")
    if sep and tail.strip() and any(re.search(p, head, re.I) for p in loose):
        return tail
    s = title
    for p in loose:
        if m := re.match(r"^\s*" + p + NAME_END, s, re.I):
            s = LEAD_SEP.sub("", s[m.end():])
            break
    if s != title and (m := re.match(r"^\(([^()]*)\)", s)) \
            and any(re.search(p, m.group(1), re.I) for p in loose):
        s = s[m.end():]
    return LEAD_SEP.sub("", s)


def work_title(subject: str, project: str = "", rules: ProjectRules | None = None) -> str:
    """제목에서 회신 표식, 앞머리 대괄호 태그, 날짜·고객명 꼬리, '의 건' 을 걷어낸다.

    앞머리 태그는 회사([누리마케팅])나 말머리([문의])라서 프로젝트 카드 제목이
    이미 대신한다. project 를 주면 앞머리의 그 프로젝트 이름·키워드도 뗀다. 단 기본
    프로젝트(기타)의 키워드는 업무 주제라서 남긴다. 다 걷어내 빈 문자열이 되면 한 단계 전 제목을 쓴다.
    """
    original = re.sub(r"\s+", " ", subject or "").strip()
    s = _clean_title(original) or REPLY_PREFIX.sub("", original).strip()
    if not project:
        return s
    r = rules or load_rules()
    if r.is_default(project):
        return s
    return _clean_title(_strip_project(s, [project, *r.keywords.get(project, [])])) or s


def title_key(title: str) -> str:
    """묶기 비교용 키. 띄어쓰기·문장부호 차이는 같은 업무로 본다."""
    return re.sub(r"[\W_]+", "", title or "").lower()


# ── 역할 · 소속 ─────────────────────────────────────────────────────────
def normalize_role(role: str | None, fix_tag: str | None = None,
                   rules: ProjectRules | None = None) -> tuple[str, str]:
    """(역할, 수정 태그). 설정에 없는 역할(기획·마케팅 등)은 '기타' 로 모은다.

    수정 태그는 역할이 '수정' 일 때만 붙고, 설정의 fix_tags 에 있는 값만 쓴다.
    """
    r = rules or load_rules()
    name = (role or "").strip()
    name = name if name in r.roles else FALLBACK_ROLE
    tag = (fix_tag or "").strip() if name == FIX_ROLE else ""
    return name, (tag if tag in r.fix_tags else "")


def org_for_email(email: str | None, rules: ProjectRules | None = None) -> str:
    r = rules or load_rules()
    addr = (email or "").strip().strip("<>")
    return r.org_domains.get(addr.split("@")[-1].lower(), "") if "@" in addr else ""


def canonical_org(name: str | None, rules: ProjectRules | None = None) -> str:
    """LLM 이 적은 소속 표기를 설정의 회사 이름으로 맞춘다 ('누리마케팅' -> '누리').

    설정에 없는 회사(예: 우리은행)는 적힌 그대로 둔다.
    """
    r = rules or load_rules()
    raw = (name or "").strip()
    if not raw:
        return ""
    orgs = set(r.org_domains.values())
    if raw in orgs:
        return raw
    if (canon := r.canonicalize(raw)) in orgs:
        return canon
    for org in sorted(orgs, key=len, reverse=True):     # '오로라고객경험팀' -> '오로라'
        if org.lower() in raw.lower():
            return org
    return raw


def person_key(name: str | None) -> str:
    """'홍길동 주임', '이지우 Jiwoo Lee 매니저 …' 에서 이름(첫 단어)만."""
    parts = (name or "").strip().split()
    return parts[0] if parts else ""


def people_orgs(mails: list[dict], rules: ProjectRules | None = None) -> dict[str, str]:
    """대화에서 메일을 보낸 사람 이름 -> 소속. 담당자 소속을 메일 주소로 확정하는 데 쓴다."""
    r = rules or load_rules()
    out: dict[str, str] = {}
    for m in mails:
        key = person_key(m.get("from_name"))
        if key and (org := org_for_email(m.get("from_email"), r)):
            out.setdefault(key, org)
    return out
