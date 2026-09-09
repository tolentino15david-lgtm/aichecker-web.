import os
import re
import json
import sqlite3
import base64
import mimetypes
import requests
import csv
import io
import statistics

from flask import Flask, render_template, render_template_string, request, redirect, url_for, flash, Response, session
from werkzeug.utils import secure_filename

app = Flask(__name__, template_folder='.')
app.secret_key = "yhelchecker_secret_key_navotas"
DB_NAME = "yhelchecker.db"

# ---------------------------------------------------------
# GEMINI API KEY SETUP
# ---------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6Kt3rOcefXK20yV3TdzLdDwgs_Oe2YZQSvgYayHwX3OJQ")

# ---------------------------------------------------------
# DATABASE INITIALIZATION
# ---------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT,
                lrn TEXT,
                section TEXT,
                category TEXT,
                score INTEGER DEFAULT 0,
                total_questions INTEGER DEFAULT 60,
                error_details TEXT,
                image_path TEXT,
                status TEXT DEFAULT 'Pending',
                secret_token TEXT
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS answer_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                section_cat TEXT UNIQUE,
                key_text TEXT
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS student_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT,
                lrn TEXT,
                section TEXT,
                secret_token TEXT UNIQUE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS student_report_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT NOT NULL,
                lrn TEXT,
                section TEXT NOT NULL,
                track TEXT DEFAULT 'ACADEMIC - STEM',
                subject_name TEXT NOT NULL,
                q1 REAL DEFAULT NULL,
                q2 REAL DEFAULT NULL,
                q3 REAL DEFAULT NULL,
                q4 REAL DEFAULT NULL
            )
        ''')
        
        cursor.execute("INSERT OR IGNORE INTO sections (name) VALUES ('Grade 12 - STEM A')")
        cursor.execute("INSERT OR IGNORE INTO sections (name) VALUES ('Grade 12 - STEM B')")
        conn.commit()

init_db()

# ---------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------
def normalize_answer_key(key_str):
    if not key_str:
        return ""
    cleaned = re.sub(r'[^a-zA-Z0-9\s]', '', key_str)
    return " ".join(cleaned.split())

def get_or_create_student_token(name, lrn, section):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT secret_token FROM student_tokens WHERE student_name = ? AND section = ?', (name, section))
        row = cursor.fetchone()
        if row:
            return row['secret_token']
        
        token = base64.b32encode(os.urandom(5)).decode('utf-8')[:8]
        cursor.execute('INSERT INTO student_tokens (student_name, lrn, section, secret_token) VALUES (?, ?, ?, ?)',
                       (name, lrn, section, token))
        conn.commit()
        return token

def compute_frequency_data(submissions):
    total_graded = len([s for s in submissions if s['status'] == 'Graded'])
    if total_graded == 0:
        return []

    item_errors = {i: 0 for i in range(1, 61)}

    for sub in submissions:
        if sub['status'] == 'Graded' and sub['error_details']:
            errors = re.findall(r'\b\d+\b', sub['error_details'])
            for err in errors:
                item_num = int(err)
                if 1 <= item_num <= 60:
                    item_errors[item_num] += 1

    freq_list = []
    for item in range(1, 61):
        err_count = item_errors[item]
        correct_count = total_graded - err_count
        accuracy = round((correct_count / total_graded) * 100, 1)
        freq_list.append({
            "item": item,
            "total": total_graded,
            "correct": correct_count,
            "errors": err_count,
            "accuracy": accuracy
        })

    return freq_list

def compute_deped_summary(submissions):
    students = {}
    for sub in submissions:
        if sub['status'] != 'Graded':
            continue
        key = (sub['student_name'], sub['section'])
        if key not in students:
            students[key] = {'WW': [], 'PT': [], 'QA': []}
        
        cat = sub['category']
        if cat in students[key]:
            pct = (sub['score'] / sub['total_questions']) * 100 if sub['total_questions'] > 0 else 0
            students[key][cat].append(pct)

    summary = []
    for (name, section), grades in students.items():
        ww_pct = round(sum(grades['WW']) / len(grades['WW']), 1) if grades['WW'] else 0.0
        pt_pct = round(sum(grades['PT']) / len(grades['PT']), 1) if grades['PT'] else 0.0
        qa_pct = round(sum(grades['QA']) / len(grades['QA']), 1) if grades['QA'] else 0.0

        weighted = round((ww_pct * 0.30) + (pt_pct * 0.50) + (qa_pct * 0.20), 1)
        transmuted = round(60 + (weighted * 0.4), 0) if weighted > 0 else 60

        summary.append({
            'name': name,
            'section': section,
            'grades': {
                'ww_pct': ww_pct,
                'pt_pct': pt_pct,
                'qa_pct': qa_pct,
                'weighted_pct': weighted,
                'transmuted': int(transmuted)
            }
        })
    return summary

def call_gemini_vision(image_path, answer_key):
    if not GEMINI_API_KEY:
        return {"success": False, "error": "Missing Gemini API Key."}

    if not answer_key:
        return {"success": False, "error": "Empty Answer Key. Mag-set muna ng key sa Settings."}

    if not os.path.exists(image_path):
        return {"success": False, "error": f"Image file not found at path: {image_path}"}

    mime_type, _ = mimetypes.guess_type(image_path)
    if not mime_type:
        mime_type = "image/jpeg"

    formatted_key = normalize_answer_key(answer_key)

    try:
        with open(image_path, "rb") as img_file:
            img_b64 = base64.b64encode(img_file.read()).decode('utf-8')

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
        
        prompt = f"""
        You are an advanced Optical Mark Recognition (OMR) and handwriting evaluator.
        Compare the student's answers (either handwritten text or shaded A/B/C/D bubbles on a 60-item sheet) with this Master Answer Key:
        "{formatted_key}"

        Instructions:
        1. Extract the Student Name and LRN in the header if visible.
        2. Read all shaded choices or handwritten answers.
        3. Count total correct answers based on the Master Key as 'score'.
        4. List ONLY the item numbers that were incorrect, missed, or double-shaded as a comma-separated string (e.g. "1, 4, 12").

        Return JSON ONLY in this exact structure:
        {{
            "student_name": "<Extracted Name or null>",
            "lrn": "<Extracted LRN or null>",
            "score": <integer score>,
            "total": 60,
            "errors": "<comma-separated wrong item numbers>"
        }}
        """

        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type, "data": img_b64}}
                ]
            }],
            "generationConfig": {"response_mime_type": "application/json"}
        }

        res = requests.post(url, json=payload, timeout=30)
        res_data = res.json()
        
        if 'error' in res_data:
            return {"success": False, "error": res_data['error'].get('message', 'API Request Failed')}

        text_response = res_data['candidates'][0]['content']['parts'][0]['text']
        match = re.search(r'\{.*\}', text_response, re.DOTALL)
        parsed = json.loads(match.group(0) if match else text_response)
        parsed["success"] = True
        return parsed

    except Exception as e:
        return {"success": False, "error": str(e)}

# ---------------------------------------------------------
# HTML STYLES & TEMPLATES
# ---------------------------------------------------------
COMMON_STYLE = '''
<style>
    @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap');
    
    :root {
        --bg-dark: #070a13;
        --card-bg: rgba(18, 26, 47, 0.75);
        --card-border: rgba(255, 255, 255, 0.08);
        --cyan-glow: #00f2fe;
        --purple-glow: #9d4edd;
        --green-neon: #10b981;
        --red-danger: #ef4444;
        --text-white: #f8fafc;
        --text-muted: #94a3b8;
    }

    body { margin: 0; font-family: 'Outfit', sans-serif; background-color: var(--bg-dark); color: var(--text-white); min-height: 100vh; }

    .nav-bar { background: rgba(10, 14, 26, 0.8); backdrop-filter: blur(12px); border-bottom: 1px solid var(--card-border); padding: 15px 25px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 100; }
    .burger-btn { background: none; border: none; color: var(--cyan-glow); font-size: 20px; cursor: pointer; display: flex; align-items: center; gap: 10px; font-weight: 700; }

    .sidebar { position: fixed; top: 0; left: -280px; width: 280px; height: 100%; background: rgba(10, 14, 26, 0.95); backdrop-filter: blur(15px); border-right: 1px solid var(--card-border); transition: 0.3s ease; z-index: 200; padding: 25px 20px; box-sizing: border-box; }
    .sidebar.active { left: 0; }
    .sidebar-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0, 0, 0, 0.6); display: none; z-index: 150; }
    .sidebar-overlay.active { display: block; }

    .sidebar a { display: block; color: var(--text-muted); text-decoration: none; padding: 12px; margin-bottom: 8px; border-radius: 8px; font-size: 14px; transition: 0.2s; }
    .sidebar a:hover { background: rgba(255, 255, 255, 0.05); color: var(--cyan-glow); }

    .container { max-width: 1200px; margin: 30px auto; padding: 0 20px; }
    .glass-card { background: var(--card-bg); border: 1px solid var(--card-border); backdrop-filter: blur(16px); border-radius: 16px; padding: 25px; margin-bottom: 25px; box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37); }

    .tab-content { display: none; }
    .tab-content.active { display: block; }

    table { width: 100%; border-collapse: collapse; margin-top: 15px; }
    th, td { padding: 12px; text-align: left; border-bottom: 1px solid var(--card-border); font-size: 13px; }
    th { color: var(--text-muted); font-weight: 600; text-transform: uppercase; font-size: 11px; }

    .btn { padding: 8px 16px; border-radius: 8px; border: none; font-weight: 600; cursor: pointer; font-size: 12px; text-decoration: none; display: inline-block; transition: 0.2s; }
    .btn-cyan { background: linear-gradient(135deg, #00f2fe, #4facfe); color: #000; }
    .btn-purple { background: linear-gradient(135deg, #7b2cbf, #9d4edd); color: #fff; }
    .btn-green { background: linear-gradient(135deg, #059669, #10b981); color: #fff; }
    .btn-red { background: linear-gradient(135deg, #dc2626, #ef4444); color: #fff; }
    .btn-dark { background: rgba(255,255,255,0.05); color: var(--text-muted); border: 1px solid var(--card-border); }

    .badge { padding: 4px 10px; border-radius: 20px; font-size: 10px; font-weight: 700; }
    .badge-gold { background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid #f59e0b; }
    .badge-green { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #10b981; }
    .badge-purple { background: rgba(157, 78, 221, 0.2); color: #c084fc; border: 1px solid #9d4edd; }

    .score-input, .remark-input { background: rgba(0, 0, 0, 0.4); border: 1px solid var(--card-border); color: #fff; padding: 6px; border-radius: 6px; font-family: inherit; }
    .score-input { width: 50px; text-align: center; }
    .remark-input { width: 120px; }

    .accordion-header { background: rgba(255,255,255,0.02); border: 1px solid var(--card-border); padding: 15px 20px; border-radius: 12px; margin-bottom: 10px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; }

    .speech-box { background: rgba(157, 78, 221, 0.1); border: 1px dashed var(--purple-glow); padding: 15px; border-radius: 12px; margin-bottom: 20px; }
    .listening-pulse { display: inline-block; width: 10px; height: 10px; background: #ef4444; border-radius: 50%; margin-right: 8px; animation: pulse 1s infinite; }
    @keyframes pulse { 0% { opacity: 1; transform: scale(1); } 50% { opacity: 0.4; transform: scale(1.3); } 100% { opacity: 1; transform: scale(1); } }
</style>
'''

TEACHER_DASHBOARD_HTML = COMMON_STYLE + '''
<!DOCTYPE html>
<html>
<head>
    <title>HUSaYmetrics - Teacher Portal & Voice ECR</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
    <div class="sidebar-overlay" id="overlay" onclick="toggleSidebar()"></div>
    
    <div class="sidebar" id="sidebar">
        <h3 style="margin-top:0; color: var(--cyan-glow);">YHELCHECKER AI</h3>
        <hr style="border-color: var(--card-border); margin-bottom: 20px;">
        <a href="#" onclick="switchTab('ecr-voice-tab')">🎙️ Electronic Class Record (Speech-to-Text)</a>
        <a href="#" onclick="switchTab('class-records')">📚 Class Records & Grading</a>
        <a href="#" onclick="switchTab('roster-tab')">👥 Class Roster & Tokens</a>
        <a href="#" onclick="switchTab('report-cards')">📝 Report Card Editor</a>
        <a href="#" onclick="switchTab('print-center')">🖨️ Print Center (OMR & QR)</a>
        <a href="#" onclick="switchTab('deped-engine')">📊 DepEd Weighted Engine</a>
        <a href="#" onclick="switchTab('analytics-tab')">📈 Visual Analytics & Item Chart</a>
        <a href="#" onclick="switchTab('sections-tab')">🏷️ Manage Sections</a>
        <a href="/section-analytics" class="btn btn-info">📊 View Section Analytics</a>
        <a href="#" onclick="switchTab('bulk-scanner')">⚡ Bulk Test Scanner</a>
        <a href="#" onclick="switchTab('keys-tab')">⚙️ Answer Key Settings</a>
    </div>

    <div class="nav-bar">
        <button class="burger-btn" onclick="toggleSidebar()">☰ <span>TEACHER PORTAL</span></button>
        <span class="badge badge-gold">DepEd OMR & Voice ECR Active</span>
    </div>

    <div class="container">
        {% with messages = get_flashed_messages() %}
          {% if messages %}
            <div style="background: rgba(16,185,129,0.2); border: 1px solid var(--green-neon); color: #6ee7b7; padding: 12px; border-radius: 8px; font-size: 13px; margin-bottom: 15px;">
              {{ messages[0] }}
            </div>
          {% endif %}
        {% endwith %}

        <!-- TAB 0: ELECTRONIC CLASS RECORD (ECR WITH SPEECH-TO-TEXT) -->
        <div id="ecr-voice-tab" class="tab-content active">
            <div class="glass-card">
                <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
                    <div>
                        <h2 style="margin:0; color:var(--cyan-glow);">🎙️ Electronic Class Record (Speech-to-Text)</h2>
                        <p style="color:var(--text-muted); font-size:13px; margin-top:5px;">Dictate student grades directly using your microphone. Tagalog / English support.</p>
                    </div>
                    <button type="button" class="btn btn-purple" onclick="toggleSpeechRecognition()" id="mic-toggle-btn" style="font-size:14px; padding:10px 18px;">
                        🎤 Start Voice Dictation
                    </button>
                </div>

                <div class="speech-box" id="speech-box" style="margin-top:20px;">
                    <div style="display:flex; align-items:center; gap:8px;">
                        <span id="mic-indicator" style="display:none;" class="listening-pulse"></span>
                        <strong id="speech-status" style="font-size:13px; color:var(--text-muted);">Status: Ready. Click button to speak.</strong>
                    </div>
                    <p style="font-size:11px; color:var(--text-muted); margin:8px 0 4px 0;"><strong>Halimbawa ng Command:</strong> "Juan Cruz Science 88 90 85 92" o kaya "Maria Santos Math 90 92"</p>
                    <input type="text" id="speech-transcript" placeholder="Lalabas dito ang sasabihin mo..." readonly style="width:100%; padding:10px; background:rgba(0,0,0,0.5); border:1px solid var(--card-border); color:var(--cyan-glow); font-size:14px; border-radius:8px; box-sizing:border-box;">
                </div>

                <form action="/teacher/add-grade" method="POST" id="ecr-voice-form" style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; margin-top:15px;">
                    <div>
                        <label style="font-size:11px; color:var(--text-muted);">Pangalan ng Estudyante</label>
                        <input type="text" name="student_name" id="ecr_student_name" placeholder="Student Name" required class="remark-input" style="width:100%; box-sizing:border-box;">
                    </div>
                    <div>
                        <label style="font-size:11px; color:var(--text-muted);">Section</label>
                        <select name="section" required class="remark-input" style="width:100%; box-sizing:border-box;">
                            {% for sec in sections %}<option value="{{ sec['name'] }}">{{ sec['name'] }}</option>{% endfor %}
                        </select>
                    </div>
                    <div>
                        <label style="font-size:11px; color:var(--text-muted);">LRN (Optional)</label>
                        <input type="text" name="lrn" id="ecr_lrn" placeholder="LRN" class="remark-input" style="width:100%; box-sizing:border-box;">
                    </div>
                    <div>
                        <label style="font-size:11px; color:var(--text-muted);">Subject Name</label>
                        <input type="text" name="subject_name" id="ecr_subject" placeholder="Subject (e.g. Research, Math)" required class="remark-input" style="width:100%; box-sizing:border-box;">
                    </div>
                    <div>
                        <label style="font-size:11px; color:var(--text-muted);">Q1 Grade</label>
                        <input type="number" step="0.01" name="q1" id="ecr_q1" placeholder="Q1 Grade" class="remark-input" style="width:100%; box-sizing:border-box;">
                    </div>
                    <div>
                        <label style="font-size:11px; color:var(--text-muted);">Q2 Grade</label>
                        <input type="number" step="0.01" name="q2" id="ecr_q2" placeholder="Q2 Grade" class="remark-input" style="width:100%; box-sizing:border-box;">
                    </div>
                    <div>
                        <label style="font-size:11px; color:var(--text-muted);">Q3 Grade</label>
                        <input type="number" step="0.01" name="q3" id="ecr_q3" placeholder="Q3 Grade" class="remark-input" style="width:100%; box-sizing:border-box;">
                    </div>
                    <div>
                        <label style="font-size:11px; color:var(--text-muted);">Q4 Grade</label>
                        <input type="number" step="0.01" name="q4" id="ecr_q4" placeholder="Q4 Grade" class="remark-input" style="width:100%; box-sizing:border-box;">
                    </div>
                    <div style="display:flex; align-items:flex-end;">
                        <button type="submit" class="btn btn-green" style="width:100%; padding:10px;">💾 SAVE VOICE ECR RECORD</button>
                    </div>
                </form>
            </div>
        </div>

        <!-- TAB 1: CLASS RECORDS -->
        <div id="class-records" class="tab-content">
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom: 20px;">
                <h2 style="margin:0;">Class Records Management</h2>
                <div style="display:flex; gap:10px;">
                    <a href="/teacher/export-csv-all" class="btn btn-green">🌐 EXPORT ALL SECTIONS (UNIVERSAL CSV)</a>
                    <form action="/teacher/clear-all" method="POST" onsubmit="return confirm('Sigurado ka bang gusto mong burahin ang LAHAT ng submissions sa lahat ng sections?');">
                        <button type="submit" class="btn btn-red">⚠️ CLEAR ALL SUBMISSIONS</button>
                    </form>
                </div>
            </div>
            
            {% for sec in sections %}
            {% set sec_name = sec['name'] %}
            {% set sec_subs = [] %}
            {% for sub in submissions if sub['section'] == sec_name %}
                {% set _ = sec_subs.append(sub) %}
            {% endfor %}

            <div class="accordion-header" onclick="toggleAccordion('sec-{{ loop.index }}')">
                <div>
                    <strong style="font-size: 18px; color: var(--text-white);">{{ sec_name }}</strong>
                    <span style="font-size: 12px; color: var(--text-muted); margin-left: 10px;">({{ sec_subs|length }} Test Papers)</span>
                </div>
                <div>
                    <span class="btn btn-dark" style="padding: 4px 10px;">TOGGLE ▾</span>
                </div>
            </div>

            <div class="accordion-body" id="sec-{{ loop.index }}">
                <div class="glass-card">
                    <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:15px;">
                        <div>
                            <a href="/teacher/ai-check/{{ sec_name }}" class="btn btn-cyan">⚡ RUN AI CHECKER FOR PENDING</a>
                            <a href="/teacher/export-csv/{{ sec_name }}" class="btn btn-green">📥 EXPORT SECTION CSV</a>
                        </div>
                        <form action="/teacher/clear-section/{{ sec_name }}" method="POST" onsubmit="return confirm('Sigurado ka bang gusto mong burahin ang lahat ng submissions sa {{ sec_name }}?');">
                            <button type="submit" class="btn btn-red" style="padding:6px 12px;">🗑️ CLEAR SECTION DATA</button>
                        </form>
                    </div>

                    <div style="overflow-x:auto;">
                        <table>
                            <thead>
                                <tr>
                                    <th>Rank</th>
                                    <th>Student Name</th>
                                    <th>Category</th>
                                    <th>Score / Total</th>
                                    <th>Error Item Remarks</th>
                                    <th>Status</th>
                                    <th>Secret Token</th>
                                    <th>Action</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for sub in sec_subs %}
                                <tr>
                                    <td>#{{ loop.index }}</td>
                                    <td><strong>{{ sub['student_name'] }}</strong><br><small style="color:var(--text-muted);">LRN: {{ sub['lrn'] or 'N/A' }}</small></td>
                                    <td><span class="badge badge-purple">{{ sub['category'] }}</span></td>
                                    <form action="/teacher/update-score/{{ sub['id'] }}" method="POST">
                                        <td>
                                            <input type="number" name="score" value="{{ sub['score'] }}" class="score-input"> / 
                                            <input type="number" name="total" value="{{ sub['total_questions'] }}" class="score-input">
                                        </td>
                                        <td>
                                            <input type="text" name="errors" value="{{ sub['error_details'] or '' }}" class="remark-input">
                                        </td>
                                        <td>
                                            {% if sub['status'] == 'Graded' %}
                                            <span class="badge badge-green">GRADED</span>
                                            {% else %}
                                            <span class="badge badge-gold">PENDING</span>
                                            {% endif %}
                                        </td>
                                        <td><code>{{ sub['secret_token'] or 'None' }}</code></td>
                                        <td>
                                            <button type="submit" class="btn btn-green" style="padding: 4px 8px;">SAVE</button>
                                    </form>
                                            <form action="/teacher/delete-sub/{{ sub['id'] }}" method="POST" style="display:inline;" onsubmit="return confirm('Burahin ang submission na ito?');">
                                                <button type="submit" class="btn btn-red" style="padding: 4px 8px;">🗑️</button>
                                            </form>
                                        </td>
                                </tr>
                                {% else %}
                                <tr><td colspan="8" style="text-align:center; color: var(--text-muted);">No submissions for this section yet.</td></tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
            {% endfor %}
        </div>

        <!-- TAB: CLASS ROSTER & TOKEN GENERATOR -->
        <div id="roster-tab" class="tab-content">
            <div class="glass-card">
                <h3>👥 Class Roster & Student Token Generator</h3>
                <p style="color:var(--text-muted); font-size:13px;">Mag-add ng mga estudyante bago pa man mag-exam para magkaroon na sila agad ng unique Secret Token at QR Code.</p>
                
                <form action="/teacher/add-roster" method="POST" style="margin-top:15px;">
                    <div style="margin-bottom:12px;">
                        <label style="font-size:11px; color:var(--text-muted); font-weight:700;">SELECT SECTION</label>
                        <select name="section" required style="width:100%; margin-top:5px; padding:8px; background:rgba(0,0,0,0.4); color:white; border-radius:6px; border:1px solid var(--card-border);">
                            {% for sec in sections %}
                            <option value="{{ sec['name'] }}">{{ sec['name'] }}</option>
                            {% endfor %}
                        </select>
                    </div>

                    <div style="margin-bottom:15px;">
                        <label style="font-size:11px; color:var(--text-muted); font-weight:700;">LISTAHAN NG MGA ESTUDYANTE (Isang pangalan bawat linya)</label>
                        <textarea name="students_list" rows="6" placeholder="Juan Cruz&#10;Maria Santos, 123456789012&#10;Pedro Penduko" required style="width:100%; margin-top:5px; padding:10px; background:rgba(0,0,0,0.4); color:white; border-radius:6px; border:1px solid var(--card-border); font-family:inherit; box-sizing:border-box;"></textarea>
                        <small style="color:var(--text-muted);">Format: <code>Pangalan</code> o <code>Pangalan, LRN</code></small>
                    </div>

                    <button type="submit" class="btn btn-cyan">⚡ GENERATE TOKENS FOR ROSTER</button>
                </form>
            </div>

            <div class="glass-card">
                <h3>📋 Existing Student Tokens per Section</h3>
                {% for sec in sections %}
                <h4 style="color:var(--cyan-glow); margin-top:20px;">{{ sec['name'] }}</h4>
                <div style="overflow-x:auto;">
                    <table>
                        <thead>
                            <tr>
                                <th>#</th>
                                <th>Student Name</th>
                                <th>LRN</th>
                                <th>Secret Token</th>
                                <th>Action</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% set count = namespace(val=0) %}
                            {% for st in all_tokens if st['section'] == sec['name'] %}
                            {% set count.val = count.val + 1 %}
                            <tr>
                                <td>{{ count.val }}</td>
                                <td><strong>{{ st['student_name'] }}</strong></td>
                                <td>{{ st['lrn'] or 'N/A' }}</td>
                                <td><code style="color:var(--cyan-glow); font-weight:bold;">{{ st['secret_token'] }}</code></td>
                                <td>
                                    <form action="/teacher/delete-token/{{ st['id'] }}" method="POST" style="display:inline;" onsubmit="return confirm('Burahin ang token para kay {{ st['student_name'] }}?');">
                                        <button type="submit" class="btn btn-red" style="padding:2px 8px; font-size:10px;">🗑️ Delete</button>
                                    </form>
                                </td>
                            </tr>
                            {% else %}
                            <tr><td colspan="5" style="text-align:center; color:var(--text-muted);">Walang registered tokens sa section na ito.</td></tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
                {% endfor %}
            </div>
        </div>

        <!-- TAB: REPORT CARD EDITOR -->
        <div id="report-cards" class="tab-content">
            <div class="glass-card">
                <h3>📝 Manual Report Card Entry</h3>
                <form action="/teacher/add-grade" method="POST" style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px;">
                    <input type="text" name="student_name" placeholder="Student Name" required class="remark-input" style="width:auto;">
                    <select name="section" required class="remark-input" style="width:auto;">
                        {% for sec in sections %}<option value="{{ sec['name'] }}">{{ sec['name'] }}</option>{% endfor %}
                    </select>
                    <input type="text" name="lrn" placeholder="LRN (Optional)" class="remark-input" style="width:auto;">
                    <input type="text" name="subject_name" placeholder="Subject Name (e.g. Science)" required class="remark-input" style="width:auto;">
                    <input type="number" step="0.01" name="q1" placeholder="Q1 Grade" class="remark-input" style="width:auto;">
                    <input type="number" step="0.01" name="q2" placeholder="Q2 Grade" class="remark-input" style="width:auto;">
                    <input type="number" step="0.01" name="q3" placeholder="Q3 Grade" class="remark-input" style="width:auto;">
                    <input type="number" step="0.01" name="q4" placeholder="Q4 Grade" class="remark-input" style="width:auto;">
                    <button type="submit" class="btn btn-green">💾 SAVE SUBJECT GRADE</button>
                </form>
            </div>

            <div class="glass-card">
                <h3>📊 Existing Subject Grade Records</h3>
                <div style="overflow-x:auto;">
                    <table>
                        <thead>
                            <tr>
                                <th>Student Name</th>
                                <th>Section</th>
                                <th>Subject</th>
                                <th>Q1</th>
                                <th>Q2</th>
                                <th>Q3</th>
                                <th>Q4</th>
                                <th>Action</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for rc in raw_report_cards %}
                            <tr>
                                <td><strong>{{ rc['student_name'] }}</strong></td>
                                <td>{{ rc['section'] }}</td>
                                <td>{{ rc['subject_name'] }}</td>
                                <td>{{ rc['q1'] or '-' }}</td>
                                <td>{{ rc['q2'] or '-' }}</td>
                                <td>{{ rc['q3'] or '-' }}</td>
                                <td>{{ rc['q4'] or '-' }}</td>
                                <td>
                                    <form action="/teacher/delete-grade/{{ rc['id'] }}" method="POST" onsubmit="return confirm('Burahin ang subject grade na ito?');">
                                        <button type="submit" class="btn btn-red" style="padding:2px 8px; font-size:10px;">🗑️ Delete</button>
                                    </form>
                                </td>
                            </tr>
                            {% else %}
                            <tr><td colspan="8" style="text-align:center; color:var(--text-muted);">Walang encoded subject grades.</td></tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
            
            <div class="glass-card">
                <h3>🖨️ Printable Report Cards (SF9)</h3>
                <ul>
                    {% for st in student_list %}
                    <li style="margin-bottom:10px;">
                        <strong>{{ st }}</strong> 
                        <a href="/student/print-report/{{ st }}" target="_blank" class="btn btn-purple" style="margin-left:15px; font-size:10px;">🖨️ PRINT CARD</a>
                    </li>
                    {% else %}
                    <li style="color:var(--text-muted);">No manual report cards generated yet.</li>
                    {% endfor %}
                </ul>
            </div>
        </div>

        <!-- TAB: PRINT CENTER -->
        <div id="print-center" class="tab-content">
            <div class="glass-card" style="text-align:center;">
                <h3>🖨️ Print 1-60 OMR Bubble Sheets</h3>
                <p style="color:var(--text-muted);">Ready to print for examination day. Perfect for Gemini AI scanning.</p>
                <a href="/teacher/print-bubblesheet" target="_blank" class="btn btn-cyan" style="font-size:16px; padding:15px 30px;">📄 GENERATE BUBBLE SHEETS</a>
            </div>
            
            <div class="glass-card">
                <h3>📲 Print Student QR Access Tokens</h3>
                <p style="color:var(--text-muted);">Give these to students so they can scan and see their grades instantly.</p>
                {% for sec in sections %}
                <a href="/teacher/print-qr/{{ sec['name'] }}" target="_blank" class="btn btn-dark" style="margin-right:10px; margin-bottom:10px;">🖨️ {{ sec['name'] }} QR Codes</a>
                {% endfor %}
            </div>
        </div>

        <!-- TAB: DEPED ENGINE -->
        <div id="deped-engine" class="tab-content">
            <div class="glass-card">
                <h3>📊 DepEd Order No. 8 Transmutation Grade Sheet</h3>
                <p style="color: var(--text-muted); font-size: 12px;">Automated weights: Written Work (30%), Performance Tasks (50%), Quarterly Assessment (20%).</p>
                <table>
                    <thead>
                        <tr>
                            <th>Student Name</th>
                            <th>Section</th>
                            <th>WW (30%)</th>
                            <th>PT (50%)</th>
                            <th>QA (20%)</th>
                            <th>Weighted %</th>
                            <th>Transmuted Grade</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for st in deped_summary %}
                        <tr>
                            <td><strong>{{ st.name }}</strong></td>
                            <td>{{ st.section }}</td>
                            <td>{{ st.grades.ww_pct }}%</td>
                            <td>{{ st.grades.pt_pct }}%</td>
                            <td>{{ st.grades.qa_pct }}%</td>
                            <td>{{ st.grades.weighted_pct }}%</td>
                            <td><span class="badge badge-green" style="font-size:14px;">{{ st.grades.transmuted }}</span></td>
                        </tr>
                        {% else %}
                        <tr><td colspan="7" style="text-align:center; color: var(--text-muted);">No student grade summaries computed yet.</td></tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- TAB: CHART.JS ANALYTICS -->
        <div id="analytics-tab" class="tab-content">
            <div class="glass-card">
                <h3>📈 Item Analysis & Error Frequency</h3>
                <p style="color: var(--text-muted); font-size: 12px;">Visual distribution of test items showing error counts and student accuracy percentages.</p>
                
                <div style="position: relative; height: 320px; width: 100%; margin: 20px 0 30px 0; background: rgba(10, 14, 26, 0.5); padding: 15px; border-radius: 12px; border: 1px solid var(--card-border); box-sizing: border-box;">
                    <canvas id="itemAnalysisChart"></canvas>
                </div>

                <table>
                    <thead>
                        <tr>
                            <th>Item Number</th>
                            <th>Total Attempts</th>
                            <th>Correct</th>
                            <th>Errors</th>
                            <th>Accuracy Rate</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for freq in frequency_data %}
                        <tr>
                            <td><strong>Item #{{ freq.item }}</strong></td>
                            <td>{{ freq.total }}</td>
                            <td style="color:var(--green-neon);">{{ freq.correct }}</td>
                            <td style="color:var(--red-danger);">{{ freq.errors }}</td>
                            <td><strong>{{ freq.accuracy }}%</strong></td>
                        </tr>
                        {% else %}
                        <tr><td colspan="5" style="text-align:center; color: var(--text-muted);">No graded item data available yet.</td></tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- TAB: SECTIONS -->
        <div id="sections-tab" class="tab-content">
            <div class="glass-card">
                <h3>🏷️ Section Management</h3>
                <form action="/teacher/manage-sections" method="POST" style="display:flex; gap:10px; margin-bottom:20px;">
                    <input type="text" name="section_name" placeholder="New Section Name (e.g., Grade 12 - STEM C)" required style="flex:1; padding: 10px; border-radius: 8px; border: 1px solid var(--card-border); background: rgba(0,0,0,0.3); color: white;">
                    <button type="submit" class="btn btn-cyan">+ ADD SECTION</button>
                </form>
                
                <div style="display:flex; flex-direction:column; gap:10px;">
                    {% for sec in sections %}
                    <div style="display:flex; align-items:center; justify-content:space-between; background:rgba(255,255,255,0.03); padding:10px 15px; border-radius:8px; border:1px solid var(--card-border);">
                        <strong>{{ sec['name'] }}</strong>
                        <form action="/teacher/delete-section/{{ sec['id'] }}" method="POST" onsubmit="return confirm('Sigurado ka bang buburahin ang section na ito?');">
                            <button type="submit" class="btn btn-red" style="padding:4px 10px; font-size:11px;">🗑️ Delete Section</button>
                        </form>
                    </div>
                    {% endfor %}
                </div>
            </div>
        </div>

        <!-- TAB: BULK SCANNER -->
        <div id="bulk-scanner" class="tab-content">
            <div class="glass-card">
                <h3>⚡ Bulk Test Paper Scanner</h3>
                <p style="color: var(--text-muted); font-size: 13px;">Select section and target category, then upload batch test images for AI processing.</p>
                <form action="/upload" method="POST" enctype="multipart/form-data">
                    <div style="display:grid; grid-template-columns: 1fr 1fr; gap:15px; margin-bottom:15px;">
                        <div>
                            <label style="font-size:11px; color:var(--text-muted); font-weight:700;">TARGET SECTION</label>
                            <select name="section" style="width:100%; margin-top:5px; padding:8px; background:rgba(0,0,0,0.4); color:white; border-radius:6px;">
                                {% for sec in sections %}
                                <option value="{{ sec['name'] }}">{{ sec['name'] }}</option>
                                {% endfor %}
                            </select>
                        </div>
                        <div>
                            <label style="font-size:11px; color:var(--text-muted); font-weight:700;">CATEGORY</label>
                            <select name="category" style="width:100%; margin-top:5px; padding:8px; background:rgba(0,0,0,0.4); color:white; border-radius:6px;">
                                <option value="WW">Written Work (WW)</option>
                                <option value="PT">Performance Task (PT)</option>
                                <option value="QA">Quarterly Assessment (QA)</option>
                            </select>
                        </div>
                    </div>
                    <label style="font-size:11px; color:var(--text-muted); font-weight:700;">SELECT TEST IMAGES</label>
                    <input type="file" name="test_paper" multiple accept="image/*" required style="width:100%; margin: 5px 0 20px 0;">
                    <button type="submit" class="btn btn-purple" style="padding: 12px 20px;">UPLOAD & QUEUE FOR AI SCAN</button>
                </form>
            </div>
        </div>

        <!-- TAB: SETTINGS & ANSWER KEYS -->
        <div id="keys-tab" class="tab-content">
            <div class="glass-card">
                <h3>⚙️ Master Answer Keys Settings</h3>
                <p style="color: var(--text-muted); font-size: 12px;">Set the answer keys per section and category (e.g. 1.A 2.B 3.C or A B C D).</p>
                {% for sec in sections %}
                <div style="margin-bottom: 25px; border-bottom: 1px solid var(--card-border); padding-bottom: 15px;">
                    <h4 style="color: var(--cyan-glow); margin-bottom: 10px;">{{ sec['name'] }} Answer Keys</h4>
                    <form action="/teacher/save-key" method="POST" style="display: grid; grid-template-columns: 1fr 1fr 1fr auto; gap: 10px; align-items: end;">
                        <input type="hidden" name="section" value="{{ sec['name'] }}">
                        <div>
                            <label style="font-size: 10px; color: var(--text-muted);">WW KEY</label>
                            <input type="text" name="key_ww" value="{{ answer_keys.get(sec['name'] ~ '_WW', '') }}" placeholder="1.A 2.B 3.C..." style="width:100%; padding:6px; background:rgba(0,0,0,0.4); color:white; border-radius:6px; border:1px solid var(--card-border);">
                        </div>
                        <div>
                            <label style="font-size: 10px; color: var(--text-muted);">PT KEY</label>
                            <input type="text" name="key_pt" value="{{ answer_keys.get(sec['name'] ~ '_PT', '') }}" placeholder="1.A 2.B 3.C..." style="width:100%; padding:6px; background:rgba(0,0,0,0.4); color:white; border-radius:6px; border:1px solid var(--card-border);">
                        </div>
                        <div>
                            <label style="font-size: 10px; color: var(--text-muted);">QA KEY</label>
                            <input type="text" name="key_qa" value="{{ answer_keys.get(sec['name'] ~ '_QA', '') }}" placeholder="1.A 2.B 3.C..." style="width:100%; padding:6px; background:rgba(0,0,0,0.4); color:white; border-radius:6px; border:1px solid var(--card-border);">
                        </div>
                        <button type="submit" class="btn btn-green" style="padding: 8px 15px;">SAVE KEYS</button>
                    </form>
                </div>
                {% endfor %}
            </div>
        </div>

    </div>

    <script>
        function toggleSidebar() {
            document.getElementById('sidebar').classList.toggle('active');
            document.getElementById('overlay').classList.toggle('active');
        }

        function switchTab(tabId) {
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
            document.getElementById(tabId).classList.add('active');
            toggleSidebar();
        }

        function toggleAccordion(id) {
            const body = document.getElementById(id);
            body.style.display = (body.style.display === 'none' || body.style.display === '') ? 'block' : 'none';
        }

        // -----------------------------------------------------
        // SPEECH TO TEXT ENGINE FOR ECR
        // -----------------------------------------------------
        let isListening = false;
        let recognition = null;

        function toggleSpeechRecognition() {
            if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
                alert('Hindi supported ang Speech-to-Text sa browser na ito. Pakigamit ang Google Chrome.');
                return;
            }

            if (isListening) {
                stopSpeech();
            } else {
                startSpeech();
            }
        }

        function startSpeech() {
            const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
            recognition = new SpeechRecognition();
            recognition.lang = 'fil-PH'; // Tagalog/Filipino language dictation
            recognition.continuous = false;
            recognition.interimResults = false;

            recognition.onstart = function() {
                isListening = true;
                document.getElementById('mic-indicator').style.display = 'inline-block';
                document.getElementById('speech-status').innerText = '🎙️ Nakikinig na... Magsalita ngayon!';
                document.getElementById('mic-toggle-btn').innerText = '⏹️ Stop Dictation';
            };

            recognition.onresult = function(event) {
                const text = event.results[0][0].transcript;
                document.getElementById('speech-transcript').value = text;
                parseSpeechToForm(text);
            };

            recognition.onerror = function(event) {
                document.getElementById('speech-status').innerText = '❌ Error: ' + event.error;
                stopSpeech();
            };

            recognition.onend = function() {
                stopSpeech();
            };

            recognition.start();
        }

        function stopSpeech() {
            isListening = false;
            if (recognition) recognition.stop();
            document.getElementById('mic-indicator').style.display = 'none';
            document.getElementById('speech-status').innerText = 'Status: Ready. Click button to speak.';
            document.getElementById('mic-toggle-btn').innerText = '🎤 Start Voice Dictation';
        }

        function parseSpeechToForm(speechText) {
            // Extract numerical grades from spoken text
            const numbers = speechText.match(/\d+(\.\d+)?/g);
            if (numbers) {
                if (numbers[0]) document.getElementById('ecr_q1').value = numbers[0];
                if (numbers[1]) document.getElementById('ecr_q2').value = numbers[1];
                if (numbers[2]) document.getElementById('ecr_q3').value = numbers[2];
                if (numbers[3]) document.getElementById('ecr_q4').value = numbers[3];
            }

            // Remove numbers to isolate Name & Subject
            let cleanText = speechText.replace(/\d+(\.\d+)?/g, '').trim();
            
            // Standard Subjects list check
            const knownSubjects = ['Math', 'Science', 'English', 'Filipino', 'Research', 'PE', 'History', 'ICT', 'Empowerment Technologies'];
            let foundSubject = '';

            for (let subj of knownSubjects) {
                const regex = new RegExp('\\b' + subj + '\\b', 'i');
                if (regex.test(cleanText)) {
                    foundSubject = subj;
                    cleanText = cleanText.replace(regex, '').trim();
                    break;
                }
            }

            if (foundSubject) {
                document.getElementById('ecr_subject').value = foundSubject;
            } else if (!document.getElementById('ecr_subject').value) {
                document.getElementById('ecr_subject').value = 'General Subject';
            }

            if (cleanText) {
                document.getElementById('ecr_student_name').value = cleanText;
            }
        }

        document.addEventListener("DOMContentLoaded", function() {
            const freqData = {{ frequency_data | tojson | safe }};
            if (freqData && freqData.length > 0) {
                const labels = freqData.map(d => 'Item ' + d.item);
                const errors = freqData.map(d => d.errors);
                const accuracy = freqData.map(d => d.accuracy);

                const ctx = document.getElementById('itemAnalysisChart').getContext('2d');
                new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: labels,
                        datasets: [
                            {
                                label: 'Error Count',
                                data: errors,
                                backgroundColor: 'rgba(239, 68, 68, 0.5)',
                                borderColor: '#ef4444',
                                borderWidth: 1.5,
                                borderRadius: 4,
                                yAxisID: 'y'
                            },
                            {
                                label: 'Accuracy Rate (%)',
                                data: accuracy,
                                type: 'line',
                                borderColor: '#00f2fe',
                                backgroundColor: 'rgba(0, 242, 254, 0.1)',
                                borderWidth: 2,
                                pointBackgroundColor: '#00f2fe',
                                tension: 0.3,
                                fill: true,
                                yAxisID: 'y1'
                            }
                        ]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        interaction: { mode: 'index', intersect: false },
                        scales: {
                            x: { ticks: { color: '#a0aec0', font: { family: 'Outfit', size: 10 } }, grid: { color: 'rgba(255,255,255,0.05)' } },
                            y: { type: 'linear', display: true, position: 'left', title: { display: true, text: 'Errors', color: '#ef4444', font: { size: 11, weight: 'bold' } }, ticks: { color: '#a0aec0', precision: 0 }, grid: { color: 'rgba(255,255,255,0.05)' }, min: 0 },
                            y1: { type: 'linear', display: true, position: 'right', title: { display: true, text: 'Accuracy %', color: '#00f2fe', font: { size: 11, weight: 'bold' } }, min: 0, max: 100, ticks: { color: '#a0aec0' }, grid: { drawOnChartArea: false } }
                        },
                        plugins: {
                            legend: { labels: { color: '#ffffff', font: { family: 'Outfit', size: 12 } } },
                            tooltip: { backgroundColor: '#0c101d', borderColor: 'rgba(255, 255, 255, 0.2)', borderWidth: 1, titleColor: '#00f2fe', bodyColor: '#ffffff' }
                        }
                    }
                });
            }
        });
    </script>
</body>
</html>
'''

STUDENT_PORTAL_HTML = COMMON_STYLE + '''
<!DOCTYPE html>
<html>
<head><title>Student Portal - ESP32 Offline Access</title><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="display:flex; justify-content:center; align-items:center; min-height:100vh; padding: 20px 0; box-sizing: border-box;">
    <div style="max-width:480px; width:100%;">
        {% with messages = get_flashed_messages() %}
          {% if messages %}
            <div style="background: rgba(16,185,129,0.2); border: 1px solid var(--green-neon); color: #6ee7b7; padding: 12px; border-radius: 8px; font-size: 13px; margin-bottom: 15px; text-align: center;">
              {{ messages[0] }}
            </div>
          {% endif %}
        {% endwith %}

        <div class="glass-card" style="text-align:center;">
            <h2 style="color:var(--cyan-glow); margin-top:0;">Student Grade Portal</h2>
            <p style="font-size:13px; color:var(--text-muted);">Enter your 8-character Secret Token or scan your QR Code to view your Report Card.</p>
            <form action="/student/view" method="POST" style="margin-top:15px;">
                <input type="text" name="token" placeholder="e.g. A1B2C3D4" required style="width:90%; font-size:20px; text-align:center; text-transform:uppercase; margin-bottom:15px; letter-spacing:3px; background:rgba(0,0,0,0.4); border:1px solid var(--card-border); color:#fff; padding:10px; border-radius:6px;">
                <button type="submit" class="btn btn-cyan" style="width:95%; font-size:15px; padding:10px;">🔍 VIEW MY REPORT CARD</button>
            </form>
        </div>

        <div class="glass-card">
            <h3 style="color:var(--purple-glow); margin-top:0; text-align:center;">📤 Upload Test Paper (ESP32 Hotspot)</h3>
            <p style="font-size:12px; color:var(--text-muted); text-align:center;">Kahit offline, pwedeng mag-upload ng kuha ng iyong test paper habang nakakonekta sa ESP32 Hotspot.</p>
            <form action="/student/upload" method="POST" enctype="multipart/form-data" style="margin-top:15px; display:flex; flex-direction:column; gap:10px;">
                <div>
                    <label style="font-size:11px; color:var(--text-muted);">Pangalan ng Estudyante</label>
                    <input type="text" name="student_name" placeholder="Pangalan (e.g. Juan Cruz)" required style="width:100%; padding:8px; background:rgba(0,0,0,0.4); border:1px solid var(--card-border); color:#fff; border-radius:6px; box-sizing:border-box;">
                </div>
                <div>
                    <label style="font-size:11px; color:var(--text-muted);">LRN (Optional)</label>
                    <input type="text" name="lrn" placeholder="12-Digit LRN" style="width:100%; padding:8px; background:rgba(0,0,0,0.4); border:1px solid var(--card-border); color:#fff; border-radius:6px; box-sizing:border-box;">
                </div>
                <div>
                    <label style="font-size:11px; color:var(--text-muted);">Section</label>
                    <select name="section" required style="width:100%; padding:8px; background:rgba(0,0,0,0.4); border:1px solid var(--card-border); color:#fff; border-radius:6px; box-sizing:border-box;">
                        {% for sec in sections %}
                        <option value="{{ sec['name'] }}">{{ sec['name'] }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div>
                    <label style="font-size:11px; color:var(--text-muted);">Category</label>
                    <select name="category" required style="width:100%; padding:8px; background:rgba(0,0,0,0.4); border:1px solid var(--card-border); color:#fff; border-radius:6px; box-sizing:border-box;">
                        <option value="WW">Written Work (WW)</option>
                        <option value="PT">Performance Task (PT)</option>
                        <option value="QA">Quarterly Assessment (QA)</option>
                    </select>
                </div>
                <div>
                    <label style="font-size:11px; color:var(--text-muted);">Litrato ng Test Paper</label>
                    <input type="file" name="test_paper" accept="image/*" required style="width:100%; color:#fff; font-size:12px; margin-top:4px;">
                </div>
                <button type="submit" class="btn btn-purple" style="font-size:14px; padding:10px; margin-top:10px;">⚡ PASS TEST PAPER</button>
            </form>
        </div>
    </div>
</body>
</html>
'''

PRINTABLE_BUBBLESHEET_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>60-Item Answer Sheet</title>
    <style>
        body { font-family: Arial, sans-serif; padding: 20px; }
        .sheet-container { width: 700px; margin: 0 auto; border: 2px solid #000; padding: 20px; }
        .header-box { border-bottom: 2px solid #000; padding-bottom: 10px; margin-bottom: 15px; }
        .field { margin: 5px 0; font-size: 14px; }
        .grid-container { display: flex; justify-content: space-between; }
        .column { width: 30%; }
        .item-row { display: flex; align-items: center; margin-bottom: 5px; font-size: 12px; }
        .item-num { width: 25px; font-weight: bold; }
        .bubble { display: inline-block; width: 14px; height: 14px; border: 1.5px solid #000; border-radius: 50%; text-align: center; line-height: 14px; font-size: 9px; margin: 0 3px; color: #ccc;}
        @media print { .no-print { display: none; } }
    </style>
</head>
<body>
<div class="no-print" style="text-align:center; margin-bottom:15px;">
    <button onclick="window.print()" style="padding:10px 20px; font-size:16px; cursor:pointer; background:#2563eb; color:white; border:none; border-radius:5px;">🖨️ PRINT BUBBLE SHEETS</button>
</div>
<div class="sheet-container">
    <div class="header-box">
        <h3 style="text-align:center; margin:0 0 10px 0;">OMR EXAMINATION ANSWER SHEET (1-60)</h3>
        <div class="field"><strong>NAME:</strong> ____________________________________ &nbsp;&nbsp; <strong>DATE:</strong> ___________</div>
        <div class="field"><strong>LRN:</strong> _____________________________________ &nbsp;&nbsp; <strong>SCORE:</strong> __________</div>
        <p style="font-size:10px; text-align:center; margin:5px 0 0 0;"><em>Shade the circle of your answer completely using a dark pen or pencil.</em></p>
    </div>
    <div class="grid-container">
        <div class="column">
            {% for i in range(1, 21) %}
            <div class="item-row"><span class="item-num">{{ i }}.</span><span class="bubble">A</span><span class="bubble">B</span><span class="bubble">C</span><span class="bubble">D</span></div>
            {% endfor %}
        </div>
        <div class="column">
            {% for i in range(21, 41) %}
            <div class="item-row"><span class="item-num">{{ i }}.</span><span class="bubble">A</span><span class="bubble">B</span><span class="bubble">C</span><span class="bubble">D</span></div>
            {% endfor %}
        </div>
        <div class="column">
            {% for i in range(41, 61) %}
            <div class="item-row"><span class="item-num">{{ i }}.</span><span class="bubble">A</span><span class="bubble">B</span><span class="bubble">C</span><span class="bubble">D</span></div>
            {% endfor %}
        </div>
    </div>
</div>
</body>
</html>
'''

REPORT_CARD_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>DepEd School Form 9 (SF9) - Learner's Performance Report</title>
    <style>
        @page { size: A4 portrait; margin: 8mm; }
        body { font-family: 'Arial', sans-serif; color: #000; margin: 0; padding: 10px; font-size: 10px; background: #f3f4f6; }
        .no-print { text-align: center; margin-bottom: 15px; background: #fff; padding: 10px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .btn-print { padding: 10px 24px; background: #059669; color: white; border: none; cursor: pointer; font-weight: bold; border-radius: 6px; font-size: 14px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .btn-print:hover { background: #047857; }
        .edit-hint { font-size: 12px; color: #4b5563; margin-top: 5px; font-style: italic; }
        
        [contenteditable="true"]:hover { background-color: #fef08a !important; outline: 1px dashed #ca8a04; cursor: text; }
        [contenteditable="true"]:focus { background-color: #fef9c3 !important; outline: 2px solid #eab308; }

        .sf9-card { width: 100%; max-width: 960px; margin: 0 auto; border: 2px solid #000; padding: 15px; box-sizing: border-box; background: white; }
        .header-section { text-align: center; line-height: 1.2; margin-bottom: 8px; }
        .header-section h5 { margin: 1px 0; font-size: 10px; font-weight: normal; text-transform: uppercase; }
        .header-section h4 { margin: 2px 0; font-size: 12px; font-weight: bold; text-transform: uppercase; }
        .header-section h3 { margin: 4px 0; font-size: 14px; font-weight: bold; text-transform: uppercase; letter-spacing: 0.5px; }
        
        .info-grid { display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 4px 12px; margin-bottom: 8px; font-size: 10px; border-bottom: 1.5px solid #000; padding-bottom: 6px; }
        .info-item { display: flex; align-items: flex-end; }
        .info-label { font-weight: bold; white-space: nowrap; margin-right: 4px; }
        .info-value { border-bottom: 1px solid #000; flex-grow: 1; text-align: center; font-weight: bold; min-height: 14px; }

        .letter-box { font-size: 9px; margin-bottom: 8px; border: 1px solid #666; padding: 5px; line-height: 1.2; background-color: #fafafa; }
        .main-layout { display: grid; grid-template-columns: 1.4fr 1fr; gap: 12px; }
        
        table.sf9-table { width: 100%; border-collapse: collapse; margin-bottom: 8px; font-size: 9.5px; }
        table.sf9-table th, table.sf9-table td { border: 1px solid #000; padding: 3px 4px; text-align: center; }
        table.sf9-table th { background-color: #f3f4f6; font-weight: bold; text-transform: uppercase; font-size: 8.5px; }
        table.sf9-table td.left { text-align: left; padding-left: 5px; font-weight: 500; }

        .section-title { font-weight: bold; text-transform: uppercase; background: #e5e7eb; border: 1px solid #000; padding: 2px 5px; font-size: 9px; text-align: center; margin-bottom: 4px; letter-spacing: 0.5px; }
        .sign-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; text-align: center; margin-top: 10px; font-size: 9px; }
        .sign-line { border-top: 1px solid #000; margin-top: 20px; font-weight: bold; padding-top: 2px; }
        .remarks-box { border: 1px solid #000; padding: 4px; font-size: 8.5px; min-height: 35px; margin-bottom: 6px; }

        @media print {
            .no-print { display: none !important; }
            body { padding: 0; background: white; }
            .sf9-card { border: none; padding: 0; width: 100%; }
            [contenteditable="true"]:hover, [contenteditable="true"]:focus { background-color: transparent !important; outline: none !important; }
        }
    </style>
</head>
<body>

<div class="no-print">
    <button onclick="window.print()" class="btn-print">🖨️ PRINT SCHOOL FORM 9 (SF9)</button>
    <div class="edit-hint">💡 <strong>Tip:</strong> Pwede mong i-click at baguhin ang anumang text sa report card na ito bago i-print!</div>
</div>

<div class="sf9-card">
    <div class="header-section">
        <h5>Republic of the Philippines</h5>
        <h5>Department of Education</h5>
        <h5 contenteditable="true">REGION / SCHOOLS DIVISION OFFICE</h5>
        <h4 contenteditable="true">ABC NATIONAL HIGH SCHOOL - SENIOR HIGH SCHOOL</h4>
        <h3>LEARNER'S PERFORMANCE REPORT (SF9)</h3>
        <p style="margin:2px 0; font-weight:bold; font-size:10px;">School Year <span contenteditable="true">2026–2027</span></p>
    </div>

    <div class="info-grid">
        <div class="info-item"><span class="info-label">Name:</span><span class="info-value" contenteditable="true">{{ st_name }}</span></div>
        <div class="info-item"><span class="info-label">Age:</span><span class="info-value" contenteditable="true">17</span></div>
        <div class="info-item"><span class="info-label">Sex:</span><span class="info-value" contenteditable="true">Male</span></div>
        <div class="info-item"><span class="info-label">LRN:</span><span class="info-value" contenteditable="true">{{ st_lrn or '' }}</span></div>
        <div class="info-item"><span class="info-label">Grade Level:</span><span class="info-value" contenteditable="true">12</span></div>
        <div class="info-item"><span class="info-label">Section:</span><span class="info-value" contenteditable="true">{{ st_section }}</span></div>
        <div class="info-item" style="grid-column: span 3;"><span class="info-label">Track / Strand (SHS):</span><span class="info-value" contenteditable="true">{{ st_track }}</span></div>
    </div>

    <div class="letter-box">
        <strong>Dear Parents/Guardians:</strong><br>
        This Performance Report presents your child's progress and achievement in the different learning areas. The school welcomes you to reach out should you wish to know more about your child's learning and performance.
    </div>

    <div class="main-layout">
        <div>
            <div class="section-title">LEARNING PROGRESS AND ACHIEVEMENT</div>
            <table class="sf9-table">
                <thead>
                    <tr>
                        <th rowspan="2" style="width: 44%;">Learning Areas</th>
                        <th colspan="2">1st Semester</th>
                        <th colspan="2">2nd Semester</th>
                        <th rowspan="2" style="width: 13%;">Final Grade</th>
                        <th rowspan="2" style="width: 15%;">Remarks</th>
                    </tr>
                    <tr>
                        <th style="width: 7%;">Q1</th>
                        <th style="width: 7%;">Q2</th>
                        <th style="width: 7%;">Q3</th>
                        <th style="width: 7%;">Q4</th>
                    </tr>
                </thead>
                <tbody>
                    {% set ns = namespace(total=0, count=0) %}
                    {% for g in grades %}
                        {% set valid_q = [] %}
                        {% if g.q1 %}{% set _ = valid_q.append(g.q1) %}{% endif %}
                        {% if g.q2 %}{% set _ = valid_q.append(g.q2) %}{% endif %}
                        {% if g.q3 %}{% set _ = valid_q.append(g.q3) %}{% endif %}
                        {% if g.q4 %}{% set _ = valid_q.append(g.q4) %}{% endif %}
                        {% set final_g = (valid_q | sum / valid_q | length) | round if valid_q|length > 0 else 0 %}
                        {% if final_g > 0 %}
                            {% set ns.total = ns.total + final_g %}
                            {% set ns.count = ns.count + 1 %}
                        {% endif %}
                    <tr>
                        <td class="left" contenteditable="true">{{ g.subject_name }}</td>
                        <td contenteditable="true">{{ g.q1 or '' }}</td>
                        <td contenteditable="true">{{ g.q2 or '' }}</td>
                        <td contenteditable="true">{{ g.q3 or '' }}</td>
                        <td contenteditable="true">{{ g.q4 or '' }}</td>
                        <td contenteditable="true"><strong>{{ final_g if final_g > 0 else '' }}</strong></td>
                        <td contenteditable="true" style="color:{{ 'green' if final_g >= 75 else 'red' }}; font-weight:bold;">
                            {% if final_g > 0 %}{{ 'Passed' if final_g >= 75 else 'Failed' }}{% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                    <tr style="background:#f8fafc; font-weight:bold;">
                        <td class="left">General Average</td>
                        <td colspan="4"></td>
                        <td contenteditable="true"><strong>{{ (ns.total / ns.count)|round(2) if ns.count > 0 else '' }}</strong></td>
                        <td contenteditable="true" style="color:{{ 'green' if (ns.total / ns.count) >= 75 else 'red' }};">
                            {% if ns.count > 0 %}{{ 'Passed' if (ns.total / ns.count) >= 75 else 'Failed' }}{% endif %}
                        </td>
                    </tr>
                </tbody>
            </table>

            <div class="section-title">PERFORMANCE DESCRIPTORS</div>
            <table class="sf9-table">
                <thead>
                    <tr>
                        <th>Grading Scale</th>
                        <th>Descriptor</th>
                        <th>Remarks</th>
                    </tr>
                </thead>
                <tbody>
                    <tr><td>90 – 100</td><td>Advancing</td><td>Passed</td></tr>
                    <tr><td>80 – 89</td><td>Benchmarking</td><td>Passed</td></tr>
                    <tr><td>75 – 79</td><td>Connecting</td><td>Passed</td></tr>
                    <tr><td>65 – 74</td><td>Developing</td><td>Failed</td></tr>
                    <tr><td>0 – 64</td><td>Emerging</td><td>Failed</td></tr>
                </tbody>
            </table>
        </div>

        <div>
            <div class="section-title">REPORT ON ATTENDANCE</div>
            <table class="sf9-table" style="font-size: 8px;">
                <thead>
                    <tr>
                        <th>Month</th>
                        <th>Jun</th><th>Jul</th><th>Aug</th><th>Sep</th><th>Oct</th><th>Nov</th><th>Dec</th><th>Jan</th><th>Feb</th><th>Mar</th><th>Apr</th><th>Total</th>
                    </tr>
                </thead>
                <tbody>
                    <tr><td class="left" style="font-weight:bold;">No. Class Days</td>{% for _ in range(12) %}<td contenteditable="true"></td>{% endfor %}</tr>
                    <tr><td class="left" style="font-weight:bold;">No. Present</td>{% for _ in range(12) %}<td contenteditable="true"></td>{% endfor %}</tr>
                    <tr><td class="left" style="font-weight:bold;">No. Absent</td>{% for _ in range(12) %}<td contenteditable="true"></td>{% endfor %}</tr>
                </tbody>
            </table>

            <div class="section-title">TEACHER'S COMMENTS / REMARKS</div>
            <div class="remarks-box" contenteditable="true"><strong>Q1 / Q2:</strong> </div>
            <div class="remarks-box" contenteditable="true"><strong>Q3 / Q4:</strong> </div>

            <div class="section-title">CERTIFICATE OF TRANSFER</div>
            <p style="font-size: 8.5px; margin: 2px 0;">This is to certify that the above-named learner has satisfactorily completed the requirements for the grade level indicated.</p>
            <div style="font-size:8.5px; margin-top:4px;">
                <p style="margin:2px 0;"><strong>Admitted to Grade:</strong> <span contenteditable="true">_____</span></p>
                <p style="margin:2px 0;"><strong>Eligible for Admission to Grade:</strong> <span contenteditable="true">_____</span></p>
            </div>

            <div class="sign-grid">
                <div><div class="sign-line" contenteditable="true">Class Adviser</div></div>
                <div><div class="sign-line" contenteditable="true">School Head</div></div>
            </div>
            
            <div style="margin-top: 10px; text-align: center;">
                <div style="border-top: 1px solid #000; width: 80%; margin: 15px auto 2px auto;"></div>
                <span style="font-size: 8px; font-weight: bold; text-transform: uppercase;">Parent / Guardian Signature</span>
            </div>
        </div>

    </div>
</div>

</body>
</html>
'''

QR_PRINT_HTML = '''
<!DOCTYPE html>
<html>
<head><title>Print QR Codes</title><style>body{font-family:Arial;} .grid{display:grid; grid-template-columns:repeat(3, 1fr); gap:20px; padding:20px;} .card{border:1px dashed #000; padding:15px; text-align:center;} @media print{.no-print{display:none;}}</style></head>
<body>
    <div class="no-print" style="text-align:center; padding:10px;"><button onclick="window.print()" style="padding:10px;">PRINT QR CARDS</button></div>
    <div class="grid">
        {% for t in tokens %}
        <div class="card">
            <h4 style="margin:0 0 5px 0; font-size:14px;">{{ t['student_name'] }}</h4>
            <p style="margin:0 0 10px 0; font-size:10px;">Section: {{ t['section'] }}</p>
            <img src="https://api.qrserver.com/v1/create-qr-code/?size=100x100&data=http://127.0.0.1:5000/student/qr/{{ t['secret_token'] }}" alt="QR">
            <p style="margin:10px 0 0 0; font-size:12px; font-weight:bold;">Token: {{ t['secret_token'] }}</p>
        </div>
        {% endfor %}
    </div>
</body>
</html>
'''

# ---------------------------------------------------------
# FLASK ROUTES (Teacher & Student)
# ---------------------------------------------------------
@app.route('/')
def home():
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/dashboard')
def teacher_dashboard():
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM sections')
        sections = cursor.fetchall()

        cursor.execute('SELECT * FROM submissions ORDER BY id DESC')
        submissions = cursor.fetchall()

        cursor.execute('SELECT * FROM answer_keys')
        keys_rows = cursor.fetchall()
        answer_keys = {k['section_cat']: k['key_text'] for k in keys_rows}

        cursor.execute('SELECT DISTINCT student_name FROM student_report_cards')
        student_list = [r['student_name'] for r in cursor.fetchall()]

        cursor.execute('SELECT * FROM student_report_cards ORDER BY student_name, subject_name')
        raw_report_cards = cursor.fetchall()

        cursor.execute('SELECT * FROM student_tokens ORDER BY section, student_name')
        all_tokens = cursor.fetchall()

    freq_data = compute_frequency_data(submissions)
    deped_summary = compute_deped_summary(submissions)

    return render_template_string(
        TEACHER_DASHBOARD_HTML,
        sections=sections,
        submissions=submissions,
        answer_keys=answer_keys,
        frequency_data=freq_data,
        deped_summary=deped_summary,
        student_list=student_list,
        raw_report_cards=raw_report_cards,
        all_tokens=all_tokens
    )

@app.route('/teacher/add-roster', methods=['POST'])
def add_roster():
    section = request.form.get('section')
    students_raw = request.form.get('students_list', '')

    lines = students_raw.strip().split('\n')
    added_count = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split(',')
        name = parts[0].strip()
        lrn = parts[1].strip() if len(parts) > 1 else 'N/A'

        if name:
            get_or_create_student_token(name, lrn, section)
            added_count += 1

    flash(f"✅ Matagumpay na na-generate ang Secret Tokens para sa {added_count} estudyante sa {section}!")
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/delete-token/<int:token_id>', methods=['POST'])
def delete_token(token_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM student_tokens WHERE id = ?', (token_id,))
        conn.commit()
    flash("Binura ang student token.")
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/ai-check/<section_name>')
def teacher_ai_check(section_name):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM submissions WHERE section = ? AND status = "Pending"', (section_name,))
        pending_subs = cursor.fetchall()

        if not pending_subs:
            flash("Walang pending test papers para i-grade sa section na ito.")
            return redirect(url_for('teacher_dashboard'))

        processed_count = 0

        for sub in pending_subs:
            key_code = f"{sub['section']}_{sub['category']}"
            cursor.execute('SELECT key_text FROM answer_keys WHERE section_cat = ?', (key_code,))
            key_row = cursor.fetchone()
            answer_key = key_row['key_text'] if key_row else ""

            if not answer_key:
                flash(f"⚠️ Walang Answer Key para sa {key_code}! Mag-set muna sa Answer Key Settings tab.")
                return redirect(url_for('teacher_dashboard'))

            ai_res = call_gemini_vision(sub['image_path'], answer_key)

            if not ai_res.get("success"):
                flash(f"⚠️ Error sa Paper ID #{sub['id']}: {ai_res.get('error')}")
                continue

            ext_name = ai_res.get("student_name") or sub['student_name']
            ext_lrn = ai_res.get("lrn") or sub['lrn']
            score = int(ai_res.get("score", 0))
            total = int(ai_res.get("total", 60))
            errors = str(ai_res.get("errors", ""))

            token = get_or_create_student_token(ext_name, ext_lrn, section_name)

            cursor.execute('''
                UPDATE submissions 
                SET student_name = ?, lrn = ?, score = ?, total_questions = ?, error_details = ?, status = 'Graded', secret_token = ?
                WHERE id = ?
            ''', (ext_name, ext_lrn, score, total, errors, token, sub['id']))
            processed_count += 1

        conn.commit()

    if processed_count > 0:
        flash(f"✅ Matagumpay na na-grade ang {processed_count} test paper(s)!")
    return redirect(url_for('teacher_dashboard'))

@app.route('/upload', methods=['POST'])
def upload_file():
    section = request.form.get('section')
    category = request.form.get('category')
    files = request.files.getlist('test_paper')

    os.makedirs('uploads', exist_ok=True)
    count = 0

    with get_db() as conn:
        cursor = conn.cursor()
        for file in files:
            if file and file.filename:
                filename = secure_filename(file.filename)
                save_path = os.path.join('uploads', filename)
                file.save(save_path)
                cursor.execute('''
                    INSERT INTO submissions (student_name, lrn, section, category, image_path, status)
                    VALUES (?, ?, ?, ?, ?, 'Pending')
                ''', ('Unassigned Student', 'N/A', section, category, save_path))
                count += 1
        conn.commit()

    flash(f"✅ Na-upload ang {count} test paper(s) sa queue!")
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/save-key', methods=['POST'])
def save_key():
    section = request.form.get('section')
    key_ww = request.form.get('key_ww', '')
    key_pt = request.form.get('key_pt', '')
    key_qa = request.form.get('key_qa', '')

    with get_db() as conn:
        cursor = conn.cursor()
        for cat, val in [('WW', key_ww), ('PT', key_pt), ('QA', key_qa)]:
            code = f"{section}_{cat}"
            cursor.execute('''
                INSERT INTO answer_keys (section_cat, key_text) VALUES (?, ?)
                ON CONFLICT(section_cat) DO UPDATE SET key_text = excluded.key_text
            ''', (code, val))
        conn.commit()

    flash(f"✅ Na-save ang Answer Keys para sa {section}!")
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/manage-sections', methods=['POST'])
def manage_sections():
    name = request.form.get('section_name')
    if name:
        with get_db() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute('INSERT INTO sections (name) VALUES (?)', (name,))
                conn.commit()
                flash(f"✅ Bagong Section Added: {name}")
            except sqlite3.IntegrityError:
                flash("⚠️ Umiiral na ang section name na ito.")
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/delete-section/<int:sec_id>', methods=['POST'])
def delete_section(sec_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM sections WHERE id = ?', (sec_id,))
        conn.commit()
    flash("✅ Section deleted successfully.")
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/update-score/<int:sub_id>', methods=['POST'])
def update_score(sub_id):
    score = request.form.get('score')
    total = request.form.get('total')
    errors = request.form.get('errors')

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE submissions 
            SET score = ?, total_questions = ?, error_details = ?, status = 'Graded'
            WHERE id = ?
        ''', (score, total, errors, sub_id))
        conn.commit()

    flash("✅ Updated submission record!")
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/delete-sub/<int:sub_id>', methods=['POST'])
def delete_sub(sub_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM submissions WHERE id = ?', (sub_id,))
        conn.commit()
    flash("Record deleted.")
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/clear-all', methods=['POST'])
def clear_all():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM submissions')
        conn.commit()
    flash("Lahat ng submissions ay binura na.")
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/clear-section/<section_name>', methods=['POST'])
def clear_section(section_name):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM submissions WHERE section = ?', (section_name,))
        conn.commit()
    flash(f"✅ Lahat ng submissions sa {section_name} ay nabura na.")
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/export-csv/<section_name>')
def export_csv(section_name):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM submissions WHERE section = ?', (section_name,))
        rows = cursor.fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Student Name', 'LRN', 'Section', 'Category', 'Score', 'Total', 'Errors', 'Status', 'Token'])

    for r in rows:
        writer.writerow([r['student_name'], r['lrn'], r['section'], r['category'], r['score'], r['total_questions'], r['error_details'], r['status'], r['secret_token']])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={section_name}_grades.csv"}
    )

@app.route('/teacher/export-csv-all')
def export_csv_all():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM submissions ORDER BY section, student_name')
        rows = cursor.fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Student Name', 'LRN', 'Section', 'Category', 'Score', 'Total', 'Errors', 'Status', 'Token'])

    for r in rows:
        writer.writerow([r['student_name'], r['lrn'], r['section'], r['category'], r['score'], r['total_questions'], r['error_details'], r['status'], r['secret_token']])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=all_sections_grades.csv"}
    )

# --- STUDENT AND PRINT ROUTES ---

@app.route('/student')
def student_portal():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM sections')
        sections = cursor.fetchall()
    return render_template_string(STUDENT_PORTAL_HTML, sections=sections)

@app.route('/student/upload', methods=['POST'])
def student_upload():
    student_name = request.form.get('student_name', 'Unassigned Student')
    lrn = request.form.get('lrn', 'N/A')
    section = request.form.get('section')
    category = request.form.get('category')
    file = request.files.get('test_paper')

    if file and file.filename:
        os.makedirs('uploads', exist_ok=True)
        filename = secure_filename(file.filename)
        save_path = os.path.join('uploads', filename)
        file.save(save_path)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO submissions (student_name, lrn, section, category, image_path, status)
                VALUES (?, ?, ?, ?, ?, 'Pending')
            ''', (student_name, lrn, section, category, save_path))
            conn.commit()
        flash("✅ Matagumpay na na-upload ang iyong test paper! Hihintayin na lang ang pag-grade ng guro.")
    else:
        flash("⚠️ Walang larawan na naipasa.")
    return redirect(url_for('student_portal'))

@app.route('/student/view', methods=['POST'])
def student_view_post():
    token = request.form.get('token', '').strip().upper()
    return redirect(f"/student/qr/{token}")

@app.route('/student/qr/<token>')
def student_view_qr(token):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT student_name FROM student_tokens WHERE secret_token = ?', (token,))
        row = cursor.fetchone()
        if not row:
            return "Invalid Token or No Grades Found.", 404
        return redirect(f"/student/print-report/{row['student_name']}")

@app.route('/student/print-report/<student_name>')
def print_report(student_name):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM student_report_cards WHERE student_name = ?', (student_name,))
        grades = cursor.fetchall()
        
    if not grades:
        return "No manual report card records yet for this student. Teacher needs to input subjects first.", 404
        
    st_section = grades[0]['section']
    st_lrn = grades[0]['lrn']
    st_track = grades[0]['track']

    return render_template_string(REPORT_CARD_HTML, st_name=student_name, st_section=st_section, st_lrn=st_lrn, st_track=st_track, grades=grades)

@app.route('/teacher/add-grade', methods=['POST'])
def add_grade():
    st_name = request.form.get('student_name')
    sec = request.form.get('section')
    lrn = request.form.get('lrn')
    subj = request.form.get('subject_name')
    q1 = request.form.get('q1') or None
    q2 = request.form.get('q2') or None
    q3 = request.form.get('q3') or None
    q4 = request.form.get('q4') or None

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO student_report_cards (student_name, lrn, section, subject_name, q1, q2, q3, q4)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (st_name, lrn, sec, subj, q1, q2, q3, q4))
        conn.commit()
    flash(f"✅ Grade saved for {st_name} - {subj}")
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/delete-grade/<int:grade_id>', methods=['POST'])
def delete_grade(grade_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM student_report_cards WHERE id = ?', (grade_id,))
        conn.commit()
    flash("✅ Subject grade entry deleted.")
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/print-bubblesheet')
def print_bubblesheet():
    return render_template_string(PRINTABLE_BUBBLESHEET_HTML)

@app.route('/teacher/print-qr/<section_name>')
def print_qr(section_name):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM student_tokens WHERE section = ?', (section_name,))
        tokens = cursor.fetchall()
    return render_template_string(QR_PRINT_HTML, tokens=tokens)

@app.route('/section-analytics')
def section_analytics():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT section, score FROM submissions WHERE score IS NOT NULL")
        rows = cursor.fetchall()

    grouped_data = {}
    for section, score in rows:
        if section and score is not None:
            try:
                val = float(score)
                if section not in grouped_data:
                    grouped_data[section] = []
                grouped_data[section].append(val)
            except (ValueError, TypeError):
                continue

    stats = []
    for sec_name, scores in grouped_data.items():
        if len(scores) > 0:
            mean_val = round(statistics.mean(scores), 2)
            stdev_val = round(statistics.stdev(scores), 2) if len(scores) > 1 else 0.0
            
            stats.append({
                'section': sec_name,
                'count': len(scores),
                'mean': mean_val,
                'stdev': stdev_val,
                'min': min(scores),
                'max': max(scores)
            })

    return render_template('section_analytics.html', stats=stats)


if __name__ == '__main__':
    print("Starting YhelChecker AI & Voice ECR Server...")
    app.run(debug=True, host='0.0.0.0', port=5000)