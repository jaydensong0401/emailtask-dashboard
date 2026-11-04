"""AI 전송 복사본의 전화번호와 서명만 정리한다. 원문 저장소/메일 객체는 변경하지 않는다."""
from __future__ import annotations

import re

REDACTION_VERSION = "2026-10-19.2"
PHONE_MARKER = "[전화번호]"

# 탐지용 복사본만 1문자→1문자로 정규화한다. 원문의 나머지 문자열/ID는 그대로 반환한다.
_TRANSLATION = {ord(a): b for a, b in zip("０１２３４５６７８９＋（）", "0123456789+()")}
_TRANSLATION.update({ord(ch): "-" for ch in "‐‑‒–—―−﹘﹣－"})
_TRANSLATION.update({ord(ch): " " for ch in "\u00a0\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u200b\u202f\u205f\u3000"})
_SPACE = r"[ \t]*"
_SEP = r"[ \t.\-]*"
_PREFIX = r"(?:01[016789]|02|0(?:3[1-3]|4[1-4]|5[1-5]|6[1-4])|0[678]0|050[2-8]?)"
_DOMESTIC = rf"\(?{_PREFIX}\)?{_SEP}\(?[0-9]{{3,4}}\)?{_SEP}\(?[0-9]{{4}}\)?"
_SERVICE = rf"\(?1[568][0-9]{{2}}\)?{_SEP}\(?[0-9]{{4}}\)?"
# '+'가 있는 국제전화는 국가번호 포함 8~15자리. +82(0)도 괄호를 포함해 인식한다.
_INTERNATIONAL = r"\(?\+[ \t]*[1-9](?:[ \t.()\-]*[0-9]){7,14}\)?"
_EXTENSION = rf"(?:{_SPACE}\(?{_SPACE}(?:내선(?:번호)?|ext(?:ension)?\.?|x|\#){_SPACE}[:.\-]?{_SPACE}[0-9]{{1,6}}{_SPACE}\)?)?"
_PHONE = re.compile(
    rf"(?<![A-Za-z0-9])(?:{_INTERNATIONAL}|{_DOMESTIC}|{_SERVICE})"
    # 국제전화 뒤 수량을 전화의 일부로 삼키지 않는다 (예: '+44 ... 5개').
    rf"(?![A-Za-z0-9]|[.\-/][0-9]|개|건|명|원|%|년|월|일|시|분|초){_EXTENSION}(?![A-Za-z0-9])", re.I)
_STANDALONE_EXT = re.compile(
    rf"(?<![A-Za-z0-9.])(?:내선(?:번호)?|ext(?:ension)?\.?){_SPACE}[:.\-]?{_SPACE}[0-9]{{1,6}}(?![A-Za-z0-9])", re.I)
_IDENTIFIER_LABEL = re.compile(
    r"(?:주문(?:번호|코드|\s*ID)|발주(?:번호|코드)|견적(?:번호|코드)|계약(?:번호|코드)|"
    r"접수(?:번호|코드)|상품(?:번호|코드)|모델(?:명|번호)|품번|일련번호|"
    r"order\s*(?:no\.?|number|id)|invoice\s*(?:no\.?|number|id)|sku|serial(?:\s*number)?)"
    r"\s*[:#：=]?\s*$", re.I)
_IDENTIFIER_PREFIX = re.compile(r"(?:ORD(?:ER)?|SKU|INV(?:OICE)?|PO|ITEM|PRODUCT|PART|SERIAL|TICKET)[-_.]$", re.I)


def _phone_label(value: str, *, keep_last_four: bool = False) -> str:
    """국내번호는 지역/통신 앞자리만 남기고 가입자번호·내선은 복원하지 않는다."""
    main = re.split(r"(?:내선|ext|\bx|#)", value, maxsplit=1, flags=re.I)[0]
    digits = re.sub(r"\D", "", main)
    if "+" in main:
        if not digits.startswith("82"):
            return PHONE_MARKER
        digits = "0" + digits[2:].lstrip("0")
    prefix = re.match(_PREFIX, digits)
    if prefix:
        head = prefix.group()
        middle = len(digits) - len(head) - 4
        tail = digits[-4:] if keep_last_four else "****"
        return f"{head}-{'*' * max(3, min(4, middle))}-{tail}"
    if re.fullmatch(r"1[568][0-9]{6}", digits):
        return digits[:4] + "-****"
    return PHONE_MARKER


def mask_phone_numbers(text: str | None, *, keep_last_four: bool = False) -> str:
    """전화 형식만 가린다. 명시된 주문/상품/일련번호와 일반 날짜·수량은 유지한다."""
    original = text or ""
    if not keep_last_four:
        original = re.sub(rf"(?<![0-9])({_PREFIX}-\*{{3,4}}-)[0-9]{{4}}(?![0-9])", r"\g<1>****", original)
    normalized = original.translate(_TRANSLATION)
    pieces: list[str] = []
    cursor = 0
    for match in _PHONE.finditer(normalized):
        left = normalized[max(0, normalized.rfind("\n", 0, match.start()) + 1):match.start()]
        if _IDENTIFIER_LABEL.search(left):
            continue
        # 주문/상품 ID의 명시적 접두사만 보존한다. sender-/thread- 이메일 속 번호는 가린다.
        right = normalized[match.end():].split("\n", 1)[0]
        in_email = bool(re.match(r"[^\s<>]*@[^\s<>]+", right))
        if _IDENTIFIER_PREFIX.search(left) and not in_email:
            continue
        pieces.extend((original[cursor:match.start()], _phone_label(match.group(), keep_last_four=keep_last_four)))
        cursor = match.end()
    pieces.append(original[cursor:])
    masked = "".join(pieces)
    # 줄이 나뉜 내선도 남기지 않는다. x123/#123은 독립 ID일 수 있어 위 전화 뒤에서만 가린다.
    normalized = masked.translate(_TRANSLATION)
    pieces, cursor = [], 0
    for match in _STANDALONE_EXT.finditer(normalized):
        pieces.extend((masked[cursor:match.start()], PHONE_MARKER))
        cursor = match.end()
    pieces.append(masked[cursor:])
    return "".join(pieces)


_QUOTE_PREFIX = re.compile(r"^[ \t]*(?:>[ \t]*)*")
_HEAD_WORD = re.compile(r"^(?:from|sent|to|cc|subject|date|보낸\s*사람|받는\s*사람|보낸\s*날짜|참조|제목)\s*$", re.I)
_MAIL_HEADER = re.compile(r"^(?:from|sent|to|cc|subject|date|보낸\s*사람|받는\s*사람|보낸\s*날짜|참조|제목)\s*:", re.I)
_ORIGIN = re.compile(r"^(?:original\s+message|forwarded\s+message|원본\s*메(?:일|시지)|전달된\s*메(?:일|시지))$", re.I)
_DASHES = re.compile(r"^[-=_]{2,}$")
_MAILPLUG = re.compile(r"\s*(?:</?x*mailplug(?:head|html|body)>\s*)+", re.I)
_INLINE_ORIGIN = re.compile(r"(?<=[^\s-])(-{2,}[ \t]*(?:original message|forwarded message|원본[ \t]*메(?:일|시지))[ \t]*-{2,})", re.I)
_BOUNDARY = re.compile(
    r"^(?:(?:from|보낸\s*사람)\s*(?::|$)|"
    r"[-=_]*\s*(?:original\s+message|forwarded\s+message|원본\s*메(?:일|시지)|전달된\s*메(?:일|시지))\s*[-=_]*$|"
    r"on\s+.{0,300}\s+wrote\s*:|.{0,150}님이\s*작성\s*:)", re.I)
_KOREAN_SIGNOFF = re.compile(
    r"^(?:(?:[김이박최정강조윤장임한오서신권황안송류유홍전고문양손배백허노남심하주구곽성차우진민][가-힣]{1,4})"
    r"(?:\s+(?:매니저|선임|책임|대리|과장|차장|부장|이사|사원|대표|수석))?\s*|"
    r"[A-Za-z][A-Za-z .'-]{0,40}\s+)?(?:드림|올림|배상)[.!~。]*$", re.I)
_KOREAN_SHORT_SIGNOFF = re.compile(r"^(?:[가-힣A-Za-z·().&/*\-]{1,16}[ \t]+){1,3}(드림|배상|올림)[.!~。]*$", re.I)
_ENGLISH_SIGNOFF = re.compile(r"^(?:(?:best|kind|warm)\s+regards|regards|best\s+wishes|sincerely|yours\s+(?:sincerely|faithfully)|cheers|best)[,.!]*$", re.I)
_THANKS = re.compile(r"^(?:감사합니다|감사드립니다|고맙습니다|수고하세요|수고하십시오|thanks(?:\s+and\s+regards)?|thank\s+you)[,.!~。]*$", re.I)
_MOBILE = re.compile(
    r"^(?:sent\s+(?:from|with)\s+(?:my\s+)?(?:iphone|ipad|android|samsung|outlook|mail\s+for).{0,100}|"
    r"(?:iphone|ipad|android|삼성|갤럭시|아이폰|아이패드|outlook).{0,60}(?:보냄|보냈습니다|보낸\s*메일)|"
    r".{0,30}모바일.{0,30}(?:보냄|보냈습니다))[.!]*$", re.I)
_WORK = re.compile(
    r"(?:업무|업무요청|부탁|주세요|바랍니다|하겠습니다|해야\s|필요합니다|예정입니다|완료했습니다|"
    r"검토해|확인해|반영해|회신해|첨부파일\s*:|다음\s*(?:단계|주|일정)|기한\s*:|요청사항|전달사항)|"
    r"\b(?:please|review|confirm|approve|update|deadline|attached|attachment|next\s+steps|action\s+items|task|business)\b|"
    r"^(?:we|i|you|our|your|the\s+project)\s+", re.I)
_CONTACT = re.compile(
    r"^(?:tel(?:ephone)?|t|m|f|fax|mobile|phone|e-?mail|e|web|homepage|office|연락처|전화|휴대폰|팩스|이메일|주소)\s*[:.)\-]|"
    r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?:https?://|www\.)", re.I)
_SIGNATURE_META = re.compile(
    r"(?:팀|본부|부서|그룹|센터|주식(?:회사)|㈜|대표|매니저|선임|책임|사원|대리|과장|차장|부장|이사|수석|"
    r"서울|경기도|인천|부산|[가-힣]+(?:로|길)\s*\d|서명|signature|manager|director|engineer|\binc\b|\bcorp\b|\bltd\b)", re.I)


def _parts(line: str) -> tuple[int, str, str]:
    prefix = _QUOTE_PREFIX.match(line).group()
    return prefix.count(">"), prefix, line[len(prefix):].strip()


def _normalize_boundaries(text: str) -> list[str]:
    """쪼개진 Outlook/웹메일 머리말과 mailplug 경계를 공통 함수 안에서 복구한다."""
    text = _INLINE_ORIGIN.sub(r"\n\1", _MAILPLUG.sub("\n", text))
    lines, out, index = text.splitlines(), [], 0
    while index < len(lines):
        depth, prefix, content = _parts(lines[index])
        if index + 1 < len(lines):
            next_depth, _, next_content = _parts(lines[index + 1])
            if depth == next_depth and _HEAD_WORD.fullmatch(content) and next_content.startswith(":"):
                out.append(prefix + content + next_content)
                index += 2
                continue
            if depth == next_depth and _DASHES.fullmatch(content) and _ORIGIN.fullmatch(next_content):
                out.append(prefix + "-----Original Message-----")
                index += 2
                if index < len(lines) and _DASHES.fullmatch(_parts(lines[index])[2]):
                    index += 1
                continue
        out.append(lines[index])
        index += 1
    return out


def _is_work(content: str) -> bool:
    return bool(_WORK.search(content))


def _has_contact_tail(lines: list[str], start: int, *, conservative: bool = False) -> bool:
    for line in lines[start:start + 30]:
        content = _parts(line)[2]
        if not content:
            continue
        if _MAIL_HEADER.match(content):
            return False                      # 인용 머리말의 이름/주소는 서명 증거가 아니다.
        if _is_work(content):
            return False
        normalized = content.translate(_TRANSLATION)
        if _CONTACT.search(content) or _PHONE.search(normalized) or _MOBILE.fullmatch(content):
            return True
        if conservative and not (_SIGNATURE_META.search(content)
                                 or re.fullmatch(r"[A-Za-z0-9 ._&()/|+\-]{1,100}", content)
                                 or re.fullmatch(r"[가-힣]{2,4}(?:\s+[가-힣]{1,5})?", content)):
            return False
    return False


def _sanitize_block(lines: list[str]) -> list[str]:
    out, index = [], 0
    header_pending = bool(lines and (_BOUNDARY.match(_parts(lines[0])[2])
                                     or _MAIL_HEADER.match(_parts(lines[0])[2])))
    while index < len(lines):
        content = _parts(lines[index])[2]
        short_signoff = _KOREAN_SHORT_SIGNOFF.fullmatch(content)
        # '올림'은 자료 업로드 업무일 수 있으므로 별칭/부서명일 때 연락처 꼬리도 확인한다.
        named_signoff = bool(_KOREAN_SIGNOFF.fullmatch(content) or (short_signoff and (
            short_signoff.group(1) != "올림" or _has_contact_tail(lines, index + 1))))
        strong = bool(named_signoff or _ENGLISH_SIGNOFF.fullmatch(content)
                      or _MOBILE.fullmatch(content))
        thanks = bool(_THANKS.fullmatch(content) and _has_contact_tail(lines, index + 1, conservative=True))
        divider = bool(_DASHES.fullmatch(content) and _has_contact_tail(lines, index + 1))
        contact_block = bool(not content and out and not header_pending
                             and _has_contact_tail(lines, index + 1, conservative=True))
        if strong or thanks or divider or contact_block:
            if thanks:
                out.append(lines[index])
            index += 1
            # 새 업무 문장이 뒤에 있으면 서명만 걷어내고 그 문장부터 다시 처리한다.
            while index < len(lines) and not _is_work(_parts(lines[index])[2]):
                index += 1
            continue
        out.append(lines[index])
        if content and not (_MAIL_HEADER.match(content) or _BOUNDARY.match(content) or _DASHES.fullmatch(content)):
            header_pending = False
        index += 1
    return out


def strip_signatures(text: str | None) -> str:
    """원문을 바꾸지 않고 각 인용의 서명만 제거한다. 별칭 처리는 이 다음에 한다."""
    lines = _normalize_boundaries(text or "")
    output, block = [], []
    depth = None
    for line in lines:
        next_depth, _, content = _parts(line)
        if block and (next_depth != depth or _BOUNDARY.match(content)):
            output.extend(_sanitize_block(block))
            block = []
        block.append(line)
        depth = next_depth
    output.extend(_sanitize_block(block))
    clean = "\n".join(output)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean


def sanitize_body(text: str | None) -> str:
    """기본 전송용 정리. 이름·이메일 별칭은 strip_signatures 다음 단계에 적용한다."""
    return mask_phone_numbers(strip_signatures(text))
