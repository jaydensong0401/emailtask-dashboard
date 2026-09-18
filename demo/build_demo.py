"""데모 데이터로 대시보드 만들기 - 실제 메일함 · Claude 호출 없이 화면을 그대로 재현한다.

  python demo/build_demo.py     docs/index.html (서버 없이 여는 데모 - GitHub Pages. 버튼 기록은 방문자 브라우저에)
  python demo/serve_demo.py     http://127.0.0.1:8790 (데모 서버 - 기록을 데모 DB 에)

모든 회사 · 사람 · 메일은 가상이다. 도메인은 예시 전용(.example)이라 실제로 연결되지 않는다.
업무 정리(LLM) 결과는 미리 써 넣은 값을 쓰고, 날짜는 2026-09-18(금) 11:00 기준으로 고정한다.
화면은 실제 코드(build_model -> render_html)로 그리므로 운영 화면과 모양이 같다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nwmail.client import Mail                                      # noqa: E402
from nwmail.dashboard import build_model, esc, render_html, task_id  # noqa: E402
from nwmail.feedback import MARKS, FeedbackStore                    # noqa: E402
from nwmail.filters import add_filter, add_protected, reapply       # noqa: E402
from nwmail.server import _due_json, _mark_json                     # noqa: E402
from nwmail.store import Store                                      # noqa: E402

HERE = Path(__file__).resolve().parent
DEMO_MAIL = HERE / "demo_mail.db"
DEMO_FB = HERE / "demo_feedback.db"
PAGES = ROOT / "docs" / "index.html"  # 서버 없이 여는 데모 (GitHub Pages · 파일로 열어도 됨). 저장소에 올린다
SERVED = HERE / "served.html"         # 데모 서버가 쓰는 페이지 (실행할 때마다 토큰이 바뀌어 올리지 않는다)
RESET_LINK = ' 버튼 기록은 이 브라우저에만 저장돼요. <a href="#" id="demo-reset">처음 상태로</a>'
PORT = 8790

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 18, 11, 0, tzinfo=KST)
ME = "gdhong@lumi.example"
ME_NAME = "홍길동 선임"
TEAM = "team@lumi.example"
NOTICE = "데모 화면입니다 — 회사 · 사람 · 메일은 모두 가상이며 실제 메일은 들어 있지 않습니다."
OPEN_PROJECT = "오로라 스토어"        # 처음부터 펼쳐 둘 프로젝트 카드 (업무 묶음 예시)

# 이름 -> (보낸사람 표시, 주소)
PEOPLE = {
    "me": (ME_NAME, ME),
    "이지우": ("이지우 매니저", "jiwoo.lee@aurora.example"),
    "서다은": ("서다은 책임", "daeun.seo@aurora.example"),
    "윤서진": ("윤서진 매니저", "seojin@nurimktg.co.example"),
    "강민재": ("강민재 대리", "minjae@nurimktg.co.example"),
    "오세린": ("오세린 매니저", "serin@nurimktg.co.example"),
    "차도현": ("차도현 AE", "dohyun@bluead.example"),
    "문태오": ("문태오 매니저", "taeo@voltlink.example"),
    "배수아": ("배수아 과장", "sua@haneul-tour.example"),
    "정다온": ("정다온 PM", "daon@lumi.example"),
    "김하늘": ("김하늘 책임", "haneul@lumi.example"),
    "박도윤": ("박도윤 디자이너", "doyun@lumi.example"),
}


def at(month: int, day: int, hm: str) -> str:
    h, m = map(int, hm.split(":"))
    return datetime(2026, month, day, h, m, tzinfo=KST).isoformat()


def addr(who: str) -> str:
    return PEOPLE[who][1] if who in PEOPLE else who


def task(text, role, assignee="", org="", deadline="", fix=""):
    return {"text": text, "role": role, "fix_type": fix, "assignee": assignee,
            "assignee_org": org, "deadline": deadline}


def act(text, deadline="", urgency="medium"):
    return {"text": text, "deadline": deadline, "urgency": urgency}


def dec(text, by, date):
    return {"text": text, "by": by, "date": date}


# 대화: 제목 · 메일 (보낸사람, 시각, 받는사람, 참조, 본문) · 업무 정리 결과 (None 이면 정리 대기)
THREADS = [
    dict(key="store-gift", subject="[오로라 스토어] 추석 기프트 기획전 페이지 검토 요청", mails=[
        ("이지우", at(9, 14, "10:20"), ["me"], ["김하늘"],
         "추석 기프트 기획전 페이지 1차 시안 공유드립니다. 9월 19일까지 검토 의견 부탁드립니다.\n\n"
         "감사합니다.\n이지우 드림\n오로라 고객경험팀\nMobile +82 10 0000 0000"),
        ("me", at(9, 15, "09:10"), ["이지우"], ["김하늘"],
         "확인했습니다. 오픈일은 9월 22일로 맞추겠습니다. 메인 배너는 두 가지 안으로 준비하겠습니다."),
        ("이지우", at(9, 17, "15:30"), ["me"], ["박도윤"],
         "메인 배너는 B안으로 진행해 주세요. 상품 설명 문구 수정본과 쿠폰 코드는 내일 전달드리겠습니다."),
     ], x=dict(project="오로라 스토어", status="진행중",
               summary="추석 기획전 페이지 시안 검토 중. 메인 배너 B안 확정, 9/22 오픈 예정.",
               decisions=[dec("기획전 오픈일 9월 22일 확정", "이지우", "2026-09-15"),
                          dec("메인 배너는 B안으로 결정", "이지우", "2026-09-17")],
               tasks=[task("기획전 상세 페이지 개발", "개발", "김하늘", "루미", "2026-09-21"),
                      task("메인 배너 B안 최종 시안 제작", "디자인", "박도윤", "루미", "2026-09-19"),
                      task("상품 설명 문구 수정 반영", "수정", "박도윤", "루미", "2026-09-18", "디자인"),
                      task("기획전 쿠폰 코드 전달", "기타", "이지우", "오로라", "2026-09-19")],
               my_actions=[act("기획전 페이지 최종 검수 후 오픈 승인 요청", "2026-09-21", "high")])),
    dict(key="store-delay", subject="[루미] 스토어몰 배송 지연 고객 안내 문자 발송 건", mails=[
        ("윤서진", at(9, 16, "14:05"), ["me"], [],
         "택배사 물량 증가로 일부 주문 배송이 2~3일 늦어질 예정입니다. 안내 문자 문구 확인 부탁드립니다."),
     ], x=dict(project="오로라 스토어", status="진행중",
               summary="배송 지연 주문 고객에게 안내 문자 발송 예정. 문구 확정 필요.",
               decisions=[],
               tasks=[task("안내 문자 발송 대상 주문 추출", "기타", "윤서진", "누리", "2026-09-18"),
                      task("주문 상세 화면 배송 안내 문구 수정", "수정", "김하늘", "루미", "2026-09-19", "개발")],
               my_actions=[act("배송 지연 안내 문구 확정 회신", "2026-09-17", "high")])),
    dict(key="store-shoot", subject="오로라 스토어 10월 입점 브랜드 굿즈 촬영 일정", mails=[
        ("서다은", at(9, 12, "11:00"), ["박도윤"], ["me"],
         "10월 입점 브랜드 굿즈 촬영은 9월 26일 오후로 잡았습니다. 컷 리스트는 24일까지 공유드릴게요."),
     ], x=dict(project="오로라 스토어", status="진행중",
               summary="10월 입점 굿즈 촬영 일정 확정, 컷 리스트 공유 대기.",
               decisions=[],
               tasks=[task("굿즈 촬영 컷 리스트 공유", "기타", "서다은", "오로라", "2026-09-24"),
                      task("굿즈 상세 이미지 제작", "디자인", "박도윤", "루미", "2026-10-02")],
               my_actions=[act("촬영 컷 리스트 확인", "", "low")])),
    dict(key="tennis-open", subject="[누리마케팅] AX9 프라이빗 테니스클럽 레슨 신청 페이지 오픈 요청의 건", mails=[
        ("강민재", at(9, 15, "16:40"), ["me"], [],
         "AX9 오너 대상 테니스 레슨 신청 페이지 오픈을 요청드립니다. 신청 기간은 9/24~10/5입니다."),
        ("me", at(9, 16, "10:15"), ["강민재"], [],
         "요청 확인했습니다. 회차별 정원 설정값을 알려주시면 23일까지 반영하겠습니다."),
        ("강민재", at(9, 18, "09:40"), ["me"], [],
         "회차별 정원은 첨부 표와 같습니다. 신청 완료 알림톡 문구도 조금 바꾸려고 합니다."),
     ], x=dict(project="AX9 프라이빗 테니스클럽", status="진행중",
               summary="AX9 오너 테니스 레슨 신청 페이지 오픈 준비. 신청 기간 9/24~10/5.",
               decisions=[dec("레슨 신청 기간은 9/24~10/5로 확정", "강민재", "2026-09-16")],
               tasks=[task("레슨 신청 페이지 개발", "개발", "김하늘", "루미", "2026-09-23"),
                      task("신청 완료 알림톡 문구 변경", "수정", "강민재", "누리", "2026-09-19", "데이터")],
               my_actions=[act("레슨 회차별 정원 설정값 확인 회신", "2026-09-18", "medium")])),
    dict(key="tennis-list", subject="RE: AX9 프라이빗 테니스클럽 참가자 명단 공유_20260917", mails=[
        ("이지우", at(9, 17, "10:10"), ["오세린"], ["me"],
         "1차 참가자 명단 공유드립니다. CRM 등록은 22일까지 부탁드립니다."),
     ], x=dict(project="AX9 프라이빗 테니스클럽", status="진행중",
               summary="1차 참가자 명단 공유, CRM 등록 요청.",
               decisions=[],
               tasks=[task("참가자 명단 CRM 등록", "수정", "오세린", "누리", "2026-09-22", "데이터")],
               my_actions=[])),
    dict(key="plcc-banner", subject="오로라 한결카드 PLCC 3.0 발급 이벤트 배너 시안 전달드립니다", mails=[
        ("차도현", at(9, 11, "15:20"), ["me"], ["이지우"],
         "PLCC 3.0 발급 이벤트 배너 1차 시안 전달드립니다. 메인 카피 후보 3개를 함께 넣었습니다."),
        ("me", at(9, 12, "10:30"), ["차도현"], ["이지우"],
         "메인 카피는 '매일 쌓이는 혜택'으로 가겠습니다. 버튼 영역만 조금 키워 주세요."),
        ("차도현", at(9, 15, "17:10"), ["me"], ["이지우"],
         "2차 시안 준비 중입니다. 발급 버튼 연동 방식 확인 부탁드립니다."),
     ], x=dict(project="오로라 한결카드 PLCC", status="진행중",
               summary="PLCC 3.0 발급 이벤트 배너 2차 시안 준비 중. 메인 카피 확정.",
               decisions=[dec("배너 메인 카피는 '매일 쌓이는 혜택'으로 결정", "이지우", "2026-09-12")],
               tasks=[task("이벤트 배너 2차 시안 제작", "디자인", "차도현", "블루애드", "2026-09-23"),
                      task("이벤트 페이지 카드 발급 버튼 연동", "개발", "김하늘", "루미", "2026-09-25")],
               my_actions=[act("배너 시안 피드백 정리해 회신", "2026-09-19", "medium")])),
    dict(key="classic-vip", subject="[문의] 오로라 클래식 VIP 티켓 발송 일정", mails=[
        ("서다은", at(9, 11, "09:30"), ["me"], [],
         "오로라 클래식 VIP 티켓 발송 일정 확인 부탁드립니다."),
        ("me", at(9, 11, "13:00"), ["서다은"], [],
         "9월 12일 오전에 일괄 발송하겠습니다."),
        ("서다은", at(9, 12, "16:00"), ["정다온"], ["me"],
         "발송 확인했습니다. 감사합니다."),
     ], x=dict(project="오로라 클래식", status="완료",
               summary="VIP 티켓 9/12 일괄 발송 완료.",
               decisions=[dec("VIP 티켓은 9월 12일 일괄 발송", "서다은", "2026-09-11")],
               tasks=[], my_actions=[])),
    dict(key="gallery-copy", subject="오로라 갤러리 가을 전시 도슨트 예약 화면 문구 변경", mails=[
        ("이지우", at(9, 18, "10:20"), ["me"], [],
         "가을 전시 도슨트 예약 화면의 안내 문구를 첨부 내용으로 바꿔 주실 수 있을까요?"),
     ], x=None),                                  # 방금 온 메일 -> 다음 정리 때 반영 (정리 대기)
    dict(key="forum-invite", subject="[요청] 오로라 포럼 - 가을 음악회 초청 안내 발송 (10/25)", mails=[
        ("오세린", at(9, 16, "11:20"), ["me"], [],
         "10월 25일 가을 음악회 초청 안내를 준비하고 있습니다. 초청 대상 기준 확인 부탁드립니다."),
     ], x=dict(project="오로라 포럼", status="진행중",
               summary="10/25 가을 음악회 초청 안내 준비. 초청 대상은 최근 1년 구매 고객.",
               decisions=[dec("초청 대상은 최근 1년 구매 고객으로 한정", "오세린", "2026-09-16")],
               tasks=[task("초청 신청 페이지 개설", "개발", "김하늘", "루미", "2026-09-30"),
                      task("초청 안내 알림톡 발송", "기타", "오세린", "누리", "2026-10-10")],
               my_actions=[act("초청 대상 고객 등급 기준 확인", "2026-09-22", "medium")])),
    dict(key="sig-1", subject="[NURI] 시그니처 멤버십 신청 기한 연장 요청의 건_강○희 고객", mails=[
        ("윤서진", at(9, 15, "13:30"), ["김하늘"], ["me"],
         "시그니처 멤버십 신청 기한 연장 요청입니다. CRM 기한 변경 부탁드립니다."),
     ], x=dict(project="오로라 운영(기타)", status="진행중",
               summary="고객 요청으로 멤버십 신청 기한 연장 처리 필요.",
               decisions=[],
               tasks=[task("신청 기한 연장 처리", "수정", "김하늘", "루미", "2026-09-18", "데이터")],
               my_actions=[])),
    dict(key="sig-2", subject="[NURI] 시그니처 멤버십 신청 기한 연장 요청의 건_문○준 고객", mails=[
        ("윤서진", at(9, 17, "11:10"), ["김하늘"], ["me"],
         "같은 건으로 한 분 더 연장 요청이 있어 전달드립니다."),
     ], x=dict(project="오로라 운영(기타)", status="진행중",
               summary="추가 고객의 신청 기한 연장 요청.",
               decisions=[],
               tasks=[task("연장 처리 결과 고객 안내", "기타", "윤서진", "누리", "2026-09-19")],
               my_actions=[])),
    dict(key="ops-open", subject="신규 매장 오픈 고객 초청 인원 확인", mails=[
        ("이지우", at(9, 14, "09:00"), ["me"], [],
         "신규 매장 오픈 행사 초청 인원 집계 부탁드립니다."),
        ("me", at(9, 16, "17:30"), ["이지우"], [],
         "초청 인원 최종 집계 공유드립니다. 총 120명입니다."),
     ], x=dict(project="오로라 운영(기타)", status="완료",
               summary="오픈 행사 초청 인원 120명으로 집계 완료.",
               decisions=[dec("오픈 행사 초청 인원 120명 확정", "이지우", "2026-09-16")],
               tasks=[],
               my_actions=[act("초청 인원 최종 집계 공유", "2026-09-16", "high")])),
    dict(key="ops-race", subject="썬더 레이싱 시네마 워치파티 이벤트 페이지 수정 요청", mails=[
        ("강민재", at(9, 17, "17:40"), ["me"], [],
         "워치파티 일정이 바뀌어 이벤트 페이지 일정 영역 수정이 필요합니다."),
     ], x=dict(project="오로라 운영(기타)", status="진행중",
               summary="워치파티 일정 변경으로 이벤트 페이지 수정 필요.",
               decisions=[],
               tasks=[task("워치파티 일정 영역 수정", "수정", "김하늘", "루미", "2026-09-21", "개발")],
               my_actions=[act("수정 범위 확인 후 일정 회신", "2026-09-19", "high")])),
    dict(key="volt-api", subject="[KX볼트링크] 충전 요금 안내 페이지 API 연동 확인", mails=[
        ("문태오", at(8, 20, "10:00"), ["me"], [],
         "충전 요금 안내 페이지에 요금 조회 API 연동이 필요합니다."),
        ("문태오", at(9, 16, "15:00"), ["김하늘"], ["me"],
         "요금 조회는 기존 API v2를 그대로 쓰기로 했습니다. 응답 형식 확인 부탁드립니다."),
     ], x=dict(project="KX볼트링크", status="진행중",
               summary="요금 조회 API v2 연동 확정, 응답 형식 확인 단계.",
               decisions=[dec("요금 조회는 기존 API v2를 그대로 사용", "문태오", "2026-09-16")],
               tasks=[task("요금 조회 API 응답 형식 확인", "개발", "김하늘", "루미", "2026-09-24"),
                      task("요금표 이미지 교체", "수정", "박도윤", "루미", "", "디자인")],
               my_actions=[act("API 테스트 계정 요청", "", "low")])),
    dict(key="haneul-deal", subject="[하늘투어] 제휴 여행상품 판매 입점 관련 논의의 건", mails=[
        ("배수아", at(9, 15, "10:00"), ["me"], [],
         "제휴 여행상품 입점 관련해 상품 구성과 수수료 조건을 제안드립니다."),
        ("me", at(9, 15, "15:20"), ["배수아"], [],
         "제안 감사합니다. 내부 검토 후 24일까지 회신드리겠습니다."),
     ], x=dict(project="하늘투어", status="진행중",
               summary="제휴 여행상품 입점 제안 검토 중. 국내 패키지 3종으로 시작.",
               decisions=[dec("입점 상품은 국내 여행 패키지 3종으로 시작", "배수아", "2026-09-15")],
               tasks=[task("입점 상품 목록 전달", "기타", "배수아", "하늘투어", "2026-09-25")],
               my_actions=[act("입점 수수료 조건 내부 검토", "2026-09-24", "medium")])),
    dict(key="workshop", subject="['26 전사 워크숍] 참석자 명단 회신 요청 (~9/19(금) 18:00)", mails=[
        ("정다온", at(9, 16, "09:00"), [TEAM], [],
         "전사 워크숍 참석 여부를 19일 18시까지 회신 부탁드립니다."),
     ], x=dict(project="", status="진행중",
               summary="전사 워크숍 참석 여부 회신 요청.",
               decisions=[], tasks=[],
               my_actions=[act("워크숍 참석 여부 회신", "2026-09-19", "low")])),
    dict(key="job-1", subject="[잡스테이션] 채용 트렌드 리포트가 도착했습니다", mails=[
        ("marketing@jobstation.example", at(9, 13, "08:00"), ["members@jobstation.example"], [],
         "9월 채용 트렌드 리포트를 확인해 보세요."),
     ], x=dict(project="", status="완료", summary="채용 트렌드 뉴스레터.", decisions=[],
               tasks=[], my_actions=[])),
    dict(key="job-2", subject="[잡스테이션] HR 인사이트 리포트 9월호", mails=[
        ("marketing@jobstation.example", at(9, 16, "08:00"), ["members@jobstation.example"], [],
         "HR 인사이트 리포트 9월호가 나왔습니다."),
     ], x=dict(project="", status="완료", summary="HR 뉴스레터.", decisions=[],
               tasks=[], my_actions=[])),
    dict(key="weekly", subject="주간 회의록 공유", mails=[
        ("정다온", at(9, 14, "18:00"), [TEAM], [], "이번 주 회의록 공유드립니다."),
     ], x=dict(project="", status="완료", summary="주간 회의록 공유.", decisions=[],
               tasks=[], my_actions=[])),
    dict(key="daily", subject="9월 17일 기준 일일 통계 데이터 공유", mails=[
        ("report@lumi.example", at(9, 18, "08:50"), [TEAM], [], "어제 기준 스토어몰 일일 통계입니다."),
     ], x=None),                                  # 정기 보고 -> 대시보드에만 (정리 제외)
    dict(key="travel-1", subject="여행 매거진 뉴스레터 9월호", mails=[
        ("news@travelmag.example", at(9, 12, "07:30"), ["subscriber@travelmag.example"], [],
         "가을 여행지 추천"),
     ], x=None),
    dict(key="travel-2", subject="가을 여행 특집 뉴스레터", mails=[
        ("news@travelmag.example", at(9, 17, "07:30"), ["subscriber@travelmag.example"], [],
         "단풍 명소 모음"),
     ], x=None),
    # 최근 7일 창 밖 대화 - 기한 있는 남은 할 일은 '지난 대화' 로 오늘 · 일정 탭에 남는다
    dict(key="settle", subject="[루미] 8월 정산 자료 요청", mails=[
        ("정다온", at(8, 27, "10:00"), ["me"], [], "8월 스토어몰 정산 자료 준비 부탁드립니다."),
        ("me", at(8, 28, "09:00"), ["정다온"], [], "다음 주 중 정리해 드리겠습니다."),
     ], x=dict(project="오로라 스토어", status="진행중",
               summary="8월 정산 자료 준비 요청.",
               decisions=[],
               tasks=[task("정산표 검수", "기타", "김하늘", "루미", "2026-09-25")],
               my_actions=[act("8월 정산 자료 회신", "2026-09-17", "high")])),
]

# 차단 메일 (규칙에 걸리는 것) - (보낸 주소, 시각, 제목, 수신거부 헤더)
BLOCKED = [
    ("ad@shopmall.example", at(9, 16, "12:00"), "(광고) 가을 신상품 최대 50% 할인", True),
    ("event@giftmall.example", at(9, 17, "09:00"), "(광고) 추석 선물세트 사전 예약 안내", True),
    ("news@cadnova.example", at(9, 15, "06:00"), "CadNova 플러그인 뉴스 - 9월", True),
    ("zine@cadnova.example", at(9, 17, "06:00"), "CadNova3Dzine - September", True),
    ("seminar@eduhub.example", at(9, 16, "10:00"), "커머스 데이터 분석 실무 세미나 참석 안내", False),
    ("support+notifications@designhub.example", at(9, 17, "14:00"), "스토어 배너 파일에 새 댓글이 달렸습니다", False),
    ("support+notifications@designhub.example", at(9, 18, "10:00"), "편집 권한 요청이 도착했습니다", False),
]
RULES = [   # (종류, 문구, 메모, 구분, 켜짐)
    ("subject", "(광고)", "법정 광고 표기", "광고·홍보", True),
    ("domain", "cadnova.example", "", "뉴스레터·구독", True),
    ("subject", "세미나", "", "행사·안내", True),
    ("sender", "support+notifications@designhub.example", "디자인 툴 알림", "자동 알림", True),
    ("sender", "promo@oldshop.example", "예전 쇼핑몰", "기타", False),
]


def seed() -> None:
    """데모 DB 두 개를 처음부터 다시 만든다."""
    for p in (DEMO_MAIL, DEMO_FB):
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(str(p) + suffix).unlink(missing_ok=True)
    uid = 0
    roots: dict[str, str] = {}
    with Store(DEMO_MAIL) as st:
        inbox, sent = [], []
        for th in THREADS:
            ids: list[str] = []
            for i, (who, when, to, cc, body) in enumerate(th["mails"]):
                uid += 1
                mid = f"<{th['key']}.{i}@demo.example>"
                name, email = PEOPLE.get(who, ("", who))
                m = Mail(mail_id=str(uid), subject=("RE: " if i else "") + th["subject"],
                         from_name=name, from_email=email, received_time=when, status="Read",
                         body=body, to=[addr(t) for t in to], cc=[addr(c) for c in cc],
                         message_id=mid, references=list(ids), in_reply_to=ids[-1] if ids else "",
                         folder="Sent" if who == "me" else "INBOX")
                (sent if who == "me" else inbox).append(m)
                ids.append(mid)
            roots[th["key"]] = ids[0]
        for sender, when, subject, unsub in BLOCKED:
            uid += 1
            inbox.append(Mail(mail_id=str(uid), subject=subject, from_name="", from_email=sender,
                              received_time=when, status="Read", body="(데모) 안내 메일",
                              to=[ME], message_id=f"<b{uid}@demo.example>",
                              list_unsubscribe=unsub, folder="INBOX"))
        inbox.sort(key=lambda m: m.received_time)
        sent.sort(key=lambda m: m.received_time)
        st.save_mails(inbox, me=ME)
        st.save_mails(sent, me=ME, folder="Sent")
        for kind, pattern, note, category, enabled in RULES:
            add_filter(st, kind, pattern, note, category, enabled=enabled)
        add_protected(st, "bluead.example", "거래처")
        reapply(st)
        for when in (at(9, 17, "16:50"), at(9, 18, "10:50")):
            st.start_run(3, "sync", started_at=datetime.fromisoformat(when))
        for when in (at(9, 17, "17:00"), at(9, 18, "09:00")):
            st.start_run(5, "extract", started_at=datetime.fromisoformat(when))
        key = {k: st.conn.execute("SELECT thread_key FROM mails WHERE message_id = ?",
                                  (mid,)).fetchone()["thread_key"] for k, mid in roots.items()}
        extracted_at = datetime(2026, 9, 18, 9, 5, tzinfo=KST).astimezone(timezone.utc).isoformat()
        for th in THREADS:
            if th["x"]:
                st.conn.execute(
                    "INSERT INTO extractions (thread_key, project, payload, source_hash, backend,"
                    " extracted_at) VALUES (?,?,?,?,?,?)",
                    (key[th["key"]], th["x"]["project"], json.dumps(th["x"], ensure_ascii=False),
                     "demo", "demo", extracted_at))
        st.conn.commit()

        def last_in(k: str) -> str:
            return st.conn.execute("SELECT MAX(sent_at) FROM mails WHERE thread_key = ? AND is_mine = 0",
                                   (key[k],)).fetchone()[0]

        subject = {th["key"]: th["subject"] for th in THREADS}
        marks = [("thread", key["store-delay"], "answered", last_in("store-delay"), subject["store-delay"]),
                 ("project", "오로라 클래식", "done", last_in("classic-vip"), ""),
                 ("project", "잡스테이션", "deleted", "", "")]
        labeled = [   # (대화, 종류, 문구, 표시, 사유, 스냅샷 추가)
            ("ops-open", "me", "초청 인원 최종 집계 공유", "done", "",
             {"deadline": "2026-09-16", "urgency": "high", "project": "오로라 운영(기타)"}),
            ("store-gift", "task", "상품 설명 문구 수정 반영", "done", "",
             {"role": "수정", "assignee": "박도윤", "project": "오로라 스토어"}),
            ("forum-invite", "task", "초청 안내 알림톡 발송", "excluded", "이미 다른 사람이 처리",
             {"role": "기타", "assignee": "오세린", "project": "오로라 포럼"}),
            ("volt-api", "me", "API 테스트 계정 요청", "deleted", "이미 끝난 일",
             {"urgency": "low", "project": "KX볼트링크"}),
        ]
        with FeedbackStore(DEMO_FB) as fb:
            when = NOW - timedelta(minutes=50)
            for target, k, label, ref_at, title in marks:
                fb.mark(target, k, label, ref_at or "", title, now=when)
            for i, (k, kind, text, label, reason, extra) in enumerate(labeled):
                snap = {"text": text, "kind": kind, "thread_key": key[k], "subject": subject[k], **extra}
                fb.apply(task_id(key[k], kind, text), label, reason, snapshot=snap,
                         now=when + timedelta(minutes=10 * i))
            fb.set_due(task_id(key["haneul-deal"], "me", "입점 수수료 조건 내부 검토"), "2026-09-23",
                       {"text": "입점 수수료 조건 내부 검토", "kind": "me", "deadline": "2026-09-24",
                        "project": "하늘투어", "subject": subject["haneul-deal"]}, now=when)
            fb.token()


def feedback_seed(fb: FeedbackStore) -> dict:
    """서버의 GET /api/feedback 과 같은 모양의 기록 (서버 없는 페이지의 처음 상태)."""
    states = {k: {"label": v["label"], "reason": v["reason"], "labeled_at": v["labeled_at"]}
              for k, v in fb.states().items()}
    return {"states": states, "dues": {k: _due_json(v) for k, v in fb.dues().items()},
            **{f"{t}s": {k: _mark_json(v) for k, v in fb.marks(t).items()} for t in MARKS}}


def render(server: dict | None) -> str:
    """server 가 있으면 그 서버로 기록하는 페이지, 없으면 서버 없이 여는 페이지 (demo_api.js 가 대신)."""
    with Store(DEMO_MAIL) as st, FeedbackStore(DEMO_FB) as fb:
        model = build_model(st, me=ME, now=NOW, window_days=7, feedback=fb)
        seed_state = feedback_seed(fb)
        if server is not None:
            server = dict(server, token=fb.token())
    standalone = server is None
    html = render_html(model, notice=NOTICE, server=server or {"same_origin": True})
    # 데모 안내는 경고가 아니라 알림으로
    html = html.replace('<div class="banner warn" role="alert">', '<div class="banner" role="note">', 1)
    html = re.sub(r'(<details class="proj"[^>]*data-project="' + re.escape(OPEN_PROJECT) + r'"[^>]*)>',
                  r"\1 open>", html, count=1)
    if standalone:
        html = html.replace(esc(NOTICE) + "</div>", esc(NOTICE) + RESET_LINK + "</div>", 1)
        seed = json.dumps(seed_state, ensure_ascii=False).replace("<", "\\u003c")
        version = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
        api = (HERE / "demo_api.js").read_text(encoding="utf-8")
        html = html.replace(
            '<script type="application/json" id="nw-conf">',
            f'<script type="application/json" id="demo-seed" data-version="{version}">{seed}</script>'
            f'<script>{api}</script><script type="application/json" id="nw-conf">', 1)
    return html


def write(path: Path, html: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    p = argparse.ArgumentParser(description="데모 데이터로 대시보드 만들기")
    p.add_argument("--keep", action="store_true", help="데모 DB 를 다시 만들지 않고 화면만 새로")
    args = p.parse_args()
    if not args.keep or not DEMO_MAIL.exists():
        seed()
    PAGES.parent.mkdir(exist_ok=True)
    write(PAGES, render(None))
    (PAGES.parent / ".nojekyll").touch()           # GitHub Pages 가 파일을 그대로 내보내도록
    with Store(DEMO_MAIL) as st:
        s = st.stats(None)
    print(f"[OK] {PAGES.relative_to(ROOT)}  (메일 {s.get('total')}통, 서버 없이 열리는 데모)")
    print("  데모 서버로 열려면: python demo/serve_demo.py  -> http://127.0.0.1:8790/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
