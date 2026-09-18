# 클라우드(Cloud Run) 데모 - 가상 데이터로 대시보드를 띄운다 (실제 메일함 접속 · AI 호출 없음)
# 받을 주소는 서비스 환경변수 DEMO_HOSTS 로 정한다 (README 의 '클라우드 데모' 참고)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

CMD ["python", "demo/serve_demo.py", "--cloud"]
