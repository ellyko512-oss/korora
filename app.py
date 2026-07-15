# -*- coding: utf-8 -*-
"""
Reqly — 사내 업무요청 서비스 PoC
- 문제 정의: 요청이 '관리 가능한 객체(티켓)'로 전환되는 순간이 없다는 것
- 해결: 티켓화 + 상태 머신 + 활동 이력 자동 축적 + AI 접수 에이전트
- 역할 모델: 역할은 계정 속성이 아니라 '티켓과의 관계' — 누구나 요청자이면서,
  자기 팀으로 들어온 티켓 앞에서는 처리자가 된다. (사용자 선택 + 모드 전환으로 표현)
- 의도적 제외: 로그인(사용자 선택으로 대체), 알림, 파일 첨부, 검색, 통계 고도화
"""

import json
import sqlite3
from datetime import datetime, timedelta

import streamlit as st

# ──────────────────────────────────────────────
# 0. 상수 정의 (상태 머신 / 분류 체계 / 조직)
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

# 담당팀별 처리 가능 인원 — 담당자 후보는 티켓의 담당팀 기준으로 결정
TEAM_MEMBERS = {
    "IT지원팀": ["김지원", "한도윤"],
    "총무팀": ["박민수", "오세라"],
    "시설관리팀": ["이서연", "강태오"],
}

# 전 직원 — 모든 부서의 누구나 요청자가 될 수 있고,
# 처리팀 소속이라도 다른 팀 소관 업무에서는 요청자다 (역할 = 티켓과의 관계)
EMPLOYEES = {
    "정하늘": "마케팅팀", "최수진": "영업팀", "이준호": "재무팀",
    "김다은": "인사팀", "박서준": "디자인팀", "홍길동": "전략기획팀",
    "김지원": "IT지원팀", "한도윤": "IT지원팀",
    "박민수": "총무팀", "오세라": "총무팀",
    "이서연": "시설관리팀", "강태오": "시설관리팀",
}

DB_PATH = "reqly.db"


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
            type TEXT NOT NULL,         -- CREATED / STATUS / ASSIGN / COMMENT
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()

    # 시드 데이터: 비어있을 때만 생성 (심사자가 접속 즉시 체험 가능)
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
        ("VPN 접속 불가", "재택근무 중 VPN 연결이 계속 끊깁니다. 오류 코드 691.", "IT", "높음", "ASSIGNED", "김다은", "한도윤", 0),
        ("사무용 의자 바퀴 파손", "6층 자리 의자 바퀴가 빠졌습니다. 교체 부탁드립니다.", "비품", "보통", "ASSIGNED", "박서준", "박민수", 2),
        ("화장실 세면대 누수", "5층 남자화장실 세면대 아래에서 물이 샙니다.", "시설", "높음", "IN_PROGRESS", "홍길동", "강태오", 1),
        ("공용 프린터 토너 교체", "2층 복합기 토너 부족 경고가 떴습니다.", "비품", "낮음", "DONE", "최수진", "오세라", 7),
        ("노트북 지급 요청", "신규 입사자용 노트북 1대가 필요합니다. 입사일은 다음 주 월요일입니다.", "IT", "보통", "REJECTED", "정하늘", "김지원", 3),
        # 역할 = 티켓과의 관계를 보여주는 시드: IT지원팀 김지원도 비품 앞에서는 '요청자'
        ("모니터 받침대 요청", "듀얼 모니터 설치용 받침대 1개가 필요합니다.", "비품", "낮음", "NEW", "김지원", None, 0),
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
    row = conn.execute("SELECT status FROM requests WHERE id=?", (request_id,)).fetchone()
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
    return True


def assign_handler(request_id, handler, actor):
    conn = get_conn()
    conn.execute("UPDATE requests SET handler=?, updated_at=? WHERE id=?", (handler, now_str(), request_id))
    add_activity(conn, request_id, actor, "ASSIGN", f"담당자 지정: {handler}")
    conn.commit()
    conn.close()


def add_comment(request_id, actor, text):
    conn = get_conn()
    add_activity(conn, request_id, actor, "COMMENT", text)
    conn.execute("UPDATE requests SET updated_at=? WHERE id=?", (now_str(), request_id))
    conn.commit()
    conn.close()


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
# 4. UI 컴포넌트
# ──────────────────────────────────────────────

def render_badge_row(r):
    return (
        f"{STATUS_LABELS[r['status']]} · {r['category']} · 우선순위 {r['priority']}"
        f" · 담당 {r['handler'] or '미지정'} · {r['created_at']}"
    )


def go_detail(rid):
    """목록 → 상세 이동. 위젯 key를 직접 갱신해 한 번의 클릭으로 전환."""
    st.session_state["selected_id"] = rid
    st.session_state["menu"] = "상세"


def page_create(user):
    st.subheader("📝 업무 요청하기")
    st.caption("무슨 일이 있는지 자유롭게 적어주세요. AI가 제목·분류·우선순위를 제안하고, 최종 결정은 직접 하시면 됩니다.")

    text = st.text_area("요청 내용", placeholder="예) 3층 회의실 모니터가 안 켜져요. 오후 2시에 고객 미팅이 있어서 급합니다.", height=120)

    col1, col2 = st.columns([1, 3])
    if col1.button("🤖 AI 분석", type="primary", disabled=not text.strip()):
        with st.spinner("AI가 요청을 분석하는 중..."):
            suggestion = ai_intake(text)
        # 재실행 대비: 제안 결과를 session_state에 보존
        st.session_state["ai_suggestion"] = suggestion
        st.session_state["ai_input_text"] = text
        st.session_state["ai_failed"] = suggestion is None

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
            submitted = st.form_submit_button("요청 제출", type="primary")
            if submitted:
                if not title.strip():
                    st.error("제목을 입력해주세요.")
                else:
                    body = st.session_state.get("ai_input_text", text)
                    rid = create_request(title, body, category, priority, user, suggestion)
                    st.session_state.pop("ai_suggestion", None)
                    st.session_state.pop("ai_failed", None)
                    st.success(f"요청 #{rid} 이(가) 접수되었습니다. '목록'에서 진행 상황을 확인하세요.")


def page_list(mode, user):
    if mode == "처리자 모드":
        st.subheader("📋 요청 목록")
        st.caption(f"{EMPLOYEES[user]} 소관 티켓을 처리할 수 있어요. 다른 팀 티켓은 조회만 가능합니다.")
    else:
        st.subheader("📋 내 요청")
    conn = get_conn()
    if mode == "처리자 모드":
        rows = conn.execute("SELECT * FROM requests ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM requests WHERE requester=? ORDER BY id DESC", (user,)).fetchall()
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
                    # on_click 콜백: 재실행 전에 상태가 갱신되어 한 번의 클릭으로 이동
                    c2.button("상세", key=f"detail_{status}_{r['id']}",
                              on_click=go_detail, args=(r["id"],))


def page_detail(mode, user):
    rid = st.session_state.get("selected_id")
    if not rid:
        st.info("요청 목록에서 항목을 선택해주세요.")
        return
    conn = get_conn()
    r = conn.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    acts = conn.execute("SELECT * FROM activities WHERE request_id=? ORDER BY id", (rid,)).fetchall()
    conn.close()

    st.button("← 목록으로", on_click=lambda: st.session_state.update(menu="목록"))

    st.subheader(f"#{r['id']} {r['title']}")
    st.caption(render_badge_row(r))
    st.markdown(f"> {r['body']}")
    st.markdown(f"**요청자:** {r['requester']} ({EMPLOYEES.get(r['requester'], '외부')}) · **담당팀:** {r['team']}")

    # 처리 권한: 처리자 모드 + 현재 사용자가 해당 티켓 담당팀 소속일 때
    can_handle = mode == "처리자 모드" and EMPLOYEES.get(user) == r["team"]
    if mode == "처리자 모드" and not can_handle:
        st.info(f"이 티켓의 담당팀은 {r['team']}입니다. {EMPLOYEES[user]} 소속인 {user} 님은 조회와 코멘트만 가능해요.")

    if can_handle:
        st.divider()
        c1, c2 = st.columns(2)
        with c1:
            # 담당자 후보 = 티켓 담당팀 소속 인원 (모든 부서에서 해당 팀이 처리 주체)
            candidates = TEAM_MEMBERS[r["team"]]
            handler = st.selectbox("담당자 지정", ["선택"] + candidates,
                                   index=(candidates.index(r["handler"]) + 1) if r["handler"] in candidates else 0)
            if handler != "선택" and handler != r["handler"]:
                if st.button("담당자 저장"):
                    assign_handler(rid, handler, user)
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
                        change_status(rid, new_status, user, reason)
                        st.rerun()
            else:
                st.caption("종결된 요청입니다. 더 이상 상태를 변경할 수 없습니다.")

    # 활동 타임라인 — 모든 이력이 요청 단위로 자동 축적됨 (P3 해결)
    st.divider()
    st.markdown("**활동 이력**")
    for a in acts:
        icon = {"CREATED": "🆕", "STATUS": "🔄", "ASSIGN": "👤", "COMMENT": "💬"}[a["type"]]
        st.markdown(f"{icon} `{a['created_at']}` **{a['actor']}** — {a['content']}")

    with st.form("comment_form", clear_on_submit=True):
        comment = st.text_input("코멘트 남기기")
        if st.form_submit_button("등록") and comment.strip():
            add_comment(rid, user, comment)
            st.rerun()


def page_dashboard():
    st.subheader("📊 현황 대시보드")
    st.caption("PoC 범위: 상태별 건수만 제공. 통계 고도화는 데이터 축적 후 확장 영역.")
    conn = get_conn()
    counts = {s: conn.execute("SELECT COUNT(*) FROM requests WHERE status=?", (s,)).fetchone()[0]
              for s in STATUS_LABELS}
    conn.close()
    cols = st.columns(len(STATUS_LABELS))
    for col, (s, label) in zip(cols, STATUS_LABELS.items()):
        col.metric(label, counts[s])


# ──────────────────────────────────────────────
# 5. 메인
# ──────────────────────────────────────────────

def main():
    st.set_page_config(page_title="Reqly — 사내 업무요청", page_icon="📮", layout="wide")
    init_db()

    with st.sidebar:
        st.title("📮 Reqly")
        st.caption("모든 요청을, 잃어버리지 않게.")

        # 로그인 대신 사용자 선택 — 모든 부서의 누구나 요청자가 될 수 있음
        user = st.selectbox("사용자 (로그인 대체)", list(EMPLOYEES.keys()), key="user",
                            format_func=lambda n: f"{n} · {EMPLOYEES[n]}")

        # 역할은 계정이 아니라 티켓과의 관계 → '모드'로 표현
        mode = st.radio("모드", ["요청자 모드", "처리자 모드"], key="mode")
        st.caption("역할은 계정이 아니라 티켓과의 관계예요. 누구나 요청자이면서, 자기 팀 티켓 앞에서는 처리자가 됩니다.")
        st.divider()

        pages = ["요청하기", "목록", "상세"] if mode == "요청자 모드" else ["목록", "상세", "대시보드"]
        # key 기반 라디오: index 재계산으로 인한 '두 번 클릭' 문제 방지.
        # 모드 전환으로 현재 메뉴가 유효하지 않을 때만 초기화.
        if st.session_state.get("menu") not in pages:
            st.session_state["menu"] = pages[0]
        page = st.radio("메뉴", pages, key="menu")

    if page == "요청하기":
        page_create(user)
    elif page == "목록":
        page_list(mode, user)
    elif page == "상세":
        page_detail(mode, user)
    elif page == "대시보드":
        page_dashboard()


if __name__ == "__main__":
    main()
