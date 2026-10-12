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
# 메일플러그(누리마케팅 등)는 인용 머리말과 본문 사이에 <mailplughead></mailplughtml> 같은
# 가짜 태그를 글자로 남긴다. 회신이 쌓이면 <xxmailplughead> 처럼 x 가 붙는다
MAILPLUG_TAG = re.compile(r"\s*(?:</?x*mailplug(?:head|html|body)>\s*)+", re.I)
# 줄바꿈이 모두 사라진 채 온 본문은 인용 표식이 문장 중간에 붙어 있다 (줄 맨 앞의 표식은 그대로)
INLINE_QUOTE = re.compile(
    r"(?<=[^\s-])[ \t]*(-{2,}[ \t]*(?:original message|forwarded message|원본[ \t]*메(?:일|시지))[ \t]*-{2,})",
    re.I)


def join_split_headers(body: str | None) -> str:
    """쪼개진 인용 머리말을 한 줄로 모으고, 줄 중간에 붙은 인용 표식은 새 줄로 뗀다.
    메일플러그 가짜 태그는 줄바꿈으로 바꾼다. 그 외 줄은 그대로 둔다."""
    text = INLINE_QUOTE.sub(r"\n\1", MAILPLUG_TAG.sub("\n", body or ""))
    lines = text.splitlines()
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


# 줄바꿈 없이 온 본문은 한글 문장 끝(마침표 바로 뒤에 글자)에서 줄을 나눈다. 주소·메일·소수(영문.영문, 1.5)는 그대로
SENTENCE_END = re.compile(r"(?<=[가-힣)])([.!?])(?=[가-힣A-Za-z0-9(\[■•·※])")
CLOSING_WORDS = r"(?:감사합니다|감사드립니다|고맙습니다|수고하세요|수고하십시오)"
CLOSING = re.compile(rf"^\s*{CLOSING_WORDS}\s*[.!~]*\s*$")
GLUED_CLOSING = re.compile(rf"(?<=[0-9A-Za-z])(?={CLOSING_WORDS}[.!~]*\n)")   # "...056905감사합니다."
CONTACT = re.compile(r"@|\b(?:tel|fax|mobile|e-?mail)\b|\d{2,4}-\d{3,4}-\d{4}|\+82", re.I)


def unglue(line: str) -> str:
    """줄바꿈이 사라진 문단("...입니다.바쁘신...")을 문장 끝에서 나눈다. 두 군데 이상 붙어 있을 때만."""
    if len(SENTENCE_END.findall(line)) < 2:
        return line
    return GLUED_CLOSING.sub("\n", SENTENCE_END.sub("\\1\n", line) + "\n").rstrip("\n")


def cut_contact_block(text: str) -> str:
    """마지막 맺음 인사("감사합니다.") 뒤가 연락처 서명이면 잘라낸다. "홍길동 드림"이 없는 메일용."""
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if CLOSING.match(lines[i]):
            tail = "\n".join(lines[i + 1:])
            if tail.strip() and CONTACT.search(tail) and len(tail) <= 600:
                return "\n".join(lines[:i + 1]).rstrip()
            break
    return text


def preview_text(body: str | None) -> str:
    """대시보드 '전체' 보기. 새로 쓴 부분만 남기고(인용 이력·전달 헤더·서명 제거) 줄바꿈은 살린다."""
    from .rules import SIGNATURE_CUT

    new, _ = split_quoted(body)
    lines = new.splitlines()
    while lines and _is_head(lines[0]):
        lines.pop(0)                          # 전달 메일 맨 위의 헤더 블록
    text = cut_signature("\n".join(lines))
    for m in SIGNATURE_CUT.finditer(text):
        tail = text[m.end():]
        if m.group().lstrip()[:1] in "-=" and not (CONTACT.search(tail) and len(tail) <= 600):
            continue                          # 구분선 아래가 본문(표·목록)이면 자르지 않는다
        text = text[:m.start()]
        break
    text = "\n".join(unglue(ln.strip()) for ln in text.splitlines())
    text = cut_contact_block("\n".join(ln.strip() for ln in text.splitlines()))
    return tidy(text)
