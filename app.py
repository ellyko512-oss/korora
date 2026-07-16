# -*- coding: utf-8 -*-
"""
Korora — 사내 업무요청 서비스 PoC
- 문제 정의: 요청이 '관리 가능한 객체(티켓)'로 전환되는 순간이 없다는 것
- 해결: 티켓화 + 상태 머신 + 활동 이력 자동 축적 + AI 접수 에이전트
- 의도적 제외: 로그인(역할 토글로 대체), 알림, 파일 첨부, 검색, 통계 고도화
"""

import json
import sqlite3
import uuid
import calendar
import html
from datetime import datetime, timedelta
from urllib.parse import urlencode

import altair as alt
import pandas as pd
import streamlit as st

# ──────────────────────────────────────────────
# 0. 상수 정의 (상태 머신 / 분류 체계)
# ──────────────────────────────────────────────

STATUS_LABELS = {
    "NEW": "🔵 신규",
    "ASSIGNED": "🟣 할당됨",
    "IN_PROGRESS": "🟠 진행중",
    "DONE": "🟢 완료",
    "REJECTED": "⚪ 반려",
}

# 허용된 상태 전이만 정의 — 이 외의 전이는 UI에서 불가능
TRANSITIONS = {
    "NEW": ["ASSIGNED", "REJECTED"],
    "ASSIGNED": ["IN_PROGRESS", "REJECTED"],
    "IN_PROGRESS": ["DONE", "REJECTED"],
    "DONE": [],       # 종결 상태
    "REJECTED": [],   # 종결 상태
}

CATEGORIES = ["IT", "비품", "시설"]
PRIORITIES = ["높음", "보통", "낮음"]
TEAMS = {"IT": "IT지원팀", "비품": "총무팀", "시설": "시설관리팀"}
HANDLERS = ["김지원", "박민수", "이서연"]
REQUESTERS = ["정하늘", "최수진", "이준호", "김다은", "박서준", "홍길동"]

# 데모 조직도. 화면에서는 사람 이름만 단독으로 보이지 않도록 소속을 함께 표시한다.
PERSON_TEAMS = {
    "김지원": "IT지원팀",
    "박민수": "총무팀",
    "이서연": "시설관리팀",
    "정하늘": "사업기획팀",
    "최수진": "영업팀",
    "이준호": "인사팀",
    "김다은": "개발팀",
    "박서준": "마케팅팀",
    "홍길동": "재무팀",
    "관리자": "운영관리팀",
}

# 데모용 이메일 매핑 (실제 인증 시스템이 없으므로 이름 → 이메일 고정 매핑으로 대체)
EMAILS = {
    "김지원": "kim.jiwon@Korora-demo.com",
    "박민수": "park.minsu@Korora-demo.com",
    "이서연": "lee.seoyeon@Korora-demo.com",
    "정하늘": "jung.haneul@Korora-demo.com",
    "최수진": "choi.sujin@Korora-demo.com",
    "이준호": "lee.junho@Korora-demo.com",
    "김다은": "kim.daeun@Korora-demo.com",
    "박서준": "park.seojun@Korora-demo.com",
    "홍길동": "hong.gildong@Korora-demo.com",
}

DB_PATH = "Korora.db"
ACTIVITY_ICONS = {"CREATED": "🆕", "STATUS": "🔄", "ASSIGN": "👤", "COMMENT": "💬", "ATTACHMENT": "📎"}


# ──────────────────────────────────────────────
# 1. DB 초기화 + 시드 데이터
#    (Streamlit Cloud는 파일시스템이 휘발성 →
#     재시작 시에도 항상 데모 가능한 상태를 보장하는 설계)
# ──────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            category TEXT NOT NULL,
            priority TEXT NOT NULL,
            team TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'NEW',
            requester TEXT NOT NULL,
            handler TEXT,
            ai_suggested TEXT,          -- AI 원본 제안(JSON) 보존 → 추후 정확도 측정용
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            actor TEXT NOT NULL,
            type TEXT NOT NULL,         -- CREATED / STATUS / ASSIGN / COMMENT / ATTACHMENT
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            s3_key TEXT NOT NULL,
            content_type TEXT,
            size_bytes INTEGER,
            uploaded_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notification_emails (
            user_name TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    conn.commit()

    # 시드 데이터: 비어있을 때만 8건 생성 (심사자가 접속 즉시 체험 가능)
    if conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0:
        seed(conn)
    conn.close()


def seed(conn):
    now = datetime.now()
    rows = [
        # (title, body, category, priority, status, requester, handler, days_ago)
        ("3층 회의실 모니터 전원 불량", "3층 대회의실 모니터가 켜지지 않습니다. 오후 2시 고객 미팅 전에 확인 부탁드립니다.", "IT", "높음", "IN_PROGRESS", "최수진", "김지원", 1),
        ("탕비실 커피 원두 재고 소진", "4층 탕비실 원두가 다 떨어졌습니다. 디카페인도 함께 요청드려요.", "비품", "낮음", "NEW", "정하늘", None, 0),
        ("주차장 출입카드 인식 오류", "지하 1층 주차장에서 사원증 인식이 간헐적으로 실패합니다.", "시설", "보통", "DONE", "이준호", "이서연", 5),
        ("VPN 접속 불가", "재택근무 중 VPN 연결이 계속 끊깁니다. 오류 코드 691.", "IT", "높음", "ASSIGNED", "김다은", "김지원", 0),
        ("사무용 의자 바퀴 파손", "6층 자리 의자 바퀴가 빠졌습니다. 교체 부탁드립니다.", "비품", "보통", "ASSIGNED", "박서준", "박민수", 2),
        ("화장실 세면대 누수", "5층 남자화장실 세면대 아래에서 물이 샙니다.", "시설", "높음", "IN_PROGRESS", "홍길동", "이서연", 1),
        ("공용 프린터 토너 교체", "2층 복합기 토너 부족 경고가 떴습니다.", "비품", "낮음", "DONE", "최수진", "박민수", 7),
        ("노트북 지급 요청", "신규 입사자용 노트북 1대가 필요합니다. 입사일은 다음 주 월요일입니다.", "IT", "보통", "REJECTED", "정하늘", "김지원", 3),
    ]
    for title, body, cat, pri, status, requester, handler, days in rows:
        created = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            "INSERT INTO requests (title, body, category, priority, team, status, requester, handler, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (title, body, cat, pri, TEAMS[cat], status, requester, handler, created, created),
        )
        rid = cur.lastrowid
        conn.execute(
            "INSERT INTO activities (request_id, actor, type, content, created_at) VALUES (?,?,?,?,?)",
            (rid, requester, "CREATED", "요청 생성", created),
        )
        if handler:
            conn.execute(
                "INSERT INTO activities (request_id, actor, type, content, created_at) VALUES (?,?,?,?,?)",
                (rid, "시스템", "ASSIGN", f"담당자 지정: {handler}", created),
            )
        if status == "REJECTED":
            conn.execute(
                "INSERT INTO activities (request_id, actor, type, content, created_at) VALUES (?,?,?,?,?)",
                (rid, handler or "처리자", "STATUS", "상태 변경: 신규 → 반려 (사유: 자산 구매 프로세스로 접수 필요)", created),
            )
    conn.commit()


# ──────────────────────────────────────────────
# 2. 데이터 접근 함수
# ──────────────────────────────────────────────

def now_str():
    """감사 이력의 선후 관계를 명확히 하기 위해 초 단위까지 저장한다."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def display_person(name):
    """이름 뒤에 소속을 붙여 일관되게 표시한다. 시스템 표기는 그대로 둔다."""
    if not name:
        return "미지정"
    team = PERSON_TEAMS.get(name)
    return f"{name}({team})" if team else name


def display_timestamp(value):
    """기존 분 단위 시드/DB 데이터도 화면에서는 초 단위 형식으로 보정한다."""
    return f"{value}:00" if value and len(value) == 16 else value


def display_activity_content(activity):
    """기존 이력의 담당자 지정 문구에도 소속을 보완한다."""
    content = activity["content"]
    if activity["type"] == "ASSIGN" and content.startswith("담당자 지정: "):
        return f"담당자 지정: {display_person(content.removeprefix('담당자 지정: '))}"
    return content


def add_activity(conn, request_id, actor, type_, content):
    conn.execute(
        "INSERT INTO activities (request_id, actor, type, content, created_at) VALUES (?,?,?,?,?)",
        (request_id, actor, type_, content, now_str()),
    )


def get_notification_email(user_name):
    """사용자가 직접 저장한 수신 주소를 우선하고, 없으면 데모 기본 주소를 사용한다."""
    conn = get_conn()
    row = conn.execute(
        "SELECT email FROM notification_emails WHERE user_name=?", (user_name,)
    ).fetchone()
    conn.close()
    return row["email"] if row else EMAILS.get(user_name, "")


def save_notification_email(user_name, email):
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO notification_emails (user_name, email, updated_at) VALUES (?,?,?)
        ON CONFLICT(user_name) DO UPDATE SET email=excluded.email, updated_at=excluded.updated_at
        """,
        (user_name, email, now_str()),
    )
    conn.commit()
    conn.close()


def notify_ticket_event(recipients, subject, body, exclude_name=None):
    """중복 수신을 제거해 요청자·처리자에게 티켓 이벤트를 알린다."""
    for recipient in {name for name in recipients if name and name != "시스템"}:
        if recipient != exclude_name:
            send_email(recipient, subject, body)


def get_broadcast_email():
    """이 환경에서 벌어지는 모든 활동을 받아볼 전체 구독 이메일. 사용자 개인별이 아닌 전역 설정이다."""
    conn = get_conn()
    row = conn.execute("SELECT value FROM app_settings WHERE key='broadcast_email'").fetchone()
    conn.close()
    return row["value"] if row else ""


def save_broadcast_email(email):
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO app_settings (key, value) VALUES ('broadcast_email', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (email,),
    )
    conn.commit()
    conn.close()


def create_request(title, body, category, priority, requester, ai_suggested=None):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO requests (title, body, category, priority, team, status, requester, ai_suggested, created_at, updated_at)"
        " VALUES (?,?,?,?,?,'NEW',?,?,?,?)",
        (title, body, category, priority, TEAMS[category], requester,
         json.dumps(ai_suggested, ensure_ascii=False) if ai_suggested else None,
         now_str(), now_str()),
    )
    rid = cur.lastrowid
    add_activity(conn, rid, requester, "CREATED", "요청 생성")
    conn.commit()
    conn.close()
    subject = f"[Korora] 요청 #{rid}이 접수되었습니다"
    body = f"'{title}' 요청이 접수되었습니다. 담당팀: {TEAMS[category]}\n현재 상태: {STATUS_LABELS['NEW']}"
    send_email(requester, subject, body)
    broadcast_activity(subject, f"{display_person(requester)}님이 새 요청을 접수했습니다.\n{body}")
    return rid


def change_status(request_id, new_status, actor, reason="", force=False):
    conn = get_conn()
    row = conn.execute("SELECT status, title, requester, handler FROM requests WHERE id=?", (request_id,)).fetchone()
    current = row["status"]
    # 관리자는 감사 이력을 남기면서 상태 머신 제한을 우회할 수 있다.
    if not force and new_status not in TRANSITIONS[current]:
        conn.close()
        return False
    conn.execute("UPDATE requests SET status=?, updated_at=? WHERE id=?", (new_status, now_str(), request_id))
    prefix = "관리자 강제 " if force and new_status not in TRANSITIONS[current] else ""
    content = f"{prefix}상태 변경: {STATUS_LABELS[current]} → {STATUS_LABELS[new_status]}"
    if reason:
        content += f" (사유: {reason})"
    add_activity(conn, request_id, actor, "STATUS", content)
    conn.commit()
    conn.close()
    subject = f"[Korora] 요청 #{request_id} 상태가 변경되었습니다"
    body = f"'{row['title']}' 요청이 {STATUS_LABELS[new_status]} 상태로 변경되었습니다.\n{content}"
    notify_ticket_event([row["requester"], row["handler"]], subject, body, exclude_name=actor)
    broadcast_activity(subject, f"{display_person(actor)}님이 변경했습니다.\n{body}")
    return True


def assign_handler(request_id, handler, actor):
    conn = get_conn()
    row = conn.execute("SELECT title, requester FROM requests WHERE id=?", (request_id,)).fetchone()
    conn.execute("UPDATE requests SET handler=?, updated_at=? WHERE id=?", (handler, now_str(), request_id))
    add_activity(conn, request_id, actor, "ASSIGN", f"담당자 지정: {handler}")
    conn.commit()
    conn.close()
    subject = f"[Korora] 요청 #{request_id} 담당자가 지정되었습니다"
    body = f"'{row['title']}' 요청의 담당자가 {display_person(handler)}(으)로 지정되었습니다."
    notify_ticket_event([handler, row["requester"]], subject, body)
    broadcast_activity(subject, f"{display_person(actor)}님이 지정했습니다.\n{body}")


def add_comment(request_id, actor, text):
    conn = get_conn()
    row = conn.execute("SELECT title, requester, handler FROM requests WHERE id=?", (request_id,)).fetchone()
    add_activity(conn, request_id, actor, "COMMENT", text)
    conn.execute("UPDATE requests SET updated_at=? WHERE id=?", (now_str(), request_id))
    conn.commit()
    conn.close()
    # 요청자가 남기면 담당자에게, 담당자(혹은 그 외)가 남기면 요청자에게 알림
    subject = f"[Korora] 요청 #{request_id}에 새 코멘트가 등록되었습니다"
    body = f"'{row['title']}' 요청에 {display_person(actor)}님이 코멘트를 남겼습니다:\n{text}"
    notify_target = row["handler"] if actor == row["requester"] else row["requester"]
    if notify_target:
        send_email(notify_target, subject, body)
    broadcast_activity(subject, body)


# ──────────────────────────────────────────────
# 3. AI 접수 에이전트 (Claude API)
#    원칙: AI는 제안, 사람이 결정 / 실패 시 수동 입력 폴백
# ──────────────────────────────────────────────

def ai_intake(text: str):
    """자유 텍스트 → {title, category, priority, reason} 구조화. 실패 시 None."""
    try:
        import anthropic
        api_key = st.secrets.get("ANTHROPIC_API_KEY")
        if not api_key:
            return None
        client = anthropic.Anthropic(api_key=api_key)
        prompt = f"""다음 사내 업무 요청 텍스트를 분석해서 JSON으로만 응답해.
다른 설명, 마크다운 백틱 없이 JSON 객체 하나만 출력해.

요청 텍스트: {text}

JSON 형식:
{{"title": "20자 이내 요약 제목", "category": "IT|비품|시설 중 하나", "priority": "높음|보통|낮음 중 하나", "reason": "분류 근거 한 문장"}}

우선순위 기준: 업무 중단/다수 영향/시간 제약 → 높음, 불편하지만 대안 존재 → 보통, 소모품·여유 있는 요청 → 낮음"""
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        # 응답 검증: 허용된 값이 아니면 폴백
        if data.get("category") not in CATEGORIES or data.get("priority") not in PRIORITIES:
            return None
        return data
except Exception as e:
        st.error(f"AI 오류: {e}")
        return None


# ──────────────────────────────────────────────
# 3-1. 외부 연동: 파일 첨부(S3) / 이메일 알림(SES)
#      원칙: 자격증명 미설정·호출 실패는 업무 흐름을 막지 않고 조용히 스킵
# ──────────────────────────────────────────────

def get_secret(key, default=None):
    """secrets.toml 자체가 없으면 st.secrets.get()도 예외를 던지므로 항상 이 헬퍼로 접근."""
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


def get_s3_client():
    try:
        import boto3
        key, secret = get_secret("AWS_ACCESS_KEY_ID"), get_secret("AWS_SECRET_ACCESS_KEY")
        if not key or not secret:
            return None
        return boto3.client(
            "s3", region_name=get_secret("AWS_REGION", "ap-northeast-2"),
            aws_access_key_id=key, aws_secret_access_key=secret,
        )
    except Exception:
        return None


def get_ses_client():
    try:
        import boto3
        key, secret = get_secret("AWS_ACCESS_KEY_ID"), get_secret("AWS_SECRET_ACCESS_KEY")
        if not key or not secret:
            return None
        return boto3.client(
            "ses", region_name=get_secret("AWS_REGION", "ap-northeast-2"),
            aws_access_key_id=key, aws_secret_access_key=secret,
        )
    except Exception:
        return None


def upload_attachment(request_id, uploaded_file, actor) -> bool:
    """업로드 파일 → S3 저장 + attachments 테이블 기록 + 활동 이력 추가. 실패 시 False."""
    client = get_s3_client()
    bucket = get_secret("S3_BUCKET_NAME")
    if not client or not bucket:
        st.warning(f"'{uploaded_file.name}' 첨부 실패: S3가 설정되지 않았습니다. (관리자에게 문의하세요)")
        return False
    s3_key = f"Korora/{request_id}/{uuid.uuid4().hex}_{uploaded_file.name}"
    try:
        client.upload_fileobj(
            uploaded_file, bucket, s3_key,
            ExtraArgs={"ContentType": uploaded_file.type or "application/octet-stream"},
        )
    except Exception:
        st.warning(f"'{uploaded_file.name}' 업로드 중 오류가 발생했습니다.")
        return False
    conn = get_conn()
    request_row = conn.execute(
        "SELECT title, requester, handler FROM requests WHERE id=?", (request_id,)
    ).fetchone()
    conn.execute(
        "INSERT INTO attachments (request_id, filename, s3_key, content_type, size_bytes, uploaded_by, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (request_id, uploaded_file.name, s3_key, uploaded_file.type, uploaded_file.size, actor, now_str()),
    )
    add_activity(conn, request_id, actor, "ATTACHMENT", f"파일 첨부: {uploaded_file.name}")
    conn.commit()
    conn.close()
    subject = f"[Korora] 요청 #{request_id}에 첨부 파일이 등록되었습니다"
    body = f"'{request_row['title']}' 요청에 {display_person(actor)}님이 '{uploaded_file.name}' 파일을 첨부했습니다."
    notify_target = request_row["handler"] if actor == request_row["requester"] else request_row["requester"]
    if notify_target:
        send_email(notify_target, subject, body)
    broadcast_activity(subject, body)
    return True


def get_attachments(request_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM attachments WHERE request_id=? ORDER BY id", (request_id,)
    ).fetchall()
    conn.close()
    return rows


def get_download_url(s3_key: str):
    client = get_s3_client()
    bucket = get_secret("S3_BUCKET_NAME")
    if not client or not bucket:
        return None
    try:
        return client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": s3_key}, ExpiresIn=3600
        )
    except Exception:
        return None


def _send_raw_email(to_email: str, subject: str, body: str) -> bool:
    """실제 SES 발송. 실패해도 예외를 올리지 않아 업무 흐름을 막지 않는다."""
    client = get_ses_client()
    sender = get_secret("SES_SENDER_EMAIL")
    if not client or not sender or not to_email:
        return False
    to_email = get_secret("EMAIL_TEST_OVERRIDE") or to_email
    try:
        client.send_email(
            Source=sender,
            Destination={"ToAddresses": [to_email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
            },
        )
        return True
    except Exception:
        return False


def send_email(to_name: str, subject: str, body: str) -> bool:
    """저장된 수신 주소(없으면 기본 매핑)로 SES 발송."""
    return _send_raw_email(get_notification_email(to_name), subject, body)


def broadcast_activity(subject: str, body: str) -> None:
    """전체 활동 구독 이메일이 설정돼 있으면, 시스템에서 벌어지는 모든 활동을 그 주소로도 보낸다."""
    target = get_broadcast_email()
    if target:
        _send_raw_email(target, subject, body)


# ──────────────────────────────────────────────
# 4. UI 컴포넌트
# ──────────────────────────────────────────────

def render_badge_row(r):
    return (
        f"{STATUS_LABELS[r['status']]} · {r['category']} · 우선순위 {r['priority']}"
        f" · 담당 {display_person(r['handler'])} · {display_timestamp(r['created_at'])}"
    )


def render_clickable_request_card(r, role, role_name):
    """요청 정보를 표시하는 카드 전체를 상세 화면으로 연결한다."""
    def esc(value):
        return html.escape(str(value or ""))

    body = esc(r["body"]).replace("\n", "<br>")
    href = html.escape(
        f"?{urlencode({'request_id': r['id'], 'role': role, 'user': role_name})}", quote=True
    )
    st.markdown(
        f"""
        <a href="{href}" target="_self" style="color:inherit; text-decoration:none; display:block;">
          <div style="border:1px solid rgba(151, 151, 180, .45); border-radius:.75rem; padding:1.15rem 1.3rem; margin:.45rem 0 .8rem; cursor:pointer;">
            <div style="display:flex; justify-content:space-between; gap:1rem; align-items:start;">
              <strong style="font-size:1.05rem;">#{r['id']} {esc(r['title'])}</strong>
              <span style="white-space:nowrap; font-weight:600;">{esc(STATUS_LABELS[r['status']])}</span>
            </div>
            <div style="margin:1.2rem 0; color:rgba(49, 51, 63, .78);">{body}</div>
            <div style="display:flex; flex-wrap:wrap; gap:.65rem 1.6rem; color:rgba(49, 51, 63, .78); font-size:.86rem;">
              <span>접수 · {esc(display_timestamp(r['created_at']))}</span>
              <span>분류 · {esc(r['category'])}</span>
              <span>요청 · {esc(display_person(r['requester']))}</span>
              <span>담당 · {esc(display_person(r['handler']))}</span>
              <span>우선순위 · {esc(r['priority'])}</span>
            </div>
          </div>
        </a>
        """,
        unsafe_allow_html=True,
    )


def page_create(role_name):
    st.subheader("📝 업무 요청하기")
    st.caption("필요하신 업무 요청 사항을 적어주세요. AI가 제목·분류·우선순위를 제안하고, 최종 결정은 직접 하시면 됩니다.")

    text = st.text_area("요청 내용", placeholder="예) 3층 회의실 모니터가 안 켜져요. 오후 2시에 고객 미팅이 있어서 급합니다.", height=120)

    col1, col2 = st.columns([1, 3])
    if col1.button("🤖 요청", type="primary"):
        if not text.strip():
            st.warning("요청 내용을 입력해주세요.")
        else:
            with st.spinner("AI가 요청을 분석하는 중..."):
                suggestion = ai_intake(text)
            # 재실행 대비: 제안 결과를 session_state에 보존
            st.session_state["ai_suggestion"] = suggestion
            st.session_state["ai_input_text"] = text
            if suggestion is None:
                st.session_state["ai_failed"] = True
            else:
                st.session_state["ai_failed"] = False

    suggestion = st.session_state.get("ai_suggestion")
    ai_failed = st.session_state.get("ai_failed", False)

    if ai_failed:
        st.warning("AI 분석을 사용할 수 없어 수동 입력으로 진행합니다. (API 키 미설정 또는 호출 실패)")

    # AI 제안이 있든 없든 동일한 확정 폼 사용 — AI는 기본값을 채워줄 뿐
    if suggestion or ai_failed:
        if suggestion:
            st.success(f"AI 제안 근거: {suggestion.get('reason', '')}")
        with st.form("confirm_form"):
            title = st.text_input("제목", value=(suggestion or {}).get("title", ""))
            c1, c2 = st.columns(2)
            category = c1.selectbox("카테고리", CATEGORIES,
                                    index=CATEGORIES.index(suggestion["category"]) if suggestion else 0)
            priority = c2.selectbox("우선순위", PRIORITIES,
                                    index=PRIORITIES.index(suggestion["priority"]) if suggestion else 1)
            st.caption(f"담당팀 자동 배정: {TEAMS[category]} (카테고리 기준)")
            files = st.file_uploader("첨부 파일 (선택)", accept_multiple_files=True)
            submitted = st.form_submit_button("요청 제출", type="primary")
            if submitted:
                if not title.strip():
                    st.error("제목을 입력해주세요.")
                else:
                    body = st.session_state.get("ai_input_text", text)
                    rid = create_request(title, body, category, priority, role_name, suggestion)
                    for f in files:
                        upload_attachment(rid, f, role_name)
                    st.session_state.pop("ai_suggestion", None)
                    st.session_state.pop("ai_failed", None)
                    st.success(f"요청 #{rid} 이(가) 접수되었습니다. '요청 목록'에서 진행 상황을 확인하세요.")


def page_list(role, role_name):
    st.subheader("📋 내 요청" if role == "요청자" else "📋 요청 목록")
    conn = get_conn()
    if role == "관리자":
        rows = conn.execute("SELECT * FROM requests ORDER BY id DESC").fetchall()
    elif role == "처리자":
        rows = conn.execute(
            "SELECT * FROM requests WHERE handler=? OR handler IS NULL ORDER BY id DESC", (role_name,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM requests WHERE requester=? ORDER BY id DESC", (role_name,)).fetchall()
    conn.close()

    tabs = st.tabs(["전체"] + [STATUS_LABELS[s] for s in STATUS_LABELS])
    status_keys = [None] + list(STATUS_LABELS.keys())

    for tab, status in zip(tabs, status_keys):
        with tab:
            filtered = [r for r in rows if status is None or r["status"] == status]
            if not filtered:
                st.caption("해당 요청이 없습니다.")
            for r in filtered:
                render_clickable_request_card(r, role, role_name)


def page_detail(role, role_name):
    rid = st.session_state.get("selected_id")
    if not rid:
        st.info("해당 메뉴는 없어도 될 것 같습니다.")
        return
    conn = get_conn()
    r = conn.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    acts = conn.execute("SELECT * FROM activities WHERE request_id=? ORDER BY id", (rid,)).fetchall()
    conn.close()

    if st.button("← 목록으로"):
        st.session_state["nav_target"] = "목록"
        st.rerun()

    st.subheader(f"#{r['id']} {r['title']}")
    st.caption(render_badge_row(r))
    st.markdown(f"> {r['body']}")
    st.markdown(f"**요청자:** {display_person(r['requester'])} · **담당팀:** {r['team']} · **처리 담당자:** {display_person(r['handler'])}")

    # 처리자 전용: 담당자 지정 + 상태 변경 (허용된 전이만 노출)
    if role in ["처리자", "관리자"]:
        st.divider()
        if role == "관리자":
            st.info("관리자 모드: 담당자와 처리 상태를 상태 전이 제한 없이 변경할 수 있습니다. 모든 변경은 활동 이력에 기록됩니다.")
        c1, c2 = st.columns(2)
        handler = r["handler"] or "선택"
        with c1:
            # 종결 요청은 처리자가 추가 배정할 필요가 없다. 관리자는 강제 수정 권한으로 예외다.
            if role == "관리자" or TRANSITIONS[r["status"]]:
                handler = st.selectbox("담당자 지정", ["선택"] + HANDLERS,
                                       index=(HANDLERS.index(r["handler"]) + 1) if r["handler"] in HANDLERS else 0,
                                       format_func=lambda h: "선택" if h == "선택" else display_person(h))
                if handler != "선택" and handler != r["handler"]:
                    if st.button("담당자 저장"):
                        assign_handler(rid, handler, role_name)
                        st.rerun()
        with c2:
            next_statuses = [s for s in STATUS_LABELS if s != r["status"]] if role == "관리자" else TRANSITIONS[r["status"]]
            status_options = [r["status"]] + next_statuses
            if next_statuses:
                label = "처리 상태 강제 변경" if role == "관리자" else "상태 변경"
                new_status = st.selectbox(
                    label, status_options, index=status_options.index(r["status"]),
                    key=f"status_select_{rid}_{r['status']}",
                    format_func=lambda s: f"{STATUS_LABELS[s]} (현재)" if s == r["status"] else STATUS_LABELS[s],
                )
                reason = ""
                if new_status == "REJECTED":
                    reason = st.text_input("반려 사유 (필수)")
                if st.button("상태 저장", type="primary", disabled=(new_status == r["status"])):
                    if new_status == "REJECTED" and not reason.strip():
                        st.error("반려 시 사유를 입력해야 합니다.")
                    elif new_status == "ASSIGNED" and not r["handler"] and handler == "선택":
                        st.error("할당 전에 담당자를 먼저 지정해주세요.")
                    else:
                        change_status(rid, new_status, role_name, reason, force=(role == "관리자"))
                        st.rerun()
            else:
                st.caption("종결된 요청입니다. 더 이상 상태를 변경할 수 없습니다.")

    # 첨부 파일 — 기본은 접힌 상태이며, 사용자가 열었을 때만 내용을 확인한다.
    st.divider()
    with st.expander("📎 첨부 파일", expanded=False):
        attachments = get_attachments(rid)
        if not attachments:
            st.caption("첨부된 파일이 없습니다.")
        for a in attachments:
            url = get_download_url(a["s3_key"])
            label = f"📎 {a['filename']} · {display_person(a['uploaded_by'])} · {display_timestamp(a['created_at'])}"
            if url:
                st.markdown(f"[{label}]({url})")
            else:
                st.caption(f"{label} (다운로드 링크를 가져올 수 없습니다)")
        with st.form("attach_form", clear_on_submit=True):
            new_files = st.file_uploader("파일 추가", accept_multiple_files=True)
            if st.form_submit_button("업로드") and new_files:
                for f in new_files:
                    upload_attachment(rid, f, role_name)
                st.rerun()

    # 활동 타임라인 — 모든 이력이 요청 단위로 자동 축적됨 (P3 해결)
    st.divider()
    st.markdown("**활동 이력**")
    for a in acts:
        st.markdown(f"{ACTIVITY_ICONS[a['type']]} `{display_timestamp(a['created_at'])}` **{display_person(a['actor'])}** — {display_activity_content(a)}")

    with st.form("comment_form", clear_on_submit=True):
        comment = st.text_input("코멘트 남기기")
        if st.form_submit_button("등록") and comment.strip():
            add_comment(rid, role_name, comment)
            st.rerun()


def page_activity_log():
    st.subheader("🗂️ 전체 활동 로그")
    st.info("모든 요청의 생성·담당자 지정·상태 변경·댓글·첨부 이력을 최신순으로 확인합니다. 활동 유형과 작성자로 좁힌 뒤, 오른쪽 요청 코드 버튼을 누르면 해당 요청 상세로 이동합니다.")
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT a.id, a.request_id, a.actor, a.type, a.content, a.created_at,
               r.title AS request_title, r.body AS request_body
        FROM activities a JOIN requests r ON a.request_id = r.id
        ORDER BY a.id DESC
        """
    ).fetchall()
    conn.close()

    all_actors = sorted({r["actor"] for r in rows})
    c1, c2, c3 = st.columns([1, 1, 2])
    type_filter = c1.multiselect("활동 유형", list(ACTIVITY_ICONS.keys()),
                                 format_func=lambda t: f"{ACTIVITY_ICONS[t]} {t}")
    actor_filter = c2.multiselect("작성자", all_actors, format_func=display_person)
    search_text = c3.text_input("제목 또는 내용 검색", placeholder="요청 제목, 요청 내용 또는 활동 내용을 입력하세요")
    search_keyword = search_text.strip().casefold()

    filtered = [
        r for r in rows
        if (not type_filter or r["type"] in type_filter)
        and (not actor_filter or r["actor"] in actor_filter)
        and (not search_keyword or search_keyword in " ".join([
            r["request_title"], r["request_body"], r["content"]
        ]).casefold())
    ]
    st.caption(f"{len(filtered)}건")
    for r in filtered:
        detail_col, request_col = st.columns([8, 1])
        detail_col.markdown(
            f"{ACTIVITY_ICONS[r['type']]} `{display_timestamp(r['created_at'])}` "
            f"**{r['request_title']}** · **{display_person(r['actor'])}** — {display_activity_content(r)}"
        )
        if request_col.button(f"#{r['request_id']}", key=f"log_request_{r['id']}", help="해당 요청으로 이동"):
            st.session_state["selected_id"] = r["request_id"]
            st.session_state["nav_target"] = "상세"
            st.rerun()


def page_dashboard():
    st.subheader("📊 현황 대시보드")
    conn = get_conn()
    df = pd.read_sql_query("SELECT * FROM requests", conn)
    conn.close()

    counts = df["status"].value_counts().to_dict()
    cols = st.columns(len(STATUS_LABELS))
    for col, (s, label) in zip(cols, STATUS_LABELS.items()):
        col.metric(label, counts.get(s, 0))

    if df.empty:
        return

    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**카테고리별 상태 분포**")
        category_counts = df.groupby(["category", "status"]).size().reset_index(name="건수")
        category_counts["상태"] = category_counts["status"].map(STATUS_LABELS)
        category_chart = alt.Chart(category_counts).mark_bar().encode(
            x=alt.X("category:N", title="카테고리", axis=alt.Axis(labelAngle=90)),
            y=alt.Y("건수:Q", title="요청 수"),
            color=alt.Color("상태:N", title="상태", scale=alt.Scale(
                domain=list(STATUS_LABELS.values()), range=["#4C78A8", "#9C6ADE", "#F58518", "#54A24B", "#BAB0AC"]
            )),
            tooltip=["category:N", "상태:N", "건수:Q"],
        )
        st.altair_chart(category_chart, use_container_width=True)
    with c2:
        st.markdown("**우선순위 분포**")
        priority_counts = df["priority"].value_counts().rename_axis("priority").reset_index(name="건수")
        priority_chart = alt.Chart(priority_counts).mark_bar().encode(
            x=alt.X("priority:N", title="우선순위", axis=alt.Axis(labelAngle=90), sort=PRIORITIES),
            y=alt.Y("건수:Q", title="요청 수"),
            color=alt.Color("priority:N", legend=None),
            tooltip=["priority:N", "건수:Q"],
        )
        st.altair_chart(priority_chart, use_container_width=True)

    st.divider()
    st.markdown("**요청 캘린더 (기준일: 오늘)**")
    today = datetime.now().date()
    # created_at은 과거 분 단위 데이터와 현재 초 단위 데이터가 섞여 있을 수 있어
    # display_timestamp로 먼저 형식을 통일한 뒤 파싱한다 (그렇지 않으면 pandas가
    # 첫 값 기준으로 포맷을 추론하다 형식이 다른 값에서 예외를 던진다).
    df["created_date"] = pd.to_datetime(df["created_at"].apply(display_timestamp)).dt.date
    month_requests = df[(df["created_date"].apply(lambda d: d.year) == today.year) &
                        (df["created_date"].apply(lambda d: d.month) == today.month)]
    st.caption(f"{today.year}년 {today.month}월 · 오늘 {today.strftime('%Y-%m-%d')}")
    for week in calendar.monthcalendar(today.year, today.month):
        day_cols = st.columns(7)
        for col, day in zip(day_cols, week):
            with col:
                if not day:
                    st.caption(" ")
                    continue
                day_date = today.replace(day=day)
                marker = "오늘 · " if day_date == today else ""
                st.markdown(f"**{marker}{day}일**")
                items = month_requests[month_requests["created_date"] == day_date]
                if items.empty:
                    st.caption("—")
                else:
                    for item in items.itertuples():
                        st.caption(f"#{item.id} {item.title}")

    st.divider()
    st.markdown("**일별 접수 추이 (최근 14일)**")
    since = (datetime.now() - timedelta(days=13)).date()
    daily = df[df["created_date"] >= since].groupby("created_date").size()
    daily = daily.reindex(pd.date_range(since, datetime.now().date()).date, fill_value=0)
    st.line_chart(daily)

    st.divider()
    c3, c4 = st.columns(2)
    with c3:
        st.markdown("**담당자별 처리 현황**")
        done = df[df["status"] == "DONE"].copy()
        if done.empty:
            st.caption("완료된 요청이 없습니다.")
        else:
            done["hours"] = (
                pd.to_datetime(done["updated_at"].apply(display_timestamp))
                - pd.to_datetime(done["created_at"].apply(display_timestamp))
            ).dt.total_seconds() / 3600
            summary = done.groupby("handler").agg(완료건수=("id", "count"), 평균처리시간_시간=("hours", "mean")).round(1)
            summary.index = [display_person(name) for name in summary.index]
            st.dataframe(summary, use_container_width=True)
    with c4:
        st.markdown("**반려율**")
        rejected = (df["status"] == "REJECTED").sum()
        st.metric("전체 대비 반려", f"{rejected / len(df) * 100:.1f}%", help=f"{rejected}건 / 전체 {len(df)}건")


# ──────────────────────────────────────────────
# 5. 메인
# ──────────────────────────────────────────────

def main():
    st.set_page_config(page_title="Korora — 사내 업무요청", page_icon="📮", layout="wide")
    init_db()

    # 요청 목록 카드의 링크를 통해 들어온 경우, 상세 화면으로 이동한다.
    linked_request_id = st.query_params.get("request_id")
    linked_role = st.query_params.get("role")
    linked_user = st.query_params.get("user")
    if linked_request_id:
        try:
            st.session_state["selected_id"] = int(linked_request_id)
            st.session_state["show_detail"] = True
        except (TypeError, ValueError):
            pass
        if linked_role in ["요청자", "처리자", "관리자"]:
            st.session_state["role"] = linked_role
            if linked_role == "요청자" and linked_user in REQUESTERS:
                st.session_state["current_requester"] = linked_user
            elif linked_role == "처리자" and linked_user in HANDLERS:
                st.session_state["current_handler"] = linked_user
        for param in ["request_id", "role", "user"]:
            if param in st.query_params:
                del st.query_params[param]

    with st.sidebar:
        st.title("📮 Korora")
        st.caption("모든 요청을, 잃어버리지 않게.")
        # 로그인 대신 역할 전환 토글 — PoC 검증 대상이 인증이 아니기 때문
        prev_role = st.session_state.get("_role_last")
        role = st.radio("역할 전환 (로그인 대체)", ["요청자", "처리자", "관리자"], key="role")
        role_changed = prev_role is not None and role != prev_role
        st.session_state["_role_last"] = role
        if role == "요청자":
            role_name = st.selectbox("현재 요청자", REQUESTERS, format_func=display_person, key="current_requester")
        elif role == "처리자":
            role_name = st.selectbox("현재 처리자", HANDLERS, format_func=display_person, key="current_handler")
        else:
            role_name = "관리자"
        st.caption(f"현재 사용자: {display_person(role_name)}")
        st.divider()
        # 상세는 목록의 개별 요청에서만 진입한다.
        pages = ["요청하기", "목록"] if role == "요청자" else ["목록", "대시보드", "활동 로그"]

        # "메뉴" 라디오를 직접 클릭했는지는, 위젯을 새로 그리기 전 시점의 값을
        # 지난 실행 종료 시점에 저장해둔 스냅샷과 비교해야만 판별할 수 있다.
        # (키가 바인딩된 위젯은 클릭 즉시 스크립트 실행 전에 session_state가
        # 갱신되므로, 이 시점의 값만으로는 "그냥 재실행"과 구분이 안 된다.)
        prev_menu = st.session_state.get("_menu_last")
        current_menu_value = st.session_state.get("page")
        direct_menu_click = prev_menu is not None and current_menu_value is not None and current_menu_value != prev_menu

        # 상세는 메뉴 항목이 아니라 목록 카드 클릭으로 진입하는 화면이다. 위젯 생성
        # 전에만 이동 요청을 반영해 Streamlit 세션 상태 충돌을 피한다.
        nav_target = st.session_state.pop("nav_target", None)
        if nav_target == "상세":
            st.session_state["show_detail"] = True
        elif nav_target is not None:
            st.session_state["show_detail"] = False
            if nav_target in pages:
                st.session_state["page"] = nav_target
        elif direct_menu_click or role_changed:
            # 상세 화면을 보던 중 메뉴를 직접 클릭하거나 역할을 바꾼 경우에도 빠져나가야 한다.
            st.session_state["show_detail"] = False

        if st.session_state.get("page") not in pages:
            st.session_state["page"] = pages[0]
        menu_page = st.radio("메뉴", pages, key="page")
        st.session_state["_menu_last"] = menu_page
        page = "상세" if st.session_state.get("show_detail") else menu_page

        st.divider()
        with st.expander("✉️ 이메일 수신 설정"):
            st.caption(f"{display_person(role_name)}님이 티켓 알림을 받을 이메일 주소를 설정합니다.")
            with st.form(f"email_settings_{role_name}"):
                notification_email = st.text_input(
                    "받으실 이메일 주소",
                    value=get_notification_email(role_name),
                    placeholder="name@example.com",
                )
                if st.form_submit_button("이메일 주소 저장"):
                    notification_email = notification_email.strip()
                    if "@" not in notification_email or notification_email.startswith("@"):
                        st.error("유효한 이메일 주소를 입력해주세요.")
                    else:
                        save_notification_email(role_name, notification_email)
                        st.success("이메일 수신 주소를 저장했습니다.")
            st.caption("요청 등록, 담당자 배정, 상태 변경, 코멘트, 첨부 파일 등록 시 알림을 보냅니다.")

        with st.expander("🔔 전체 활동 알림 구독"):
            st.caption("역할·담당자와 무관하게, 이 환경에서 벌어지는 모든 요청·상태변경·코멘트·첨부 활동을 한 주소로 받습니다. (데모/QA용 전역 설정)")
            with st.form("broadcast_email_settings"):
                broadcast_email = st.text_input(
                    "구독할 이메일 주소", value=get_broadcast_email(), placeholder="name@example.com",
                )
                if st.form_submit_button("구독 저장"):
                    broadcast_email = broadcast_email.strip()
                    if broadcast_email and ("@" not in broadcast_email or broadcast_email.startswith("@")):
                        st.error("유효한 이메일 주소를 입력해주세요.")
                    else:
                        save_broadcast_email(broadcast_email)
                        st.success("전체 활동 구독 이메일을 저장했습니다." if broadcast_email else "전체 활동 구독을 해지했습니다.")

    if page == "요청하기":
        page_create(role_name)
    elif page == "목록":
        page_list(role, role_name)
    elif page == "상세":
        page_detail(role, role_name)
    elif page == "대시보드":
        page_dashboard()
    elif page == "활동 로그":
        page_activity_log()


if __name__ == "__main__":
    main()
