import streamlit as st
import sqlite3
import json
import pandas as pd
from datetime import datetime

# ==========================================
# 1. DATABASE SETUP
# ==========================================
def init_db():
    conn = sqlite3.connect("tasks.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_request TEXT NOT NULL,
            summary TEXT,
            category TEXT,
            priority TEXT,
            assigned_team TEXT,
            status TEXT DEFAULT '대기',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn

conn = init_db()

def insert_task(raw_request, summary, category, priority, assigned_team):
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO tasks (raw_request, summary, category, priority, assigned_team, status)
        VALUES (?, ?, ?, ?, ?, '대기')
    """, (raw_request, summary, category, priority, assigned_team))
    conn.commit()

def get_all_tasks():
    return pd.read_sql_query("SELECT * FROM tasks ORDER BY id DESC", conn)

def update_task_status(task_id, new_status):
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE tasks 
        SET status = ?, updated_at = ?
        WHERE id = ?
    """, (new_status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id))
    conn.commit()

# ==========================================
# 2. MOCK AI AGENT (FALLBACK LOGIC)
# ==========================================
# If API key is not configured, this smart heuristic logic categorizes requests.
# This ensures the PoC runs perfectly immediately without setup!
def mock_ai_triage(text):
    text_lower = text.lower()
    
    # Simple Rule-based Triage
    if any(word in text_lower for word in ["모니터", "마우스", "키보드", "맥북", "컴퓨터", "노트북", "인터넷", "와이파이", "wifi", "로그인", "계정"]):
        category = "IT 지원"
        assigned_team = "IT 지원팀"
        summary = f"IT 장비/계정 요청: {text[:15]}..."
    elif any(word in text_lower for word in ["에어컨", "히터", "난방", "전등", "형광등", "소방", "누수", "정수기", "문고리", "의자"]):
        category = "시설 관리"
        assigned_team = "총무/시설팀"
        summary = f"시설 보수 요청: {text[:15]}..."
    elif any(word in text_lower for word in ["명함", "필기구", "노트", "펜", "A4", "용지", "다이어리", "비품"]):
        category = "사무 비품"
        assigned_team = "구매/총무팀"
        summary = f"사무 비품 요청: {text[:15]}..."
    else:
        category = "기타 일반"
        assigned_team = "운영지원팀"
        summary = f"일반 문의: {text[:15]}..."
        
    # Priority Heuristics
    if any(word in text_lower for word in ["급함", "긴급", "고장", "안됨", "마비", "당장", "오늘내로", "에러", "경고"]):
        priority = "High"
    elif any(word in text_lower for word in ["조금", "언제든", "천천히", "참고"]):
        priority = "Low"
    else:
        priority = "Medium"
        
    return {
        "summary": summary,
        "category": category,
        "priority": priority,
        "assigned_team": assigned_team
    }

# ==========================================
# 3. REAL LLM AI AGENT (OPTIONAL)
# ==========================================
def real_ai_triage(text, api_key):
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        
        prompt = f"""
        You are an AI triage assistant for Company A's internal helpdesk.
        Analyze the following unstructured employee request and extract structured details.
        
        Employee Request: "{text}"
        
        Respond ONLY with a valid JSON object matching this schema. Do not include any markdown, code blocks, or extra explanation. Just raw JSON.
        {{
            "summary": "Brief 5-10 word summary in Korean",
            "category": "One of: IT 지원, 시설 관리, 사무 비품, 기타 일반",
            "priority": "One of: High, Medium, Low (Use 'High' if it blocks work completely)",
            "assigned_team": "One of: IT 지원팀, 총무/시설팀, 구매/총무팀, 운영지원팀"
        }}
        """
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1
        )
        
        result_text = response.choices[0].message.content.strip()
        # Clean potential markdown formatting
        if result_text.startswith("```json"):
            result_text = result_text.split("```json")[1].split("```")[0].strip()
        elif result_text.startswith("```"):
            result_text = result_text.split("```")[1].split("```")[0].strip()
            
        return json.loads(result_text)
    except Exception as e:
        st.warning(f"실제 LLM 호출 실패 (Fallback 작동): {e}")
        return mock_ai_triage(text)


# ==========================================
# 4. STREAMLIT UI DESIGN
# ==========================================
st.set_page_config(page_title="A사 사내 업무요청 AI Agent PoC", page_icon="🤖", layout="wide")

# Sidebar Configuration
st.sidebar.title("🛠️ 시스템 설정")
st.sidebar.markdown("---")
api_mode = st.sidebar.selectbox("AI 작동 모드", ["Mock AI (즉시 실행 가능)", "OpenAI GPT-4o-mini"])
api_key = ""

if api_mode == "OpenAI GPT-4o-mini":
    api_key = st.sidebar.text_input("OpenAI API Key 입력", type="password", help="발급받은 OpenAI API Key를 입력하세요.")
    if not api_key:
        st.sidebar.info("🔑 API Key를 입력하기 전까지는 모의(Mock) AI로 동작합니다.")

st.sidebar.markdown("""
### 📌 PoC 안내
**KAIST 컴공 출신 기획자**의 핵심 컨셉:
- **Zero-Form Entry**: 직원은 상세 양식 없이 자연어 한 줄로 요청을 작성합니다.
- **AI Auto-Triage**: AI가 문맥을 분석해 제목 요약, 카테고리 분류, 우선순위, 담당 부서를 자동 매핑합니다.
- **SQLite DB**: 로컬 파일 시스템에 실시간 적재되어 영구 보존됩니다.
""")

# Main Content Header
st.title("🤖 A사 사내 업무요청 자동화 서비스 (PoC)")
st.caption("사내 메일/메신저 누락 방지를 위한 AI Agent 기반의 자동 분류 및 추적 시스템")

# Tab Navigation
tab1, tab2 = st.tabs(["✍️ 직원 요청 등록 (Employee)", "📊 담당자 대시보드 (Manager)"])

# -----------------
# TAB 1: 직원 요청 등록
# -----------------
with tab1:
    st.subheader("📝 무엇을 도와드릴까요?")
    st.info("💡 메일이나 메신저로 보내던 내용을 아래에 자유롭게 작성해 주세요. AI가 자동으로 분류하여 담당 부서에 전달합니다.")
    
    with st.form("request_form", clear_on_submit=True):
        raw_input = st.text_area(
            "요청 사항 입력", 
            placeholder="예시 1: 제 자리 모니터 전원이 안 켜져요. 교체 부탁드립니다.
예시 2: 3층 회의실 에어컨 바람이 너무 약해서 더워요. 필터 청소 좀 해주세요.",
            height=150
        )
        submit_btn = st.form_submit_button("요청 제출하기")
        
        if submit_btn:
            if not raw_input.strip():
                st.error("요청 내용을 입력해주세요.")
            else:
                with st.spinner("AI 에이전트가 요청을 분석하고 분류하는 중..."):
                    # Triage Run
                    if api_mode == "OpenAI GPT-4o-mini" and api_key:
                        triage_result = real_ai_triage(raw_input, api_key)
                    else:
                        triage_result = mock_ai_triage(raw_input)
                    
                    # Insert to DB
                    insert_task(
                        raw_request=raw_input,
                        summary=triage_result.get("summary", "요약 없음"),
                        category=triage_result.get("category", "기타 일반"),
                        priority=triage_result.get("priority", "Medium"),
                        assigned_team=triage_result.get("assigned_team", "운영지원팀")
                    )
                    
                    st.success("🎉 요청이 성공적으로 등록되었습니다!")
                    
                    # Show analyzed result in cards
                    col1, col2, col3, col4 = st.columns(4)
                    with col1:
                        st.metric(label="📄 AI 자동 요약", value=triage_result.get("summary"))
                    with col2:
                        st.metric(label="🏷️ 카테고리 분류", value=triage_result.get("category"))
                    with col3:
                        st.metric(label="🚨 우선순위", value=triage_result.get("priority"))
                    with col4:
                        st.metric(label="👥 담당 배정 부서", value=triage_result.get("assigned_team"))

# -----------------
# TAB 2: 담당자 대시보드
# -----------------
with tab2:
    st.subheader("📊 사내 업무요청 현황판")
    
    # Reload and Get Data
    df = get_all_tasks()
    
    if df.empty:
        st.write("등록된 요청 사항이 없습니다. 첫 번째 탭에서 요청을 먼저 등록해 보세요!")
    else:
        # Simple Metrics Summary
        m_col1, m_col2, m_col3, m_col4 = st.columns(4)
        m_col1.metric("총 요청 건수", len(df))
        m_col2.metric("대기 중", len(df[df['status'] == '대기']))
        m_col3.metric("진행 중", len(df[df['status'] == '진행중']))
        m_col4.metric("완료됨", len(df[df['status'] == '완료']))
        
        st.markdown("---")
        
        # Grid View for updating task status
        for index, row in df.iterrows():
            # Styling based on priority
            color_badge = "🔴" if row['priority'] == "High" else "🟡" if row['priority'] == "Medium" else "🟢"
            
            with st.expander(f"{color_badge} [{row['category']}] {row['summary']} | 상태: {row['status']} (담당: {row['assigned_team']})"):
                st.markdown(f"**원본 요청 사항:**\n{row['raw_request']}")
                st.write(f"*등록 시간: {row['created_at']} | 최근 수정: {row['updated_at']}*")
                
                # Update Status Action
                cols = st.columns([2, 1, 1, 1])
                with cols[0]:
                    status_options = ['대기', '진행중', '완료']
                    try:
                        current_idx = status_options.index(row['status'])
                    except ValueError:
                        current_idx = 0
                    
                    new_status = st.selectbox(
                        "상태 변경", 
                        status_options, 
                        index=current_idx, 
                        key=f"status_select_{row['id']}"
                    )
                
                with cols[1]:
                    st.write("") # padding
                    st.write("") # padding
                    if st.button("적용", key=f"apply_btn_{row['id']}"):
                        update_task_status(row['id'], new_status)
                        st.success(f"ID #{row['id']} 상태가 '{new_status}'(으)로 변경되었습니다!")
                        st.rerun()

# DB Connection Close (optional because sqlite usually handles it, but good practice)
