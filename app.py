import streamlit as st
import streamlit.components.v1 as components
import json
from supabase import create_client, Client
from google import genai
import pandas as pd
import re
import time
from datetime import datetime

# =========================================================================
# 1. ตั้งค่าการเชื่อมต่อ (Credentials) 
# =========================================================================
SUPABASE_URL = "https://tmotmvjjdxqexzpnqeuf.supabase.co"
SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]

# เชื่อมต่อ Supabase
@st.cache_resource
def init_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)

supabase: Client = init_supabase()

# เชื่อมต่อ Gemini API
ai_client = genai.Client(api_key=GEMINI_API_KEY)


# =========================================================================
# 2. ฟังก์ชันหลักสำหรับค้นหาข้อมูลและประมวลผลด้วย AI
# =========================================================================

def get_machines_data(machine_ids: list):
    """ฟังก์ชันดึงข้อมูลของเครื่องจักรหลายเครื่องพร้อมกันในรอบเดียว (ลด API Calls)"""
    try:
        # คำนวณโควต้าข้อมูลตามจำนวนเครื่อง (สมมติเครื่องละ 25 แถว)
        total_limit = len(machine_ids) * 25
        response = supabase.table("machines") \
                           .select("*") \
                           .in_("machine_id", machine_ids) \
                           .order('timestamp', desc=True) \
                           .limit(total_limit) \
                           .execute()
        
        return response.data if response.data else []
    except Exception as e:
        print(f"Supabase Error: {e}")
        return []

def ask_cwm_agent(user_question: str):
    """AI Agent สาย Data Analytics: ดึงข้อมูลแบบยืดหยุ่นและวิเคราะห์ Insights"""
    
    current_date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # 1. ค้นหาหมายเลขเครื่องที่ปรากฏในคำถาม
    mentioned_machines = [int(n) for n in re.findall(r'\d+', user_question)]
    available_machines = [1, 2, 7, 9] 
    valid_machines = [m for m in mentioned_machines if m in available_machines]
    
    # กำหนดเครื่องจักรเป้าหมาย
    target_machines = valid_machines if valid_machines else available_machines
    is_multi_machine = len(target_machines) > 1

    # 2. ดึงข้อมูลทีเดียวทั้งหมด (ลดเวลา Query)
    m_data = get_machines_data(target_machines)

    db_context = ""
    history_df = None

    if m_data:
        # แปลงเป็น DataFrame และจัดการ Data Type
        df = pd.DataFrame(m_data)
        df = df.drop_duplicates().reset_index(drop=True)
        df['production_qty'] = pd.to_numeric(df['production_qty'], errors='coerce')
        df['temperature'] = pd.to_numeric(df['temperature'], errors='coerce')
        df['operating_hours'] = pd.to_numeric(df['operating_hours'], errors='coerce')
        
        # เพิ่มคอลัมน์วิเคราะห์: อัตราการผลิตต่อชั่วโมง (ป้องกันหารด้วย 0)
        df['production_rate'] = df.apply(
            lambda x: x['production_qty'] / x['operating_hours'] if x['operating_hours'] > 0 else 0, 
            axis=1
        )
        
        history_df = df.sort_values('timestamp', ascending=False)
        
        # 3. สร้าง "Data Analytics Profile" ที่มีมิติการวิเคราะห์ลึกขึ้น
        summary_stats = df.groupby('machine_id').agg({
            'production_qty': ['sum', 'mean', 'max'],
            'production_rate': ['mean', 'max'],
            'temperature': ['mean', 'max', 'min', 'std'],
            'operating_hours': 'max'
        }).round(2).to_string()
        
        status_distribution = df.groupby(['machine_id', 'status']).size().to_string()
        shutdown_events = df[df['status'].astype(str).str.contains('หยุด|Down|Stop', case=False, na=False)].head(5).to_string()
        high_temp_events = df[df['temperature'] > 55].head(5).to_string()
        recent_logs = history_df.head(15).to_string()
        
        analytics_profile = f"""
        === FACTUAL DATA ANALYTICS PROFILE ===
        Report Generated: {current_date_str}
        Total Records: {len(df)}
        Machines Analyzed: {target_machines}
        
        [1. SUMMARY STATISTICS & PERFORMANCE]
        (Note: 'std' on temperature indicates stability. Higher std = highly fluctuating temp)
        {summary_stats}
        
        [2. MACHINE STATUS DISTRIBUTION]
        {status_distribution}
        
        [3. CRITICAL EVENTS: SHUTDOWNS]
        {shutdown_events if not 'Empty DataFrame' in shutdown_events else 'No shutdown events detected.'}
        
        [4. CRITICAL EVENTS: HIGH TEMP (>55°C)]
        {high_temp_events if not 'Empty DataFrame' in high_temp_events else 'No high temp events detected.'}
        
        [5. RECENT TIME-SERIES LOGS (TOP 15)]
        {recent_logs}
        ======================================
        """
        db_context = analytics_profile
    else:
        db_context = "ไม่พบข้อมูลใด ๆ ในระบบฐานข้อมูล MES สำหรับเครื่องจักรกลุ่มนี้"

    # 4. Prompt สำหรับ AI
    final_prompt = f"""
    คุณคือ Senior Data Analyst ประจำ Smart Factory หน้าที่ของคุณคือตอบคำถามวิศวกรโดยอ้างอิงจาก "DATA ANALYTICS PROFILE" ด้านล่าง
    
    กฎเหล็ก:
    1. ตอบให้ตรงประเด็น หากถามเปรียบเทียบ ให้วิเคราะห์หาเครื่องที่ดีที่สุด/แย่ที่สุด พร้อมเหตุผล
    2. ใช้ตัวเลขจาก Profile เท่านั้น ห้ามมโนตัวเลข หากวิเคราะห์ production_rate หรือ temp std ให้ยกมาอธิบายด้วยว่าส่งผลต่อประสิทธิภาพอย่างไร
    3. นำเสนอให้อ่านง่าย สั้น กระชับ ใช้ Bullet point หรือตัวหนา (Bold) เน้นจุดสำคัญ
    4. ห้ามใช้คำศัพท์ทางเทคนิคที่เยิ่นเย้อเกินไป ให้ตอบแบบวิศวกรคุยกัน
    
    ข้อมูลสรุปเพื่อการวิเคราะห์:
    {db_context}
    
    คำถามของผู้ใช้: "{user_question}"
    
    คำตอบ (ภาษาไทย):
    """
    
    # 5. ประมวลผลผ่าน LLM พร้อมระบบ Retry เมื่อ Server ทำงานหนัก
    max_retries = 3
    answer = "🤖 ขออภัยครับ ระบบไม่สามารถประมวลผล LLM ได้เนื่องจากเซิร์ฟเวอร์ API หนาแน่น โปรดลองพิมพ์ถามใหม่อีกครั้ง"
    
    for attempt in range(max_retries):
        try:
            final_response = ai_client.models.generate_content(
                model='gemini-2.5-flash', 
                contents=final_prompt,
            )
            answer = final_response.text
            break # สำเร็จแล้วให้ออกลูปทันที
            
        except Exception as e:
            error_msg = str(e)
            # ดักจับ Error 503 หรือ UNAVAILABLE ให้รอ 2 วิ แล้วทำซ้ำ
            if "503" in error_msg or "UNAVAILABLE" in error_msg:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                    
            answer = f"🤖 ขออภัยครับ ระบบไม่สามารถประมวลผล LLM ได้ในขณะนี้ Error: {error_msg}"
            break
            
    # ---> ส่วนที่หายไป: ต้อง return ค่าออกไปให้ UI ทำงานต่อ <---
    return {
        "answer": answer, 
        "dataframe": history_df, 
        "target_machines": target_machines,
        "is_multi": is_multi_machine
    }

QUICK_PROMPTS = [
    ("🏭 ภาพรวมโรงงาน",         "ภาพรวมสถานะโรงงานวันนี้เป็นยังไง?"),
    ("⚠️ เครื่องผิดปกติ",        "เครื่องไหนมีสถานะผิดปกติบ้าง?"),
    ("🛑 เครื่องหยุด",           "มีเครื่องไหนหยุดทำงานอยู่บ้าง?"),
    ("🌡️ อุณหภูมิสูง",          "เครื่องไหนมีอุณหภูมิสูงผิดปกติ?"),
    ("📊 เปรียบเทียบประสิทธิภาพ", "เปรียบเทียบประสิทธิภาพการผลิตของทุกเครื่อง"),
    ("🏆 ผู้นำการผลิต",          "เครื่องไหนผลิตได้เยอะที่สุด?"),
]

# =========================================================================
# 3. ส่วนการแสดงผลหน้าจอ (Streamlit UI)
# =========================================================================

# --- Theme Colors (แก้ที่นี่ที่เดียว) ---
COLOR_BG          = "#0e1117"   # พื้นหลัง app
COLOR_SECONDARY   = "#f0f2f6"   # พื้นหลังปุ่ม / sidebar
COLOR_TEXT        = "#31333f"   # สีตัวอักษร
COLOR_PRIMARY     = "#ff4b4b"   # accent / hover
COLOR_BORDER      = "rgba(49,51,63,0.2)"  # เส้นขอบ

st.set_page_config(page_title="CWM - Town Square", page_icon="🤖", layout="wide")

col_title, col_logo = st.columns([8, 1])
with col_title:
    st.title("CWM Research beteween Faculty of Information Technology and Digital Innovation, KMUTNB and NOVELBIZ")
    st.caption("CWM Research beteween Faculty of Information Technology and Digital Innovation, KMUTNB and NOVELBIZ")
with col_logo:
    # logo width 80px , use_container_width=True
    st.image("assets/logo.jpg", width=120 )

# สร้างส่วนจำลองกล่องแชต
if "messages" not in st.session_state:
    st.session_state.messages = []

# แสดงข้อความประวัติการแชต
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

llm_status = st.empty()

_prompts_json = json.dumps(
    [{"label": lb, "prompt": pr} for lb, pr in QUICK_PROMPTS],
    ensure_ascii=False
)

_css = (
    f"#cwm-sq-bar{{position:fixed;bottom:0;left:0;right:0;"
    f"background:{COLOR_BG};"
    f"border-top:1px solid {COLOR_BORDER};"
    f"padding:10px 20px 14px;"
    f"display:flex;flex-wrap:wrap;align-items:center;gap:8px;"
    f"z-index:1001}}"
    f".cwm-btn{{border-radius:20px;"
    f"border:1px solid {COLOR_BORDER};"
    f"background:{COLOR_SECONDARY};"
    f"color:{COLOR_TEXT};"
    f"font-size:.82rem;"
    f"padding:6px 14px;cursor:pointer;white-space:nowrap;font-family:inherit}}"
    f".cwm-btn:hover{{border-color:{COLOR_PRIMARY};color:{COLOR_PRIMARY}}}"
    f"[data-testid=stBottom]{{bottom:60px!important}}"
    f"section[data-testid=stMain] .block-container{{padding-bottom:180px!important}}"
    f"[data-testid=stStatusWidget]{{position:fixed;bottom:125px;left:45px;transform:translateX(-50%);}}"
)

components.html(f"""
<script>
(function() {{
    try {{
        var par = window.parent;
        var doc = par.document;
        if (!doc || !doc.body) return;

        var old = doc.getElementById("cwm-sq-bar");
        if (old) old.remove();

        par.cwmSend = function(text) {{
            var ta = doc.querySelector("[data-testid=stChatInputTextArea]");
            if (!ta) return;
            var s = Object.getOwnPropertyDescriptor(par.HTMLTextAreaElement.prototype, "value").set;
            s.call(ta, text);
            ta.dispatchEvent(new par.Event("input", {{bubbles: true}}));
            setTimeout(function() {{
                var b = doc.querySelector("[data-testid=stChatInputSubmitButton]");
                if (b) b.click();
            }}, 100);
        }};

        if (!doc.getElementById("cwm-sq-style")) {{
            var style = doc.createElement("style");
            style.id = "cwm-sq-style";
            style.textContent = "{_css}";
            doc.head.appendChild(style);
        }}

        var bar = doc.createElement("div");
        bar.id = "cwm-sq-bar";

        var lbl = doc.createElement("span");
        lbl.style.cssText = "font-size:.75rem;color:#888;white-space:nowrap";
        lbl.textContent = "💡 คำถาม :";
        bar.appendChild(lbl);

        var ps = {_prompts_json};
        ps.forEach(function(p) {{
            var btn = doc.createElement("button");
            btn.className = "cwm-btn";
            btn.textContent = p.label;
            btn.onclick = function() {{ par.cwmSend(p.prompt); }};
            bar.appendChild(btn);
        }});

        doc.body.appendChild(bar);

    }} catch(e) {{
        console.error("CWM bar error:", e);
    }}
}})();
</script>
""", height=0, scrolling=False)

user_input = st.chat_input("พิมพ์คำถาม เช่น 'ขอข้อมูลเครื่องจักร 7', 'เครื่องไหนอุณหภูมิแกว่งสุด?', 'ภาพรวมวันนี้เป็นยังไง?'")

if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    llm_status.markdown("🟢 **LLM กำลังประมวลผล...**")

    with st.chat_message("assistant"):
        result = ask_cwm_agent(user_input)

        st.markdown(result["answer"])

        if result["dataframe"] is not None:
            machines_str = ", ".join(map(str, result["target_machines"]))
            expander_title = f"📊 ดูบันทึกข้อมูลย้อนหลังของเครื่องจักรทั้งหมดที่เกี่ยวข้อง (ID: {machines_str})" if result["is_multi"] else f"📊 ดูบันทึกข้อมูลย้อนหลังของเครื่องจักรหมายเลข {result['target_machines'][0]}"

            with st.expander(expander_title):
                show_cols = ["timestamp", "machine_id", "status", "temperature", "operating_hours", "production_qty", "production_rate"]
                show_cols = [c for c in show_cols if c in result["dataframe"].columns]
                st.dataframe(result["dataframe"][show_cols], use_container_width=True)

    llm_status.empty()
    st.session_state.messages.append({"role": "assistant", "content": result["answer"]})