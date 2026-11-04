"""연락처가 포함된 메일 필드를 SQLite에 넣기 전에 가린다."""
from dataclasses import replace
import re

from .client import Mail
from .identity_map import EMAIL_RE, IdentityMap, _AFTER_NAME, _ANGLE_NAME, _mail_name
from .mail_redaction import mask_phone_numbers


_NAMED_LINE = re.compile(
    r"(?:^[ \t>]*(?:이름|성명|담당자)\s*[:：]\s*(?P<label>[가-힣]{2,6})\b|"
    r"^[ \t>]*(?P<sign>[가-힣]{2,6})[ \t]+(?:드림|올림|배상)[.!~]?[ \t]*$)", re.M,
)


def mask_email_addresses(text: str) -> str:
    def mask(match):
        local, domain = match["email"].rsplit("@", 1)
        if re.fullmatch(r"[^*]{0,2}\*{4}", local):
            return match["email"]
        return f"{local[:min(2, len(local) - 1)]}****@{domain}"

    return EMAIL_RE.sub(mask, text or "")


def _mask_name(name: str) -> str:
    if len(name) <= 2:
        return name[:1] + "*"
    return name[0] + "*" * (len(name) - 2) + name[-1]


def mask_collected_mail(mail: Mail) -> Mail:
    names = {name for value in (mail.from_name, *mail.recipient_names)
             if (name := _mail_name(value.partition("_")[0]))}
    for text in (mail.subject, mail.body):
        for pattern in (_ANGLE_NAME, _AFTER_NAME):
            names.update(name for match in pattern.finditer(text or "") if (name := _mail_name(match["name"])))
        names.update(match["label"] or match["sign"] for match in _NAMED_LINE.finditer(text or ""))
    name_pattern = "|".join(map(re.escape, sorted(names, key=lambda name: (-len(name), name))))
    pattern = re.compile(rf"{EMAIL_RE.pattern}|(?:{name_pattern})", re.I) if names else EMAIL_RE

    def redact(text: str) -> str:
        text = text or ""
        boundaries = text.replace("_", " ")

        def mask(match):
            if match["email"]:
                return mask_email_addresses(match.group())
            if IdentityMap._name_boundary(boundaries, match.start(), match.end()):
                return _mask_name(match.group())
            return match.group()

        return mask_phone_numbers(pattern.sub(mask, text), keep_last_four=True)

    return replace(mail, subject=redact(mail.subject), body=redact(mail.body),
                   from_name=redact(mail.from_name), from_email=mask_email_addresses(mail.from_email),
                   to=[mask_email_addresses(address) for address in mail.to],
                   cc=[mask_email_addresses(address) for address in mail.cc], recipient_names=[])
