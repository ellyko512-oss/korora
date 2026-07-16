# -*- coding: utf-8 -*-
"""
Reqly — 사내 업무요청 서비스 PoC
- 문제 정의: 요청이 '관리 가능한 객체(티켓)'로 전환되는 순간이 없다는 것
- 해결: 티켓화 + 상태 머신 + 활동 이력 자동 축적 + AI 접수 에이전트
- 의도적 제외: 로그인(역할 토글로 대체), 알림, 파일 첨부, 검색, 통계 고도화
"""

import json
import sqlite3
import uuid
from datetime import datetime, timedelta

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

# 데모용 이메일 매핑 (실제 인증 시스템이 없으므로 이름 → 이메일 고정 매핑으로 대체)
EMAILS = {
    "김지원": "kim.jiwon@reqly-demo.com",
    "박민수": "park.minsu@reqly-demo.com",
    "이서연": "lee.seoyeon@reqly-demo.com",
    "정하늘": "jung.haneul@reqly-demo.com",
    "최수진": "choi.sujin@reqly-demo.com",
    "이준호": "lee.junho@reqly-demo.com",
    "김다은": "kim.daeun@reqly-demo.com",
    "박서준": "park.seojun@reqly-demo.com",
    "홍길동": "hong.gildong@reqly-demo.com",
}

DB_PATH = "reqly.db"
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
        created = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
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
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def add_activity(conn, request_id, actor, type_, content):
    conn.execute(
        "INSERT INTO activities (request_id, actor, type, content, created_at) VALUES (?,?,?,?,?)",
        (request_id, actor, type_, content, now_str()),
    )


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
    return rid


def change_status(request_id, new_status, actor, reason=""):
    conn = get_conn()
    row = conn.execute("SELECT status, title, requester FROM requests WHERE id=?", (request_id,)).fetchone()
    current = row["status"]
    # 상태 머신 강제: 허용되지 않은 전이는 거부
    if new_status not in TRANSITIONS[current]:
        conn.close()
        return False
    conn.execute("UPDATE requests SET status=?, updated_at=? WHERE id=?", (new_status, now_str(), request_id))
    content = f"상태 변경: {STATUS_LABELS[current]} → {STATUS_LABELS[new_status]}"
    if reason:
        content += f" (사유: {reason})"
    add_activity(conn, request_id, actor, "STATUS", content)
    conn.commit()
    conn.close()
    send_email(
        row["requester"], f"[Reqly] 요청 #{request_id} 상태가 변경되었습니다",
        f"'{row['title']}' 요청이 {STATUS_LABELS[new_status]} 상태로 변경되었습니다.\n{content}",
    )
    return True


def assign_handler(request_id, handler, actor):
    conn = get_conn()
    title = conn.execute("SELECT title FROM requests WHERE id=?", (request_id,)).fetchone()["title"]
    conn.execute("UPDATE requests SET handler=?, updated_at=? WHERE id=?", (handler, now_str(), request_id))
    add_activity(conn, request_id, actor, "ASSIGN", f"담당자 지정: {handler}")
    conn.commit()
    conn.close()
    send_email(
        handler, f"[Reqly] 새 업무 요청이 배정되었습니다 (#{request_id})",
        f"'{title}' (#{request_id}) 요청의 담당자로 지정되었습니다.",
    )


def add_comment(request_id, actor, text):
    conn = get_conn()
    row = conn.execute("SELECT title, requester, handler FROM requests WHERE id=?", (request_id,)).fetchone()
    add_activity(conn, request_id, actor, "COMMENT", text)
    conn.execute("UPDATE requests SET updated_at=? WHERE id=?", (now_str(), request_id))
    conn.commit()
    conn.close()
    # 요청자가 남기면 담당자에게, 담당자(혹은 그 외)가 남기면 요청자에게 알림
    notify_target = row["handler"] if actor == row["requester"] else row["requester"]
    if notify_target:
        send_email(
            notify_target, f"[Reqly] 요청 #{request_id}에 새 코멘트가 등록되었습니다",
            f"'{row['title']}' 요청에 {actor}님이 코멘트를 남겼습니다:\n{text}",
        )


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
    except Exception:
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
    s3_key = f"reqly/{request_id}/{uuid.uuid4().hex}_{uploaded_file.name}"
    try:
        client.upload_fileobj(
            uploaded_file, bucket, s3_key,
            ExtraArgs={"ContentType": uploaded_file.type or "application/octet-stream"},
        )
    except Exception:
        st.warning(f"'{uploaded_file.name}' 업로드 중 오류가 발생했습니다.")
        return False
    conn = get_conn()
    conn.execute(
        "INSERT INTO attachments (request_id, filename, s3_key, content_type, size_bytes, uploaded_by, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (request_id, uploaded_file.name, s3_key, uploaded_file.type, uploaded_file.size, actor, now_str()),
    )
    add_activity(conn, request_id, actor, "ATTACHMENT", f"파일 첨부: {uploaded_file.name}")
    conn.commit()
    conn.close()
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


def send_email(to_name: str, subject: str, body: str) -> bool:
    """EMAILS 매핑에서 수신자 조회 후 SES로 발송. 실패해도 예외를 올리지 않음."""
    client = get_ses_client()
    sender = get_secret("SES_SENDER_EMAIL")
    to_email = EMAILS.get(to_name)
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


# ──────────────────────────────────────────────
# 4. UI 컴포넌트
# ──────────────────────────────────────────────

def render_badge_row(r):
    return (
        f"{STATUS_LABELS[r['status']]} · {r['category']} · 우선순위 {r['priority']}"
        f" · 담당 {r['handler'] or '미지정'} · {r['created_at']}"
    )


def page_create(role_name):
    st.subheader("📝 업무 요청하기")
    st.caption("필요하신 업무 요청 사항을 적어주세요. AI가 제목·분류·우선순위를 제안하고, 최종 결정은 직접 하시면 됩니다.")

    text = st.text_area("요청 내용", placeholder="예) 3층 회의실 모니터가 안 켜져요. 오후 2시에 고객 미팅이 있어서 급합니다.", height=120)

    col1, col2 = st.columns([1, 3])
    if col1.button("🤖 AI 분석", type="primary", disabled=not text.strip()):
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
    st.subheader("📋 요청 목록" if role == "처리자" else "📋 내 요청")
    conn = get_conn()
    if role == "처리자":
        rows = conn.execute("SELECT * FROM requests ORDER BY id DESC").fetchall()
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
                with st.container(border=True):
                    c1, c2 = st.columns([5, 1])
                    c1.markdown(f"**#{r['id']} {r['title']}**")
                    c1.caption(render_badge_row(r))
                    if c2.button("상세", key=f"detail_{status}_{r['id']}"):
                        st.session_state["selected_id"] = r["id"]
                        st.session_state["nav_target"] = "상세"
                        st.rerun()


def page_detail(role, role_name):
    rid = st.session_state.get("selected_id")
    if not rid:
        st.info("요청 목록에서 항목을 선택해주세요.")
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
    st.markdown(f"**요청자:** {r['requester']} · **담당팀:** {r['team']}")

    # 처리자 전용: 담당자 지정 + 상태 변경 (허용된 전이만 노출)
    if role == "처리자":
        st.divider()
        c1, c2 = st.columns(2)
        with c1:
            handler = st.selectbox("담당자 지정", ["선택"] + HANDLERS,
                                   index=(HANDLERS.index(r["handler"]) + 1) if r["handler"] in HANDLERS else 0)
            if handler != "선택" and handler != r["handler"]:
                if st.button("담당자 저장"):
                    assign_handler(rid, handler, role_name)
                    st.rerun()
        with c2:
            allowed = TRANSITIONS[r["status"]]
            if allowed:
                new_status = st.selectbox("상태 변경", allowed, format_func=lambda s: STATUS_LABELS[s])
                reason = ""
                if new_status == "REJECTED":
                    reason = st.text_input("반려 사유 (필수)")
                if st.button("상태 저장", type="primary"):
                    if new_status == "REJECTED" and not reason.strip():
                        st.error("반려 시 사유를 입력해야 합니다.")
                    elif new_status == "ASSIGNED" and not r["handler"] and handler == "선택":
                        st.error("할당 전에 담당자를 먼저 지정해주세요.")
                    else:
                        change_status(rid, new_status, role_name, reason)
                        st.rerun()
            else:
                st.caption("종결된 요청입니다. 더 이상 상태를 변경할 수 없습니다.")

    # 첨부 파일 — 요청자·처리자 모두 열람/업로드 가능
    st.divider()
    st.markdown("**첨부 파일**")
    attachments = get_attachments(rid)
    if not attachments:
        st.caption("첨부된 파일이 없습니다.")
    for a in attachments:
        url = get_download_url(a["s3_key"])
        label = f"📎 {a['filename']} · {a['uploaded_by']} · {a['created_at']}"
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
        st.markdown(f"{ACTIVITY_ICONS[a['type']]} `{a['created_at']}` **{a['actor']}** — {a['content']}")

    with st.form("comment_form", clear_on_submit=True):
        comment = st.text_input("코멘트 남기기")
        if st.form_submit_button("등록") and comment.strip():
            add_comment(rid, role_name, comment)
            st.rerun()


def page_activity_log():
    st.subheader("🗂️ 전체 활동 로그")
    st.caption("모든 요청을 가로질러 발생한 작업 이력을 시간 역순으로 확인합니다. (감사용)")
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT a.id, a.request_id, a.actor, a.type, a.content, a.created_at, r.title AS request_title
        FROM activities a JOIN requests r ON a.request_id = r.id
        ORDER BY a.id DESC
        """
    ).fetchall()
    conn.close()

    all_actors = sorted({r["actor"] for r in rows})
    c1, c2 = st.columns(2)
    type_filter = c1.multiselect("활동 유형", list(ACTIVITY_ICONS.keys()),
                                 format_func=lambda t: f"{ACTIVITY_ICONS[t]} {t}")
    actor_filter = c2.multiselect("작성자", all_actors)

    filtered = [
        r for r in rows
        if (not type_filter or r["type"] in type_filter)
        and (not actor_filter or r["actor"] in actor_filter)
    ]
    st.caption(f"{len(filtered)}건")
    for r in filtered:
        st.markdown(
            f"{ACTIVITY_ICONS[r['type']]} `{r['created_at']}` **{r['actor']}** — {r['content']}"
            f"  <span style='color:gray'>(#{r['request_id']} {r['request_title']})</span>",
            unsafe_allow_html=True,
        )


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
        pivot = df.pivot_table(index="category", columns="status", values="id", aggfunc="count", fill_value=0)
        pivot = pivot.rename(columns=STATUS_LABELS)
        st.bar_chart(pivot)
    with c2:
        st.markdown("**우선순위 분포**")
        st.bar_chart(df["priority"].value_counts())

    st.divider()
    st.markdown("**일별 접수 추이 (최근 14일)**")
    df["created_date"] = pd.to_datetime(df["created_at"]).dt.date
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
            done["hours"] = (pd.to_datetime(done["updated_at"]) - pd.to_datetime(done["created_at"])).dt.total_seconds() / 3600
            summary = done.groupby("handler").agg(완료건수=("id", "count"), 평균처리시간_시간=("hours", "mean")).round(1)
            st.dataframe(summary, use_container_width=True)
    with c4:
        st.markdown("**반려율**")
        rejected = (df["status"] == "REJECTED").sum()
        st.metric("전체 대비 반려", f"{rejected / len(df) * 100:.1f}%", help=f"{rejected}건 / 전체 {len(df)}건")


# ──────────────────────────────────────────────
# 5. 메인
# ──────────────────────────────────────────────

def main():
    st.set_page_config(page_title="Reqly — 사내 업무요청", page_icon="📮", layout="wide")
    init_db()

    with st.sidebar:
        st.title("📮 Reqly")
        st.caption("모든 요청을, 잃어버리지 않게.")
        # 로그인 대신 역할 전환 토글 — PoC 검증 대상이 인증이 아니기 때문
        role = st.radio("역할 전환 (로그인 대체)", ["요청자", "처리자"])
        role_name = "정하늘" if role == "요청자" else "김지원"
        st.caption(f"현재 사용자: {role_name}")
        st.divider()
        pages = ["요청하기", "목록", "상세"] if role == "요청자" else ["목록", "상세", "대시보드", "활동 로그"]
        # "메뉴" 위젯이 key="page"로 세션 상태를 직접 소유하므로, 위젯 인스턴스화 이후에는
        # st.session_state["page"]를 다시 쓸 수 없음 (StreamlitAPIException).
        # 다른 곳(예: 목록의 "상세" 버튼)에서의 프로그래밍적 이동 요청은 별도 키(nav_target)에
        # 남겨두고, 위젯을 만들기 "전"인 여기서만 반영한다.
        if "nav_target" in st.session_state:
            st.session_state["page"] = st.session_state.pop("nav_target")
        if st.session_state.get("page") not in pages:
            st.session_state["page"] = pages[0]
        page = st.radio("메뉴", pages, key="page")

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
