"""메일 본문 텍스트 처리.

회신 메일 본문 아래에는 이전 메일 전체가 인용돼 붙어 온다. 실측으로 이 회사 메일은
본문의 97% 가 인용 이력이었다. 스레드 전체를 모델에 보여줄 때 인용을 그대로 두면
같은 내용이 메일 수만큼 반복되므로, 새로 쓴 부분과 인용된 부분을 가른다.
"""
from __future__ import annotations

import re

# 전달·인용 헤더 한 줄 (From:/Sent:/보낸 사람: ...)
HEADER_LINE = re.compile(
    r"^\s*(?:from|sent|to|cc|subject|date|보낸\s*사람|받는\s*사람|보낸\s*날짜|참조|제목)\s*:.*$",
    re.I)
# 여기서부터 이전 메일 인용이 시작된다는 표식
QUOTE_LINE = re.compile(
    r"^\s*(?:-{2,}\s*(?:original message|forwarded message|원본\s*메(?:일|시지))\s*-{2,}"
    r"|(?:from|sent|보낸\s*사람|보낸\s*날짜)\s*:"
    r"|on\s.{0,80}\swrote:)", re.I)
# "홍길동 드림" 같은 맺음말. 이 뒤는 서명(소속·주소·연락처)이다
SIGN_OFF = re.compile(r"^\s*(?:\S{1,12}\s+){0,2}\S{1,12}\s*(?:드림|올림|배상)\s*[.!]?\s*$", re.M)
PHONE = re.compile(
    r"(?:\+82[\s-]?|\b0)1[016789][\s-]?\d{3,4}[\s-]?\d{4}"
    r"|\+82[\s-]?\d{1,2}[\s-]?\d{3,4}[\s-]?\d{4}")


# 네이버웍스·두레이 웹메일은 인용 머리말을 여러 줄로 쪼개 보낸다. 한 줄로 붙여야 인용으로 잡힌다.
#   "\t---------- \n원본 메일\n ----------"   ·   "보낸사람\n: 홍길동 <a@b.com>"
HEAD_WORD = re.compile(
    r"^\s*(?:from|sent|to|cc|subject|date|보낸\s*사람|받는\s*사람|보낸\s*날짜|참조|제목)\s*$", re.I)
DASH_LINE = re.compile(r"^\s*[-=_]{4,}\s*$")
ORIGIN_LINE = re.compile(
    r"^\s*(?:원본\s*메(?:일|시지)|original message|forwarded message)\s*$", re.I)


def join_split_headers(body: str | None) -> str:
    """쪼개진 인용 머리말을 한 줄로 모은다. 그 외 줄은 그대로 둔다."""
    lines = (body or "").splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line, nxt = lines[i], (lines[i + 1] if i + 1 < len(lines) else "")
        if HEAD_WORD.match(line) and nxt.lstrip().startswith(":"):
            out.append(f"{line.strip()}{nxt.strip()}")        # 보낸사람 + : 홍길동
            i += 2
            continue
        if ORIGIN_LINE.match(line) and out and DASH_LINE.match(out[-1]):
            out[-1] = "---------- 원본 메일 ----------"        # 구분선 + 원본 메일 + 구분선
            i += 1 + (1 if i + 1 < len(lines) and DASH_LINE.match(lines[i + 1]) else 0)
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _is_head(line: str) -> bool:
    return not line.strip() or bool(HEADER_LINE.match(line) or QUOTE_LINE.match(line))


def split_quoted(body: str | None) -> tuple[str, str]:
    """(새로 쓴 부분, 인용된 이전 메일). 인용 표식이 없으면 전부 새 내용이다.

    전달 메일은 맨 위가 헤더 블록(From:/Sent:/To:)으로 시작하는데, 그건 인용이
    아니라 전달된 내용의 머리이므로 건너뛰고 그 아래에서 인용 시작점을 찾는다.
    """
    lines = join_split_headers(body).splitlines()
    i = 0
    while i < len(lines) and _is_head(lines[i]):
        i += 1
    for j in range(i, len(lines)):
        if QUOTE_LINE.match(lines[j]):
            return "\n".join(lines[:j]).strip(), "\n".join(lines[j:]).strip()
    return "\n".join(lines).strip(), ""


def tidy(text: str) -> str:
    """전화번호를 가리고 빈 줄을 줄인다."""
    return re.sub(r"\n{3,}", "\n\n", PHONE.sub("[전화번호]", text or "")).strip()


def cut_signature(text: str) -> str:
    """맺음말("홍길동 드림") 이후의 서명을 잘라낸다."""
    m = SIGN_OFF.search(text or "")
    return text[:m.start()].rstrip() if m else (text or "")


def message_text(body: str | None) -> tuple[str, str]:
    """모델에 보여줄 (새로 쓴 내용, 인용된 이전 대화). 서명은 자르고 전화번호는 가린다."""
    new, quoted = split_quoted(body)
    return tidy(cut_signature(new)), tidy(quoted)


def snippet(body: str | None, limit: int = 120) -> str:
    """대시보드 미리보기 한 줄. 인용·서명·전달 헤더를 걷어내고 전화번호는 가린다."""
    from .rules import _clean_body

    new, _ = split_quoted(body)
    lines = new.splitlines()
    while lines and _is_head(lines[0]):
        lines.pop(0)                          # 전달 메일 맨 위의 헤더 블록
    text = tidy(_clean_body(cut_signature("\n".join(lines))))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")
