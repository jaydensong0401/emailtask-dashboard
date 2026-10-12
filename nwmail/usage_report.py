"""기록된 토큰 사용량 비교와 보존용 보고서. 메일 내용이나 AI 호출은 필요 없다.

완료된 한국 날짜만 비교한다. 캐시/분석 제외는 실행 기록의 대화 횟수이고,
증분은 호출 방식별 실측 사용량이다. 반사실적인 '절약했을 토큰'은 계산하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path

from .config import ROOT
from .store import KST, Store
from .usage import PURPOSES, TOKEN_FIELDS, kst_day

REPORT_DIR = ROOT / "reports" / "token-usage"
SCHEMA_VERSION = 1
_TOKEN_SQL = " + ".join(f"COALESCE({f}, 0)" for f in TOKEN_FIELDS)
_CALL_SUMS = (
    f"COUNT(*) AS calls, COALESCE(SUM({_TOKEN_SQL}), 0) AS tokens, "
    + ", ".join(f"COALESCE(SUM({f}), 0) AS {f}" for f in TOKEN_FIELDS)
    + ", COALESCE(SUM(cost_usd), 0) AS cost_usd, "
    "COALESCE(SUM(ok = 0), 0) AS failed_calls, "
    f"COALESCE(SUM(ok = 1 AND ({_TOKEN_SQL}) = 0), 0) AS unmetered_calls, "
    "COALESCE(SUM(threads), 0) AS sent_threads, "
    f"COALESCE(SUM(CASE WHEN purpose = 'extract' THEN {_TOKEN_SQL} ELSE 0 END), 0) AS extract_tokens, "
    "COALESCE(SUM(CASE WHEN purpose = 'extract' THEN threads ELSE 0 END), 0) AS extract_sent_threads, "
    "COALESCE(SUM(prompt_chars), 0) AS prompt_chars"
)
_RUN_SUMS = (
    "COUNT(*) AS runs, COALESCE(SUM(threads), 0) AS considered_threads, "
    "COALESCE(SUM(cached), 0) AS cached_threads, COALESCE(SUM(skipped), 0) AS skipped_threads, "
    "COALESCE(SUM(extracted), 0) AS extracted_threads, COALESCE(SUM(failed), 0) AS failed_threads"
)
NOTES = (
    "오늘은 진행 중이므로 비교에서 제외합니다. 양쪽 모두 같은 길이의 완료된 한국 날짜입니다.",
    "토큰은 입력 + 출력 + 캐시 읽기 + 캐시 쓰기의 기록 합계입니다. 실패 호출에 기록된 토큰도 포함합니다.",
    "기록의 감소는 업무량·호출 수·모델·실패율 변화에도 영향을 받습니다. 특정 최적화의 인과 효과로 단정하지 않습니다.",
    "캐시 재사용·분석 제외는 실행마다 센 대화 횟수입니다. 같은 대화가 여러 실행에서 중복 집계될 수 있습니다.",
    "분석 제외 기록은 정기 메일 제외와 사용자 제외를 합친 값이며, 과거 기록에서 둘을 구분할 수 없습니다.",
    "증분·캐시 읽기 토큰은 실제 사용량입니다. 전체 전송 시의 대조 토큰이 없으므로 별도의 절감 토큰 추정은 하지 않습니다.",
    "성공했지만 토큰이 0인 호출은 미집계로 표시합니다. 기록이 없던 날은 미사용이 확인된 날과 구분할 수 없습니다.",
    "API 환산 금액은 기존 호출 기록의 참고값이며 구독의 실제 청구 절감액을 뜻하지 않습니다.",
)


def _midnight(day: date) -> str:
    return datetime.combine(day, datetime.min.time(), tzinfo=KST).astimezone(timezone.utc).isoformat()


def _rounded(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["cost_usd"] = round(float(result.get("cost_usd") or 0), 6)
    threads = int(result.get("extract_sent_threads") or 0)
    result["tokens_per_sent_thread"] = round(result["extract_tokens"] / threads, 2) if threads else None
    return result


def _period(conn: sqlite3.Connection, first: date, end: date) -> dict:
    """end 는 제외. SQL 집계만 읽어 큰 기록에서도 본문/제목/detail 을 메모리로 읽지 않는다."""
    bounds = (_midnight(first), _midnight(end))
    where = "WHERE at >= ? AND at < ?"
    calls = _rounded(conn.execute(f"SELECT {_CALL_SUMS} FROM llm_calls {where}", bounds).fetchone())
    runs = dict(conn.execute(f"SELECT {_RUN_SUMS} FROM llm_runs {where}", bounds).fetchone())
    purposes = [_rounded(r) for r in conn.execute(
        f"SELECT purpose, {_CALL_SUMS} FROM llm_calls {where} GROUP BY purpose ORDER BY purpose", bounds)]
    modes = [_rounded(r) for r in conn.execute(
        f"SELECT COALESCE(NULLIF(mode, ''), 'unknown') AS mode, {_CALL_SUMS} FROM llm_calls "
        f"{where} AND purpose = 'extract' GROUP BY COALESCE(NULLIF(mode, ''), 'unknown') ORDER BY mode", bounds)]
    days = {r["day"]: _rounded(r) for r in conn.execute(
        f"SELECT date(at, '+9 hours') AS day, {_CALL_SUMS} FROM llm_calls {where} GROUP BY day", bounds)}
    day_list = []
    for i in range((end - first).days):
        day = (first + timedelta(days=i)).isoformat()
        item = days.get(day) or {k: 0 for k in calls if k != "tokens_per_sent_thread"}
        day_list.append({**item, "day": day, "has_call_record": day in days})
    return {
        "start": first.isoformat(), "end": (end - timedelta(days=1)).isoformat(),
        **calls, **runs, "purposes": purposes, "modes": modes, "daily": day_list,
        "cache_read_share": calls["cache_read_tokens"] / (
            calls["input_tokens"] + calls["cache_read_tokens"] + calls["cache_write_tokens"])
        if calls["input_tokens"] + calls["cache_read_tokens"] + calls["cache_write_tokens"] else None,
    }


def build_report(store: Store | sqlite3.Connection, now: datetime | None = None, days: int = 7) -> dict:
    """최근 완료 days 일과 직전 days 일 비교. 반환값에는 메일 내용/식별자가 없다.

    comparison 의 양수 saved_tokens/reduction_pct 는 감소, 음수는 증가다.
    comparable 이 False 면 headline 은 절감 달성 대신 기록 부족/미집계를 표시한다.
    """
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 366:
        raise ValueError("보고 기간은 1~366일이어야 합니다")
    conn = store if isinstance(store, sqlite3.Connection) else store.conn
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = now.astimezone(KST).date()
    first, prev_first = today - timedelta(days=days), today - timedelta(days=2 * days)
    report = {
        "schema_version": SCHEMA_VERSION, "generated_at": now.astimezone(timezone.utc).isoformat(),
        "as_of": today.isoformat(), "timezone": "Asia/Seoul", "days": days,
        "has_data": False, "available": True, "notes": list(NOTES),
        "estimates": {"avoided_tokens": None, "reason": "대조 호출이 없어 캐시·증분·분석 제외의 절감 토큰을 산정하지 않음"},
    }
    try:
        # 읽기 한 번의 스냅샷 안에서 집계하여 진행 중인 새 호출과 기간 합계를 섞지 않는다.
        conn.execute("SAVEPOINT token_usage_report")
        try:
            report["current"] = _period(conn, first, today)
            report["previous"] = _period(conn, prev_first, first)
            source = dict(conn.execute(
                f"SELECT MIN(at) AS first_at, MAX(at) AS last_at, MAX(id) AS last_call_id, {_CALL_SUMS} "
                "FROM llm_calls WHERE at < ?", (_midnight(today),)).fetchone())
            source["last_run_id"] = conn.execute("SELECT MAX(id) FROM llm_runs WHERE at < ?",
                                                  (_midnight(today),)).fetchone()[0]
            report["source"] = source
        finally:
            conn.execute("RELEASE SAVEPOINT token_usage_report")
    except sqlite3.Error:
        report.update(available=False, headline="사용량 기록을 읽을 수 없어 비교를 보류합니다.",
                      comparison={"comparable": False, "status": "unavailable", "saved_tokens": None,
                                  "reduction_pct": None, "change_tokens": None, "change_pct": None})
        return report

    current, previous = report["current"], report["previous"]
    report["has_data"] = bool(source["calls"] or source["last_run_id"])
    coverage_start = kst_day(source["first_at"])
    early_enough = coverage_start is not None and coverage_start <= prev_first
    measured = not (current["unmetered_calls"] or previous["unmetered_calls"])
    current_activity = bool(current["calls"] or current["runs"])
    comparable = bool(early_enough and previous["tokens"] and measured and current_activity)
    saved = previous["tokens"] - current["tokens"]
    pct = saved / previous["tokens"] if previous["tokens"] else None
    if not report["has_data"]:
        status, headline = "no_data", "완료된 날짜의 사용량 기록이 없습니다. 기록이 쌓이면 절감 여부를 비교합니다."
    elif not current_activity:
        status, headline = "no_current_activity", "현재 비교기간 기록이 없어 절감 여부를 판단할 수 없습니다."
    elif not early_enough or not previous["tokens"]:
        status, headline = "insufficient_history", "직전 비교기간의 기록이 부족해 절감률을 확정할 수 없습니다."
    elif not measured:
        status, headline = "unmetered", "토큰이 미집계된 성공 호출이 있어 절감률을 확정할 수 없습니다."
    elif saved > 0:
        status, headline = "reduced", f"기록된 토큰이 직전 {days}일보다 {saved:,}개 ({pct:.1%}) 감소했습니다."
    elif saved < 0:
        status, headline = "increased", f"기록된 토큰이 직전 {days}일보다 {-saved:,}개 ({-pct:.1%}) 증가했습니다."
    else:
        status, headline = "unchanged", "기록된 토큰 사용량이 직전 기간과 같습니다."
    report["headline"] = headline
    report["comparison"] = {
        "comparable": comparable, "status": status, "saved_tokens": saved,
        "reduction_pct": pct, "change_tokens": -saved, "change_pct": -pct if pct is not None else None,
        "saved_cost_usd": round(previous["cost_usd"] - current["cost_usd"], 6),
        "calls_change": current["calls"] - previous["calls"],
        "previous_period_covered": early_enough,
    }
    return report


def _percent(value: float | None) -> str:
    return f"{value:.1%}" if value is not None else "산정 불가"


def _comparison_rows(report: dict) -> list[tuple[str, str, str]]:
    cur, prev = report["current"], report["previous"]
    fields = (
        ("기록된 토큰 합계", "tokens"), ("입력", "input_tokens"), ("출력", "output_tokens"),
        ("캐시 읽기", "cache_read_tokens"), ("캐시 쓰기", "cache_write_tokens"),
        ("호출 수", "calls"), ("실패 호출", "failed_calls"), ("미집계 성공 호출", "unmetered_calls"),
        ("전송 대화 횟수 (재시도 포함)", "sent_threads"), ("캐시 재사용 대화 횟수", "cached_threads"),
        ("분석 제외 대화 횟수", "skipped_threads"), ("새로 정리된 대화 횟수", "extracted_threads"),
    )
    rows = [(label, f"{prev[key]:,}", f"{cur[key]:,}") for label, key in fields]
    rows.append(("업무 정리 전송 대화 1회당 토큰 (업무량 참고)",
                 f"{prev['tokens_per_sent_thread']:,.2f}" if prev['tokens_per_sent_thread'] is not None else "—",
                 f"{cur['tokens_per_sent_thread']:,.2f}" if cur['tokens_per_sent_thread'] is not None else "—"))
    rows.append(("입력 중 캐시 읽기 비율", _percent(prev["cache_read_share"]), _percent(cur["cache_read_share"])))
    rows.append(("API 환산 금액 (USD)", f"${prev['cost_usd']:.6f}", f"${cur['cost_usd']:.6f}"))
    return rows


def render_report_html(report: dict) -> str:
    """usage.html 의 보고서 탭에서 쓸 fragment. 기존 usage-page CSS 클래스 사용."""
    intro = f'<h4 class="ih">토큰 절감 보고서</h4><p class="inote">{escape(report["headline"])}</p>'
    if not report["available"]:
        return intro
    cur, prev, comparison = report["current"], report["previous"], report["comparison"]
    trs = "".join(f'<tr><td>{escape(label)}</td><td class="num">{before}</td><td class="num">{after}</td></tr>'
                  for label, before, after in _comparison_rows(report))
    measured = (f'기록된 사용량 변화 {comparison["change_tokens"]:+,} 토큰 · '
                f'{comparison["change_pct"]:+.1%}' if comparison["comparable"]
                else "비교 보류 · 기록을 더 확인해야 합니다")
    mode_labels = {"full": "대화 전체", "delta": "증분", "refresh": "갱신", "mixed": "혼합", "unknown": "이전 기록 (방식 없음)"}
    mode_rows = "".join(
        f'<tr><td>{escape(mode_labels.get(r["mode"], "기타 방식"))}</td><td class="num">{r["calls"]:,}</td>'
        f'<td class="num">{r["tokens"]:,}</td><td class="num">{r["sent_threads"]:,}</td></tr>' for r in cur["modes"])
    modes = ('<div class="tbl-wrap"><table class="tbl"><thead><tr><th>이번 기간 업무 정리 방식</th>'
             '<th class="num">호출</th><th class="num">실측 토큰</th><th class="num">전송 대화 횟수</th></tr></thead>'
             f'<tbody>{mode_rows}</tbody></table></div>' if mode_rows else '<p class="inote">이번 기간 업무 정리 호출 기록 없음</p>')
    notes = "".join(f'<li>{escape(note)}</li>' for note in report["notes"])
    return (intro + f'<p class="inote">보고 기준 {report["as_of"]} · {escape(measured)} · 오늘 제외</p>'
            '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>실측 기록</th>'
            f'<th class="num">직전 {prev["start"]} ~ {prev["end"]}</th>'
            f'<th class="num">최근 {cur["start"]} ~ {cur["end"]}</th></tr></thead><tbody>{trs}</tbody></table></div>'
            + '<h4 class="ih">절감 방식의 실행 기록</h4>' + modes
            + f'<ul class="inote">{notes}</ul>'
            + '<p class="inote">대시보드 생성 시 reports/token-usage 에 날짜별 JSON·Markdown 기록을 보존합니다. '
              '같은 날짜에도 집계 내용이 바뀌면 별도 기록으로 남깁니다.</p>')


def render_markdown(report: dict) -> str:
    lines = ["# 토큰 절감 보고서", "", f"보고 기준: {report['as_of']} (한국 날짜, 오늘 제외)",
             f"생성 시각: {report['generated_at']}", "", report["headline"], ""]
    if not report["available"]:
        return "\n".join(lines)
    cur, prev, comp = report["current"], report["previous"], report["comparison"]
    lines.extend([f"직전: {prev['start']} ~ {prev['end']} / 최근: {cur['start']} ~ {cur['end']}", ""])
    if comp["comparable"]:
        lines.extend([f"기록된 토큰 변화량: {comp['change_tokens']:+,} / 변화율: {comp['change_pct']:+.1%}",
                      f"직전 대비 감소량: {comp['saved_tokens']:+,} / 감소율: {_percent(comp['reduction_pct'])} "
                      "(감소량·감소율이 음수이면 증가)", ""])
    else:
        lines.extend(["절감 판정 보류: 아래 수치는 현재 남아 있는 기록의 합계입니다.", ""])
    lines.extend(["| 항목 | 직전 | 최근 |", "|---|---:|---:|"])
    lines.extend(f"| {label} | {before} | {after} |" for label, before, after in _comparison_rows(report))
    lines.extend(["", "## 용도별 실측 기록", "", "| 기간 | 용도 | 호출 | 토큰 |", "|---|---|---:|---:|"])
    for label, period in (("직전", prev), ("최근", cur)):
        lines.extend(f"| {label} | {PURPOSES.get(r['purpose'], '기타')} | {r['calls']:,} | {r['tokens']:,} |"
                     for r in period["purposes"])
    lines.extend(["", "## 업무 정리 방식별 실측 기록", "", "| 기간 | 방식 | 호출 | 토큰 | 전송 대화 횟수 |",
                  "|---|---|---:|---:|---:|"])
    for label, period in (("직전", prev), ("최근", cur)):
        lines.extend(f"| {label} | {r['mode'] if r['mode'] in ('full', 'delta', 'refresh', 'mixed') else '방식 미상'} "
                     f"| {r['calls']:,} | {r['tokens']:,} | {r['sent_threads']:,} |" for r in period["modes"])
    lines.extend(["", "## 일별 실측 기록", "", "| 날짜 | 토큰 | 호출 | 실패 | 호출 기록 |", "|---|---:|---:|---:|---|"])
    for period in (prev, cur):
        lines.extend(f"| {r['day']} | {r['tokens']:,} | {r['calls']:,} | {r['failed_calls']:,} "
                     f"| {'있음' if r['has_call_record'] else '없음 (미사용 확정 아님)'} |" for r in period["daily"])
    lines.extend(["", "## 해석과 한계", ""])
    lines.extend(f"- {note}" for note in report["notes"])
    lines.extend(["", "기존 llm_calls·llm_runs 기록은 변경하거나 삭제하지 않습니다. "
                  "보고서에는 메일 본문·제목·주소·대화 식별자를 담지 않습니다.", ""])
    return "\n".join(lines)


def _write_once(path: Path, text: str) -> None:
    """완성된 파일만 공개하며 이미 존재하는 기록은 덮어쓰지 않는다."""
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".report-", suffix=".tmp", delete=False) as f:
        temporary = Path(f.name)
        f.write(text)
    try:
        # Windows rename 은 기존 대상이 있으면 실패하고, 완성 파일을 한 번에 공개한다.
        # POSIX rename 은 덮어쓸 수 있으므로 같은 폴더의 완성 파일을 hard link 한다.
        if os.name == "nt":
            temporary.rename(path)
        else:
            os.link(temporary, path)
    except FileExistsError:
        pass
    finally:
        temporary.unlink(missing_ok=True)


def save_report(report: dict, directory: Path | str = REPORT_DIR) -> Path:
    """날짜·기간·내용 해시로 누적 저장. 동일 집계는 중복 생성을 피하고 이전 보고서는 보존한다.

    생성 시각만 달라진 동일 보고서는 기존 파일을 반환한다. JSON과 Markdown을 함께 저장한다.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stable = {key: value for key, value in report.items() if key != "generated_at"}
    digest = hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    stem = f"{report['as_of']}-{report['days']}d-{digest}"
    json_path, markdown_path = directory / f"{stem}.json", directory / f"{stem}.md"
    _write_once(json_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    report = json.loads(json_path.read_text(encoding="utf-8"))
    _write_once(markdown_path, render_markdown(report))
    return markdown_path
