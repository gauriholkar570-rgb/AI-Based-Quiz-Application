from flask import Flask, render_template, request, redirect, session, flash, url_for, Response, send_from_directory
from flask import jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import sqlite3, os
import uuid
import re
import zipfile
import secrets
import json
from functools import wraps
import datetime 
import random
import traceback
import urllib.parse
import urllib.request
import urllib.error
import csv
import base64
from io import StringIO, BytesIO
from xml.sax.saxutils import escape as xml_escape
from xml.etree import ElementTree
import tempfile
try:
    from PyPDF2 import PdfReader
except Exception:
    PdfReader = None
try:
    import openpyxl
except Exception:
    openpyxl = None
import os
from dotenv import load_dotenv
from openai import OpenAI

# Load variables from .env for local dev (Vercel ignores .env)
load_dotenv()

# The client will now automatically find OPENAI_API_KEY and OPENAI_BASE_URL
client = OpenAI()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(BASE_DIR, 'templates')
static_dir = os.path.join(BASE_DIR, 'static')
app = Flask(
    __name__,
    template_folder=template_dir,
    static_folder="static"
)


basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'kahoot_app.db')
app.config['SECRET_KEY'] = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

# Detect Vercel runtime early (used by DB config below)
IS_VERCEL = bool(os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"))

# Prefer Vercel Postgres env vars, fall back to generic DATABASE_URL.
# Uses local sqlite if nothing is provided (dev/testing).
database_url = (
    os.environ.get("POSTGRES_URL")
    or os.environ.get("POSTGRES_URL_NON_POOLING")
    or os.environ.get("POSTGRES_PRISMA_URL")
    or os.environ.get("DATABASE_URL")
)
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
if database_url and "sslmode=" not in database_url.lower():
    separator = "&" if "?" in database_url else "?"
    database_url = f"{database_url}{separator}sslmode=require"
if IS_VERCEL and not database_url:
    raise RuntimeError("Vercel Postgres env vars missing. Set POSTGRES_URL in Vercel.")
USE_POSTGRES = bool(database_url)
if USE_POSTGRES:
    import psycopg2                             
    import psycopg2.extras

DB_FILE = os.path.join(os.path.dirname(__file__), "kahoot_app.db")
app.config['SQLALCHEMY_DATABASE_URI'] = database_url or f"sqlite:///{DB_FILE}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

ALLOWED_QUESTION_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
ALLOWED_AVATAR_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
UPLOAD_PREFIX = "tmp_uploads" if IS_VERCEL else "uploads"
UPLOAD_BASE_DIR = (
    os.path.join(tempfile.gettempdir(), "oqa_uploads")
    if IS_VERCEL
    else os.path.join(os.path.dirname(__file__), "static", "uploads")
)
QUESTION_UPLOAD_DIR = os.path.join(UPLOAD_BASE_DIR, "questions")
AVATAR_UPLOAD_DIR = os.path.join(UPLOAD_BASE_DIR, "avatars")

# ---------------- DB CONNECTION ----------------
# Use a single canonical DB file across app and utility scripts.
# This avoids "saved but not visible" issues caused by writing to a different DB file.
DATABASE = DB_FILE
DEPARTMENTS = ["Computer", "Mechanical", "Electrical", "Civil"]
# OpenRouter configuration (using OPENAI_* env vars for compatibility)
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "google/gemini-2.0-flash-001")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
# Alias for backward compatibility
OPENROUTER_MODEL = OPENAI_MODEL

class DBConnection:
    def __init__(self, db_path):
        self.db_path = db_path
        self.is_postgres = USE_POSTGRES
        if self.is_postgres:
            self.conn = psycopg2.connect(
                database_url,
                connect_timeout=10,
                cursor_factory=psycopg2.extras.RealDictCursor
            )
        else:
            self.conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            # Give SQLite a chance to wait when DB is locked
            try:
                self.conn.execute("PRAGMA busy_timeout = 5000")
            except Exception:
                pass
            # Try to enable WAL mode for better concurrent access, but continue if DB is locked
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError as e:
                print(f"Warning: could not set WAL mode: {e}")
            # Ensure foreign key enforcement
            try:
                self.conn.execute("PRAGMA foreign_keys = ON")
            except Exception:
                pass
    
    def __enter__(self):
        # Return wrapper so callers can access is_postgres and helpers
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        self.conn.close()

    def _translate_sql(self, sql):
        if not self.is_postgres:
            return sql
        s = sql
        s = re.sub(r"\bDATETIME\b", "TIMESTAMP", s, flags=re.IGNORECASE)
        # Normalize SQLite AUTOINCREMENT to Postgres SERIAL
        s = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\b", "SERIAL PRIMARY KEY", s, flags=re.IGNORECASE)
        s = re.sub(r"\bAUTOINCREMENT\b", "", s, flags=re.IGNORECASE)
        s = re.sub(r"datetime\('now'\)", "CURRENT_TIMESTAMP", s, flags=re.IGNORECASE)
        s = re.sub(r"date\('now'\)", "CURRENT_DATE", s, flags=re.IGNORECASE)
        if re.match(r"\s*INSERT\s+OR\s+IGNORE\b", s, flags=re.IGNORECASE):
            s = re.sub(r"\bINSERT\s+OR\s+IGNORE\b", "INSERT", s, flags=re.IGNORECASE)
            if "ON CONFLICT" not in s.upper():
                s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        return s

    def _convert_params(self, sql):
        if not self.is_postgres:
            return sql
        # Convert qmark placeholders to psycopg2 style.
        return re.sub(r"\?", "%s", sql)

    def execute(self, sql, params=None):
        if not self.is_postgres:
            if params is None:
                return self.conn.execute(sql)
            return self.conn.execute(sql, params)
        q = self._translate_sql(sql)
        q = self._convert_params(q)
        cur = self.conn.cursor()
        if params is None:
            cur.execute(q)
        else:
            cur.execute(q, params)
        return cur

    def executemany(self, sql, seq_of_params):
        if not self.is_postgres:
            return self.conn.executemany(sql, seq_of_params)
        q = self._translate_sql(sql)
        q = self._convert_params(q)
        cur = self.conn.cursor()
        cur.executemany(q, seq_of_params)
        return cur
    
    # Allow direct access to connection methods for non-with usage
    def __getattr__(self, name):
        return getattr(self.conn, name)

def get_db_connection():
    return DBConnection(DATABASE)

def _insert_and_get_id(conn, sql, params, id_col):
    if conn.is_postgres:
        stmt = sql.rstrip().rstrip(";") + f" RETURNING {id_col}"
        cur = conn.execute(stmt, params)
        row = cur.fetchone()
        return row[id_col] if row else None
    cur = conn.execute(sql, params)
    return cur.lastrowid

def _parse_db_datetime(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value))
    except ValueError:
        try:
            return datetime.datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

def media_url(path):
    if not path:
        return ""
    if path.startswith("http://") or path.startswith("https://") or path.startswith("data:"):
        return path
    if path.startswith(f"{UPLOAD_PREFIX}/"):
        rel_path = path[len(f"{UPLOAD_PREFIX}/"):]
        return url_for("serve_tmp_upload", path=rel_path)
    return url_for('static', filename=path)

app.jinja_env.globals["media_url"] = media_url

def _save_question_image(upload):
    if not upload or not getattr(upload, "filename", ""):
        return None
    ext = os.path.splitext(upload.filename)[1].lower()
    if ext not in ALLOWED_QUESTION_IMAGE_EXTS:
        raise ValueError("Unsupported image type.")
    os.makedirs(QUESTION_UPLOAD_DIR, exist_ok=True)
    base = secure_filename(os.path.splitext(upload.filename)[0]) or "question"
    filename = f"{base}-{uuid.uuid4().hex}{ext}"
    save_path = os.path.join(QUESTION_UPLOAD_DIR, filename)
    upload.save(save_path)
    return f"{UPLOAD_PREFIX}/questions/{filename}"

def _ensure_avatar_dir():
    os.makedirs(AVATAR_UPLOAD_DIR, exist_ok=True)

def _avatar_url_from_profile_pic(profile_pic):
    default_avatar = url_for('static', filename='avatars/default.png')
    if not profile_pic:
        return default_avatar
    return media_url(profile_pic)

def _save_avatar_upload(upload):
    if not upload or not getattr(upload, "filename", ""):
        return None
    ext = os.path.splitext(upload.filename)[1].lower()
    if ext not in ALLOWED_AVATAR_EXTS:
        raise ValueError("Unsupported avatar file type.")
    _ensure_avatar_dir()
    base = secure_filename(os.path.splitext(upload.filename)[0]) or "avatar"
    filename = f"{base}-{uuid.uuid4().hex}{ext}"
    save_path = os.path.join(AVATAR_UPLOAD_DIR, filename)
    upload.save(save_path)
    return f"{UPLOAD_PREFIX}/avatars/{filename}"

def _save_avatar_svg(svg_text):
    if not svg_text or "<svg" not in svg_text.lower():
        raise ValueError("Invalid SVG data.")
    _ensure_avatar_dir()
    filename = f"avatar-{uuid.uuid4().hex}.svg"
    save_path = os.path.join(AVATAR_UPLOAD_DIR, filename)
    with open(save_path, "w", encoding="utf-8") as f:
        f.write(svg_text)
    return f"{UPLOAD_PREFIX}/avatars/{filename}"

def _save_avatar_data_url(data_url):
    if not data_url or not data_url.startswith("data:image/"):
        raise ValueError("Invalid image data.")
    header, b64 = data_url.split(",", 1)
    ext = "png"
    if "image/jpeg" in header:
        ext = "jpg"
    elif "image/webp" in header:
        ext = "webp"
    elif "image/gif" in header:
        ext = "gif"
    _ensure_avatar_dir()
    filename = f"avatar-{uuid.uuid4().hex}.{ext}"
    save_path = os.path.join(AVATAR_UPLOAD_DIR, filename)
    with open(save_path, "wb") as f:
        f.write(base64.b64decode(b64))
    return f"{UPLOAD_PREFIX}/avatars/{filename}"

@app.route("/uploads/<path:path>")
def serve_tmp_upload(path):
    # Serve uploaded assets from temp storage (Vercel) or local static uploads.
    if IS_VERCEL:
        return send_from_directory(UPLOAD_BASE_DIR, path)
    return send_from_directory(os.path.join(app.static_folder, "uploads"), path)

def _csv_response(filename, headers, rows):
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

def _xlsx_col_name(index):
    name = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name

def _sanitize_sheet_name(name, used_names):
    cleaned = re.sub(r"[:\\\\/?*\\[\\]]", " ", str(name or "Sheet")).strip() or "Sheet"
    cleaned = cleaned[:31]
    base = cleaned
    i = 1
    while cleaned in used_names:
        suffix = f" {i}"
        cleaned = (base[:31 - len(suffix)] + suffix).strip()
        i += 1
    used_names.add(cleaned)
    return cleaned

def _build_sheet_xml(rows, merges=None, col_widths=None):
    merges = merges or []
    col_widths = col_widths or []

    def _cell_payload(cell):
        if isinstance(cell, dict):
            return cell.get("v"), cell.get("s")
        return cell, None

    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
    ]
    if col_widths:
        xml_parts.append('<cols>')
        for idx, width in enumerate(col_widths, start=1):
            safe_width = max(8, min(float(width), 80))
            xml_parts.append(
                f'<col min="{idx}" max="{idx}" width="{safe_width:.2f}" customWidth="1"/>'
            )
        xml_parts.append('</cols>')
    xml_parts.append('<sheetData>')
    for r_idx, row in enumerate(rows, start=1):
        xml_parts.append(f'<row r="{r_idx}">')
        for c_idx, value in enumerate(row, start=1):
            value, style_id = _cell_payload(value)
            if value is None and style_id is None:
                continue
            cell_ref = f"{_xlsx_col_name(c_idx)}{r_idx}"
            style_attr = f' s="{int(style_id)}"' if style_id is not None else ""
            if isinstance(value, bool):
                value = int(value)
            if isinstance(value, (int, float)):
                xml_parts.append(f'<c r="{cell_ref}"{style_attr}><v>{value}</v></c>')
            else:
                txt = xml_escape(str(value))
                xml_parts.append(
                    f'<c r="{cell_ref}"{style_attr} t="inlineStr"><is><t>{txt}</t></is></c>'
                )
        xml_parts.append('</row>')
    xml_parts.append('</sheetData>')
    if merges:
        xml_parts.append(f'<mergeCells count="{len(merges)}">')
        for ref in merges:
            xml_parts.append(f'<mergeCell ref="{xml_escape(ref)}"/>')
        xml_parts.append('</mergeCells>')
    xml_parts.append('</worksheet>')
    return "".join(xml_parts).encode("utf-8")

def _styled_row(values, style_id):
    return [{"v": v, "s": style_id} for v in values]

def _auto_col_widths(rows):
    def _cell_value(cell):
        if isinstance(cell, dict):
            return cell.get("v")
        return cell

    max_cols = max((len(r) for r in rows), default=0)
    widths = [12.0] * max_cols
    for row in rows:
        for i, cell in enumerate(row):
            value = _cell_value(cell)
            if value is None:
                continue
            text = str(value)
            lines = text.splitlines() or [text]
            longest = max((len(line) for line in lines), default=0)
            # Slight padding for readability.
            widths[i] = max(widths[i], min(80.0, longest + 2))
    return widths

def _xlsx_response(filename, sheets):
    used_names = set()
    prepared = []
    for idx, sheet in enumerate(sheets, start=1):
        name = _sanitize_sheet_name(sheet.get("name", f"Sheet {idx}"), used_names)
        rows = sheet.get("rows") or []
        prepared.append({"name": name, "rows": rows})

    content_types = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    ]
    for i in range(1, len(prepared) + 1):
        content_types.append(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
    content_types.append('</Types>')
    content_types_xml = "".join(content_types).encode("utf-8")

    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    ).encode("utf-8")

    workbook_parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" ',
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
    ]
    for i, s in enumerate(prepared, start=1):
        workbook_parts.append(f'<sheet name="{xml_escape(s["name"])}" sheetId="{i}" r:id="rId{i}"/>')
    workbook_parts.append('</sheets></workbook>')
    workbook_xml = "".join(workbook_parts).encode("utf-8")

    workbook_rels_parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    ]
    for i in range(1, len(prepared) + 1):
        workbook_rels_parts.append(
            f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
        )
    workbook_rels_parts.append(
        f'<Relationship Id="rId{len(prepared) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    )
    workbook_rels_parts.append('</Relationships>')
    workbook_rels_xml = "".join(workbook_rels_parts).encode("utf-8")

    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="3">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="12"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font>'
        '</fonts>'
        '<fills count="6">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF4C1D95"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF7E43B5"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF1E4FB8"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFF4F5F8"/><bgColor indexed="64"/></patternFill></fill>'
        '</fills>'
        '<borders count="2">'
        '<border><left/><right/><top/><bottom/><diagonal/></border>'
        '<border><left style="thin"/><right style="thin"/><top style="thin"/><bottom style="thin"/><diagonal/></border>'
        '</borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="12">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/>'
        '<xf numFmtId="0" fontId="1" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"/>'
        '<xf numFmtId="0" fontId="2" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1"/>'
        '<xf numFmtId="0" fontId="1" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/>'
        '<xf numFmtId="0" fontId="0" fillId="5" borderId="1" xfId="0" applyFill="1" applyBorder="1"/>'
        '<xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center"/></xf>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left"/></xf>'
        '<xf numFmtId="0" fontId="1" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center"/></xf>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left"/></xf>'
        '</cellXfs>'
        '</styleSheet>'
    ).encode("utf-8")

    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", rels_xml)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        zf.writestr("xl/styles.xml", styles_xml)
        for i, s in enumerate(prepared, start=1):
            zf.writestr(
                f"xl/worksheets/sheet{i}.xml",
                _build_sheet_xml(
                    s["rows"],
                    merges=s.get("merges"),
                    col_widths=s.get("col_widths") or _auto_col_widths(s["rows"])
                )
            )

    return Response(
        output.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

def _difficulty_bucket(correct_count, attempts):
    if not attempts:
        return "no_data"
    rate = correct_count / attempts
    if rate >= 0.8:
        return "easy"
    if rate >= 0.5:
        return "medium"
    return "difficult"

def _get_live_question_state(conn, session_data):
    if not session_data:
        return {"finished": True, "started": False}

    if session_data["started"] != 1:
        return {"finished": False, "started": False}

    current_q = session_data["current_question"] or 0
    total_row = conn.execute(
        "SELECT COUNT(*) AS total FROM questions WHERE quiz_id=?",
        (session_data["quiz_id"],)
    ).fetchone()
    total_questions = total_row["total"] if total_row else 0
    question = conn.execute(
        "SELECT * FROM questions WHERE quiz_id=? LIMIT 1 OFFSET ?",
        (session_data["quiz_id"], current_q)
    ).fetchone()

    if not question:
        return {"finished": True, "started": True}

    options = conn.execute(
        "SELECT option_id, option_text, is_correct FROM options WHERE question_id=? ORDER BY option_id",
        (question["question_id"],)
    ).fetchall()

    started_at = _parse_db_datetime(session_data["question_started_at"])
    now_utc = datetime.datetime.utcnow()
    if not started_at:
        started_at = now_utc

    time_limit = question["time_limit"] or 20
    intro_seconds = 5
    elapsed = max(0, int((now_utc - started_at).total_seconds()))
    intro_remaining = max(0, intro_seconds - elapsed)
    active_elapsed = max(0, elapsed - intro_seconds)
    time_left = max(0, time_limit - active_elapsed)

    correct_option = next((opt["option_text"] for opt in options if opt["is_correct"] == 1), None)

    return {
        "finished": False,
        "started": True,
        "question_id": question["question_id"],
        "question_index": current_q,
        "question_number": current_q + 1,
        "total_questions": total_questions,
        "is_last_question": (current_q + 1) >= total_questions if total_questions > 0 else False,
        "question_text": question["question_text"],
        "media_url": media_url(question["media_url"]),
        "time_limit": time_limit,
        "time_left": time_left,
        "intro_remaining": intro_remaining,
        "phase": "question" if time_left > 0 else "reveal",
        "options": [{"id": opt["option_id"], "text": opt["option_text"]} for opt in options],
        "correct_option": correct_option,
    }

def _get_answer_breakdown(conn, session_id, question_id):
    rows = conn.execute(
        """
        SELECT answer, player_name
        FROM player_answers
        WHERE session_id=? AND question_id=?
        ORDER BY submitted_at ASC, answer_id ASC
        """,
        (session_id, question_id)
    ).fetchall()

    breakdown = {}
    for row in rows:
        answer = row["answer"]
        if answer not in breakdown:
            breakdown[answer] = {"count": 0, "players": []}
        breakdown[answer]["count"] += 1
        breakdown[answer]["players"].append(row["player_name"])

    return breakdown

def _get_live_leaderboard_rows(conn, session_id, limit=None):
    base_sql = """
        SELECT
            p.nickname AS player_name,
            COALESCE(a.score, 0) AS score,
            COALESCE(a.correct_answers, 0) AS correct_answers,
            COALESCE(a.time_taken, 0) AS time_taken
        FROM participants p
        LEFT JOIN (
            SELECT
                player_name,
                COALESCE(SUM(score_awarded), 0) AS score,
                COALESCE(SUM(is_correct), 0) AS correct_answers,
                COALESCE(SUM(COALESCE(response_ms, 0)), 0) AS time_taken
            FROM player_answers
            WHERE session_id=?
            GROUP BY player_name
        ) a ON a.player_name = p.nickname
        WHERE p.session_id=?
        ORDER BY score DESC, correct_answers DESC, time_taken ASC, player_name ASC
    """
    params = [session_id, session_id]
    if isinstance(limit, int) and limit > 0:
        base_sql += " LIMIT ?"
        params.append(limit)
    scores = conn.execute(base_sql, tuple(params)).fetchall()

    return [dict(row) for row in scores]

def _get_live_avatar_map(conn, session_id):
    default_avatar = url_for('static', filename='avatars/default.png')
    participant_rows = conn.execute(
        "SELECT nickname, user_id FROM participants WHERE session_id=?",
        (session_id,)
    ).fetchall()
    name_to_user = { (r["nickname"] or "").strip().lower(): r["user_id"] for r in participant_rows }
    avatar_cache = {}

    def resolve(player_name):
        key = (player_name or "").strip().lower()
        if not key:
            return default_avatar
        if key in avatar_cache:
            return avatar_cache[key]
        user_id = name_to_user.get(key)
        row = None
        if user_id:
            row = conn.execute(
                "SELECT profile_pic FROM Users WHERE user_id=? LIMIT 1",
                (user_id,)
            ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT profile_pic FROM Users WHERE lower(username)=lower(?) LIMIT 1",
                (player_name,)
            ).fetchone()
        avatar_url = _avatar_url_from_profile_pic(row["profile_pic"] if row else None)
        avatar_cache[key] = avatar_url
        return avatar_url

    return resolve

def _get_player_live_rank_details(conn, session_id, player_name):
    rows = _get_live_leaderboard_rows(conn, session_id)
    total_players = len(rows)
    if total_players == 0:
        return {
            "rank": None,
            "total_players": 0,
            "score": 0,
            "points_to_next": None,
            "next_player": None
        }

    for idx, row in enumerate(rows):
        if row["player_name"] == player_name:
            rank = idx + 1
            score = row["score"] or 0
            if rank == 1:
                return {
                    "rank": rank,
                    "total_players": total_players,
                    "score": score,
                    "points_to_next": 0,
                    "next_player": None
                }
            above = rows[idx - 1]
            points_to_next = max(0, (above["score"] or 0) - score)
            return {
                "rank": rank,
                "total_players": total_players,
                "score": score,
                "points_to_next": points_to_next,
                "next_player": above["player_name"]
            }

    return {
        "rank": None,
        "total_players": total_players,
        "score": 0,
        "points_to_next": None,
        "next_player": None
    }

def _get_question_ranking(conn, session_id, question_id):
    participants = conn.execute(
        "SELECT nickname FROM participants WHERE session_id=? ORDER BY nickname ASC",
        (session_id,)
    ).fetchall()

    answer_rows = conn.execute(
        """
        SELECT player_name, answer, is_correct, response_ms, answer_id
        FROM player_answers
        WHERE session_id=? AND question_id=?
        ORDER BY answer_id ASC
        """,
        (session_id, question_id)
    ).fetchall()

    first_answer_by_player = {}
    for row in answer_rows:
        player = row["player_name"]
        if player not in first_answer_by_player:
            first_answer_by_player[player] = dict(row)

    ranking = []
    for participant in participants:
        name = participant["nickname"]
        row = first_answer_by_player.get(name)
        if not row:
            ranking.append({
                "player_name": name,
                "answer": None,
                "is_correct": 0,
                "response_ms": None,
                "status": "no_answer"
            })
            continue

        status = "correct" if row["is_correct"] == 1 else "wrong"
        ranking.append({
            "player_name": name,
            "answer": row["answer"],
            "is_correct": 1 if row["is_correct"] == 1 else 0,
            "response_ms": row["response_ms"],
            "status": status
        })

    def sort_key(item):
        if item["status"] == "correct":
            return (0, item["response_ms"] if item["response_ms"] is not None else 10**12, item["player_name"].lower())
        if item["status"] == "wrong":
            return (1, item["response_ms"] if item["response_ms"] is not None else 10**12, item["player_name"].lower())
        return (2, 10**12, item["player_name"].lower())

    ranking.sort(key=sort_key)
    top3 = [r for r in ranking if r["status"] == "correct"][:3]

    return {
        "top3": top3,
        "rows": ranking
    }

def _get_player_question_answer(conn, session_id, question_id, player_name):
    if not player_name:
        return None
    row = conn.execute(
        """
        SELECT answer, is_correct, response_ms, score_awarded
        FROM player_answers
        WHERE session_id=? AND question_id=? AND player_name=?
        ORDER BY answer_id ASC
        LIMIT 1
        """,
        (session_id, question_id, player_name)
    ).fetchone()
    return dict(row) if row else None


def ensure_legacy_practice_quiz_row(conn, quiz_id, quiz_name, description):
    if conn.is_postgres:
        return
    """Keep legacy PracticeQuizzes row in sync when old FK still points to it."""
    try:
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='PracticeQuizzes'"
        ).fetchone()
        if not table_exists:
            return

        conn.execute(
            """
            INSERT OR IGNORE INTO PracticeQuizzes (quiz_id, title, description, created_at)
            VALUES (?, ?, ?, datetime('now'))
            """,
            (quiz_id, quiz_name, description),
        )
        conn.execute(
            "UPDATE PracticeQuizzes SET title=?, description=? WHERE quiz_id=?",
            (quiz_name, description, quiz_id),
        )
    except Exception as e:
        print(f"Warning: could not sync legacy PracticeQuizzes row: {e}")

def migrate_practice_tables():
    """Migrate existing Practice_Quizzes table to include new columns"""
    with get_db_connection() as conn:
        if conn.is_postgres:
            return
        try:
            # Check if Practice_Quizzes table exists
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='Practice_Quizzes'"
            )
            if cursor.fetchone():
                # Table exists, check if it has the created_by column
                cursor = conn.execute("PRAGMA table_info(Practice_Quizzes)")
                columns = [row[1] for row in cursor.fetchall()]
                if 'practice_id' in columns and 'quiz_id' not in columns:
                    conn.execute("ALTER TABLE Practice_Quizzes RENAME COLUMN practice_id TO quiz_id")
                    cursor = conn.execute("PRAGMA table_info(Practice_Quizzes)")
                    columns = [row[1] for row in cursor.fetchall()]
                if 'title' in columns and 'quiz_name' not in columns:
                    conn.execute("ALTER TABLE Practice_Quizzes RENAME COLUMN title TO quiz_name")
                    cursor = conn.execute("PRAGMA table_info(Practice_Quizzes)")
                    columns = [row[1] for row in cursor.fetchall()]
                
                # Add missing columns
                if 'created_by' not in columns:
                    conn.execute("ALTER TABLE Practice_Quizzes ADD COLUMN created_by INTEGER DEFAULT 1")
                    print("✅ Added created_by column to Practice_Quizzes")
                
                if 'teacher_id' not in columns:
                    conn.execute("ALTER TABLE Practice_Quizzes ADD COLUMN teacher_id INTEGER")
                    conn.execute("UPDATE Practice_Quizzes SET teacher_id = created_by WHERE teacher_id IS NULL")
                if 'created_at' not in columns:
                    conn.execute("ALTER TABLE Practice_Quizzes ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                    print("✅ Added created_at column to Practice_Quizzes")
                
                conn.commit()
            
            # Check PracticeQuestions table
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='PracticeQuestions'"
            )
            if cursor.fetchone():
                cursor = conn.execute("PRAGMA table_info(PracticeQuestions)")
                columns = [row[1] for row in cursor.fetchall()]
                if 'practice_id' in columns and 'quiz_id' not in columns:
                    conn.execute("ALTER TABLE PracticeQuestions RENAME COLUMN practice_id TO quiz_id")
                    cursor = conn.execute("PRAGMA table_info(PracticeQuestions)")
                    columns = [row[1] for row in cursor.fetchall()]
                
                if 'question_type' not in columns:
                    conn.execute("ALTER TABLE PracticeQuestions ADD COLUMN question_type TEXT DEFAULT 'MCQ'")
                    print("✅ Added question_type column to PracticeQuestions")
                
                if 'explanation' not in columns:
                    conn.execute("ALTER TABLE PracticeQuestions ADD COLUMN explanation TEXT")
                    print("✅ Added explanation column to PracticeQuestions")
                
                if 'media_url' not in columns:
                    conn.execute("ALTER TABLE PracticeQuestions ADD COLUMN media_url TEXT")
                    print("✅ Added media_url column to PracticeQuestions")
                
                if 'created_at' not in columns:
                    conn.execute("ALTER TABLE PracticeQuestions ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                    print("✅ Added created_at column to PracticeQuestions")
                
                conn.commit()
            
            # Check PracticeOptions table
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='PracticeOptions'"
            )
            if cursor.fetchone():
                cursor = conn.execute("PRAGMA table_info(PracticeOptions)")
                columns = [row[1] for row in cursor.fetchall()]
                
                if 'option_order' not in columns:
                    conn.execute("ALTER TABLE PracticeOptions ADD COLUMN option_order INTEGER")
                    print("✅ Added option_order column to PracticeOptions")
                
                conn.commit()
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='PracticeProgress'"
            )
            if cursor.fetchone():
                cursor = conn.execute("PRAGMA table_info(PracticeProgress)")
                columns = [row[1] for row in cursor.fetchall()]
                if 'practice_id' in columns and 'quiz_id' not in columns:
                    conn.execute("ALTER TABLE PracticeProgress RENAME COLUMN practice_id TO quiz_id")
                    conn.commit()
                    columns = [row[1] for row in conn.execute("PRAGMA table_info(PracticeProgress)").fetchall()]

                fk_rows = conn.execute("PRAGMA foreign_key_list(PracticeProgress)").fetchall()
                has_valid_quiz_fk = any(
                    (row[2] == 'Practice_Quizzes' and row[3] == 'quiz_id' and row[4] == 'quiz_id')
                    for row in fk_rows
                )
                if 'quiz_id' in columns and not has_valid_quiz_fk:
                    conn.execute("PRAGMA foreign_keys = OFF")
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS PracticeProgress_new (
                            progress_id INTEGER PRIMARY KEY,
                            user_id INTEGER NOT NULL,
                            quiz_id INTEGER NOT NULL,
                            score INTEGER DEFAULT 0,
                            correct_answers INTEGER DEFAULT 0,
                            total_questions INTEGER DEFAULT 0,
                            time_spent INTEGER DEFAULT 0,
                            completed_at TIMESTAMP,
                            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY (user_id) REFERENCES Users(user_id),
                            FOREIGN KEY (quiz_id) REFERENCES Practice_Quizzes(quiz_id),
                            UNIQUE(user_id, quiz_id)
                        )
                    """)
                    conn.execute("""
                        INSERT OR REPLACE INTO PracticeProgress_new
                            (progress_id, user_id, quiz_id, score, correct_answers, total_questions, time_spent, completed_at, started_at)
                        SELECT
                            progress_id, user_id, quiz_id, score, correct_answers, total_questions, time_spent, completed_at, started_at
                        FROM PracticeProgress
                    """)
                    conn.execute("DROP TABLE PracticeProgress")
                    conn.execute("ALTER TABLE PracticeProgress_new RENAME TO PracticeProgress")
                    conn.execute("PRAGMA foreign_keys = ON")
                    conn.commit()
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='PracticeQuizzes'"
            )
            if cursor.fetchone():
                conn.execute(
                    """
                    INSERT OR IGNORE INTO PracticeQuizzes (quiz_id, title, description, created_at)
                    SELECT quiz_id, quiz_name, COALESCE(description, ''), COALESCE(created_at, datetime('now'))
                    FROM Practice_Quizzes
                    """
                )
                conn.execute(
                    """
                    UPDATE PracticeQuizzes
                    SET
                        title = COALESCE((SELECT pq.quiz_name FROM Practice_Quizzes pq WHERE pq.quiz_id = PracticeQuizzes.quiz_id), title),
                        description = COALESCE((SELECT pq.description FROM Practice_Quizzes pq WHERE pq.quiz_id = PracticeQuizzes.quiz_id), description)
                    WHERE EXISTS (SELECT 1 FROM Practice_Quizzes pq WHERE pq.quiz_id = PracticeQuizzes.quiz_id)
                    """
                )
                conn.commit()
        except Exception as e:
            print(f"✅ Database schema up to date: {e}")

def _init_practice_tables(conn):
    # Ensure Users exists before FK references (especially important for Postgres deployments).
    conn.execute("""
    CREATE TABLE IF NOT EXISTS Users(
        user_id INTEGER PRIMARY KEY,
        username TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL,
        department TEXT DEFAULT 'Computer',
        profile_pic TEXT
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS Practice_Quizzes (
        quiz_id INTEGER PRIMARY KEY,
        quiz_name TEXT NOT NULL,
        description TEXT,
        teacher_id INTEGER,
        created_by INTEGER NOT NULL,
        department TEXT,
        target_departments TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (created_by) REFERENCES Users(user_id)
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS PracticeQuestions (
        question_id INTEGER PRIMARY KEY,
        quiz_id INTEGER NOT NULL,
        question_text TEXT NOT NULL,
        question_type TEXT DEFAULT 'MCQ',
        explanation TEXT,
        media_url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (quiz_id) REFERENCES Practice_Quizzes(quiz_id)
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS PracticeOptions (
        option_id INTEGER PRIMARY KEY,
        question_id INTEGER NOT NULL,
        option_text TEXT NOT NULL,
        is_correct INTEGER DEFAULT 0,
        option_order INTEGER,
        FOREIGN KEY (question_id) REFERENCES PracticeQuestions(question_id)
    )
    """)
    # Progress tracking table
    conn.execute("""
    CREATE TABLE IF NOT EXISTS PracticeProgress (
        progress_id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL,
        quiz_id INTEGER NOT NULL,
        score INTEGER DEFAULT 0,
        correct_answers INTEGER DEFAULT 0,
        total_questions INTEGER DEFAULT 0,
        time_spent INTEGER DEFAULT 0,
        completed_at TIMESTAMP,
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES Users(user_id),
        FOREIGN KEY (quiz_id) REFERENCES Practice_Quizzes(quiz_id),
        UNIQUE(user_id, quiz_id)
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS PracticeAnswers (
        answer_id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL,
        quiz_id INTEGER NOT NULL,
        question_id INTEGER NOT NULL,
        selected_option_id INTEGER,
        is_correct INTEGER DEFAULT 0,
        submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES Users(user_id),
        FOREIGN KEY (quiz_id) REFERENCES Practice_Quizzes(quiz_id),
        FOREIGN KEY (question_id) REFERENCES PracticeQuestions(question_id),
        FOREIGN KEY (selected_option_id) REFERENCES PracticeOptions(option_id)
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS PracticeFirstAnswers (
        answer_id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL,
        quiz_id INTEGER NOT NULL,
        question_id INTEGER NOT NULL,
        selected_option_id INTEGER,
        is_correct INTEGER DEFAULT 0,
        submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES Users(user_id),
        FOREIGN KEY (quiz_id) REFERENCES Practice_Quizzes(quiz_id),
        FOREIGN KEY (question_id) REFERENCES PracticeQuestions(question_id),
        FOREIGN KEY (selected_option_id) REFERENCES PracticeOptions(option_id),
        UNIQUE(user_id, quiz_id, question_id)
    )
    """)

def init_practice_table(conn=None):
    if conn is None:
        with get_db_connection() as conn:
            _init_practice_tables(conn)
        return
    _init_practice_tables(conn)

# ---------------- DB INIT ----------------
def init_db():
    with get_db_connection() as conn:
        # Users Table
        conn.execute("""
        CREATE TABLE IF NOT EXISTS Users(
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL,
            department TEXT DEFAULT 'Computer',
            profile_pic TEXT
        )""")
        # Ensure practice tables exist before any FK references in this init.
        _init_practice_tables(conn)
        # Quizzes Table
        conn.execute("""
CREATE TABLE IF NOT EXISTS Quizzes(
    quiz_id INTEGER PRIMARY KEY,

    quiz_name TEXT NOT NULL,
    description TEXT,

    created_by INTEGER NOT NULL,      -- teacher_id

    subject TEXT,                     -- optional
    quiz_code TEXT UNIQUE,            -- practice join code

    mode TEXT DEFAULT 'practice',     -- 'practice' or 'live'

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (created_by) REFERENCES Users(user_id)
)
""")

        # Questions Table
        conn.execute("""
CREATE TABLE IF NOT EXISTS Questions(
    question_id INTEGER PRIMARY KEY,

    quiz_id INTEGER NOT NULL,

    question_text TEXT NOT NULL,

    question_type TEXT DEFAULT 'MCQ',

    time_limit INTEGER DEFAULT 30,          -- live quiz timer

    marks INTEGER DEFAULT 10,               -- score per question

    explanation TEXT,                       -- practice mode explanation

    media_url TEXT,

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (quiz_id) REFERENCES Quizzes(quiz_id)
)
""")

        # Options Table
        conn.execute("""
CREATE TABLE IF NOT EXISTS Options(
    option_id INTEGER PRIMARY KEY,

    question_id INTEGER NOT NULL,

    option_text TEXT NOT NULL,

    is_correct INTEGER DEFAULT 0,   -- 0 = wrong, 1 = correct

    option_order INTEGER,           -- display order (A,B,C,D)

    FOREIGN KEY (question_id) REFERENCES Questions(question_id)
)
""")

        # GameSessions Table
        conn.execute("""
        CREATE TABLE IF NOT EXISTS GameSessions(
            session_id INTEGER PRIMARY KEY,
            quiz_id INTEGER,
            game_pin INTEGER UNIQUE,
            start_time DATETIME DEFAULT CURRENT_TIMESTAMP,
            end_time DATETIME,
            FOREIGN KEY (quiz_id) REFERENCES Quizzes(quiz_id)
        )""")
        # PlayerScores Table
        conn.execute("""
        CREATE TABLE IF NOT EXISTS PlayerScores(
            score_id INTEGER PRIMARY KEY,
            session_id INTEGER,
            user_id INTEGER,
            score INTEGER DEFAULT 0,
            correct_answers INTEGER DEFAULT 0,
            time_taken INTEGER DEFAULT 0,
            FOREIGN KEY (session_id) REFERENCES GameSessions(session_id),
            FOREIGN KEY (user_id) REFERENCES Users(user_id)
        )""")

        # ---------------- Live Sessions Table ----------------
        conn.execute("""
        CREATE TABLE IF NOT EXISTS live_sessions (
    session_id INTEGER PRIMARY KEY,
    quiz_id INTEGER NOT NULL,
    pin INTEGER UNIQUE NOT NULL,
    is_active INTEGER DEFAULT 1,
    created_by INTEGER,
    start_time DATETIME DEFAULT NULL,
    current_question INTEGER DEFAULT 0,
    started INTEGER DEFAULT 0,
    question_started_at DATETIME DEFAULT NULL,
    final_released INTEGER DEFAULT 0,
    scoreboard_released INTEGER DEFAULT 0,
    FOREIGN KEY (quiz_id) REFERENCES Quizzes(quiz_id),
    FOREIGN KEY (created_by) REFERENCES Users(user_id)
)
""")

        # ---------------- Participants Table ----------------
        conn.execute("""
        CREATE TABLE IF NOT EXISTS participants (
            participant_id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL,
            nickname TEXT NOT NULL,
            user_id INTEGER,
            joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES live_sessions(session_id),
            FOREIGN KEY (user_id) REFERENCES Users(user_id)
        )""")

        conn.execute("""
        CREATE TABLE IF NOT EXISTS player_answers (
    answer_id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    question_id INTEGER,
    question_index INTEGER DEFAULT 0,
    player_name TEXT NOT NULL,
    answer TEXT NOT NULL,
    is_correct INTEGER DEFAULT 0,
    response_ms INTEGER DEFAULT 0,
    score_awarded INTEGER DEFAULT 0,
    submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES live_sessions(session_id)
)

""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS DailyTips(
            tip_id INTEGER PRIMARY KEY,
            content_text TEXT NOT NULL,
            content_type TEXT NOT NULL,
            subject TEXT DEFAULT 'general',
            difficulty_level TEXT DEFAULT 'beginner',
            language TEXT DEFAULT 'en',
            is_active INTEGER DEFAULT 1,
            publish_date DATE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS UserTipViews(
            view_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            tip_id INTEGER NOT NULL,
            viewed_on DATE NOT NULL,
            reward_points INTEGER DEFAULT 0,
            viewed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, viewed_on),
            FOREIGN KEY (user_id) REFERENCES Users(user_id),
            FOREIGN KEY (tip_id) REFERENCES DailyTips(tip_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS StudyNotes(
            note_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES Users(user_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS Flashcards(
            card_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            front_text TEXT NOT NULL,
            back_text TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES Users(user_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS DailyGoals(
            goal_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            goal_text TEXT NOT NULL,
            target_date DATE,
            is_completed INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES Users(user_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS StudyJournal(
            journal_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            study_date DATE NOT NULL,
            minutes_spent INTEGER DEFAULT 0,
            topics TEXT,
            notes TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES Users(user_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS ResourceLibrary(
            resource_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            resource_type TEXT DEFAULT 'link',
            url TEXT NOT NULL,
            description TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES Users(user_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS StudyReminders(
            reminder_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            due_date DATE,
            is_done INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES Users(user_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS MindMaps(
            map_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            central_topic TEXT NOT NULL,
            related_topics TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES Users(user_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS SelfAssessment(
            assessment_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            topic_name TEXT NOT NULL,
            status TEXT DEFAULT 'learning',
            notes TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES Users(user_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS PomodoroLogs(
            log_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            focus_minutes INTEGER DEFAULT 25,
            break_minutes INTEGER DEFAULT 5,
            cycles_completed INTEGER DEFAULT 1,
            logged_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES Users(user_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS PracticeFirstAttempts(
            attempt_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            quiz_id INTEGER NOT NULL,
            score INTEGER DEFAULT 0,
            correct_answers INTEGER DEFAULT 0,
            total_questions INTEGER DEFAULT 0,
            attempted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, quiz_id),
            FOREIGN KEY (user_id) REFERENCES Users(user_id),
            FOREIGN KEY (quiz_id) REFERENCES Practice_Quizzes(quiz_id)
        )
        """)
        # Keep only the earliest stored first-attempt row per student+quiz,
        # then enforce uniqueness for legacy databases that may have duplicates.
        conn.execute("""
            DELETE FROM PracticeFirstAttempts
            WHERE attempt_id NOT IN (
                SELECT MIN(attempt_id)
                FROM PracticeFirstAttempts
                GROUP BY user_id, quiz_id
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_practice_first_attempt_unique
            ON PracticeFirstAttempts(user_id, quiz_id)
        """)
        conn.execute("""
            INSERT OR IGNORE INTO PracticeFirstAnswers
                (user_id, quiz_id, question_id, selected_option_id, is_correct, submitted_at)
            SELECT
                pa.user_id, pa.quiz_id, pa.question_id, pa.selected_option_id, pa.is_correct, pa.submitted_at
            FROM PracticeAnswers pa
            JOIN PracticeFirstAttempts pfa
                ON pfa.user_id = pa.user_id AND pfa.quiz_id = pa.quiz_id
        """)
        if not conn.is_postgres:
            live_session_columns = [row[1] for row in conn.execute("PRAGMA table_info(live_sessions)").fetchall()]
            if 'started' not in live_session_columns:
                conn.execute("ALTER TABLE live_sessions ADD COLUMN started INTEGER DEFAULT 0")
            if 'question_started_at' not in live_session_columns:
                conn.execute("ALTER TABLE live_sessions ADD COLUMN question_started_at DATETIME DEFAULT NULL")
            if 'final_released' not in live_session_columns:
                conn.execute("ALTER TABLE live_sessions ADD COLUMN final_released INTEGER DEFAULT 0")
            if 'scoreboard_released' not in live_session_columns:
                conn.execute("ALTER TABLE live_sessions ADD COLUMN scoreboard_released INTEGER DEFAULT 0")

            answer_columns = [row[1] for row in conn.execute("PRAGMA table_info(player_answers)").fetchall()]
            if 'question_id' not in answer_columns:
                conn.execute("ALTER TABLE player_answers ADD COLUMN question_id INTEGER")
            if 'question_index' not in answer_columns:
                conn.execute("ALTER TABLE player_answers ADD COLUMN question_index INTEGER DEFAULT 0")
            if 'is_correct' not in answer_columns:
                conn.execute("ALTER TABLE player_answers ADD COLUMN is_correct INTEGER DEFAULT 0")
            if 'response_ms' not in answer_columns:
                conn.execute("ALTER TABLE player_answers ADD COLUMN response_ms INTEGER DEFAULT 0")
            if 'score_awarded' not in answer_columns:
                conn.execute("ALTER TABLE player_answers ADD COLUMN score_awarded INTEGER DEFAULT 0")
            question_columns = [row[1] for row in conn.execute("PRAGMA table_info(Questions)").fetchall()]
            if 'media_url' not in question_columns:
                conn.execute("ALTER TABLE Questions ADD COLUMN media_url TEXT")
            # Backward-compatible schema updates
            user_columns = [row[1] for row in conn.execute("PRAGMA table_info(Users)").fetchall()]
            if 'department' not in user_columns:
                conn.execute("ALTER TABLE Users ADD COLUMN department TEXT DEFAULT 'Computer'")
                conn.execute("UPDATE Users SET department='Computer' WHERE department IS NULL OR department=''")
            if 'profile_pic' not in user_columns:
                conn.execute("ALTER TABLE Users ADD COLUMN profile_pic TEXT")
            if 'theme_mode' not in user_columns:
                conn.execute("ALTER TABLE Users ADD COLUMN theme_mode TEXT DEFAULT 'light'")
            if 'font_scale' not in user_columns:
                conn.execute("ALTER TABLE Users ADD COLUMN font_scale TEXT DEFAULT 'medium'")
            if 'app_language' not in user_columns:
                conn.execute("ALTER TABLE Users ADD COLUMN app_language TEXT DEFAULT 'en'")
            if 'email_alerts' not in user_columns:
                conn.execute("ALTER TABLE Users ADD COLUMN email_alerts INTEGER DEFAULT 1")
            if 'mute_notifications' not in user_columns:
                conn.execute("ALTER TABLE Users ADD COLUMN mute_notifications INTEGER DEFAULT 0")
            if 'session_version' not in user_columns:
                conn.execute("ALTER TABLE Users ADD COLUMN session_version INTEGER DEFAULT 0")

            practice_table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='Practice_Quizzes'"
            ).fetchone()
            if practice_table_exists:
                practice_quiz_columns = [row[1] for row in conn.execute("PRAGMA table_info(Practice_Quizzes)").fetchall()]
                if 'department' not in practice_quiz_columns:
                    conn.execute("ALTER TABLE Practice_Quizzes ADD COLUMN department TEXT")
                conn.execute("""
                    UPDATE Practice_Quizzes
                    SET department = (
                        SELECT COALESCE(NULLIF(u.department, ''), 'Computer')
                        FROM Users u
                        WHERE u.user_id = Practice_Quizzes.created_by
                    )
                    WHERE department IS NULL OR department = ''
                """)
                if 'target_departments' not in practice_quiz_columns:
                    conn.execute("ALTER TABLE Practice_Quizzes ADD COLUMN target_departments TEXT")
                conn.execute("""
                    UPDATE Practice_Quizzes
                    SET target_departments = COALESCE(NULLIF(department, ''), 'Computer')
                    WHERE target_departments IS NULL OR target_departments = ''
                """)

            participant_columns = [row[1] for row in conn.execute("PRAGMA table_info(participants)").fetchall()]
            if 'user_id' not in participant_columns:
                conn.execute("ALTER TABLE participants ADD COLUMN user_id INTEGER")

            tip_view_columns = [row[1] for row in conn.execute("PRAGMA table_info(UserTipViews)").fetchall()]
            if 'reward_points' not in tip_view_columns:
                conn.execute("ALTER TABLE UserTipViews ADD COLUMN reward_points INTEGER DEFAULT 0")

        # Prevent duplicate live joins and duplicate answer submissions.
        # Clean legacy duplicate rows first so unique indexes can be created safely.
        try:
            conn.execute("""
                DELETE FROM participants
                WHERE participant_id NOT IN (
                    SELECT MIN(participant_id)
                    FROM participants
                    GROUP BY session_id, nickname
                )
            """)
        except Exception:
            pass

        try:
            conn.execute("""
                DELETE FROM player_answers
                WHERE answer_id NOT IN (
                    SELECT MIN(answer_id)
                    FROM player_answers
                    WHERE question_id IS NOT NULL
                    GROUP BY session_id, question_id, player_name
                )
                AND question_id IS NOT NULL
            """)
        except Exception:
            pass

        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_participants_session_nickname
            ON participants(session_id, nickname)
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_player_answers_once
            ON player_answers(session_id, question_id, player_name)
            WHERE question_id IS NOT NULL
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_participants_session
            ON participants(session_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_player_answers_session_player
            ON player_answers(session_id, player_name)
        """)

        tip_count = conn.execute("SELECT COUNT(*) AS c FROM DailyTips").fetchone()["c"]
        if tip_count == 0:
            sample_tips = [
                ("Did you know honey never spoils?", "fact", "general", "beginner", "en"),
                ("When you learn a new term, use it once in a sentence for better memory.", "life_hack", "general", "beginner", "en"),
                ("Break a large problem into smaller steps before solving.", "tip", "general", "beginner", "en"),
                ("Revision after 24 hours improves long-term retention.", "tip", "general", "intermediate", "en"),
                ("Prime numbers are numbers greater than 1 with exactly two factors.", "fact", "math", "beginner", "en"),
                ("In physics, acceleration is change in velocity per unit time.", "fact", "science", "beginner", "en"),
                ("Use active voice in most English sentences for clarity.", "tip", "grammar", "beginner", "en"),
                ("आज का विचार: छोटे-छोटे निरंतर प्रयास बड़ी सफलता बनाते हैं।", "quote", "general", "beginner", "hi"),
                ("मराठी टिप: नवीन शब्द लक्षात ठेवायचा असेल तर तो वाक्यात वापरा.", "tip", "general", "beginner", "mr"),
                ("Practice spaced repetition: revisit difficult topics at increasing intervals.", "tip", "general", "advanced", "en"),
                ("Reward yourself after completing a focused 25-minute study sprint.", "life_hack", "general", "beginner", "en"),
                ("Science fact: Water expands when it freezes, unlike most liquids.", "fact", "science", "intermediate", "en")
            ]
            conn.executemany("""
                INSERT INTO DailyTips (content_text, content_type, subject, difficulty_level, language, is_active, publish_date)
                VALUES (?, ?, ?, ?, ?, 1, date('now'))
            """, sample_tips)

        multilingual_tips = [
            ("हिंदी टिप: नया कॉन्सेप्ट सीखने के बाद उसे किसी दोस्त को समझाकर देखें।", "tip", "general", "beginner", "hi"),
            ("हिंदी तथ्य: मानव मस्तिष्क नींद के दौरान यादों को मजबूत करता है।", "fact", "science", "intermediate", "hi"),
            ("हिंदी जीवन मंत्र: कठिन काम को 10 मिनट के छोटे हिस्सों में शुरू करें।", "life_hack", "general", "beginner", "hi"),
            ("मराठी तथ्य: मध योग्यरीत्या साठवला तर तो दीर्घकाळ खराब होत नाही.", "fact", "general", "beginner", "mr"),
            ("मराठी अभ्यास टिप: २५ मिनिटे लक्ष केंद्रित करून अभ्यास करा, मग ५ मिनिटे विश्रांती घ्या.", "life_hack", "general", "beginner", "mr"),
            ("मराठी प्रेरणा: रोज थोडी प्रगती केली तरी मोठा बदल घडतो.", "quote", "general", "beginner", "mr")
        ]
        for tip in multilingual_tips:
            exists = conn.execute(
                "SELECT 1 FROM DailyTips WHERE content_text=? LIMIT 1",
                (tip[0],)
            ).fetchone()
            if not exists:
                conn.execute("""
                    INSERT INTO DailyTips (content_text, content_type, subject, difficulty_level, language, is_active, publish_date)
                    VALUES (?, ?, ?, ?, ?, 1, date('now'))
                """, tip)


init_db()

# ---------------- PASSWORD CHECK ----------------
def is_password_strong(p):
    return len(p) >= 8 and any(c.isalpha() for c in p) and any(c.isdigit() for c in p) and any(c in "!@#$%^&*" for c in p)

def normalize_departments(departments):
    selected = []
    for dept in departments:
        value = (dept or "").strip()
        if value in DEPARTMENTS and value not in selected:
            selected.append(value)
    return selected

def slugify_filename(value):
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', (value or "").strip())
    return safe.strip("._-") or "quiz"

def _require_student():
    if 'user_id' not in session or session.get('role') != 'Student':
        flash("Please login as student")
        return False
    return True

def _determine_tip_subject(conn, user_id, role):
    if role == "Teacher":
        row = conn.execute("""
            SELECT COALESCE(NULLIF(subject, ''), 'general') AS subject, COUNT(*) AS c
            FROM Quizzes
            WHERE created_by=?
            GROUP BY subject
            ORDER BY c DESC, subject ASC
            LIMIT 1
        """, (user_id,)).fetchone()
        return (row["subject"] if row else "general") or "general"

    # Older DBs may not have Practice_Quizzes.subject, so keep this backward-compatible.
    if not conn.is_postgres:
        practice_cols = {row[1] for row in conn.execute("PRAGMA table_info(Practice_Quizzes)").fetchall()}
        if "subject" not in practice_cols:
            return "general"

    try:
        row = conn.execute("""
            SELECT COALESCE(NULLIF(p.subject, ''), 'general') AS subject, COUNT(*) AS c
            FROM PracticeProgress pp
            JOIN Practice_Quizzes p ON p.quiz_id = pp.quiz_id
            WHERE pp.user_id=?
            GROUP BY subject
            ORDER BY c DESC, subject ASC
            LIMIT 1
        """, (user_id,)).fetchone()
        return (row["subject"] if row else "general") or "general"
    except sqlite3.OperationalError:
        return "general"

def _difficulty_for_user(conn, user_id):
    row = conn.execute(
        "SELECT COUNT(*) AS views FROM UserTipViews WHERE user_id=?",
        (user_id,)
    ).fetchone()
    views = row["views"] if row else 0
    if views < 7:
        return "beginner"
    if views < 21:
        return "intermediate"
    return "advanced"

def _pick_rotating_tip(rows):
    if not rows:
        return None
    day_index = int(datetime.datetime.utcnow().strftime("%j"))
    return rows[day_index % len(rows)]

def _calculate_tip_streak(conn, user_id):
    rows = conn.execute("""
        SELECT viewed_on
        FROM UserTipViews
        WHERE user_id=?
        ORDER BY viewed_on DESC
    """, (user_id,)).fetchall()
    if not rows:
        return 0

    streak = 0
    expected_date = datetime.date.today()
    for row in rows:
        viewed_date = datetime.date.fromisoformat(row["viewed_on"])
        if viewed_date == expected_date:
            streak += 1
            expected_date = expected_date - datetime.timedelta(days=1)
        elif viewed_date > expected_date:
            continue
        else:
            break
    return streak

def get_daily_tip_for_user(conn, user_id, role, language="en"):
    preferred_language = (language or "en").strip().lower()
    if preferred_language not in ("en", "hi", "mr"):
        preferred_language = "en"

    subject = _determine_tip_subject(conn, user_id, role)
    difficulty = _difficulty_for_user(conn, user_id)

    candidate_rows = conn.execute("""
        SELECT *
        FROM DailyTips
        WHERE is_active=1
          AND language=?
          AND difficulty_level=?
          AND (subject=? OR subject='general')
        ORDER BY tip_id ASC
    """, (preferred_language, difficulty, subject)).fetchall()
    tip = _pick_rotating_tip(candidate_rows)

    if not tip:
        fallback = conn.execute("""
            SELECT *
            FROM DailyTips
            WHERE is_active=1 AND language=?
            ORDER BY tip_id ASC
        """, (preferred_language,)).fetchall()
        tip = _pick_rotating_tip(fallback)

    if not tip and preferred_language != "en":
        fallback_en = conn.execute("""
            SELECT *
            FROM DailyTips
            WHERE is_active=1 AND language='en'
            ORDER BY tip_id ASC
        """).fetchall()
        tip = _pick_rotating_tip(fallback_en)

    if not tip:
        any_tip = conn.execute("""
            SELECT *
            FROM DailyTips
            WHERE is_active=1
            ORDER BY tip_id ASC
        """).fetchall()
        tip = _pick_rotating_tip(any_tip)

    return tip, subject, difficulty, preferred_language

# ---------------- LOGIN REQUIRED DECORATOR ----------------
from functools import wraps

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please login first")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def _valid_email(email):
    value = (email or "").strip()
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value))

def _to_int_flag(raw_value):
    return 1 if str(raw_value).strip().lower() in ("1", "true", "on", "yes") else 0

def _delete_user_account(conn, user_id):
    quiz_ids = [row["quiz_id"] for row in conn.execute(
        "SELECT quiz_id FROM Quizzes WHERE created_by=?",
        (user_id,)
    ).fetchall()]
    for quiz_id in quiz_ids:
        live_session_ids = [row["session_id"] for row in conn.execute(
            "SELECT session_id FROM live_sessions WHERE quiz_id=?",
            (quiz_id,)
        ).fetchall()]
        for sid in live_session_ids:
            conn.execute("DELETE FROM participants WHERE session_id=?", (sid,))
            conn.execute("DELETE FROM player_answers WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM live_sessions WHERE quiz_id=?", (quiz_id,))

        game_session_ids = [row["session_id"] for row in conn.execute(
            "SELECT session_id FROM GameSessions WHERE quiz_id=?",
            (quiz_id,)
        ).fetchall()]
        for sid in game_session_ids:
            conn.execute("DELETE FROM PlayerScores WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM GameSessions WHERE quiz_id=?", (quiz_id,))

        question_ids = [row["question_id"] for row in conn.execute(
            "SELECT question_id FROM Questions WHERE quiz_id=?",
            (quiz_id,)
        ).fetchall()]
        for qid in question_ids:
            conn.execute("DELETE FROM Options WHERE question_id=?", (qid,))
        conn.execute("DELETE FROM Questions WHERE quiz_id=?", (quiz_id,))
        conn.execute("DELETE FROM Quizzes WHERE quiz_id=?", (quiz_id,))

    practice_quiz_ids = [row["quiz_id"] for row in conn.execute(
        "SELECT quiz_id FROM Practice_Quizzes WHERE created_by=?",
        (user_id,)
    ).fetchall()]
    for quiz_id in practice_quiz_ids:
        practice_question_ids = [row["question_id"] for row in conn.execute(
            "SELECT question_id FROM PracticeQuestions WHERE quiz_id=?",
            (quiz_id,)
        ).fetchall()]
        for qid in practice_question_ids:
            conn.execute("DELETE FROM PracticeOptions WHERE question_id=?", (qid,))
        conn.execute("DELETE FROM PracticeQuestions WHERE quiz_id=?", (quiz_id,))
        conn.execute("DELETE FROM PracticeAnswers WHERE quiz_id=?", (quiz_id,))
        conn.execute("DELETE FROM PracticeFirstAnswers WHERE quiz_id=?", (quiz_id,))
        conn.execute("DELETE FROM PracticeProgress WHERE quiz_id=?", (quiz_id,))
        conn.execute("DELETE FROM Practice_Quizzes WHERE quiz_id=?", (quiz_id,))

        if not conn.is_postgres:
            legacy_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='PracticeQuizzes'"
            ).fetchone()
            if legacy_exists:
                conn.execute("DELETE FROM PracticeQuizzes WHERE quiz_id=?", (quiz_id,))

    conn.execute("DELETE FROM PracticeAnswers WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM PracticeFirstAnswers WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM PracticeProgress WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM UserTipViews WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM PlayerScores WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM StudyNotes WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM Flashcards WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM DailyGoals WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM StudyJournal WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM ResourceLibrary WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM StudyReminders WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM MindMaps WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM SelfAssessment WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM PomodoroLogs WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM Users WHERE user_id=?", (user_id,))

@app.before_request
def _enforce_login_session_version():
    if request.endpoint == 'static':
        return None
    if 'user_id' not in session:
        return None

    user_id = session.get('user_id')
    with get_db_connection() as conn:
        user = conn.execute(
            """
            SELECT user_id, username, role, department, session_version,
                   theme_mode, font_scale, app_language,
                   email_alerts, mute_notifications
            FROM Users
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

    if not user:
        session.clear()
        flash("Account not found. Please login again.")
        return redirect(url_for('login'))

    db_session_version = user["session_version"] or 0
    if session.get("session_version", db_session_version) != db_session_version:
        session.clear()
        flash("You were logged out because account sessions were reset.")
        return redirect(url_for('login'))

    session['username'] = user['username']
    session['role'] = user['role']
    session['department'] = user['department'] or 'Computer'
    session['session_version'] = db_session_version
    session['theme_mode'] = user['theme_mode'] or 'light'
    session['font_scale'] = user['font_scale'] or 'medium'
    session['app_language'] = user['app_language'] or 'en'
    session['email_alerts'] = 1 if (user['email_alerts'] or 0) else 0
    session['mute_notifications'] = 1 if (user['mute_notifications'] or 0) else 0


# ---------------- HELPER FUNCTION ----------------
def get_teacher_quizzes(user_id):
    with get_db_connection() as conn:
        quizzes = conn.execute("SELECT * FROM Quizzes WHERE created_by=?", (user_id,)).fetchall()
    return quizzes

# ---------------- LOGIN ----------------
@app.route('/', methods=['GET', 'POST'])
@app.route('/login', methods=['GET', 'POST'])
def login():
    locked = False  # optional: future use for account lock
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']

        with get_db_connection() as conn:
            user = conn.execute("SELECT * FROM Users WHERE email=?", (email,)).fetchone()

        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['user_id']
            session['username'] = user['username']
            session['role'] = user['role']
            session['department'] = (user['department'] or 'Computer') if 'department' in user.keys() else 'Computer'
            session['session_version'] = (user['session_version'] or 0) if 'session_version' in user.keys() else 0
            session['theme_mode'] = (user['theme_mode'] or 'light') if 'theme_mode' in user.keys() else 'light'
            session['font_scale'] = (user['font_scale'] or 'medium') if 'font_scale' in user.keys() else 'medium'
            session['app_language'] = (user['app_language'] or 'en') if 'app_language' in user.keys() else 'en'
            session['tip_language'] = session['app_language']
            session['email_alerts'] = 1 if ((user['email_alerts'] if 'email_alerts' in user.keys() else 1) or 0) else 0
            session['mute_notifications'] = 1 if ((user['mute_notifications'] if 'mute_notifications' in user.keys() else 0) or 0) else 0
            return redirect('/teacher_dashboard' if user['role'] == "Teacher" else '/student_dashboard')

        flash("Invalid login")

    return render_template('auth/login.html', locked=locked)

# ---------------- REGISTER ----------------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        role = request.form['role']
        department = (request.form.get('department') or '').strip()
        captcha = request.form.get('captcha')

        if role == "Student":
            if department not in DEPARTMENTS:
                flash("Please select a valid department for students.")
                return redirect('/register')
        else:
            department = None

        # CAPTCHA validation
        try:
            if int(captcha) != int(session.get('captcha_answer', -1)):
                flash("Incorrect CAPTCHA")
                return redirect('/register')
        except (TypeError, ValueError):
            flash("Please enter a valid CAPTCHA answer")
            return redirect('/register')

        # Password strength check
        if not is_password_strong(password):
            flash("Weak password")
            return redirect('/register')

        # Insert into DB
        try:
            with get_db_connection() as conn:
                conn.execute(
                    "INSERT INTO Users(username,email,password,role,department) VALUES(?,?,?,?,?)",
                    (username, email, generate_password_hash(password), role, department)
                )
                conn.commit()
            flash("Registered successfully")
            return redirect('/login')
        except sqlite3.IntegrityError:
            flash("Email already exists")
            return redirect('/register')
        except Exception:
            flash("Registration failed. Please try again.")
            return redirect('/register')

    # GET request → generate captcha numbers
    questions = ["Your first pet's name?", "Favorite color?", "Mother's maiden name?"]
    a, b = random.randint(1, 20), random.randint(1, 20)
    session['captcha_answer'] = a + b
    return render_template('auth/register.html', questions=questions, a=a, b=b)


# ---------- CREATE PRACTICE QUIZ ----------
@app.route("/create_practice_quiz", methods=["GET", "POST"])
@login_required
def create_practice_quiz():
    if request.method == "POST":
        try:
            title = request.form.get('title')
            description = request.form.get('description', '')
            created_by = session['user_id']
            selected_departments = normalize_departments(request.form.getlist('departments'))
            print(f"DEBUG: title={title}, created_by={created_by}")

            # Get all questions and options from form FIRST
            questions = request.form.getlist("question_text[]")
            explanations = request.form.getlist("explanation[]")
            question_images = request.files.getlist("question_image[]") if request.files else []
            print(f"DEBUG: Questions received: {len(questions)} questions")
            print(f"DEBUG: Explanations: {len(explanations)}")

            if not questions:
                flash("Please add at least one question", "danger")
                return redirect(url_for("create_practice_quiz"))
            if not selected_departments:
                flash("Please select at least one department", "danger")
                return redirect(url_for("create_practice_quiz"))

            with get_db_connection() as conn:
                # Insert quiz
                print(f"DEBUG: Inserting quiz: quiz_name={title}, created_by={created_by}")
                quiz_id = _insert_and_get_id(
                    conn,
                    """
                    INSERT INTO Practice_Quizzes (quiz_name, description, created_by, teacher_id, target_departments)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (title, description, created_by, created_by, ",".join(selected_departments)),
                    "quiz_id"
                )
                print(f"DEBUG: Created quiz with ID: {quiz_id}")
                ensure_legacy_practice_quiz_row(conn, quiz_id, title, description)

                # Insert all questions and options
                for idx, q_text in enumerate(questions):
                    explanation = explanations[idx] if idx < len(explanations) else ""
                    print(f"DEBUG: Inserting question {idx}: {q_text}")

                    media_url = None
                    if idx < len(question_images):
                        media_url = _save_question_image(question_images[idx])
                    
                    question_id = _insert_and_get_id(
                        conn,
                        """
                        INSERT INTO PracticeQuestions (quiz_id, question_text, explanation, media_url)
                        VALUES (?, ?, ?, ?)
                        """,
                        (quiz_id, q_text, explanation, media_url),
                        "question_id"
                    )
                    print(f"DEBUG: Created question ID: {question_id}")

                    # Get the correct option for this question
                    correct_option = request.form.get(f"correct_option_{idx}", "A")
                    print(f"DEBUG: Correct option for question {idx}: {correct_option}")

                    # Insert options for this question
                    for opt_label in ["A", "B", "C", "D"]:
                        opt_text = request.form.get(f"option_{opt_label}_{idx}")
                        is_correct = 1 if correct_option == opt_label else 0
                        print(f"DEBUG: Option {opt_label}: is_correct={is_correct}, text={opt_text}")
                        
                        if opt_text:  # Only insert if option text is provided
                            opt_order = ord(opt_label) - ord('A')  # Convert A,B,C,D to 0,1,2,3
                            conn.execute("""
                                INSERT INTO PracticeOptions (question_id, option_text, is_correct, option_order)
                                VALUES (?, ?, ?, ?)
                            """, (question_id, opt_text, is_correct, opt_order))

                print("DEBUG: Transaction committed successfully (via context manager)")

            flash("Practice Quiz created successfully! ✅", "success")
            return redirect(url_for("list_practice_quizzes"))
            
        except ValueError as e:
            flash(str(e), "danger")
            return redirect(url_for("create_practice_quiz"))
        except Exception as e:
            print(f"ERROR in create_practice_quiz: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            flash(f"Error: {str(e)}", "danger")
            return redirect(url_for("create_practice_quiz"))

    return render_template("teacher/create_practice_quiz.html")


# ---------- LIST PRACTICE QUIZZES ----------
@app.route("/list_practice_quizzes", methods=["GET"])
@login_required
def list_practice_quizzes():
    if session.get('role') != 'Teacher':
        return redirect('/login')
    
    with get_db_connection() as conn:
        quizzes = conn.execute("""
            SELECT p.*, COUNT(pq.question_id) AS question_count
            FROM Practice_Quizzes p
            LEFT JOIN PracticeQuestions pq ON p.quiz_id = pq.quiz_id
            WHERE p.created_by = ?
            GROUP BY p.quiz_id
            ORDER BY p.quiz_id DESC
        """, (session['user_id'],)).fetchall()
    
    return render_template('teacher/list_practice_quizzes.html', quizzes=quizzes)


# ---------- OPEN/VIEW PRACTICE QUIZ ----------
@app.route("/open_practice_quiz/<int:quiz_id>", methods=["GET"])
@login_required
def open_practice_quiz(quiz_id):
    if session.get('role') != 'Teacher':
        return redirect('/login')
    
    with get_db_connection() as conn:
        quiz = conn.execute(
            "SELECT * FROM Practice_Quizzes WHERE quiz_id=? AND created_by=?",
            (quiz_id, session['user_id'])
        ).fetchone()
        
        if not quiz:
            flash("Quiz not found")
            return redirect(url_for('list_practice_quizzes'))
        
        questions = conn.execute(
            "SELECT * FROM PracticeQuestions WHERE quiz_id=?",
            (quiz_id,)
        ).fetchall()
        
        questions_with_options = []
        for q in questions:
            options = conn.execute(
                "SELECT * FROM PracticeOptions WHERE question_id=?",
                (q['question_id'],)
            ).fetchall()
            questions_with_options.append({
                "question": q,
                "options": options
            })
    
    return render_template(
        'teacher/open_practice_quiz.html',
        quiz=quiz,
        questions=questions_with_options
    )


# ---------- EDIT PRACTICE QUIZ ----------
@app.route("/edit_practice_quiz/<int:quiz_id>", methods=["GET", "POST"])
@login_required
def edit_practice_quiz(quiz_id):
    if session.get('role') != 'Teacher':
        return redirect('/login')
    
    with get_db_connection() as conn:
        quiz = conn.execute(
            "SELECT * FROM Practice_Quizzes WHERE quiz_id=? AND created_by=?",
            (quiz_id, session['user_id'])
        ).fetchone()
        
        if not quiz:
            flash("Quiz not found")
            return redirect(url_for('list_practice_quizzes'))
        
        if request.method == 'POST':
            print(f"DEBUG: Form data keys: {list(request.form.keys())}")
            print(f"DEBUG: Quiz ID: {quiz_id}")
            print(f"DEBUG: Existing question IDs: {request.form.getlist('existing_question_id[]')}")
            print(f"DEBUG: New question texts: {request.form.getlist('new_question_text[]')}")
            
            # Update quiz metadata
            title = request.form.get('title', quiz['quiz_name'])
            description = request.form.get('description', quiz['description'] or '')
            conn.execute(
                "UPDATE Practice_Quizzes SET quiz_name=?, description=? WHERE quiz_id=?",
                (title, description, quiz_id)
            )
            ensure_legacy_practice_quiz_row(conn, quiz_id, title, description)
            
            # Update existing questions
            existing_ids = request.form.getlist('existing_question_id[]')
            question_texts = request.form.getlist('question_text[]')
            explanations = request.form.getlist('explanation[]')
            existing_images = request.files.getlist('question_image_existing[]') if request.files else []
            
            if existing_ids:
                print(f"DEBUG: Updating {len(existing_ids)} existing questions")
                
                for idx, q_id in enumerate(existing_ids):
                    q_id = int(q_id)
                    q_text = question_texts[idx] if idx < len(question_texts) else ''
                    explanation = explanations[idx] if idx < len(explanations) else ''
                    media_url = None
                    if idx < len(existing_images):
                        try:
                            media_url = _save_question_image(existing_images[idx])
                        except ValueError as e:
                            flash(str(e))
                            return redirect(url_for('edit_practice_quiz', quiz_id=quiz_id))
                    
                    print(f"DEBUG: Updating question {q_id}: {q_text}")
                    
                    # Get correct answer letter from radio button
                    correct_letter = request.form.get(f'correct_option_{q_id}', 'A')
                    correct_index = ord(correct_letter) - ord('A')
                    
                    # Update question
                    if media_url:
                        conn.execute(
                            "UPDATE PracticeQuestions SET question_text=?, explanation=?, media_url=? WHERE question_id=?",
                            (q_text, explanation, media_url, q_id)
                        )
                    else:
                        conn.execute(
                            "UPDATE PracticeQuestions SET question_text=?, explanation=? WHERE question_id=?",
                            (q_text, explanation, q_id)
                        )
                    
                    # Get and update options
                    option_texts = request.form.getlist(f'option_text_{q_id}[]')
                    existing_options = conn.execute(
                        "SELECT option_id FROM PracticeOptions WHERE question_id=? ORDER BY option_order",
                        (q_id,)
                    ).fetchall()
                    
                    for opt_idx, opt_text in enumerate(option_texts):
                        is_correct = 1 if opt_idx == correct_index else 0
                        if opt_idx < len(existing_options):
                            conn.execute(
                                "UPDATE PracticeOptions SET option_text=?, is_correct=?, option_order=? WHERE option_id=?",
                                (opt_text, is_correct, opt_idx, existing_options[opt_idx]['option_id'])
                            )
                        else:
                            conn.execute(
                                "INSERT INTO PracticeOptions (question_id, option_text, is_correct, option_order) VALUES (?, ?, ?, ?)",
                                (q_id, opt_text, is_correct, opt_idx)
                            )
                    # If user reduced options, remove extras from DB
                    if len(existing_options) > len(option_texts):
                        for extra in existing_options[len(option_texts):]:
                            conn.execute(
                                "DELETE FROM PracticeOptions WHERE option_id=?",
                                (extra['option_id'],)
                            )
            
            # Add new questions
            new_question_texts = request.form.getlist('new_question_text[]')
            new_explanations = request.form.getlist('new_explanation[]')
            
            print(f"DEBUG: New questions count: {len(new_question_texts)}")
            print(f"DEBUG: New question texts: {new_question_texts}")
            
            # Verify quiz still exists before inserting new questions
            quiz_check = conn.execute(
                "SELECT quiz_id FROM Practice_Quizzes WHERE quiz_id=?",
                (quiz_id,)
            ).fetchone()
            print(f"DEBUG: Quiz {quiz_id} exists: {quiz_check is not None}")
            
            if not quiz_check:
                flash("Quiz not found after update. Changes may not have been saved.")
                return redirect(url_for('list_practice_quizzes'))
            
            # Only process new questions if there are any
            if new_question_texts and any(q.strip() for q in new_question_texts):
                new_correct_keys = [
                    field_key for field_key in request.form.keys()
                    if field_key.startswith('new_correct_')
                ]
                new_images = request.files.getlist('new_question_image[]') if request.files else []
                
                # Process each new question
                for idx, q_text in enumerate(new_question_texts):
                    if q_text.strip():  # Only if question text is not empty
                        explanation = new_explanations[idx] if idx < len(new_explanations) else ''
                        media_url = None
                        if idx < len(new_images):
                            try:
                                media_url = _save_question_image(new_images[idx])
                            except ValueError as e:
                                flash(str(e))
                                return redirect(url_for('edit_practice_quiz', quiz_id=quiz_id))
                        
                        print(f"DEBUG: Inserting new question {idx}: {q_text}, quiz_id={quiz_id}")
                        
                        try:
                            # Insert question
                            question_id = _insert_and_get_id(
                                conn,
                                "INSERT INTO PracticeQuestions (quiz_id, question_text, explanation, media_url, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
                                (quiz_id, q_text, explanation, media_url),
                                "question_id"
                            )
                            print(f"DEBUG: Question inserted successfully with ID: {question_id}")
                            
                            # Map question fields by generated timestamp suffix
                            correct_letter = 'A'
                            option_texts = []
                            if idx < len(new_correct_keys):
                                correct_key = new_correct_keys[idx]
                                timestamp = correct_key.replace('new_correct_', '')
                                correct_letter = request.form.get(correct_key, 'A')
                                option_texts = request.form.getlist(f'new_option_{timestamp}[]')
                            
                            correct_index = ord(correct_letter) - ord('A') if correct_letter in 'ABCD' else 0
                            
                            print(f"DEBUG: Correct letter: {correct_letter}, index: {correct_index}, options: {option_texts}")
                            
                            # Insert options
                            for opt_idx, opt_text in enumerate(option_texts[:4]):  # Only take first 4
                                is_correct = 1 if opt_idx == correct_index else 0
                                if opt_text.strip():
                                    conn.execute(
                                        "INSERT INTO PracticeOptions (question_id, option_text, is_correct, option_order) VALUES (?, ?, ?, ?)",
                                        (question_id, opt_text, is_correct, opt_idx)
                                    )
                        except sqlite3.IntegrityError as e:
                            print(f"DEBUG: IntegrityError while inserting question: {e}")
                            print(f"DEBUG: quiz_id={quiz_id}, question_id={question_id if 'question_id' in locals() else 'not set'}")
                            raise
            
            conn.commit()
            flash("Quiz updated successfully ✅", "success")
            # After saving edits, go back to the list of practice quizzes
            return redirect(url_for('list_practice_quizzes'))
        
        questions = conn.execute(
            "SELECT * FROM PracticeQuestions WHERE quiz_id=?",
            (quiz_id,)
        ).fetchall()
        
        questions_with_options = []
        for q in questions:
            options = conn.execute(
                "SELECT * FROM PracticeOptions WHERE question_id=?",
                (q['question_id'],)
            ).fetchall()
            questions_with_options.append({
                "question": q,
                "options": options
            })
    
    return render_template(
        'teacher/edit_practice_quiz.html',
        quiz=quiz,
        questions=questions_with_options
    )


# ---------- ADD QUESTION TO PRACTICE QUIZ ----------
@app.route("/add_practice_question/<int:quiz_id>", methods=["POST"])
@login_required
def add_practice_question(quiz_id):
    if session.get('role') != 'Teacher':
        return redirect('/login')
    
    question_text = request.form.get('question_text')
    explanation = request.form.get('explanation', '')
    options = request.form.getlist('option_text[]')
    correct_index = int(request.form.get('correct_option', 0))
    
    with get_db_connection() as conn:
        # Verify quiz ownership
        quiz = conn.execute(
            "SELECT * FROM Practice_Quizzes WHERE quiz_id=? AND created_by=?",
            (quiz_id, session['user_id'])
        ).fetchone()
        
        if not quiz:
            flash("Quiz not found")
            return redirect(url_for('list_practice_quizzes'))
        
        # Insert question
        question_id = _insert_and_get_id(
            conn,
            "INSERT INTO PracticeQuestions (quiz_id, question_text, explanation) VALUES (?, ?, ?)",
            (quiz_id, question_text, explanation),
            "question_id"
        )
        
        # Insert options
        for i, opt in enumerate(options):
            is_correct = 1 if i == correct_index else 0
            conn.execute(
                "INSERT INTO PracticeOptions (question_id, option_text, is_correct, option_order) VALUES (?, ?, ?, ?)",
                (question_id, opt, is_correct, i)
            )
        
        conn.commit()
    
    flash("Question added successfully ✅", "success")
    return redirect(url_for('edit_practice_quiz', quiz_id=quiz_id))


# ---------- UPDATE PRACTICE QUESTION ----------
@app.route("/update_practice_question/<int:question_id>/<int:quiz_id>", methods=["POST"])
@login_required
def update_practice_question(question_id, quiz_id):
    if session.get('role') != 'Teacher':
        return redirect('/login')
    
    question_text = request.form.get('question_text')
    explanation = request.form.get('explanation', '')
    options = request.form.getlist('option_text[]')
    correct_index = int(request.form.get('correct_option', 0))
    
    with get_db_connection() as conn:
        # Verify quiz ownership
        quiz = conn.execute(
            "SELECT * FROM Practice_Quizzes WHERE quiz_id=? AND created_by=?",
            (quiz_id, session['user_id'])
        ).fetchone()
        
        if not quiz:
            flash("Quiz not found")
            return redirect(url_for('list_practice_quizzes'))
        
        # Update question
        conn.execute(
            "UPDATE PracticeQuestions SET question_text=?, explanation=? WHERE question_id=?",
            (question_text, explanation, question_id)
        )
        
        # Update options
        existing_options = conn.execute(
            "SELECT option_id FROM PracticeOptions WHERE question_id=? ORDER BY option_order",
            (question_id,)
        ).fetchall()
        
        for i, opt_text in enumerate(options):
            is_correct = 1 if i == correct_index else 0
            if i < len(existing_options):
                conn.execute(
                    "UPDATE PracticeOptions SET option_text=?, is_correct=?, option_order=? WHERE option_id=?",
                    (opt_text, is_correct, i, existing_options[i]['option_id'])
                )
            else:
                conn.execute(
                    "INSERT INTO PracticeOptions (question_id, option_text, is_correct, option_order) VALUES (?, ?, ?, ?)",
                    (question_id, opt_text, is_correct, i)
                )
        
        conn.commit()
    
    flash("Question updated successfully ✅", "success")
    return redirect(url_for('edit_practice_quiz', quiz_id=quiz_id))


# ---------- DELETE PRACTICE QUESTION ----------
@app.route("/delete_practice_question/<int:question_id>/<int:quiz_id>", methods=["POST", "GET"])
@login_required
def delete_practice_question(question_id, quiz_id):
    if session.get('role') != 'Teacher':
        return redirect('/login')
    
    with get_db_connection() as conn:
        # Verify quiz ownership
        quiz = conn.execute(
            "SELECT * FROM Practice_Quizzes WHERE quiz_id=? AND created_by=?",
            (quiz_id, session['user_id'])
        ).fetchone()
        
        if not quiz:
            flash("Quiz not found")
            return redirect(url_for('list_practice_quizzes'))
        
        # Delete options
        conn.execute("DELETE FROM PracticeOptions WHERE question_id=?", (question_id,))
        # Delete question
        conn.execute("DELETE FROM PracticeQuestions WHERE question_id=?", (question_id,))
        conn.commit()
    
    flash("Question deleted successfully ✅", "success")
    return redirect(url_for('edit_practice_quiz', quiz_id=quiz_id))


# ---------- DELETE PRACTICE QUIZ ----------
@app.route("/delete_practice_quiz/<int:quiz_id>", methods=["POST", "GET"])
@login_required
def delete_practice_quiz(quiz_id):
    if session.get('role') != 'Teacher':
        return redirect('/login')
    
    print(f"DEBUG: delete_practice_quiz called for quiz_id={quiz_id}, user_id={session.get('user_id')}, method={request.method}")
    
    try:
        with get_db_connection() as conn:
            # Verify quiz ownership
            quiz = conn.execute(
                "SELECT * FROM Practice_Quizzes WHERE quiz_id=? AND created_by=?",
                (quiz_id, session['user_id'])
            ).fetchone()
            
            if not quiz:
                print(f"DEBUG: Quiz {quiz_id} not found or not owned by user {session.get('user_id')}")
                flash("Quiz not found")
                return redirect(url_for('list_practice_quizzes'))
            
            print(f"DEBUG: Found quiz {quiz_id}, deleting related data...")

            # Temporarily disable foreign key constraints during multi-table delete (SQLite only)
            if not conn.is_postgres:
                conn.execute("PRAGMA foreign_keys = OFF")

            # 0️⃣ Delete answers (depends on questions/options)
            conn.execute("DELETE FROM PracticeAnswers WHERE quiz_id=?", (quiz_id,))
            conn.execute("DELETE FROM PracticeFirstAnswers WHERE quiz_id=?", (quiz_id,))
            print(f"DEBUG: Deleted PracticeAnswers/PracticeFirstAnswers")

            # 1️⃣ Delete options
            conn.execute("""
                DELETE FROM PracticeOptions
                WHERE question_id IN (
                    SELECT question_id FROM PracticeQuestions WHERE quiz_id=?
                )
            """, (quiz_id,))
            print(f"DEBUG: Deleted PracticeOptions")
            
            # 2️⃣ Delete questions
            conn.execute("DELETE FROM PracticeQuestions WHERE quiz_id=?", (quiz_id,))
            print(f"DEBUG: Deleted PracticeQuestions")
            
            # 3️⃣ Delete progress records
            conn.execute("DELETE FROM PracticeProgress WHERE quiz_id=?", (quiz_id,))
            print(f"DEBUG: Deleted PracticeProgress")
            
            # 4️⃣ Delete the quiz itself
            conn.execute("DELETE FROM Practice_Quizzes WHERE quiz_id=?", (quiz_id,))
            print(f"DEBUG: Deleted Practice_Quizzes")
            try:
                conn.execute("DELETE FROM PracticeQuizzes WHERE quiz_id=?", (quiz_id,))
            except Exception:
                pass
            
            # Re-enable foreign key constraints (SQLite only)
            if not conn.is_postgres:
                conn.execute("PRAGMA foreign_keys = ON")
            print(f"DEBUG: All deletions prepared for commit - quiz_id={quiz_id}")
        
        flash("Quiz deleted successfully ✅", "success")
    except Exception as e:
        print(f"ERROR: Delete practice quiz error: {type(e).__name__}: {e}")
        traceback.print_exc()
        flash("Error deleting quiz ❌", "danger")
    
    return redirect(url_for('list_practice_quizzes'))
   

# Manage Practice Quiz
# -------------------------------
@app.route('/manage_practice_quiz')
@login_required
def manage_practice_quiz():

    with get_db_connection() as conn:
        quizzes = conn.execute("""
            SELECT * FROM Quizzes
            WHERE mode='practice' AND created_by=?
        """, (session['user_id'],)).fetchall()

    return render_template('manage_practice_quiz.html', quizzes=quizzes)



# ---------------- TEACHER DASHBOARD ----------------
@app.route('/teacher_dashboard')
@login_required
def teacher_dashboard():
    if session['role'] != 'Teacher':
        flash("Access denied")
        return redirect('/login')

    with get_db_connection() as conn:

        # Regular Quizzes
        quizzes = conn.execute("""
            SELECT q.*, COUNT(ques.question_id) AS question_count
            FROM Quizzes q
            LEFT JOIN Questions ques ON q.quiz_id = ques.quiz_id
            WHERE q.created_by = ?
            GROUP BY q.quiz_id
            ORDER BY q.quiz_id DESC
        """, (session['user_id'],)).fetchall()

        # ✅ Practice Quizzes (FIXED)
        practice_quizzes = conn.execute("""
            SELECT p.*, COUNT(pq.question_id) AS question_count
            FROM Practice_Quizzes p
            LEFT JOIN PracticeQuestions pq ON p.quiz_id = pq.quiz_id
            WHERE p.created_by = ?
            GROUP BY p.quiz_id
            ORDER BY p.quiz_id DESC
        """, (session['user_id'],)).fetchall()

    return render_template(
        'teacher/dashboard.html',
        quizzes=quizzes,
        practice_quizzes=practice_quizzes
    )

@app.route('/teacher_live_quizzes')
@login_required
def teacher_live_quizzes():
    if session.get('role') != 'Teacher':
        flash("Access denied")
        return redirect('/login')

    with get_db_connection() as conn:
        quizzes = conn.execute("""
            SELECT q.*, COUNT(ques.question_id) AS question_count
            FROM Quizzes q
            LEFT JOIN Questions ques ON q.quiz_id = ques.quiz_id
            WHERE q.created_by = ?
            GROUP BY q.quiz_id
            ORDER BY q.quiz_id DESC
        """, (session['user_id'],)).fetchall()

    return render_template('teacher/live_quizzes.html', quizzes=quizzes)

@app.route('/teacher/practice_results')
@login_required
def teacher_practice_results_overview():
    if session.get('role') != 'Teacher':
        flash("Access denied")
        return redirect('/login')

    quiz_query = (request.args.get('q') or '').strip()

    with get_db_connection() as conn:
        filters = ["q.created_by=?"]
        params = [session['user_id']]
        if quiz_query:
            filters.append("LOWER(q.quiz_name) LIKE ?")
            params.append(f"%{quiz_query.lower()}%")

        quizzes = conn.execute(
            f"""
            SELECT
                q.quiz_id,
                q.quiz_name,
                q.created_at,
                COUNT(DISTINCT pfa.user_id) AS first_attempt_students
            FROM Practice_Quizzes q
            LEFT JOIN PracticeFirstAttempts pfa ON pfa.quiz_id = q.quiz_id
            WHERE {" AND ".join(filters)}
            GROUP BY q.quiz_id, q.quiz_name, q.created_at
            ORDER BY q.quiz_id DESC
            """,
            tuple(params)
        ).fetchall()

    return render_template(
        'teacher/practice_results_overview.html',
        quizzes=quizzes,
        quiz_query=quiz_query
    )

@app.route('/teacher/practice_results/<int:quiz_id>')
@login_required
def teacher_practice_quiz_results(quiz_id):
    if session.get('role') != 'Teacher':
        flash("Access denied")
        return redirect('/login')

    search_query = (request.args.get('q') or '').strip()
    selected_date = (request.args.get('date') or '').strip()

    with get_db_connection() as conn:
        quiz = conn.execute(
            "SELECT quiz_id, quiz_name FROM Practice_Quizzes WHERE quiz_id=? AND created_by=?",
            (quiz_id, session['user_id'])
        ).fetchone()
        if not quiz:
            flash("Quiz not found")
            return redirect(url_for('teacher_practice_results_overview'))

        filters = ["pfa.quiz_id=?"]
        params = [quiz_id]
        if search_query:
            filters.append("LOWER(u.username) LIKE ?")
            params.append(f"%{search_query.lower()}%")
        if selected_date:
            filters.append("date(datetime(pfa.attempted_at, '+5 hours', '+30 minutes'))=?")
            params.append(selected_date)
        where_clause = " AND ".join(filters)

        first_attempts = conn.execute(
            f"""
            SELECT
                u.user_id AS student_user_id,
                u.username AS student_name,
                COALESCE(NULLIF(u.department, ''), 'Unknown') AS branch,
                pfa.score,
                pfa.correct_answers,
                pfa.total_questions,
                datetime(pfa.attempted_at, '+5 hours', '+30 minutes') AS attempted_at,
                date(datetime(pfa.attempted_at, '+5 hours', '+30 minutes')) AS attempt_date,
                pp.score AS latest_score,
                pp.correct_answers AS latest_correct,
                pp.total_questions AS latest_total,
                pp.completed_at AS latest_completed_at
            FROM PracticeFirstAttempts pfa
            JOIN Users u ON u.user_id = pfa.user_id
            LEFT JOIN PracticeProgress pp
                ON pp.user_id = pfa.user_id AND pp.quiz_id = pfa.quiz_id
            WHERE {where_clause}
            ORDER BY pfa.score DESC, pfa.correct_answers DESC, pfa.attempted_at ASC
            """,
            tuple(params)
        ).fetchall()

        summary = conn.execute(
            f"""
            SELECT
                date(datetime(pfa.attempted_at, '+5 hours', '+30 minutes')) AS attempt_date,
                COALESCE(NULLIF(u.department, ''), 'Unknown') AS branch,
                COUNT(*) AS students_count
            FROM PracticeFirstAttempts pfa
            JOIN Users u ON u.user_id = pfa.user_id
            WHERE {where_clause}
            GROUP BY date(datetime(pfa.attempted_at, '+5 hours', '+30 minutes')), COALESCE(NULLIF(u.department, ''), 'Unknown')
            ORDER BY attempt_date DESC, branch ASC
            """,
            tuple(params)
        ).fetchall()

        total_questions = conn.execute(
            "SELECT COUNT(*) AS total FROM PracticeQuestions WHERE quiz_id=?",
            (quiz_id,)
        ).fetchone()["total"]

        question_filter_clauses = []
        question_params = [quiz_id]
        if search_query:
            question_filter_clauses.append(
                "pfa.user_id IN (SELECT user_id FROM Users WHERE role='Student' AND LOWER(username) LIKE ?)"
            )
            question_params.append(f"%{search_query.lower()}%")
        if selected_date:
            question_filter_clauses.append("date(pfa.submitted_at)=?")
            question_params.append(selected_date)
        question_filter_sql = f" AND {' AND '.join(question_filter_clauses)}" if question_filter_clauses else ""
        question_params.append(quiz_id)

        question_stats = conn.execute(
            f"""
            SELECT
                q.question_id,
                q.question_text,
                COALESCE(SUM(CASE WHEN pfa.is_correct=0 THEN 1 ELSE 0 END), 0) AS incorrect_count,
                COALESCE(SUM(CASE WHEN pfa.is_correct=1 THEN 1 ELSE 0 END), 0) AS correct_count,
                COUNT(pfa.answer_id) AS attempts
            FROM PracticeQuestions q
            LEFT JOIN PracticeFirstAnswers pfa
                ON pfa.question_id = q.question_id
               AND pfa.quiz_id = ?
               {question_filter_sql}
            WHERE q.quiz_id = ?
            GROUP BY q.question_id, q.question_text
            ORDER BY incorrect_count DESC, attempts DESC
            """,
            tuple(question_params)
        ).fetchall()

    attempted_students = len(first_attempts)
    class_avg_score = round(sum(r["score"] for r in first_attempts) / attempted_students, 2) if attempted_students else 0
    highest_score = max((r["score"] for r in first_attempts), default=0)
    lowest_score = min((r["score"] for r in first_attempts), default=0)
    top_performers = first_attempts[:5] if first_attempts else []

    total_correct = sum(r["correct_answers"] or 0 for r in first_attempts)
    total_attempted = sum(r["total_questions"] or 0 for r in first_attempts)
    total_incorrect = max(0, total_attempted - total_correct)

    difficulty_counts = {"easy": 0, "medium": 0, "difficult": 0, "no_data": 0}
    for q in question_stats:
        bucket = _difficulty_bucket(q["correct_count"], q["attempts"])
        difficulty_counts[bucket] += 1

    report = {
        "total_questions": total_questions,
        "attempted_students": attempted_students,
        "class_avg_score": class_avg_score,
        "highest_score": highest_score,
        "lowest_score": lowest_score,
        "top_performers": top_performers,
        "score_labels": [r["student_name"] for r in first_attempts],
        "score_values": [r["score"] for r in first_attempts],
        "correct_total": total_correct,
        "incorrect_total": total_incorrect,
        "difficulty_counts": difficulty_counts
    }

    return render_template(
        'teacher/practice_quiz_results_teacher.html',
        quiz=quiz,
        first_attempts=first_attempts,
        summary=summary,
        report=report,
        question_stats=question_stats,
        search_query=search_query,
        selected_date=selected_date
    )

@app.route('/teacher/practice_results/<int:quiz_id>/student/<int:student_user_id>')
@login_required
def teacher_practice_student_detail(quiz_id, student_user_id):
    if session.get('role') != 'Teacher':
        flash("Access denied")
        return redirect('/login')

    search_query = (request.args.get('q') or '').strip()
    selected_date = (request.args.get('date') or '').strip()

    with get_db_connection() as conn:
        quiz = conn.execute(
            "SELECT quiz_id, quiz_name FROM Practice_Quizzes WHERE quiz_id=? AND created_by=?",
            (quiz_id, session['user_id'])
        ).fetchone()
        if not quiz:
            flash("Quiz not found")
            return redirect(url_for('teacher_practice_results_overview'))

        student = conn.execute(
            """
            SELECT
                u.user_id AS student_user_id,
                u.username AS student_name,
                COALESCE(NULLIF(u.department, ''), 'Unknown') AS branch
            FROM Users u
            JOIN PracticeFirstAttempts pfa
                ON pfa.user_id = u.user_id
            WHERE pfa.quiz_id=? AND u.user_id=?
            """,
            (quiz_id, student_user_id)
        ).fetchone()
        if not student:
            flash("Student result not found")
            return redirect(url_for('teacher_practice_quiz_results', quiz_id=quiz_id, q=search_query, date=selected_date))

        questions = conn.execute(
            """
            SELECT question_id, question_text
            FROM PracticeQuestions
            WHERE quiz_id=?
            ORDER BY question_id ASC
            """,
            (quiz_id,)
        ).fetchall()

        answer_filter_sql = ""
        answer_params = [quiz_id, student_user_id]
        if selected_date:
            answer_filter_sql = " AND date(submitted_at)=?"
            answer_params.append(selected_date)

        latest_answers = conn.execute(
            f"""
            SELECT question_id, is_correct
            FROM PracticeFirstAnswers
            WHERE quiz_id=? AND user_id=? {answer_filter_sql}
            """,
            tuple(answer_params)
        ).fetchall()

    answer_lookup = {r["question_id"]: r["is_correct"] for r in latest_answers}
    question_rows = []
    correct_count = 0
    incorrect_count = 0
    not_attempted_count = 0

    for q in questions:
        result = answer_lookup.get(q["question_id"])
        if result is None:
            status = "not_attempted"
            not_attempted_count += 1
        elif int(result) == 1:
            status = "correct"
            correct_count += 1
        else:
            status = "incorrect"
            incorrect_count += 1
        question_rows.append({"question": q["question_text"], "status": status})

    summary = {
        "correct": correct_count,
        "incorrect": incorrect_count,
        "not_attempted": not_attempted_count
    }

    return render_template(
        'teacher/practice_student_question_detail.html',
        quiz=quiz,
        student=student,
        question_rows=question_rows,
        summary=summary,
        search_query=search_query,
        selected_date=selected_date
    )

@app.route('/teacher/practice_results/<int:quiz_id>/export')
@login_required
def teacher_practice_quiz_results_export(quiz_id):
    if session.get('role') != 'Teacher':
        flash("Access denied")
        return redirect('/login')

    with get_db_connection() as conn:
        quiz = conn.execute(
            "SELECT quiz_id, quiz_name FROM Practice_Quizzes WHERE quiz_id=? AND created_by=?",
            (quiz_id, session['user_id'])
        ).fetchone()
        if not quiz:
            flash("Quiz not found")
            return redirect(url_for('teacher_practice_results_overview'))

        rows = conn.execute(
            """
            SELECT
                u.user_id AS user_id,
                u.username AS student_name,
                COALESCE(NULLIF(u.department, ''), 'Unknown') AS branch,
                pfa.score,
                pfa.correct_answers,
                pfa.total_questions,
                datetime(pfa.attempted_at, '+5 hours', '+30 minutes') AS attempted_at,
                pp.score AS latest_score,
                pp.correct_answers AS latest_correct,
                pp.total_questions AS latest_total,
                datetime(pp.completed_at, '+5 hours', '+30 minutes') AS latest_completed_at
            FROM PracticeFirstAttempts pfa
            JOIN Users u ON u.user_id = pfa.user_id
            LEFT JOIN PracticeProgress pp
                ON pp.user_id = pfa.user_id AND pp.quiz_id = pfa.quiz_id
            WHERE pfa.quiz_id=?
            ORDER BY pfa.score DESC, pfa.correct_answers DESC, pfa.attempted_at ASC
            """,
            (quiz_id,)
        ).fetchall()
        question_stats = conn.execute(
            """
            SELECT
                q.question_id,
                q.question_text,
                COUNT(pfa.answer_id) AS attempts,
                COALESCE(SUM(CASE WHEN pfa.is_correct=1 THEN 1 ELSE 0 END), 0) AS correct_count,
                COALESCE(SUM(CASE WHEN pfa.is_correct=0 THEN 1 ELSE 0 END), 0) AS incorrect_count
            FROM PracticeQuestions q
            LEFT JOIN PracticeFirstAnswers pfa
                ON pfa.question_id = q.question_id
               AND pfa.quiz_id = q.quiz_id
            WHERE q.quiz_id=?
            GROUP BY q.question_id, q.question_text
            ORDER BY q.question_id ASC
            """,
            (quiz_id,)
        ).fetchall()

        raw_rows = conn.execute(
            """
            SELECT
                u.username AS student_name,
                q.question_text,
                CASE WHEN pfa.is_correct=1 THEN 'Correct' ELSE 'Incorrect' END AS answer_status,
                datetime(pfa.submitted_at, '+5 hours', '+30 minutes') AS submitted_at
            FROM PracticeFirstAnswers pfa
            JOIN Users u ON u.user_id = pfa.user_id
            JOIN PracticeQuestions q ON q.question_id = pfa.question_id
            WHERE pfa.quiz_id=?
            ORDER BY pfa.submitted_at ASC, pfa.answer_id ASC
            """,
            (quiz_id,)
        ).fetchall()

        latest_answers = conn.execute(
            """
            SELECT
                pfa.user_id,
                pfa.question_id,
                pfa.is_correct,
                datetime(pfa.submitted_at, '+5 hours', '+30 minutes') AS submitted_at,
                so.option_text AS selected_option
            FROM PracticeFirstAnswers pfa
            LEFT JOIN PracticeOptions so ON so.option_id = pfa.selected_option_id
            WHERE pfa.quiz_id=?
            """,
            (quiz_id,)
        ).fetchall()
        live_option_rows = conn.execute(
            """
            SELECT q.question_id, o.option_text, o.is_correct
            FROM Questions q
            JOIN Options o ON o.question_id = q.question_id
            WHERE q.quiz_id=?
            ORDER BY q.question_id ASC, o.option_id ASC
            """,
            (quiz_id,)
        ).fetchall()

        correct_option_rows = conn.execute(
            """
            SELECT question_id, option_text
            FROM PracticeOptions
            WHERE question_id IN (
                SELECT question_id FROM PracticeQuestions WHERE quiz_id=?
            )
              AND is_correct=1
            """,
            (quiz_id,)
        ).fetchall()
        practice_option_rows = conn.execute(
            """
            SELECT q.question_id, o.option_text, o.is_correct
            FROM PracticeQuestions q
            JOIN PracticeOptions o ON o.question_id = q.question_id
            WHERE q.quiz_id=?
            ORDER BY q.question_id ASC, COALESCE(o.option_order, o.option_id) ASC
            """,
            (quiz_id,)
        ).fetchall()

    attempted_students = len(rows)
    class_avg_score = round(sum((r["score"] or 0) for r in rows) / attempted_students, 2) if attempted_students else 0
    highest_score = max((r["score"] or 0 for r in rows), default=0)
    lowest_score = min((r["score"] or 0 for r in rows), default=0)
    total_correct = sum((r["correct_answers"] or 0) for r in rows)
    total_attempted = sum((r["total_questions"] or 0) for r in rows)
    total_incorrect = max(0, total_attempted - total_correct)
    correct_pct = round((total_correct / total_attempted) * 100, 2) if total_attempted else 0
    incorrect_pct = round((total_incorrect / total_attempted) * 100, 2) if total_attempted else 0
    played_on = min((r["attempted_at"] for r in rows if r["attempted_at"]), default="-")

    overview_rows = [
        _styled_row([quiz["quiz_name"], "", "", "", ""], 1),
        _styled_row(["Played on", played_on, "", "", ""], 7),
        _styled_row(["Hosted by", session.get("username", "Teacher"), "", "", ""], 7),
        _styled_row(["Played with", f"{attempted_students} players", "", "", ""], 7),
        _styled_row(["Played", f"{total_attempted} attempts", "", "", ""], 7),
        _styled_row(["", "", "", "", ""], 3),
        _styled_row(["Overall Performance", "", "", "", ""], 2),
        _styled_row(["Total correct answers (%)", f"{correct_pct:.2f}%", "", "", ""], 3),
        _styled_row(["Total incorrect answers (%)", f"{incorrect_pct:.2f}%", "", "", ""], 3),
        _styled_row(["Average score (points)", f"{class_avg_score} points", "", "", ""], 3),
        _styled_row(["", "", "", "", ""], 3),
        _styled_row(["Feedback", "", "", "", ""], 2),
        _styled_row(["Number of responses", "0", "", "", ""], 3),
        _styled_row(["How fun was it? (out of 5)", "0.00 out of 5", "", "", ""], 3),
        _styled_row(["Did you learn something?", "0.00% Yes", "0.00% No", "", ""], 3),
        _styled_row(["Do you recommend it?", "0.00% Yes", "0.00% No", "", ""], 3),
        _styled_row(["How do you feel?", "0.00% Positive", "0.00% Neutral", "0.00% Negative", ""], 3),
        _styled_row(["Switch tabs/pages to view other result breakdown", "", "", "", ""], 5),
    ]
    overview_merges = ["A1:E1", "A7:E7", "A12:E12", "A18:E18"]

    final_scores_rows = [[
        "Rank", "Student", "Branch", "First Score", "Correct", "Incorrect", "Correct %", "Total Q", "Attempted At", "Latest Score"
    ]]
    final_scores_rows = [
        _styled_row([quiz["quiz_name"], "", "", "", "", "", "", "", "", ""], 1),
        _styled_row(["Final Scores", "", "", "", "", "", "", "", "", ""], 2),
        _styled_row(final_scores_rows[0], 4),
    ]
    for idx, r in enumerate(rows, start=1):
        total_q = r["total_questions"] or 0
        correct = r["correct_answers"] or 0
        incorrect = max(0, total_q - correct)
        pct = round((correct / total_q) * 100, 2) if total_q else 0
        final_scores_rows.append(_styled_row([
            idx, r["student_name"], r["branch"], r["score"], correct, incorrect, pct, total_q,
            r["attempted_at"], r["latest_score"]
        ], 3))
    final_scores_merges = ["A1:J1", "A2:J2"]

    question_headers = ["Question", "Attempts", "Correct", "Incorrect", "Correct %"]
    question_rows = [
        _styled_row([quiz["quiz_name"], "", "", "", ""], 1),
        _styled_row(["Question Analysis", "", "", "", ""], 2),
        _styled_row(question_headers, 4),
    ]
    for q in question_stats:
        attempts = q["attempts"] or 0
        correct = q["correct_count"] or 0
        incorrect = q["incorrect_count"] or 0
        pct = round((correct / attempts) * 100, 2) if attempts else 0
        question_rows.append(_styled_row([q["question_text"], attempts, correct, incorrect, pct], 3))
    question_merges = ["A1:E1", "A2:E2"]

    raw_headers = ["Student", "Question", "Correct / Incorrect", "Submitted At"]
    raw_data_rows = [
        _styled_row([quiz["quiz_name"], "", "", ""], 1),
        _styled_row(["Raw Report Data", "", "", ""], 2),
        _styled_row(raw_headers, 4),
    ]
    for r in raw_rows:
        status_style = 8 if (r["answer_status"] == "Correct") else 10
        raw_data_rows.append([
            {"v": r["student_name"], "s": 3},
            {"v": r["question_text"], "s": 3},
            {"v": r["answer_status"], "s": status_style},
            {"v": r["submitted_at"], "s": 3},
        ])
    raw_merges = ["A1:D1", "A2:D2"]

    question_ids = [q["question_id"] for q in question_stats]
    question_labels = [f"Q{i}" for i in range(1, len(question_ids) + 1)]
    correct_option_by_qid = {r["question_id"]: r["option_text"] for r in correct_option_rows}
    latest_answer_map = {(r["user_id"], r["question_id"]): r for r in latest_answers}

    summary_headers = ["Rank", "Player", "Total Score (points)"] + question_labels
    quiz_summary_rows = [
        _styled_row([quiz["quiz_name"]] + [""] * (len(summary_headers) - 1), 1),
        _styled_row(["Quiz Application Summary"] + [""] * (len(summary_headers) - 1), 2),
        _styled_row(summary_headers, 4)
    ]
    summary_merges = [
        f"A1:{_xlsx_col_name(len(summary_headers))}1",
        f"A2:{_xlsx_col_name(len(summary_headers))}2"
    ]
    for idx, student in enumerate(rows, start=1):
        per_question = []
        for qid in question_ids:
            ans = latest_answer_map.get((student["user_id"], qid))
            if not ans:
                per_question.append({"v": "-", "s": 3})
            else:
                is_correct = int(ans["is_correct"] or 0) == 1
                per_question.append({"v": "C" if is_correct else "I", "s": 8 if is_correct else 10})
        quiz_summary_rows.append([
            {"v": idx, "s": 3},
            {"v": student["student_name"], "s": 3},
            {"v": student["score"], "s": 3},
        ] + per_question)

    options_by_qid = {}
    for row in practice_option_rows:
        options_by_qid.setdefault(row["question_id"], []).append(row)

    question_sheet_payloads = []
    for idx, q in enumerate(question_stats, start=1):
        qid = q["question_id"]
        attempts = q["attempts"] or 0
        correct_count = q["correct_count"] or 0
        correct_pct_q = round((correct_count / attempts) * 100, 2) if attempts else 0
        correct_option = correct_option_by_qid.get(qid, "-")
        question_duration = "20 seconds"
        q_rows = [
            _styled_row([quiz["quiz_name"], "", "", "", "", "", ""], 1),
            _styled_row([f"{idx} Quiz", q["question_text"], "", "", "", "", ""], 2),
            _styled_row(["Correct answers", correct_option, "", "", "", "", ""], 3),
            _styled_row(["Players correct (%)", f"{correct_pct_q}%", "", "", "", "", ""], 3),
            _styled_row(["Question duration", question_duration, "", "", "", "", ""], 3),
            _styled_row(["", "", "", "", "", "", ""], 3),
            _styled_row(["Answer Summary", "", "", "", "", "", ""], 2),
        ]
        option_rows = options_by_qid.get(qid, [])
        option_count_map = {}
        for student in rows:
            ans = latest_answer_map.get((student["user_id"], qid))
            if ans and ans["selected_option"]:
                key = (ans["selected_option"] or "").strip()
                option_count_map[key] = option_count_map.get(key, 0) + 1

        q_rows.append(_styled_row(["Answer option", "Is correct?", "Answers received", "", "", "", ""], 4))
        for opt in option_rows[:4]:
            opt_txt = opt["option_text"]
            is_ok = int(opt["is_correct"] or 0) == 1
            q_rows.append([
                {"v": opt_txt, "s": 3},
                {"v": "Yes" if is_ok else "No", "s": 8 if is_ok else 10},
                {"v": option_count_map.get((opt_txt or "").strip(), 0), "s": 3},
                {"v": "", "s": 3},
                {"v": "", "s": 3},
                {"v": "", "s": 3},
                {"v": "", "s": 3},
            ])

        player_section_row = len(q_rows) + 2
        q_rows.extend([
            _styled_row(["", "", "", "", "", "", ""], 3),
            _styled_row(["Player Details", "", "", "", "", "", ""], 2),
            _styled_row(["Player", "Answer", "Status", "Submitted At", "", "", ""], 4),
        ])
        for student in rows:
            ans = latest_answer_map.get((student["user_id"], qid))
            if not ans:
                selected_option = "-"
                status = "Not Attempted"
                submitted_at = "-"
                status_style = 3
            else:
                selected_option = ans["selected_option"] or "-"
                is_correct = int(ans["is_correct"] or 0) == 1
                status = "Correct" if is_correct else "Incorrect"
                submitted_at = ans["submitted_at"] or "-"
                status_style = 8 if is_correct else 10
            q_rows.append([
                {"v": student["student_name"], "s": 3},
                {"v": selected_option, "s": 3},
                {"v": status, "s": status_style},
                {"v": submitted_at, "s": 3},
                {"v": "", "s": 3},
                {"v": "", "s": 3},
                {"v": "", "s": 3},
            ])
        question_sheet_payloads.append({
            "name": f"{idx} Quiz",
            "rows": q_rows,
            "merges": ["A1:G1", "B2:G2", "A7:G7", f"A{player_section_row}:G{player_section_row}"]
        })

    filename = f"practice_results_{quiz_id}.xlsx"
    return _xlsx_response(filename, [
        {"name": "Overview", "rows": overview_rows, "merges": overview_merges},
        {"name": "Final Scores", "rows": final_scores_rows, "merges": final_scores_merges},
        {"name": "Quiz Application Summary", "rows": quiz_summary_rows, "merges": summary_merges},
        {"name": "Question Analysis", "rows": question_rows, "merges": question_merges},
        {"name": "Raw Report Data", "rows": raw_data_rows, "merges": raw_merges},
    ] + question_sheet_payloads)

@app.route('/teacher/live_results')
@login_required
def teacher_live_results_overview():
    if session.get('role') != 'Teacher':
        flash("Access denied")
        return redirect('/login')

    quiz_query = (request.args.get('q') or '').strip()

    with get_db_connection() as conn:
        filters = ["q.created_by=?"]
        params = [session['user_id']]
        if quiz_query:
            filters.append("LOWER(q.quiz_name) LIKE ?")
            params.append(f"%{quiz_query.lower()}%")

        quizzes = conn.execute(
            f"""
            SELECT
                q.quiz_id,
                q.quiz_name,
                q.created_at,
                COUNT(DISTINCT ls.session_id) AS session_count
            FROM Quizzes q
            LEFT JOIN live_sessions ls ON ls.quiz_id = q.quiz_id
            WHERE {" AND ".join(filters)}
            GROUP BY q.quiz_id, q.quiz_name, q.created_at
            ORDER BY q.quiz_id DESC
            """,
            tuple(params)
        ).fetchall()

    return render_template(
        'teacher/live_results_overview.html',
        quizzes=quizzes,
        quiz_query=quiz_query
    )

@app.route('/teacher/live_results/<int:quiz_id>')
@login_required
def teacher_live_quiz_results(quiz_id):
    if session.get('role') != 'Teacher':
        flash("Access denied")
        return redirect('/login')

    search_query = (request.args.get('q') or '').strip()
    selected_date = (request.args.get('date') or '').strip()

    with get_db_connection() as conn:
        quiz = conn.execute(
            "SELECT quiz_id, quiz_name FROM Quizzes WHERE quiz_id=? AND created_by=?",
            (quiz_id, session['user_id'])
        ).fetchone()
        if not quiz:
            flash("Quiz not found")
            return redirect(url_for('teacher_live_results_overview'))

        filters = []
        params = []
        if search_query:
            filters.append("LOWER(base.student_name) LIKE ?")
            params.append(f"%{search_query.lower()}%")
        if selected_date:
            filters.append("base.activity_date=?")
            params.append(selected_date)
        where_clause = " AND ".join(filters) if filters else "1=1"

        rows = conn.execute(
            f"""
            WITH base AS (
                SELECT
                    ls.quiz_id,
                    ls.session_id,
                    p.participant_id,
                    ls.pin,
                    p.nickname AS student_name,
                    COALESCE(NULLIF(u.department, ''), 'Unknown') AS branch,
                    date(COALESCE(MIN(pa.submitted_at), MIN(p.joined_at))) AS activity_date,
                    COUNT(pa.answer_id) AS answers_count,
                    COALESCE(SUM(pa.score_awarded), 0) AS score,
                    COALESCE(SUM(pa.is_correct), 0) AS correct_answers,
                    COALESCE(SUM(COALESCE(pa.response_ms, 0)), 0) AS time_taken
                FROM live_sessions ls
                JOIN participants p ON p.session_id = ls.session_id
                LEFT JOIN player_answers pa
                    ON pa.session_id = ls.session_id
                   AND pa.player_name = p.nickname
                LEFT JOIN Users u
                    ON LOWER(u.username) = LOWER(p.nickname)
                   AND u.role = 'Student'
                WHERE ls.quiz_id=?
                GROUP BY ls.quiz_id, ls.session_id, p.participant_id, ls.pin, p.nickname, COALESCE(NULLIF(u.department, ''), 'Unknown')
            )
            SELECT
                base.session_id,
                base.participant_id,
                base.pin,
                base.student_name,
                base.branch,
                base.activity_date,
                base.answers_count,
                base.score,
                base.correct_answers,
                base.time_taken
            FROM base
            WHERE {where_clause}
              AND base.answers_count > 0
            ORDER BY base.activity_date DESC, base.branch ASC, base.score DESC, base.correct_answers DESC, base.time_taken ASC, base.student_name ASC
            """,
            tuple([quiz_id] + params)
        ).fetchall()

        summary = conn.execute(
            f"""
            WITH base AS (
                SELECT
                    ls.quiz_id,
                    p.nickname AS student_name,
                    COALESCE(NULLIF(u.department, ''), 'Unknown') AS branch,
                    date(COALESCE(MIN(pa.submitted_at), MIN(p.joined_at))) AS activity_date,
                    COUNT(pa.answer_id) AS answers_count
                FROM live_sessions ls
                JOIN participants p ON p.session_id = ls.session_id
                LEFT JOIN player_answers pa
                    ON pa.session_id = ls.session_id
                   AND pa.player_name = p.nickname
                LEFT JOIN Users u
                    ON LOWER(u.username) = LOWER(p.nickname)
                   AND u.role = 'Student'
                WHERE ls.quiz_id=?
                GROUP BY ls.quiz_id, p.nickname, COALESCE(NULLIF(u.department, ''), 'Unknown')
            )
            SELECT
                base.activity_date,
                base.branch,
                COUNT(*) AS students_count
            FROM base
            WHERE {where_clause}
              AND base.answers_count > 0
            GROUP BY base.activity_date, base.branch
            ORDER BY base.activity_date DESC, base.branch ASC
            """,
            tuple([quiz_id] + params)
        ).fetchall()

        total_questions = conn.execute(
            "SELECT COUNT(*) AS total FROM Questions WHERE quiz_id=?",
            (quiz_id,)
        ).fetchone()["total"]

        participants_total = conn.execute(
            """
            SELECT COUNT(DISTINCT p.nickname) AS total
            FROM participants p
            JOIN live_sessions ls ON ls.session_id = p.session_id
            WHERE ls.quiz_id=?
            """,
            (quiz_id,)
        ).fetchone()["total"]

        live_question_filter_clauses = []
        live_question_params = [quiz_id]
        if search_query:
            live_question_filter_clauses.append("LOWER(pa.player_name) LIKE ?")
            live_question_params.append(f"%{search_query.lower()}%")
        if selected_date:
            live_question_filter_clauses.append("date(pa.submitted_at)=?")
            live_question_params.append(selected_date)
        live_question_filter_sql = f" AND {' AND '.join(live_question_filter_clauses)}" if live_question_filter_clauses else ""
        live_question_params.append(quiz_id)

        question_stats = conn.execute(
            f"""
            WITH sessions AS (
                SELECT session_id FROM live_sessions WHERE quiz_id=?
            )
            SELECT
                q.question_id,
                q.question_text,
                COALESCE(SUM(CASE WHEN pa.is_correct=0 THEN 1 ELSE 0 END), 0) AS incorrect_count,
                COALESCE(SUM(CASE WHEN pa.is_correct=1 THEN 1 ELSE 0 END), 0) AS correct_count,
                COUNT(pa.answer_id) AS attempts
            FROM Questions q
            LEFT JOIN player_answers pa
                ON pa.question_id = q.question_id
               AND pa.session_id IN (SELECT session_id FROM sessions)
               {live_question_filter_sql}
            WHERE q.quiz_id = ?
            GROUP BY q.question_id, q.question_text
            ORDER BY incorrect_count DESC, attempts DESC
            """,
            tuple(live_question_params)
        ).fetchall()

    attempted_students = len(rows)
    class_avg_score = round(sum(r["score"] for r in rows) / attempted_students, 2) if attempted_students else 0
    highest_score = max((r["score"] for r in rows), default=0)
    lowest_score = min((r["score"] for r in rows), default=0)
    top_performers = rows[:5] if rows else []

    total_correct = sum(r["correct_answers"] or 0 for r in rows)
    total_attempted = sum(r["answers_count"] or 0 for r in rows)
    total_incorrect = max(0, total_attempted - total_correct)

    difficulty_counts = {"easy": 0, "medium": 0, "difficult": 0, "no_data": 0}
    for q in question_stats:
        bucket = _difficulty_bucket(q["correct_count"], q["attempts"])
        difficulty_counts[bucket] += 1

    date_buckets = {}
    for r in rows:
        key = r["activity_date"]
        if key not in date_buckets:
            date_buckets[key] = []
        date_buckets[key].append(r["score"] or 0)
    date_labels = sorted(date_buckets.keys()) if date_buckets else []
    date_avg_scores = [
        round(sum(date_buckets[d]) / len(date_buckets[d]), 2) for d in date_labels
    ]

    report = {
        "total_questions": total_questions,
        "participants_total": participants_total,
        "attempted_students": attempted_students,
        "incomplete_students": max(0, participants_total - attempted_students),
        "class_avg_score": class_avg_score,
        "highest_score": highest_score,
        "lowest_score": lowest_score,
        "top_performers": top_performers,
        "score_labels": [r["student_name"] for r in rows],
        "score_values": [r["score"] for r in rows],
        "correct_total": total_correct,
        "incorrect_total": total_incorrect,
        "difficulty_counts": difficulty_counts,
        "date_labels": date_labels,
        "date_avg_scores": date_avg_scores
    }

    return render_template(
        'teacher/live_quiz_results_teacher.html',
        quiz=quiz,
        rows=rows,
        summary=summary,
        report=report,
        question_stats=question_stats,
        search_query=search_query,
        selected_date=selected_date
    )

@app.route('/teacher/live_results/<int:quiz_id>/session/<int:session_id>/student/<int:participant_id>')
@login_required
def teacher_live_student_detail(quiz_id, session_id, participant_id):
    if session.get('role') != 'Teacher':
        flash("Access denied")
        return redirect('/login')

    search_query = (request.args.get('q') or '').strip()
    selected_date = (request.args.get('date') or '').strip()

    with get_db_connection() as conn:
        quiz = conn.execute(
            "SELECT quiz_id, quiz_name FROM Quizzes WHERE quiz_id=? AND created_by=?",
            (quiz_id, session['user_id'])
        ).fetchone()
        if not quiz:
            flash("Quiz not found")
            return redirect(url_for('teacher_live_results_overview'))

        student = conn.execute(
            """
            SELECT
                p.participant_id,
                p.session_id,
                p.nickname AS student_name,
                ls.pin,
                COALESCE(NULLIF(u.department, ''), 'Unknown') AS branch
            FROM participants p
            JOIN live_sessions ls ON ls.session_id = p.session_id
            LEFT JOIN Users u
                ON LOWER(u.username) = LOWER(p.nickname)
               AND u.role='Student'
            WHERE ls.quiz_id=? AND p.session_id=? AND p.participant_id=?
            """,
            (quiz_id, session_id, participant_id)
        ).fetchone()
        if not student:
            flash("Student result not found")
            return redirect(url_for('teacher_live_quiz_results', quiz_id=quiz_id, q=search_query, date=selected_date))

        questions = conn.execute(
            """
            SELECT question_id, question_text
            FROM Questions
            WHERE quiz_id=?
            ORDER BY question_id ASC
            """,
            (quiz_id,)
        ).fetchall()

        answer_filter_sql = ""
        answer_params = [session_id, student["student_name"]]
        if selected_date:
            answer_filter_sql = " AND date(submitted_at)=?"
            answer_params.append(selected_date)

        latest_answers = conn.execute(
            f"""
            SELECT pa.question_id, pa.is_correct
            FROM player_answers pa
            JOIN (
                SELECT question_id, MAX(answer_id) AS latest_answer_id
                FROM player_answers
                WHERE session_id=? AND player_name=? {answer_filter_sql}
                GROUP BY question_id
            ) latest ON latest.latest_answer_id = pa.answer_id
            """,
            tuple(answer_params)
        ).fetchall()

    answer_lookup = {r["question_id"]: r["is_correct"] for r in latest_answers}
    question_rows = []
    correct_count = 0
    incorrect_count = 0
    not_attempted_count = 0

    for q in questions:
        result = answer_lookup.get(q["question_id"])
        if result is None:
            status = "not_attempted"
            not_attempted_count += 1
        elif int(result) == 1:
            status = "correct"
            correct_count += 1
        else:
            status = "incorrect"
            incorrect_count += 1
        question_rows.append({"question": q["question_text"], "status": status})

    summary = {
        "correct": correct_count,
        "incorrect": incorrect_count,
        "not_attempted": not_attempted_count
    }

    return render_template(
        'teacher/live_student_question_detail.html',
        quiz=quiz,
        student=student,
        question_rows=question_rows,
        summary=summary,
        search_query=search_query,
        selected_date=selected_date
    )

@app.route('/teacher/live_results/<int:quiz_id>/export')
@login_required
def teacher_live_quiz_results_export(quiz_id):
    if session.get('role') != 'Teacher':
        flash("Access denied")
        return redirect('/login')

    with get_db_connection() as conn:
        quiz = conn.execute(
            "SELECT quiz_id, quiz_name FROM Quizzes WHERE quiz_id=? AND created_by=?",
            (quiz_id, session['user_id'])
        ).fetchone()
        if not quiz:
            flash("Quiz not found")
            return redirect(url_for('teacher_live_results_overview'))

        rows = conn.execute(
            """
            WITH base AS (
                SELECT
                    ls.quiz_id,
                    ls.session_id,
                    ls.pin,
                    p.nickname AS student_name,
                    COALESCE(NULLIF(u.department, ''), 'Unknown') AS branch,
                    date(datetime(COALESCE(MIN(pa.submitted_at), MIN(p.joined_at)), '+5 hours', '+30 minutes')) AS activity_date,
                    COUNT(pa.answer_id) AS answers_count,
                    COALESCE(SUM(pa.score_awarded), 0) AS score,
                    COALESCE(SUM(pa.is_correct), 0) AS correct_answers,
                    COALESCE(SUM(COALESCE(pa.response_ms, 0)), 0) AS time_taken
                FROM live_sessions ls
                JOIN participants p ON p.session_id = ls.session_id
                LEFT JOIN player_answers pa
                    ON pa.session_id = ls.session_id
                   AND pa.player_name = p.nickname
                LEFT JOIN Users u
                    ON LOWER(u.username) = LOWER(p.nickname)
                   AND u.role = 'Student'
                WHERE ls.quiz_id=?
                GROUP BY ls.quiz_id, ls.session_id, ls.pin, p.nickname, COALESCE(NULLIF(u.department, ''), 'Unknown')
            )
            SELECT
                base.session_id,
                base.pin,
                base.student_name,
                base.branch,
                base.activity_date,
                base.answers_count,
                base.score,
                base.correct_answers,
                base.time_taken
            FROM base
            WHERE base.answers_count > 0
            ORDER BY base.activity_date DESC, base.branch ASC, base.score DESC, base.correct_answers DESC, base.time_taken ASC, base.student_name ASC
            """,
            (quiz_id,)
        ).fetchall()
        question_stats = conn.execute(
            """
            WITH sessions AS (
                SELECT session_id FROM live_sessions WHERE quiz_id=?
            )
            SELECT
                q.question_id,
                q.question_text,
                COUNT(pa.answer_id) AS attempts,
                COALESCE(SUM(CASE WHEN pa.is_correct=1 THEN 1 ELSE 0 END), 0) AS correct_count,
                COALESCE(SUM(CASE WHEN pa.is_correct=0 THEN 1 ELSE 0 END), 0) AS incorrect_count
            FROM Questions q
            LEFT JOIN player_answers pa
                ON pa.question_id = q.question_id
               AND pa.session_id IN (SELECT session_id FROM sessions)
            WHERE q.quiz_id=?
            GROUP BY q.question_id, q.question_text
            ORDER BY q.question_id ASC
            """,
            (quiz_id, quiz_id)
        ).fetchall()

        raw_rows = conn.execute(
            """
            SELECT
                ls.session_id,
                ls.pin,
                pa.player_name AS student_name,
                q.question_text,
                CASE WHEN pa.is_correct=1 THEN 'Correct' ELSE 'Incorrect' END AS answer_status,
                pa.response_ms,
                pa.score_awarded,
                datetime(pa.submitted_at, '+5 hours', '+30 minutes') AS submitted_at
            FROM player_answers pa
            JOIN live_sessions ls ON ls.session_id = pa.session_id
            LEFT JOIN Questions q ON q.question_id = pa.question_id
            WHERE ls.quiz_id=?
            ORDER BY pa.submitted_at ASC, pa.answer_id ASC
            """,
            (quiz_id,)
        ).fetchall()
        live_option_rows = conn.execute(
            """
            SELECT q.question_id, o.option_text, o.is_correct
            FROM Questions q
            JOIN Options o ON o.question_id = q.question_id
            WHERE q.quiz_id=?
            ORDER BY q.question_id ASC, o.option_id ASC
            """,
            (quiz_id,)
        ).fetchall()

        session_ids = sorted({r["session_id"] for r in rows})
        latest_live_answers = []
        if session_ids:
            placeholders = ",".join(["?"] * len(session_ids))
            latest_live_answers = conn.execute(
                f"""
                SELECT
                    pa.session_id,
                    pa.player_name,
                    pa.question_id,
                    pa.answer,
                    pa.is_correct,
                    pa.response_ms,
                    pa.score_awarded,
                    datetime(pa.submitted_at, '+5 hours', '+30 minutes') AS submitted_at
                FROM player_answers pa
                JOIN (
                    SELECT session_id, player_name, question_id, MAX(answer_id) AS latest_answer_id
                    FROM player_answers
                    WHERE session_id IN ({placeholders})
                    GROUP BY session_id, player_name, question_id
                ) latest ON latest.latest_answer_id = pa.answer_id
                """,
                tuple(session_ids)
            ).fetchall()

    attempted_students = len(rows)
    class_avg_score = round(sum((r["score"] or 0) for r in rows) / attempted_students, 2) if attempted_students else 0
    highest_score = max((r["score"] or 0 for r in rows), default=0)
    lowest_score = min((r["score"] or 0 for r in rows), default=0)
    total_correct = sum((r["correct_answers"] or 0) for r in rows)
    total_attempted = sum((r["answers_count"] or 0) for r in rows)
    total_incorrect = max(0, total_attempted - total_correct)
    correct_pct = round((total_correct / total_attempted) * 100, 2) if total_attempted else 0
    incorrect_pct = round((total_incorrect / total_attempted) * 100, 2) if total_attempted else 0
    played_on = min((r["activity_date"] for r in rows if r["activity_date"]), default="-")

    overview_rows = [
        _styled_row([quiz["quiz_name"], "", "", "", ""], 1),
        _styled_row(["Played on", played_on, "", "", ""], 7),
        _styled_row(["Hosted by", session.get("username", "Teacher"), "", "", ""], 7),
        _styled_row(["Played with", f"{attempted_students} players", "", "", ""], 7),
        _styled_row(["Played", f"{total_attempted} attempts", "", "", ""], 7),
        _styled_row(["", "", "", "", ""], 3),
        _styled_row(["Overall Performance", "", "", "", ""], 2),
        _styled_row(["Total correct answers (%)", f"{correct_pct:.2f}%", "", "", ""], 3),
        _styled_row(["Total incorrect answers (%)", f"{incorrect_pct:.2f}%", "", "", ""], 3),
        _styled_row(["Average score (points)", f"{class_avg_score} points", "", "", ""], 3),
        _styled_row(["", "", "", "", ""], 3),
        _styled_row(["Feedback", "", "", "", ""], 2),
        _styled_row(["Number of responses", "0", "", "", ""], 3),
        _styled_row(["How fun was it? (out of 5)", "0.00 out of 5", "", "", ""], 3),
        _styled_row(["Did you learn something?", "0.00% Yes", "0.00% No", "", ""], 3),
        _styled_row(["Do you recommend it?", "0.00% Yes", "0.00% No", "", ""], 3),
        _styled_row(["How do you feel?", "0.00% Positive", "0.00% Neutral", "0.00% Negative", ""], 3),
        _styled_row(["Switch tabs/pages to view other result breakdown", "", "", "", ""], 5),
    ]
    overview_merges = ["A1:E1", "A7:E7", "A12:E12", "A18:E18"]

    final_scores_rows = [[
        "Rank", "Date", "Branch", "Session", "PIN", "Student", "Score", "Attempted", "Correct", "Incorrect", "Time Taken (ms)"
    ]]
    final_scores_rows = [
        _styled_row([quiz["quiz_name"], "", "", "", "", "", "", "", "", "", ""], 1),
        _styled_row(["Final Scores", "", "", "", "", "", "", "", "", "", ""], 2),
        _styled_row(final_scores_rows[0], 4),
    ]
    for idx, r in enumerate(rows, start=1):
        attempted = r["answers_count"] or 0
        correct = r["correct_answers"] or 0
        final_scores_rows.append(_styled_row([
            idx, r["activity_date"], r["branch"], r["session_id"], r["pin"], r["student_name"],
            r["score"], attempted, correct, max(0, attempted - correct), r["time_taken"]
        ], 3))
    final_scores_merges = ["A1:K1", "A2:K2"]

    question_headers = ["Question", "Attempts", "Correct", "Incorrect", "Correct %"]
    question_rows = [
        _styled_row([quiz["quiz_name"], "", "", "", ""], 1),
        _styled_row(["Question Analysis", "", "", "", ""], 2),
        _styled_row(question_headers, 4),
    ]
    for q in question_stats:
        attempts = q["attempts"] or 0
        correct = q["correct_count"] or 0
        incorrect = q["incorrect_count"] or 0
        pct = round((correct / attempts) * 100, 2) if attempts else 0
        question_rows.append(_styled_row([q["question_text"], attempts, correct, incorrect, pct], 3))
    question_merges = ["A1:E1", "A2:E2"]

    raw_headers = ["Session", "PIN", "Student", "Question", "Correct / Incorrect", "Response Time (ms)", "Score (points)", "Submitted At"]
    raw_data_rows = [
        _styled_row([quiz["quiz_name"], "", "", "", "", "", "", ""], 1),
        _styled_row(["Raw Report Data", "", "", "", "", "", "", ""], 2),
        _styled_row(raw_headers, 4),
    ]
    for r in raw_rows:
        status_style = 8 if (r["answer_status"] == "Correct") else 10
        raw_data_rows.append([
            {"v": r["session_id"], "s": 3},
            {"v": r["pin"], "s": 3},
            {"v": r["student_name"], "s": 3},
            {"v": r["question_text"], "s": 3},
            {"v": r["answer_status"], "s": status_style},
            {"v": r["response_ms"], "s": 3},
            {"v": r["score_awarded"], "s": 3},
            {"v": r["submitted_at"], "s": 3},
        ])
    raw_merges = ["A1:H1", "A2:H2"]

    question_ids = [q["question_id"] for q in question_stats]
    question_labels = [f"Q{i}" for i in range(1, len(question_ids) + 1)]
    latest_live_answer_map = {
        (r["session_id"], (r["player_name"] or "").strip().lower(), r["question_id"]): r
        for r in latest_live_answers
    }

    summary_headers = ["Rank", "Player", "Total Score (points)"] + question_labels
    quiz_summary_rows = [
        _styled_row([quiz["quiz_name"]] + [""] * (len(summary_headers) - 1), 1),
        _styled_row(["Quiz Application Summary"] + [""] * (len(summary_headers) - 1), 2),
        _styled_row(summary_headers, 4)
    ]
    summary_merges = [
        f"A1:{_xlsx_col_name(len(summary_headers))}1",
        f"A2:{_xlsx_col_name(len(summary_headers))}2"
    ]
    for idx, student in enumerate(rows, start=1):
        pname = (student["student_name"] or "").strip().lower()
        per_question_points = []
        for qid in question_ids:
            ans = latest_live_answer_map.get((student["session_id"], pname, qid))
            points = (ans["score_awarded"] if ans else 0) or 0
            per_question_points.append({"v": points, "s": 8 if points > 0 else 10})
        quiz_summary_rows.append([
            {"v": idx, "s": 3},
            {"v": student["student_name"], "s": 3},
            {"v": student["score"], "s": 3}
        ] + per_question_points)

    options_by_qid = {}
    for row in live_option_rows:
        options_by_qid.setdefault(row["question_id"], []).append(row)

    question_sheet_payloads = []
    for idx, q in enumerate(question_stats, start=1):
        qid = q["question_id"]
        attempts = q["attempts"] or 0
        correct_count = q["correct_count"] or 0
        correct_pct_q = round((correct_count / attempts) * 100, 2) if attempts else 0
        q_rows = [
            _styled_row([quiz["quiz_name"], "", "", "", "", "", "", "", "", ""], 1),
            _styled_row([f"{idx} Quiz", q["question_text"], "", "", "", "", "", "", "", ""], 2),
            _styled_row(["Players correct (%)", f"{correct_pct_q}%", "", "", "", "", "", "", "", ""], 3),
            _styled_row(["Question attempts", attempts, "", "", "", "", "", "", "", ""], 3),
            _styled_row(["", "", "", "", "", "", "", "", "", ""], 3),
            _styled_row(["Answer Summary", "", "", "", "", "", "", "", "", ""], 2),
            _styled_row(["Answer option", "Is correct?", "Answers received", "Average response (ms)", "", "", "", "", "", ""], 4),
        ]

        option_rows = options_by_qid.get(qid, [])
        option_answers = []
        for student in rows:
            pname = (student["student_name"] or "").strip().lower()
            ans = latest_live_answer_map.get((student["session_id"], pname, qid))
            option_answers.append(ans)
        for opt in option_rows[:4]:
            opt_txt = opt["option_text"]
            is_ok = int(opt["is_correct"] or 0) == 1
            hit_rows = [a for a in option_answers if a and (a["answer"] or "").strip() == (opt_txt or "").strip()]
            avg_ms = round(sum((a["response_ms"] or 0) for a in hit_rows) / len(hit_rows), 2) if hit_rows else 0
            q_rows.append([
                {"v": opt_txt, "s": 3},
                {"v": "Yes" if is_ok else "No", "s": 8 if is_ok else 10},
                {"v": len(hit_rows), "s": 3},
                {"v": avg_ms, "s": 3},
                {"v": "", "s": 3},
                {"v": "", "s": 3},
                {"v": "", "s": 3},
                {"v": "", "s": 3},
                {"v": "", "s": 3},
                {"v": "", "s": 3},
            ])

        player_section_row = len(q_rows) + 2
        q_rows.extend([
            _styled_row(["", "", "", "", "", "", "", "", "", ""], 3),
            _styled_row(["Player Details", "", "", "", "", "", "", "", "", ""], 2),
            _styled_row(["Player", "Answer", "Status", "Score (points)", "Response Time (ms)", "Submitted At", "", "", "", ""], 4),
        ])
        for student in rows:
            pname = (student["student_name"] or "").strip().lower()
            ans = latest_live_answer_map.get((student["session_id"], pname, qid))
            if not ans:
                answer_text = "-"
                status = "Not Attempted"
                response_ms = "-"
                score_awarded = 0
                submitted_at = "-"
                status_style = 3
            else:
                answer_text = ans["answer"] or "-"
                is_correct = int(ans["is_correct"] or 0) == 1
                status = "Correct" if is_correct else "Incorrect"
                response_ms = ans["response_ms"]
                score_awarded = ans["score_awarded"] or 0
                submitted_at = ans["submitted_at"] or "-"
                status_style = 8 if is_correct else 10
            q_rows.append([
                {"v": student["student_name"], "s": 3},
                {"v": answer_text, "s": 3},
                {"v": status, "s": status_style},
                {"v": score_awarded, "s": 3},
                {"v": response_ms, "s": 3},
                {"v": submitted_at, "s": 3},
                {"v": "", "s": 3},
                {"v": "", "s": 3},
                {"v": "", "s": 3},
                {"v": "", "s": 3},
            ])
        question_sheet_payloads.append({
            "name": f"{idx} Quiz",
            "rows": q_rows,
            "merges": ["A1:J1", "B2:J2", "A6:J6", f"A{player_section_row}:J{player_section_row}"]
        })

    filename = f"live_results_{quiz_id}.xlsx"
    return _xlsx_response(filename, [
        {"name": "Overview", "rows": overview_rows, "merges": overview_merges},
        {"name": "Final Scores", "rows": final_scores_rows, "merges": final_scores_merges},
        {"name": "Quiz Application Summary", "rows": quiz_summary_rows, "merges": summary_merges},
        {"name": "Question Analysis", "rows": question_rows, "merges": question_merges},
        {"name": "Raw Report Data", "rows": raw_data_rows, "merges": raw_merges},
    ] + question_sheet_payloads)

@app.route('/teacher/reports')
@login_required
def teacher_reports():
    if session.get('role') != 'Teacher':
        flash("Access denied")
        return redirect('/login')

    with get_db_connection() as conn:
        live_rows = conn.execute(
            """
            WITH base AS (
                SELECT
                    p.nickname AS student_name,
                    COUNT(DISTINCT ls.session_id) AS sessions_attempted,
                    COUNT(pa.answer_id) AS answers_count,
                    COALESCE(SUM(pa.score_awarded), 0) AS total_score,
                    COALESCE(SUM(pa.is_correct), 0) AS correct_answers
                FROM live_sessions ls
                JOIN participants p ON p.session_id = ls.session_id
                LEFT JOIN player_answers pa
                    ON pa.session_id = ls.session_id
                   AND pa.player_name = p.nickname
                WHERE ls.quiz_id IN (
                    SELECT quiz_id FROM Quizzes WHERE created_by=?
                )
                GROUP BY p.nickname
            )
            SELECT * FROM base
            WHERE answers_count > 0
            ORDER BY total_score DESC, correct_answers DESC, student_name ASC
            """,
            (session['user_id'],)
        ).fetchall()

        practice_rows = conn.execute(
            """
            SELECT
                u.username AS student_name,
                COUNT(*) AS attempts,
                AVG(pfa.score) AS avg_score,
                SUM(pfa.correct_answers) AS correct_answers,
                SUM(pfa.total_questions) AS total_questions
            FROM PracticeFirstAttempts pfa
            JOIN Practice_Quizzes q ON q.quiz_id = pfa.quiz_id
            JOIN Users u ON u.user_id = pfa.user_id
            WHERE q.created_by = ?
            GROUP BY u.username
            ORDER BY avg_score DESC, student_name ASC
            """,
            (session['user_id'],)
        ).fetchall()

    live_map = {}
    for r in live_rows:
        key = (r["student_name"] or "").strip().lower()
        live_map[key] = {
            "student_name": r["student_name"],
            "sessions_attempted": r["sessions_attempted"],
            "live_total_score": r["total_score"],
            "live_avg_score": round((r["total_score"] / r["sessions_attempted"]), 2) if r["sessions_attempted"] else 0,
            "live_correct": r["correct_answers"],
            "live_answers": r["answers_count"]
        }

    practice_map = {}
    for r in practice_rows:
        key = (r["student_name"] or "").strip().lower()
        practice_map[key] = {
            "student_name": r["student_name"],
            "practice_attempts": r["attempts"],
            "practice_avg_score": round(r["avg_score"], 2) if r["avg_score"] is not None else 0,
            "practice_correct": r["correct_answers"],
            "practice_total": r["total_questions"]
        }

    all_keys = sorted(set(live_map.keys()) | set(practice_map.keys()))
    rows = []
    for key in all_keys:
        live = live_map.get(key, {})
        practice = practice_map.get(key, {})
        student_name = live.get("student_name") or practice.get("student_name") or key
        live_avg = live.get("live_avg_score", 0)
        practice_avg = practice.get("practice_avg_score", 0)
        divisor = (1 if live_avg else 0) + (1 if practice_avg else 0)
        combined = round((live_avg + practice_avg) / divisor, 2) if divisor else 0
        rows.append({
            "student_name": student_name,
            "live_avg_score": live_avg,
            "live_sessions": live.get("sessions_attempted", 0),
            "practice_avg_score": practice_avg,
            "practice_attempts": practice.get("practice_attempts", 0),
            "combined_score": combined
        })

    rows.sort(key=lambda r: (-(r["combined_score"] or 0), r["student_name"]))

    avg_live = round(
        sum(r["live_avg_score"] for r in rows if r["live_avg_score"]) / max(1, len([r for r in rows if r["live_avg_score"]])),
        2
    ) if rows else 0
    avg_practice = round(
        sum(r["practice_avg_score"] for r in rows if r["practice_avg_score"]) / max(1, len([r for r in rows if r["practice_avg_score"]])),
        2
    ) if rows else 0

    summary = {
        "total_students": len(rows),
        "avg_live_score": avg_live,
        "avg_practice_score": avg_practice,
        "top_performers": rows[:5]
    }

    return render_template(
        'teacher/reports.html',
        rows=rows,
        summary=summary
    )

@app.route('/teacher/reports/export')
@login_required
def teacher_reports_export():
    if session.get('role') != 'Teacher':
        flash("Access denied")
        return redirect('/login')

    with get_db_connection() as conn:
        live_rows = conn.execute(
            """
            WITH base AS (
                SELECT
                    p.nickname AS student_name,
                    COUNT(DISTINCT ls.session_id) AS sessions_attempted,
                    COUNT(pa.answer_id) AS answers_count,
                    COALESCE(SUM(pa.score_awarded), 0) AS total_score
                FROM live_sessions ls
                JOIN participants p ON p.session_id = ls.session_id
                LEFT JOIN player_answers pa
                    ON pa.session_id = ls.session_id
                   AND pa.player_name = p.nickname
                WHERE ls.quiz_id IN (
                    SELECT quiz_id FROM Quizzes WHERE created_by=?
                )
                GROUP BY p.nickname
            )
            SELECT * FROM base
            WHERE answers_count > 0
            """,
            (session['user_id'],)
        ).fetchall()

        practice_rows = conn.execute(
            """
            SELECT
                u.username AS student_name,
                COUNT(*) AS attempts,
                AVG(pfa.score) AS avg_score
            FROM PracticeFirstAttempts pfa
            JOIN Practice_Quizzes q ON q.quiz_id = pfa.quiz_id
            JOIN Users u ON u.user_id = pfa.user_id
            WHERE q.created_by = ?
            GROUP BY u.username
            """,
            (session['user_id'],)
        ).fetchall()

    live_map = {(r["student_name"] or "").strip().lower(): r for r in live_rows}
    practice_map = {(r["student_name"] or "").strip().lower(): r for r in practice_rows}
    all_keys = sorted(set(live_map.keys()) | set(practice_map.keys()))

    headers = [
        "Student",
        "Live Avg Score",
        "Live Sessions",
        "Practice Avg Score",
        "Practice Attempts",
        "Combined Score"
    ]
    data = []
    for key in all_keys:
        live = live_map.get(key)
        practice = practice_map.get(key)
        student_name = (live["student_name"] if live else None) or (practice["student_name"] if practice else key)
        live_avg = round((live["total_score"] / live["sessions_attempted"]), 2) if live and live["sessions_attempted"] else 0
        practice_avg = round(practice["avg_score"], 2) if practice and practice["avg_score"] is not None else 0
        divisor = (1 if live_avg else 0) + (1 if practice_avg else 0)
        combined = round((live_avg + practice_avg) / divisor, 2) if divisor else 0
        data.append([
            student_name,
            live_avg,
            live["sessions_attempted"] if live else 0,
            practice_avg,
            practice["attempts"] if practice else 0,
            combined
        ])

    filename = "teacher_reports.csv"
    return _csv_response(filename, headers, data)


def _extract_first_json_object(raw_text):
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("Empty model response.")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in model response.")
    return json.loads(text[start:end + 1])


def _extract_notes_text(upload):
    if not upload or not getattr(upload, "filename", ""):
        return ""
    filename = secure_filename(upload.filename)
    ext = os.path.splitext(filename)[1].lower()
    max_bytes = 5 * 1024 * 1024
    raw = upload.stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("Notes file too large. Max 5 MB.")
    if ext == ".txt":
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1")
    if ext == ".docx":
        try:
            with zipfile.ZipFile(BytesIO(raw)) as zf:
                xml_data = zf.read("word/document.xml")
        except Exception:
            raise ValueError("Invalid DOCX file.")
        try:
            root = ElementTree.fromstring(xml_data)
        except Exception:
            raise ValueError("Could not read DOCX content.")
        texts = []
        for node in root.iter():
            if node.tag.endswith("}t") and node.text:
                texts.append(node.text)
            if node.tag.endswith("}p"):
                texts.append("\n")
        return "".join(texts).strip()
    if ext == ".pdf":
        if PdfReader is None:
            raise ValueError("PDF support not available. Install PyPDF2.")
        try:
            reader = PdfReader(BytesIO(raw))
        except Exception:
            raise ValueError("Invalid PDF file.")
        pages_text = []
        for page in reader.pages:
            try:
                pages_text.append(page.extract_text() or "")
            except Exception:
                pages_text.append("")
        text = "\n".join(pages_text).strip()
        if not text:
            raise ValueError("Could not extract text from PDF.")
        return text
    raise ValueError("Unsupported notes file. Use .txt, .docx, or .pdf.")


def _normalize_ai_questions(payload_questions, requested_count):
    normalized = []
    for i, item in enumerate(payload_questions or []):
        if not isinstance(item, dict):
            continue
        q_text = str(item.get("question", "")).strip()
        options = item.get("options") or []
        if not q_text or not isinstance(options, list) or len(options) != 4:
            continue
        option_texts = [str(x).strip() for x in options]
        if any(not t for t in option_texts):
            continue
        correct_index = item.get("correct_index")
        try:
            correct_index = int(correct_index)
        except (TypeError, ValueError):
            continue
        if correct_index < 0 or correct_index > 3:
            continue
        normalized.append({
            "question": q_text,
            "options": option_texts,
            "correct_option": ["A", "B", "C", "D"][correct_index]
        })
        if len(normalized) >= requested_count:
            break
    return normalized


def _normalize_correct_option(correct_raw, options):
    if correct_raw is None:
        return None
    value = str(correct_raw).strip()
    if not value:
        return None
    upper = value.upper()
    if upper in ("A", "B", "C", "D"):
        return upper
    # Accept values like "A)" or "Ans: A"
    m = re.search(r"\b([A-D])\b", upper)
    if m:
        return m.group(1)
    if value.isdigit():
        idx = int(value)
        if 1 <= idx <= 4:
            return ["A", "B", "C", "D"][idx - 1]
    # Match by option text
    for idx, opt in enumerate(options):
        if str(opt).strip().lower() == value.lower():
            return ["A", "B", "C", "D"][idx]
    return None


def _normalize_mcq_row(question_text, options, correct_raw, explanation=""):
    q_text = str(question_text or "").strip()
    options = [str(o or "").strip() for o in (options or [])]
    if not q_text or len(options) != 4 or any(not o for o in options):
        return None
    correct = _normalize_correct_option(correct_raw, options)
    if not correct:
        return None
    return {
        "question": q_text,
        "options": options,
        "correct_option": correct,
        "explanation": str(explanation or "").strip()
    }


def _parse_mcq_rows_from_text(text):
    if not text:
        return []
    # Try JSON first
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            payload = json.loads(stripped)
            items = payload.get("questions") if isinstance(payload, dict) else payload
            if isinstance(items, list):
                rows = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    row = _normalize_mcq_row(
                        item.get("question"),
                        item.get("options") or [],
                        item.get("correct_option"),
                        item.get("explanation", "")
                    )
                    if row:
                        rows.append(row)
                return rows
        except Exception:
            pass
    # Fallback: try CSV/TSV
    try:
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample)
        except Exception:
            dialect = csv.excel
        reader = csv.DictReader(StringIO(text), dialect=dialect)
        rows = []
        for row in reader:
            if not row:
                continue
            row_lc = {str(k or "").strip().lower(): (v or "") for k, v in row.items()}
            q_text = row_lc.get("question", "")
            options = [
                row_lc.get("option_a", ""),
                row_lc.get("option_b", ""),
                row_lc.get("option_c", ""),
                row_lc.get("option_d", "")
            ]
            normalized = _normalize_mcq_row(q_text, options, row_lc.get("correct_option"), row_lc.get("explanation", ""))
            if normalized:
                rows.append(normalized)
        return rows
    except Exception:
        return []


def _extract_inline_mcq(question_block, correct_raw=None):
    text = str(question_block or "").strip()
    if not text:
        return None
    # Normalize spacing
    text = re.sub(r"\s+", " ", text)
    # Find option markers A) B) C) D) even if not preceded by spaces
    pattern = re.compile(r"(?:^|\W)([A-Da-d])[\)\.]\s*")
    matches = list(pattern.finditer(text))
    if len(matches) < 4:
        return None
    question = text[:matches[0].start()].strip()
    question = re.sub(r"^\d+\.\s*", "", question).strip()
    options = ["", "", "", ""]
    for i, m in enumerate(matches):
        label = m.group(1).upper()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        opt_text = text[start:end].strip()
        idx = ord(label) - ord("A")
        if 0 <= idx <= 3:
            options[idx] = opt_text
    if any(not o for o in options):
        return None
    return _normalize_mcq_row(question, options, correct_raw)


@app.route('/api/quiz/import-mcq', methods=['POST'])
@login_required
def import_mcq_file():
    if session.get('role') != 'Teacher':
        return jsonify({"ok": False, "error": "Access denied"}), 403

    upload = request.files.get("mcq_file")
    if not upload or not getattr(upload, "filename", ""):
        return jsonify({"ok": False, "error": "MCQ file is required"}), 400

    filename = secure_filename(upload.filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".csv", ".json", ".xlsx", ".txt", ".docx", ".pdf"):
        return jsonify({"ok": False, "error": "Unsupported file. Use .csv, .json, .xlsx, .txt, .docx, or .pdf"}), 400

    raw = upload.stream.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        return jsonify({"ok": False, "error": "File too large. Max 5 MB."}), 400

    questions = []
    try:
        if ext == ".xlsx":
            if openpyxl is None:
                return jsonify({"ok": False, "error": "XLSX support not available. Install openpyxl."}), 400
            wb = openpyxl.load_workbook(BytesIO(raw), data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                return jsonify({"ok": False, "error": "Empty XLSX file"}), 400
            headers = [str(h or "").strip().lower() for h in rows[0]]
            has_header = "question" in headers and "option_a" in headers
            if has_header:
                for r in rows[1:]:
                    row_map = {headers[i]: (r[i] if i < len(r) else "") for i in range(len(headers))}
                    q_text = row_map.get("question", "")
                    options = [
                        row_map.get("option_a", ""),
                        row_map.get("option_b", ""),
                        row_map.get("option_c", ""),
                        row_map.get("option_d", "")
                    ]
                    normalized = _normalize_mcq_row(q_text, options, row_map.get("correct_option"), row_map.get("explanation", ""))
                    if normalized:
                        questions.append(normalized)
            else:
                # Fallback: each row has inline MCQ text in first cell and correct option in last cell
                for r in rows:
                    if not r or all(cell is None or str(cell).strip() == "" for cell in r):
                        continue
                    first_cell = str(r[0] or "").strip().lower()
                    # Skip header rows if present
                    if first_cell in ("question", "questions"):
                        continue
                    q_block = r[0] if len(r) > 0 else ""
                    correct = r[-1] if len(r) > 0 else ""
                    normalized = _extract_inline_mcq(q_block, correct)
                    if normalized:
                        questions.append(normalized)
        elif ext == ".csv":
            text = raw.decode("utf-8", "ignore")
            questions.extend(_parse_mcq_rows_from_text(text))
        elif ext == ".json":
            text = raw.decode("utf-8", "ignore")
            questions.extend(_parse_mcq_rows_from_text(text))
        elif ext in (".txt", ".docx", ".pdf"):
            # Extract text, then parse as JSON or CSV-like if possible.
            if ext in (".docx", ".pdf"):
                fake_upload = type("U", (), {"filename": filename, "stream": BytesIO(raw)})()
                extracted = _extract_notes_text(fake_upload)
            else:
                extracted = raw.decode("utf-8", "ignore")
            questions.extend(_parse_mcq_rows_from_text(extracted))
    except Exception:
        return jsonify({"ok": False, "error": "Failed to parse MCQ file"}), 400

    if not questions:
        return jsonify({"ok": False, "error": "No valid questions found in file"}), 400

    questions = questions[:200]
    return jsonify({"ok": True, "questions": questions})


@app.route('/api/ai/generate-questions', methods=['POST'])
@login_required
def ai_generate_questions():
    if session.get('role') != 'Teacher':
        return jsonify({"ok": False, "error": "Access denied"}), 403

    data = request.get_json(silent=True) if request.is_json else None
    if data is None:
        data = request.form or {}
    topic = str(data.get("topic", "")).strip()
    difficulty_raw = str(data.get("difficulty", "")).strip().lower()
    allowed_difficulties = {"easy", "medium", "hard"}
    difficulty = difficulty_raw if difficulty_raw in allowed_difficulties else "medium"
    try:
        num_questions = int(data.get("num_questions", 5))
    except (TypeError, ValueError):
        num_questions = 5
    num_questions = max(1, min(num_questions, 20))

    notes_text = str(data.get("notes_text", "")).strip()
    notes_file = None if request.is_json else request.files.get("notes_file")
    if notes_file and getattr(notes_file, "filename", ""):
        try:
            file_notes = _extract_notes_text(notes_file)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        if file_notes:
            notes_text = f"{notes_text}\n\n{file_notes}".strip()

    if not topic and not notes_text:
        return jsonify({"ok": False, "error": "Please enter a topic or provide notes."}), 400

    api_key = OPENAI_API_KEY.strip()
    if not api_key:
        return jsonify({
            "ok": False,
            "error": "OpenRouter API key missing. Set OPENAI_API_KEY in .env file."
        }), 400

    notes_block = ""
    if notes_text:
        clipped_notes = notes_text[:20000]
        notes_block = f'\nUse ONLY the following notes as source material:\n"""\n{clipped_notes}\n"""\n'
    topic_line = f' on topic: "{topic}"' if topic else ""

    prompt = f"""
Generate exactly {num_questions} multiple-choice quiz questions{topic_line}.
Difficulty: {difficulty} (easy = basic recall, medium = conceptual/application, hard = analytical).{notes_block}
Return ONLY JSON in this exact schema:
{{
  "questions": [
    {{
      "question": "Question text",
      "options": ["Option A", "Option B", "Option C", "Option D"],
      "correct_index": 0
    }}
  ]
}}
Rules:
- exactly 4 options per question
- only one correct option
- correct_index must be integer 0-3
- avoid duplicate questions
- use proper, grammatically correct English only
- keep wording clear and student-friendly
- avoid Hinglish/Marathi or mixed-language text
- if a question includes code, include it on new lines inside the question text and preserve indentation (no markdown fences)
- no markdown, no explanation, no extra text outside JSON
""".strip()

    body = {
        "model": OPENROUTER_MODEL,
        "temperature": 0.4,
        "messages": [
            {
                "role": "system",
                "content": "You are a precise quiz generator. Use clear, proper English only. Output valid JSON only."
            },
            {"role": "user", "content": prompt}
        ]
    }
    encoded_body = json.dumps(body).encode("utf-8")
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    # OpenRouter required headers
    headers["HTTP-Referer"] = "https://quiz-app.local"
    headers["X-Title"] = "Quiz App"
    
    req = urllib.request.Request(
        f"{OPENAI_BASE_URL}/chat/completions",
        data=encoded_body,
        headers=headers,
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8")
        api_data = json.loads(raw)
        content = (((api_data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        parsed = _extract_first_json_object(content)
        questions = _normalize_ai_questions(parsed.get("questions"), num_questions)
        if len(questions) < num_questions:
            return jsonify({
                "ok": False,
                "error": f"Model returned only {len(questions)} valid questions. Try again."
            }), 502
        return jsonify({"ok": True, "questions": questions})
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            pass
        return jsonify({
            "ok": False,
            "error": f"AI API request failed ({e.code}). {err_body[:250]}"
        }), 502
    except Exception as e:
        return jsonify({"ok": False, "error": f"AI generation failed: {str(e)}"}), 502


@app.route('/create_quiz/<quiz_type>', methods=['GET', 'POST'])
@login_required
def create_quiz(quiz_type):

    if session.get('role') != 'Teacher':
        flash("Access denied")
        return redirect('/login')

    if request.method == 'POST':

        title = request.form['title']
        description = request.form.get('description', '')
        try:
            time_per_question = int(request.form.get('time_per_question', 20))
        except ValueError:
            time_per_question = 20
        time_per_question = max(5, min(time_per_question, 300))
        teacher_id = session['user_id']

        try:
            with get_db_connection() as conn:

                # 🔥 INSERT BASED ON TYPE
                if quiz_type == "live":

                    cursor = conn.execute("""
                        INSERT INTO Quizzes (quiz_name, description, created_by)
                        VALUES (?, ?, ?)
                    """, (title, description, teacher_id))

                elif quiz_type == "practice":
                    selected_departments = normalize_departments(request.form.getlist('departments'))
                    if not selected_departments:
                        flash("Please select at least one department")
                        return redirect(url_for('create_quiz', quiz_type='practice'))

                    cursor = conn.execute("""
                        INSERT INTO Practice_Quizzes (quiz_name, description, teacher_id, created_by, target_departments)
                        VALUES (?, ?, ?, ?, ?)
                    """, (title, description, teacher_id, teacher_id, ",".join(selected_departments)))

                else:
                    flash("Invalid quiz type")
                    return redirect('/teacher_dashboard')

                quiz_id = cursor.lastrowid
                if quiz_type == "practice":
                    ensure_legacy_practice_quiz_row(conn, quiz_id, title, description)

                # ---------------- Questions ----------------
                questions = request.form.getlist('question[]')
                question_time_limits = request.form.getlist('question_time_limit[]')
                option_a_list = request.form.getlist('option_a[]')
                option_b_list = request.form.getlist('option_b[]')
                option_c_list = request.form.getlist('option_c[]')
                option_d_list = request.form.getlist('option_d[]')
                question_images = request.files.getlist('question_image[]') if request.files else []

                correct_options_dict = {}
                for key in request.form.keys():
                    if key.startswith("correct_option"):
                        idx = key[key.find("[")+1:key.find("]")]
                        correct_options_dict[idx] = request.form[key]

                for i, q_text in enumerate(questions):
                    per_question_time = time_per_question
                    if quiz_type == "live":
                        try:
                            per_question_time = int(question_time_limits[i])
                        except (ValueError, IndexError):
                            per_question_time = 20
                        per_question_time = max(5, min(per_question_time, 300))

                    media_url = None
                    if i < len(question_images):
                        media_url = _save_question_image(question_images[i])

                    correct_code = (correct_options_dict.get(str(i)) or "").strip().upper()
                    if correct_code not in ("A", "B", "C", "D"):
                        raise ValueError(f"Please select the correct option for question {i + 1}.")

                    options = [
                        ('A', option_a_list[i]),
                        ('B', option_b_list[i]),
                        ('C', option_c_list[i]),
                        ('D', option_d_list[i])
                    ]

                    if quiz_type == "practice":
                        q_cursor = conn.execute("""
                            INSERT INTO PracticeQuestions
                            (quiz_id, question_text, explanation, media_url)
                            VALUES (?, ?, ?, ?)
                        """, (quiz_id, q_text, "", media_url))
                        question_id = q_cursor.lastrowid

                        for opt_idx, (code, text) in enumerate(options):
                            is_correct = 1 if code == correct_code else 0
                            conn.execute("""
                                INSERT INTO PracticeOptions
                                (question_id, option_text, is_correct, option_order)
                                VALUES (?, ?, ?, ?)
                            """, (question_id, text, is_correct, opt_idx))
                    else:
                        q_cursor = conn.execute("""
                            INSERT INTO Questions
                            (quiz_id, question_text, question_type, time_limit, media_url)
                            VALUES (?, ?, ?, ?, ?)
                        """, (quiz_id, q_text, "Multiple Choice", per_question_time, media_url))
                        question_id = q_cursor.lastrowid

                        for code, text in options:
                            is_correct = 1 if code == correct_code else 0
                            conn.execute("""
                                INSERT INTO Options
                                (question_id, option_text, is_correct)
                                VALUES (?, ?, ?)
                            """, (question_id, text, is_correct))

                conn.commit()

            flash(f"{quiz_type.capitalize()} quiz created successfully!")
            return redirect('/teacher_dashboard')

        except ValueError as e:
            flash(str(e))
            return redirect(request.url)
        except Exception as e:
            print("Error:", e)
            flash("Error creating quiz. Check console.")
            return redirect(request.url)

    return render_template('teacher/create_quiz.html', quiz_type=quiz_type)

@app.route('/start_quiz/<int:quiz_id>')
def start_quiz(quiz_id):
    if 'role' not in session or session['role'] != 'Teacher':
        flash("Please login as teacher")
        return redirect('/login')

    conn = get_db_connection()
    pin = random.randint(100000, 999999)  # 6-digit PIN

    # Insert live session
    conn.execute("""
        INSERT INTO live_sessions (quiz_id, pin, created_by)
        VALUES (?, ?, ?)
    """, (quiz_id, pin, session['user_id']))
    conn.commit()

    # Get the session_id
    session_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()['id']
    conn.close()

    return redirect(f'/host_lobby/{session_id}')

# ---------------- DELETE QUESTION ----------------

@app.route('/delete_question/<int:question_id>/<int:quiz_id>')
@login_required
def delete_question(question_id, quiz_id):

    if session.get('role') != 'Teacher':
        return redirect('/login')

    with get_db_connection() as conn:
        conn.execute("DELETE FROM Options WHERE question_id=?", (question_id,))
        conn.execute("DELETE FROM Questions WHERE question_id=?", (question_id,))
        conn.commit()

    flash("Question deleted")
    return redirect(f'/edit_quiz/{quiz_id}')


@app.route('/add_question/<int:quiz_id>', methods=['POST'])
@login_required
def add_question(quiz_id):

    question_text = request.form['question_text']
    options = request.form.getlist('option_text[]')
    correct_index = int(request.form['correct_option'])

    with get_db_connection() as conn:

        # Insert question
        cur = conn.execute(
            "INSERT INTO Questions (quiz_id, question_text) VALUES (?, ?)",
            (quiz_id, question_text)
        )
        question_id = cur.lastrowid

        # Insert options
        for i, opt in enumerate(options):
            is_correct = 1 if i == correct_index else 0
            conn.execute(
                "INSERT INTO Options (question_id, option_text, is_correct) VALUES (?, ?, ?)",
                (question_id, opt, is_correct)
            )

        conn.commit()

    flash("Question added successfully")
    return redirect(url_for('edit_quiz', quiz_id=quiz_id))

# ---------------- EDIT QUIZ ----------------
@app.route('/edit_quiz/<int:quiz_id>', methods=['GET'])
@login_required
def edit_quiz(quiz_id):
    if session.get('role') != 'Teacher':
        return redirect('/login')

    with get_db_connection() as conn:
        quiz = conn.execute(
            "SELECT * FROM Quizzes WHERE quiz_id=?",
            (quiz_id,)
        ).fetchone()

        questions = conn.execute(
            "SELECT * FROM Questions WHERE quiz_id=?",
            (quiz_id,)
        ).fetchall()

        questions_with_options = []
        for q in questions:
            options = conn.execute(
                "SELECT * FROM Options WHERE question_id=?",
                (q['question_id'],)
            ).fetchall()

            questions_with_options.append({
                "question": q,
                "options": options
            })

    return render_template(
        'teacher/edit_quiz.html',
        quiz=quiz,
        questions=questions_with_options
    )

@app.route('/delete_quiz/<int:quiz_id>', methods=['POST'])
@login_required
def delete_quiz(quiz_id):

    if session.get('role') != 'Teacher':
        return redirect('/login')

    try:
        with get_db_connection() as conn:
            quiz = conn.execute(
                "SELECT * FROM Quizzes WHERE quiz_id=? AND created_by=?",
                (quiz_id, session['user_id'])
            ).fetchone()

            if not quiz:
                flash("Quiz not found", "warning")
                return redirect(url_for('teacher_live_quizzes'))

            # 1️⃣ Delete Options
            conn.execute("""
                DELETE FROM Options
                WHERE question_id IN (
                    SELECT question_id FROM Questions WHERE quiz_id=?
                )
            """, (quiz_id,))

            # 2️⃣ Delete Questions
            conn.execute("DELETE FROM Questions WHERE quiz_id=?", (quiz_id,))

            # 2.5️⃣ Delete player answers (FK to live_sessions)
            conn.execute("""
                DELETE FROM player_answers
                WHERE session_id IN (
                    SELECT session_id FROM live_sessions WHERE quiz_id=?
                )
            """, (quiz_id,))

            # 3️⃣ Delete participants
            conn.execute("""
                DELETE FROM participants
                WHERE session_id IN (
                    SELECT session_id FROM live_sessions WHERE quiz_id=?
                )
            """, (quiz_id,))

            # 4️⃣ Delete live sessions
            conn.execute("DELETE FROM live_sessions WHERE quiz_id=?", (quiz_id,))

            # 5️⃣ Delete PlayerScores
            conn.execute("""
                DELETE FROM PlayerScores
                WHERE session_id IN (
                    SELECT session_id FROM GameSessions WHERE quiz_id=?
                )
            """, (quiz_id,))

            # 6️⃣ Delete GameSessions
            conn.execute("DELETE FROM GameSessions WHERE quiz_id=?", (quiz_id,))

            # 7️⃣ Finally delete Quiz
            conn.execute("DELETE FROM Quizzes WHERE quiz_id=?", (quiz_id,))

            conn.commit()

        flash("Quiz deleted successfully ✅", "success")

    except Exception as e:
        print("Delete quiz error:", e)
        flash("Error deleting quiz ❌", "danger")

    return redirect(url_for('teacher_live_quizzes'))



# ---------------- UPDATE QUIZ ----------------
@app.route('/update_quiz/<int:quiz_id>', methods=['POST'])
@login_required
def update_quiz(quiz_id):

    if session.get('role') != 'Teacher':
        return redirect('/login')

    conn = get_db_connection()
    cur = conn.cursor()

    try:
        # -------- UPDATE EXISTING QUESTIONS --------
        question_ids = request.form.getlist('question_id[]')
        question_texts = request.form.getlist('question_text[]')
        existing_images = request.files.getlist('question_image_existing[]') if request.files else []
        time_limits_existing = request.form.getlist('time_limit_existing[]')

        option_ids = request.form.getlist('option_id[]')
        option_texts = request.form.getlist('option_text[]')

        opt_index = 0

        for idx, (qid, qtext) in enumerate(zip(question_ids, question_texts)):
            try:
                time_limit = int(time_limits_existing[idx])
            except (ValueError, IndexError, TypeError):
                time_limit = 20
            time_limit = max(5, min(time_limit, 300))

            media_url = None
            if idx < len(existing_images):
                media_url = _save_question_image(existing_images[idx])

            if media_url:
                cur.execute(
                    "UPDATE Questions SET question_text=?, time_limit=?, media_url=? WHERE question_id=?",
                    (qtext, time_limit, media_url, qid)
                )
            else:
                cur.execute(
                    "UPDATE Questions SET question_text=?, time_limit=? WHERE question_id=?",
                    (qtext, time_limit, qid)
                )

            correct_option_id = request.form.get(f'correct_{qid}')

            for _ in range(4):
                oid = option_ids[opt_index]
                otext = option_texts[opt_index]

                is_correct = 1 if str(oid) == str(correct_option_id) else 0

                cur.execute(
                    "UPDATE Options SET option_text=?, is_correct=? WHERE option_id=?",
                    (otext, is_correct, oid)
                )

                opt_index += 1

        # -------- ADD NEW QUESTIONS --------
        new_questions = request.form.getlist('new_question[]')

        if new_questions:
            new_q_texts = request.form.getlist('question_text[]')[-len(new_questions):]
            new_images = request.files.getlist('new_question_image[]') if request.files else []
            time_limits_new = request.form.getlist('time_limit_new[]')

            option1 = request.form.getlist('option1[]')
            option2 = request.form.getlist('option2[]')
            option3 = request.form.getlist('option3[]')
            option4 = request.form.getlist('option4[]')

            for i, qtext in enumerate(new_q_texts):
                try:
                    time_limit = int(time_limits_new[i])
                except (ValueError, IndexError, TypeError):
                    time_limit = 20
                time_limit = max(5, min(time_limit, 300))

                media_url = None
                if i < len(new_images):
                    media_url = _save_question_image(new_images[i])

                cur.execute(
                    "INSERT INTO Questions (quiz_id, question_text, time_limit, media_url) VALUES (?, ?, ?, ?)",
                    (quiz_id, qtext, time_limit, media_url)
                )

                new_qid = cur.lastrowid
                correct = request.form.get(f'correct_{i}')

                options = [
                    (option1[i], 'A'),
                    (option2[i], 'B'),
                    (option3[i], 'C'),
                    (option4[i], 'D')
                ]

                for opt_text, label in options:
                    is_correct = 1 if correct == label else 0
                    cur.execute(
                        "INSERT INTO Options (question_id, option_text, is_correct) VALUES (?,?,?)",
                        (new_qid, opt_text, is_correct)
                    )
    except ValueError as e:
        conn.close()
        flash(str(e), "danger")
        return redirect(url_for('edit_quiz', quiz_id=quiz_id))

    conn.commit()
    conn.close()

    # ✅ SUCCESS MESSAGE + DASHBOARD REDIRECT
    flash("Quiz updated successfully!", "success")
    return redirect(url_for('teacher_dashboard'))




@app.route('/leaderboard/<int:session_id>')
def leaderboard(session_id):
    with get_db_connection() as conn:
        scores = conn.execute("""
            SELECT user_id, score, correct_answers FROM PlayerScores
            WHERE session_id=?
            ORDER BY score DESC
        """, (session_id,)).fetchall()
    return render_template('shared/leaderboard.html', scores=scores)


@app.route('/student_practice_quizzes')
@login_required
def student_practice_quizzes():
    if session.get('role') != 'Student':
        return redirect('/login')

    with get_db_connection() as conn:
        student_department_row = conn.execute(
            "SELECT COALESCE(NULLIF(department, ''), 'Computer') AS department FROM Users WHERE user_id=?",
            (session['user_id'],)
        ).fetchone()
        student_department = student_department_row['department'] if student_department_row else 'Computer'

        quizzes = conn.execute("""
            SELECT
                p.quiz_id,
                p.quiz_name,
                p.description,
                p.created_at,
                COALESCE(u.username, 'Teacher') AS teacher_name,
                COALESCE(
                    NULLIF(p.target_departments, ''),
                    COALESCE(NULLIF(p.department, ''), COALESCE(NULLIF(u.department, ''), 'Computer'))
                ) AS quiz_departments,
                COUNT(DISTINCT q.question_id) AS total_questions,
                pp.score AS last_score,
                pp.correct_answers AS last_correct,
                pp.total_questions AS last_total,
                pp.completed_at
            FROM Practice_Quizzes p
            LEFT JOIN Users u ON p.created_by = u.user_id
            LEFT JOIN PracticeQuestions q ON p.quiz_id = q.quiz_id
            LEFT JOIN PracticeProgress pp
                ON pp.quiz_id = p.quiz_id AND pp.user_id = ?
            WHERE (',' || COALESCE(
                NULLIF(p.target_departments, ''),
                COALESCE(NULLIF(p.department, ''), COALESCE(NULLIF(u.department, ''), 'Computer'))
            ) || ',') LIKE '%,' || ? || ',%'
              GROUP BY p.quiz_id
              ORDER BY p.quiz_id DESC
          """, (session['user_id'], student_department)).fetchall()

        total_available = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM Practice_Quizzes p
            LEFT JOIN Users u ON p.created_by = u.user_id
            WHERE (',' || COALESCE(
                NULLIF(p.target_departments, ''),
                COALESCE(NULLIF(p.department, ''), COALESCE(NULLIF(u.department, ''), 'Computer'))
            ) || ',') LIKE '%,' || ? || ',%'
            """,
            (student_department,)
        ).fetchone()["total"]

        solved_count = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM PracticeFirstAttempts
            WHERE user_id=?
            """,
            (session['user_id'],)
        ).fetchone()["total"]

    solved_pct = round((solved_count / total_available) * 100, 1) if total_available else 0
    practice_stats = {
        "total_available": total_available,
        "solved_count": solved_count,
        "solved_pct": solved_pct
    }

    return render_template(
        'student/student_practice_quizzes.html',
        quizzes=quizzes,
        student_department=student_department,
        practice_stats=practice_stats
    )


@app.route('/take_practice_quiz/<int:quiz_id>')
@login_required
def take_practice_quiz(quiz_id):
    if session.get('role') != 'Student':
        return redirect('/login')

    with get_db_connection() as conn:
        student_department_row = conn.execute(
            "SELECT COALESCE(NULLIF(department, ''), 'Computer') AS department FROM Users WHERE user_id=?",
            (session['user_id'],)
        ).fetchone()
        student_department = student_department_row['department'] if student_department_row else 'Computer'

        quiz = conn.execute(
            """
            SELECT p.*
            FROM Practice_Quizzes p
            LEFT JOIN Users u ON p.created_by = u.user_id
            WHERE p.quiz_id=?
              AND (',' || COALESCE(
                    NULLIF(p.target_departments, ''),
                    COALESCE(NULLIF(p.department, ''), COALESCE(NULLIF(u.department, ''), 'Computer'))
                  ) || ',') LIKE '%,' || ? || ',%'
            """,
            (quiz_id, student_department)
        ).fetchone()
        if not quiz:
            flash("This quiz is not available for your department")
            return redirect(url_for('student_practice_quizzes'))

        questions = conn.execute(
            "SELECT * FROM PracticeQuestions WHERE quiz_id=? ORDER BY question_id",
            (quiz_id,)
        ).fetchall()

        questions_with_options = []
        for q in questions:
            options = conn.execute(
                "SELECT * FROM PracticeOptions WHERE question_id=? ORDER BY option_order, option_id",
                (q['question_id'],)
            ).fetchall()
            questions_with_options.append({
                "question": q,
                "options": options
            })

    return render_template(
        'student/take_practice_quiz.html',
        quiz=quiz,
        questions=questions_with_options
    )


@app.route('/download_practice_quiz/<int:quiz_id>')
@login_required
def download_practice_quiz(quiz_id):
    if session.get('role') != 'Student':
        return redirect('/login')

    user_id = session['user_id']

    with get_db_connection() as conn:
        student_department_row = conn.execute(
            "SELECT COALESCE(NULLIF(department, ''), 'Computer') AS department FROM Users WHERE user_id=?",
            (user_id,)
        ).fetchone()
        student_department = student_department_row['department'] if student_department_row else 'Computer'

        quiz = conn.execute(
            """
            SELECT p.*
            FROM Practice_Quizzes p
            LEFT JOIN Users u ON p.created_by = u.user_id
            WHERE p.quiz_id=?
              AND (',' || COALESCE(
                    NULLIF(p.target_departments, ''),
                    COALESCE(NULLIF(p.department, ''), COALESCE(NULLIF(u.department, ''), 'Computer'))
                  ) || ',') LIKE '%,' || ? || ',%'
            """,
            (quiz_id, student_department)
        ).fetchone()
        if not quiz:
            flash("This quiz is not available for your department")
            return redirect(url_for('student_practice_quizzes'))

        questions = conn.execute(
            "SELECT question_id, question_text, explanation FROM PracticeQuestions WHERE quiz_id=? ORDER BY question_id",
            (quiz_id,)
        ).fetchall()

        if not questions:
            flash("No questions found in this quiz")
            return redirect(url_for('student_practice_quizzes'))

        progress = conn.execute(
            """
            SELECT completed_at
            FROM PracticeProgress
            WHERE user_id=? AND quiz_id=?
            """,
            (user_id, quiz_id)
        ).fetchone()
        if not progress or not progress['completed_at']:
            flash("Please solve and submit the quiz first, then download answers.")
            return redirect(url_for('take_practice_quiz', quiz_id=quiz_id))

        lines = [
            f"Quiz: {quiz['quiz_name']}",
            f"Description: {quiz['description'] or ''}",
            f"Downloaded By User ID: {user_id}",
            f"Downloaded At: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            ""
        ]

        for idx, q in enumerate(questions, start=1):
            options = conn.execute(
                "SELECT option_id, option_text, is_correct FROM PracticeOptions WHERE question_id=? ORDER BY option_order, option_id",
                (q['question_id'],)
            ).fetchall()
            correct_text = ""
            for opt in options:
                if opt['is_correct'] == 1:
                    correct_text = opt['option_text']
                    break
            lines.append(f"Q{idx}. {q['question_text']}")
            for opt_idx, opt in enumerate(options):
                label = chr(ord('A') + opt_idx)
                lines.append(f"   {label}. {opt['option_text']}")
            lines.append(f"Correct Answer: {correct_text or 'N/A'}")
            if q['explanation']:
                lines.append(f"Explanation: {q['explanation']}")
            lines.append("")

    content = "\n".join(lines)
    filename = f"{slugify_filename(quiz['quiz_name'])}_with_answers.txt"
    return Response(
        content,
        mimetype='text/plain; charset=utf-8',
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.route('/download_practice_quiz_page/<int:quiz_id>')
@login_required
def download_practice_quiz_page(quiz_id):
    if session.get('role') != 'Student':
        return redirect('/login')

    user_id = session['user_id']
    with get_db_connection() as conn:
        student_department_row = conn.execute(
            "SELECT COALESCE(NULLIF(department, ''), 'Computer') AS department FROM Users WHERE user_id=?",
            (user_id,)
        ).fetchone()
        student_department = student_department_row['department'] if student_department_row else 'Computer'

        quiz = conn.execute(
            """
            SELECT p.quiz_id, p.quiz_name, p.description
            FROM Practice_Quizzes p
            LEFT JOIN Users u ON p.created_by = u.user_id
            WHERE p.quiz_id=?
              AND (',' || COALESCE(
                    NULLIF(p.target_departments, ''),
                    COALESCE(NULLIF(p.department, ''), COALESCE(NULLIF(u.department, ''), 'Computer'))
                  ) || ',') LIKE '%,' || ? || ',%'
            """,
            (quiz_id, student_department)
        ).fetchone()
        if not quiz:
            flash("This quiz is not available for your department")
            return redirect(url_for('student_practice_quizzes'))

        qcount = conn.execute(
            "SELECT COUNT(*) AS total FROM PracticeQuestions WHERE quiz_id=?",
            (quiz_id,)
        ).fetchone()
        total_questions = qcount['total'] if qcount else 0

        progress = conn.execute(
            """
            SELECT completed_at
            FROM PracticeProgress
            WHERE user_id=? AND quiz_id=?
            """,
            (user_id, quiz_id)
        ).fetchone()
        can_download = 1 if (progress and progress['completed_at']) else 0

    return render_template(
        'student/download_practice_quiz.html',
        quiz=quiz,
        total_questions=total_questions,
        can_download=can_download
    )


@app.route('/submit_practice_quiz/<int:quiz_id>', methods=['POST'])
@login_required
def submit_practice_quiz(quiz_id):
    if session.get('role') != 'Student':
        return redirect('/login')

    user_id = session['user_id']

    with get_db_connection() as conn:
        student_department_row = conn.execute(
            "SELECT COALESCE(NULLIF(department, ''), 'Computer') AS department FROM Users WHERE user_id=?",
            (session['user_id'],)
        ).fetchone()
        student_department = student_department_row['department'] if student_department_row else 'Computer'

        quiz = conn.execute(
            """
            SELECT p.*
            FROM Practice_Quizzes p
            LEFT JOIN Users u ON p.created_by = u.user_id
            WHERE p.quiz_id=?
              AND (',' || COALESCE(
                    NULLIF(p.target_departments, ''),
                    COALESCE(NULLIF(p.department, ''), COALESCE(NULLIF(u.department, ''), 'Computer'))
                  ) || ',') LIKE '%,' || ? || ',%'
            """,
            (quiz_id, student_department)
        ).fetchone()
        if not quiz:
            flash("This quiz is not available for your department")
            return redirect(url_for('student_practice_quizzes'))

        questions = conn.execute(
            "SELECT question_id FROM PracticeQuestions WHERE quiz_id=? ORDER BY question_id",
            (quiz_id,)
        ).fetchall()

        existing_first_attempt = conn.execute(
            """
            SELECT attempt_id
            FROM PracticeFirstAttempts
            WHERE user_id=? AND quiz_id=?
            LIMIT 1
            """,
            (user_id, quiz_id)
        ).fetchone()
        is_first_attempt = existing_first_attempt is None

        total_questions = len(questions)
        correct_answers = 0

        # Keep only the latest answer set for this user+quiz.
        conn.execute(
            "DELETE FROM PracticeAnswers WHERE user_id=? AND quiz_id=?",
            (user_id, quiz_id)
        )

        for q in questions:
            question_id = q['question_id']
            selected_option_id = request.form.get(f'answer_{question_id}')
            selected_option_id = int(selected_option_id) if selected_option_id else None

            correct_option = conn.execute(
                """
                SELECT option_id
                FROM PracticeOptions
                WHERE question_id=? AND is_correct=1
                LIMIT 1
                """,
                (question_id,)
            ).fetchone()
            correct_option_id = correct_option['option_id'] if correct_option else None
            is_correct = 1 if selected_option_id and selected_option_id == correct_option_id else 0
            correct_answers += is_correct

            conn.execute(
                """
                INSERT INTO PracticeAnswers
                    (user_id, quiz_id, question_id, selected_option_id, is_correct, submitted_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                """,
                (user_id, quiz_id, question_id, selected_option_id, is_correct)
            )
            if is_first_attempt:
                conn.execute(
                    """
                    INSERT INTO PracticeFirstAnswers
                        (user_id, quiz_id, question_id, selected_option_id, is_correct, submitted_at)
                    VALUES (?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (user_id, quiz_id, question_id, selected_option_id, is_correct)
                )

        score = round((correct_answers / total_questions) * 100) if total_questions else 0

        try:
            conn.execute(
                """
                INSERT INTO PracticeProgress
                    (user_id, quiz_id, score, correct_answers, total_questions, completed_at, started_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(user_id, quiz_id) DO UPDATE SET
                    score=excluded.score,
                    correct_answers=excluded.correct_answers,
                    total_questions=excluded.total_questions,
                    completed_at=excluded.completed_at
                """,
                (user_id, quiz_id, score, correct_answers, total_questions)
            )
        except sqlite3.OperationalError as e:
            # Some legacy DBs have a broken FK on PracticeProgress.
            # Keep quiz answers saved even if progress upsert fails.
            print(f"Warning: PracticeProgress upsert skipped: {e}")

        if is_first_attempt:
            conn.execute(
                """
                INSERT OR IGNORE INTO PracticeFirstAttempts
                    (user_id, quiz_id, score, correct_answers, total_questions, attempted_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                """,
                (user_id, quiz_id, score, correct_answers, total_questions)
            )

    if is_first_attempt:
        flash("Quiz submitted successfully")
    else:
        flash("Retake submitted. Teacher reports still show your first attempt.")
    return redirect(url_for('practice_quiz_results', quiz_id=quiz_id))


@app.route('/practice_quiz_results/<int:quiz_id>')
@login_required
def practice_quiz_results(quiz_id):
    if session.get('role') != 'Student':
        return redirect('/login')

    user_id = session['user_id']

    with get_db_connection() as conn:
        student_department_row = conn.execute(
            "SELECT COALESCE(NULLIF(department, ''), 'Computer') AS department FROM Users WHERE user_id=?",
            (user_id,)
        ).fetchone()
        student_department = student_department_row['department'] if student_department_row else 'Computer'

        quiz = conn.execute(
            """
            SELECT p.*
            FROM Practice_Quizzes p
            LEFT JOIN Users u ON p.created_by = u.user_id
            WHERE p.quiz_id=?
              AND (',' || COALESCE(
                    NULLIF(p.target_departments, ''),
                    COALESCE(NULLIF(p.department, ''), COALESCE(NULLIF(u.department, ''), 'Computer'))
                  ) || ',') LIKE '%,' || ? || ',%'
            """,
            (quiz_id, student_department)
        ).fetchone()
        if not quiz:
            flash("This quiz is not available for your department")
            return redirect(url_for('student_practice_quizzes'))

        progress = conn.execute(
            """
            SELECT score, correct_answers, total_questions, completed_at
            FROM PracticeProgress
            WHERE user_id=? AND quiz_id=?
            """,
            (user_id, quiz_id)
        ).fetchone()

        results = conn.execute(
            """
            SELECT
                q.question_id,
                q.question_text,
                q.explanation,
                pa.is_correct,
                so.option_text AS selected_option,
                co.option_text AS correct_option
            FROM PracticeQuestions q
            LEFT JOIN PracticeAnswers pa
                ON pa.question_id = q.question_id
                AND pa.user_id = ?
                AND pa.quiz_id = ?
            LEFT JOIN PracticeOptions so ON so.option_id = pa.selected_option_id
            LEFT JOIN PracticeOptions co
                ON co.question_id = q.question_id
                AND co.is_correct = 1
            WHERE q.quiz_id = ?
            ORDER BY q.question_id
            """,
            (user_id, quiz_id, quiz_id)
        ).fetchall()

    total_questions = len(results)
    answered_questions = sum(1 for r in results if r['selected_option'] is not None)
    correct_questions = sum(1 for r in results if r['is_correct'] == 1)
    incorrect_questions = max(0, answered_questions - correct_questions)
    unanswered_questions = max(0, total_questions - answered_questions)
    score_pct = round((correct_questions / total_questions) * 100, 1) if total_questions else 0
    quiz_stats = {
        "total": total_questions,
        "answered": answered_questions,
        "correct": correct_questions,
        "incorrect": incorrect_questions,
        "unanswered": unanswered_questions,
        "score_pct": score_pct
    }

    if not progress:
        total = len(results)
        correct = sum(1 for r in results if r['is_correct'] == 1)
        score = round((correct / total) * 100) if total else 0
        progress = {
            'score': score,
            'correct_answers': correct,
            'total_questions': total,
            'completed_at': None
        }

    return render_template(
        'student/practice_quiz_results.html',
        quiz=quiz,
        progress=progress,
        results=results,
        quiz_stats=quiz_stats
    )

@app.route('/student_study_tools')
@login_required
def student_study_tools():
    if not _require_student():
        return redirect('/login')

    user_id = session['user_id']
    with get_db_connection() as conn:
        notes = conn.execute(
            "SELECT * FROM StudyNotes WHERE user_id=? ORDER BY updated_at DESC, note_id DESC",
            (user_id,)
        ).fetchall()
        flashcards = conn.execute(
            "SELECT * FROM Flashcards WHERE user_id=? ORDER BY card_id DESC",
            (user_id,)
        ).fetchall()
        goals = conn.execute(
            "SELECT * FROM DailyGoals WHERE user_id=? ORDER BY is_completed ASC, target_date ASC, goal_id DESC",
            (user_id,)
        ).fetchall()
        journal_logs = conn.execute(
            "SELECT * FROM StudyJournal WHERE user_id=? ORDER BY study_date DESC, journal_id DESC",
            (user_id,)
        ).fetchall()
        resources = conn.execute(
            "SELECT * FROM ResourceLibrary WHERE user_id=? ORDER BY resource_id DESC",
            (user_id,)
        ).fetchall()
        reminders = conn.execute(
            "SELECT * FROM StudyReminders WHERE user_id=? ORDER BY is_done ASC, due_date ASC, reminder_id DESC",
            (user_id,)
        ).fetchall()
        mind_maps = conn.execute(
            "SELECT * FROM MindMaps WHERE user_id=? ORDER BY updated_at DESC, map_id DESC",
            (user_id,)
        ).fetchall()
        assessments = conn.execute(
            "SELECT * FROM SelfAssessment WHERE user_id=? ORDER BY updated_at DESC, assessment_id DESC",
            (user_id,)
        ).fetchall()
        pomodoro_logs = conn.execute(
            "SELECT * FROM PomodoroLogs WHERE user_id=? ORDER BY log_id DESC LIMIT 20",
            (user_id,)
        ).fetchall()

    return render_template(
        'student/study_tools.html',
        notes=notes,
        flashcards=flashcards,
        goals=goals,
        journal_logs=journal_logs,
        resources=resources,
        reminders=reminders,
        mind_maps=mind_maps,
        assessments=assessments,
        pomodoro_logs=pomodoro_logs
    )

@app.route('/study/notes/add', methods=['POST'])
@login_required
def add_study_note():
    if not _require_student():
        return redirect('/login')
    title = (request.form.get('title') or '').strip()
    content = (request.form.get('content') or '').strip()
    if not title or not content:
        flash("Note title and content are required.")
        return redirect(url_for('student_study_tools'))
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO StudyNotes (user_id, title, content, created_at, updated_at)
            VALUES (?, ?, ?, datetime('now'), datetime('now'))
            """,
            (session['user_id'], title, content)
        )
    flash("Note saved.")
    return redirect(url_for('student_study_tools'))

@app.route('/study/notes/delete/<int:note_id>', methods=['POST'])
@login_required
def delete_study_note(note_id):
    if not _require_student():
        return redirect('/login')
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM StudyNotes WHERE note_id=? AND user_id=?",
            (note_id, session['user_id'])
        )
    flash("Note deleted.")
    return redirect(url_for('student_study_tools'))

@app.route('/study/flashcards/add', methods=['POST'])
@login_required
def add_flashcard():
    if not _require_student():
        return redirect('/login')
    front_text = (request.form.get('front_text') or '').strip()
    back_text = (request.form.get('back_text') or '').strip()
    if not front_text or not back_text:
        flash("Flashcard front and back are required.")
        return redirect(url_for('student_study_tools'))
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO Flashcards (user_id, front_text, back_text) VALUES (?, ?, ?)",
            (session['user_id'], front_text, back_text)
        )
    flash("Flashcard added.")
    return redirect(url_for('student_study_tools'))

@app.route('/study/flashcards/delete/<int:card_id>', methods=['POST'])
@login_required
def delete_flashcard(card_id):
    if not _require_student():
        return redirect('/login')
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM Flashcards WHERE card_id=? AND user_id=?",
            (card_id, session['user_id'])
        )
    flash("Flashcard deleted.")
    return redirect(url_for('student_study_tools'))

@app.route('/study/goals/add', methods=['POST'])
@login_required
def add_daily_goal():
    if not _require_student():
        return redirect('/login')
    goal_text = (request.form.get('goal_text') or '').strip()
    target_date = (request.form.get('target_date') or '').strip() or None
    if not goal_text:
        flash("Goal text is required.")
        return redirect(url_for('student_study_tools'))
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO DailyGoals (user_id, goal_text, target_date) VALUES (?, ?, ?)",
            (session['user_id'], goal_text, target_date)
        )
    flash("Goal added.")
    return redirect(url_for('student_study_tools'))

@app.route('/study/goals/toggle/<int:goal_id>', methods=['POST'])
@login_required
def toggle_daily_goal(goal_id):
    if not _require_student():
        return redirect('/login')
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT is_completed FROM DailyGoals WHERE goal_id=? AND user_id=?",
            (goal_id, session['user_id'])
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE DailyGoals SET is_completed=? WHERE goal_id=? AND user_id=?",
                (0 if row['is_completed'] else 1, goal_id, session['user_id'])
            )
    return redirect(url_for('student_study_tools'))

@app.route('/study/goals/delete/<int:goal_id>', methods=['POST'])
@login_required
def delete_daily_goal(goal_id):
    if not _require_student():
        return redirect('/login')
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM DailyGoals WHERE goal_id=? AND user_id=?",
            (goal_id, session['user_id'])
        )
    return redirect(url_for('student_study_tools'))

@app.route('/study/journal/add', methods=['POST'])
@login_required
def add_study_journal():
    if not _require_student():
        return redirect('/login')
    study_date = (request.form.get('study_date') or datetime.date.today().isoformat()).strip()
    topics = (request.form.get('topics') or '').strip()
    notes = (request.form.get('notes') or '').strip()
    try:
        minutes_spent = int(request.form.get('minutes_spent') or 0)
    except ValueError:
        minutes_spent = 0
    if not topics:
        flash("Topics are required for journal log.")
        return redirect(url_for('student_study_tools'))
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO StudyJournal (user_id, study_date, minutes_spent, topics, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session['user_id'], study_date, max(0, minutes_spent), topics, notes)
        )
    flash("Journal entry added.")
    return redirect(url_for('student_study_tools'))

@app.route('/study/journal/delete/<int:journal_id>', methods=['POST'])
@login_required
def delete_study_journal(journal_id):
    if not _require_student():
        return redirect('/login')
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM StudyJournal WHERE journal_id=? AND user_id=?",
            (journal_id, session['user_id'])
        )
    return redirect(url_for('student_study_tools'))

@app.route('/study/resources/add', methods=['POST'])
@login_required
def add_resource():
    if not _require_student():
        return redirect('/login')
    title = (request.form.get('title') or '').strip()
    resource_type = (request.form.get('resource_type') or 'link').strip().lower()
    url = (request.form.get('url') or '').strip()
    description = (request.form.get('description') or '').strip()
    if resource_type not in ('link', 'pdf', 'reference'):
        resource_type = 'link'
    if not title or not url:
        flash("Resource title and URL are required.")
        return redirect(url_for('student_study_tools'))
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO ResourceLibrary (user_id, title, resource_type, url, description)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session['user_id'], title, resource_type, url, description)
        )
    flash("Resource added.")
    return redirect(url_for('student_study_tools'))

@app.route('/study/resources/delete/<int:resource_id>', methods=['POST'])
@login_required
def delete_resource(resource_id):
    if not _require_student():
        return redirect('/login')
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM ResourceLibrary WHERE resource_id=? AND user_id=?",
            (resource_id, session['user_id'])
        )
    return redirect(url_for('student_study_tools'))

@app.route('/study/reminders/add', methods=['POST'])
@login_required
def add_reminder():
    if not _require_student():
        return redirect('/login')
    title = (request.form.get('title') or '').strip()
    due_date = (request.form.get('due_date') or '').strip() or None
    if not title:
        flash("Reminder title is required.")
        return redirect(url_for('student_study_tools'))
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO StudyReminders (user_id, title, due_date) VALUES (?, ?, ?)",
            (session['user_id'], title, due_date)
        )
    flash("Reminder added.")
    return redirect(url_for('student_study_tools'))

@app.route('/study/reminders/toggle/<int:reminder_id>', methods=['POST'])
@login_required
def toggle_reminder(reminder_id):
    if not _require_student():
        return redirect('/login')
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT is_done FROM StudyReminders WHERE reminder_id=? AND user_id=?",
            (reminder_id, session['user_id'])
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE StudyReminders SET is_done=? WHERE reminder_id=? AND user_id=?",
                (0 if row['is_done'] else 1, reminder_id, session['user_id'])
            )
    return redirect(url_for('student_study_tools'))

@app.route('/study/reminders/delete/<int:reminder_id>', methods=['POST'])
@login_required
def delete_reminder(reminder_id):
    if not _require_student():
        return redirect('/login')
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM StudyReminders WHERE reminder_id=? AND user_id=?",
            (reminder_id, session['user_id'])
        )
    return redirect(url_for('student_study_tools'))

@app.route('/study/mindmaps/add', methods=['POST'])
@login_required
def add_mind_map():
    if not _require_student():
        return redirect('/login')
    title = (request.form.get('title') or '').strip()
    central_topic = (request.form.get('central_topic') or '').strip()
    related_topics = (request.form.get('related_topics') or '').strip()
    if not title or not central_topic:
        flash("Mind map title and central topic are required.")
        return redirect(url_for('student_study_tools'))
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO MindMaps (user_id, title, central_topic, related_topics, created_at, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (session['user_id'], title, central_topic, related_topics)
        )
    flash("Mind map saved.")
    return redirect(url_for('student_study_tools'))

@app.route('/study/mindmaps/delete/<int:map_id>', methods=['POST'])
@login_required
def delete_mind_map(map_id):
    if not _require_student():
        return redirect('/login')
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM MindMaps WHERE map_id=? AND user_id=?",
            (map_id, session['user_id'])
        )
    return redirect(url_for('student_study_tools'))

@app.route('/study/assessment/add', methods=['POST'])
@login_required
def add_assessment_item():
    if not _require_student():
        return redirect('/login')
    topic_name = (request.form.get('topic_name') or '').strip()
    status = (request.form.get('status') or 'learning').strip().lower()
    notes = (request.form.get('notes') or '').strip()
    if status not in ('strong', 'learning', 'revise'):
        status = 'learning'
    if not topic_name:
        flash("Topic name is required.")
        return redirect(url_for('student_study_tools'))
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO SelfAssessment (user_id, topic_name, status, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (session['user_id'], topic_name, status, notes)
        )
    flash("Checklist item added.")
    return redirect(url_for('student_study_tools'))

@app.route('/study/assessment/delete/<int:assessment_id>', methods=['POST'])
@login_required
def delete_assessment_item(assessment_id):
    if not _require_student():
        return redirect('/login')
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM SelfAssessment WHERE assessment_id=? AND user_id=?",
            (assessment_id, session['user_id'])
        )
    return redirect(url_for('student_study_tools'))

@app.route('/study/pomodoro/log', methods=['POST'])
@login_required
def log_pomodoro():
    if not _require_student():
        return redirect('/login')
    try:
        focus_minutes = int(request.form.get('focus_minutes') or 25)
    except ValueError:
        focus_minutes = 25
    try:
        break_minutes = int(request.form.get('break_minutes') or 5)
    except ValueError:
        break_minutes = 5
    try:
        cycles_completed = int(request.form.get('cycles_completed') or 1)
    except ValueError:
        cycles_completed = 1

    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO PomodoroLogs (user_id, focus_minutes, break_minutes, cycles_completed)
            VALUES (?, ?, ?, ?)
            """,
            (session['user_id'], max(1, focus_minutes), max(1, break_minutes), max(1, cycles_completed))
        )
    flash("Pomodoro session logged.")
    return redirect(url_for('student_study_tools'))

@app.route('/student_dashboard')
def student_dashboard():

    # Allow live-quiz students (nickname-only session) to see the dashboard.
    if not ((session.get('role') == 'Student' and 'user_id' in session) or session.get('student_nickname')):
        return redirect('/login')

    # ❌ NO quiz fetching here
    return render_template('student/dashboard.html')

@app.route('/settings')
@login_required
def settings():
    user_id = session['user_id']
    with get_db_connection() as conn:
        user = conn.execute(
            """
            SELECT username, email, role,
                   profile_pic,
                   COALESCE(theme_mode, 'light') AS theme_mode,
                   COALESCE(font_scale, 'medium') AS font_scale,
                   COALESCE(app_language, 'en') AS app_language,
                   COALESCE(email_alerts, 1) AS email_alerts,
                   COALESCE(mute_notifications, 0) AS mute_notifications
            FROM Users
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

    if not user:
        session.clear()
        flash("Account not found. Please login again.")
        return redirect(url_for('login'))

    profile_pic = None
    if user:
        try:
            profile_pic = user["profile_pic"]
        except Exception:
            profile_pic = None
    avatar_url = _avatar_url_from_profile_pic(profile_pic)
    return render_template('shared/settings.html', user=user, avatar_url=avatar_url)

@app.route('/avatar_builder')
@login_required
def avatar_builder():
    return render_template('shared/avatar.html')

@app.route('/save_avatar', methods=['POST'])
@login_required
def save_avatar():
    user_id = session['user_id']
    profile_path = None
    if request.is_json:
        data = request.get_json(silent=True) or {}
        svg = (data.get("svg") or "").strip()
        image_data = (data.get("image") or "").strip()
        if svg:
            profile_path = _save_avatar_svg(svg)
        elif image_data:
            profile_path = _save_avatar_data_url(image_data)
    if not profile_path:
        avatar_data = (request.form.get("avatar_data") or "").strip()
        if avatar_data.startswith("data:image/"):
            profile_path = _save_avatar_data_url(avatar_data)
    if not profile_path:
        return jsonify({"success": False, "message": "No avatar data provided"}), 400

    with get_db_connection() as conn:
        conn.execute("UPDATE Users SET profile_pic=? WHERE user_id=?", (profile_path, user_id))

    return jsonify({"success": True, "avatar": profile_path})

@app.route('/settings/avatar/upload', methods=['POST'])
@login_required
def upload_avatar():
    user_id = session['user_id']
    upload = request.files.get("avatar_file")
    try:
        profile_path = _save_avatar_upload(upload)
    except ValueError as exc:
        flash(str(exc))
        return redirect(url_for('settings'))
    if not profile_path:
        flash("Please choose an image file.")
        return redirect(url_for('settings'))

    with get_db_connection() as conn:
        conn.execute("UPDATE Users SET profile_pic=? WHERE user_id=?", (profile_path, user_id))
    flash("Avatar updated.")
    return redirect(url_for('settings'))

@app.route('/settings/avatar/clear', methods=['POST'])
@login_required
def clear_avatar():
    user_id = session['user_id']
    with get_db_connection() as conn:
        conn.execute("UPDATE Users SET profile_pic=NULL WHERE user_id=?", (user_id,))
    flash("Avatar removed.")
    return redirect(url_for('settings'))


@app.route('/db_info')
@login_required
def db_info():
    # Quick diagnostic: this app uses sqlite3 connections for app data.
    db_url = os.environ.get('DATABASE_URL') or ''
    info = {
        "storage_engine": "postgresql" if USE_POSTGRES else "sqlite3",
        "sqlite_path": None if USE_POSTGRES else DATABASE,
        "database_url_set": bool(db_url),
        "sqlalchemy_uri": app.config.get('SQLALCHEMY_DATABASE_URI')
    }
    return jsonify(info)

@app.route('/settings/profile', methods=['POST'])
@login_required
def update_profile():
    user_id = session['user_id']
    username = (request.form.get('username') or '').strip()
    email = (request.form.get('email') or '').strip().lower()

    if not username:
        flash("Username is required.")
        return redirect(url_for('settings'))
    if not _valid_email(email):
        flash("Please enter a valid email address.")
        return redirect(url_for('settings'))

    with get_db_connection() as conn:
        existing = conn.execute(
            "SELECT user_id FROM Users WHERE email=? AND user_id<>?",
            (email, user_id)
        ).fetchone()
        if existing:
            flash("Email is already used by another account.")
            return redirect(url_for('settings'))

        conn.execute(
            "UPDATE Users SET username=?, email=? WHERE user_id=?",
            (username, email, user_id)
        )

    session['username'] = username
    flash("Profile updated successfully.")
    return redirect(url_for('settings'))

@app.route('/settings/password', methods=['POST'])
@login_required
def update_password():
    user_id = session['user_id']
    current_password = request.form.get('current_password') or ''
    new_password = request.form.get('new_password') or ''
    confirm_password = request.form.get('confirm_password') or ''

    if not current_password or not new_password or not confirm_password:
        flash("All password fields are required.")
        return redirect(url_for('settings'))
    if new_password != confirm_password:
        flash("New password and confirm password do not match.")
        return redirect(url_for('settings'))
    if not is_password_strong(new_password):
        flash("Weak password. Use at least 8 chars with letters, numbers, and a symbol.")
        return redirect(url_for('settings'))

    with get_db_connection() as conn:
        user = conn.execute("SELECT password FROM Users WHERE user_id=?", (user_id,)).fetchone()
        if not user or not check_password_hash(user['password'], current_password):
            flash("Current password is incorrect.")
            return redirect(url_for('settings'))

        conn.execute(
            "UPDATE Users SET password=? WHERE user_id=?",
            (generate_password_hash(new_password), user_id)
        )

    flash("Password updated successfully.")
    return redirect(url_for('settings'))

@app.route('/settings/appearance', methods=['POST'])
@login_required
def update_appearance():
    user_id = session['user_id']
    theme_mode = (request.form.get('theme_mode') or 'light').strip().lower()
    font_scale = (request.form.get('font_scale') or 'medium').strip().lower()
    app_language = (request.form.get('app_language') or 'en').strip().lower()

    if theme_mode not in ('light', 'dark'):
        theme_mode = 'light'
    if font_scale not in ('small', 'medium', 'large'):
        font_scale = 'medium'
    if app_language not in ('en', 'mr'):
        app_language = 'en'

    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE Users
            SET theme_mode=?, font_scale=?, app_language=?
            WHERE user_id=?
            """,
            (theme_mode, font_scale, app_language, user_id)
        )

    session['theme_mode'] = theme_mode
    session['font_scale'] = font_scale
    session['app_language'] = app_language
    session['tip_language'] = app_language

    flash("Appearance and language settings saved.")
    return redirect(url_for('settings'))

@app.route('/settings/notifications', methods=['POST'])
@login_required
def update_notifications():
    user_id = session['user_id']
    email_alerts = _to_int_flag(request.form.get('email_alerts'))
    mute_notifications = _to_int_flag(request.form.get('mute_notifications'))

    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE Users
            SET email_alerts=?, mute_notifications=?
            WHERE user_id=?
            """,
            (email_alerts, mute_notifications, user_id)
        )

    session['email_alerts'] = email_alerts
    session['mute_notifications'] = mute_notifications
    flash("Notification settings updated.")
    return redirect(url_for('settings'))

@app.route('/settings/logout_all_devices', methods=['POST'])
@login_required
def logout_all_devices():
    user_id = session['user_id']
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE Users SET session_version = COALESCE(session_version, 0) + 1 WHERE user_id=?",
            (user_id,)
        )

    session.clear()
    flash("Logged out from all devices.")
    return redirect(url_for('login'))

@app.route('/settings/delete_account', methods=['POST'])
@login_required
def delete_account():
    user_id = session['user_id']
    current_password = request.form.get('current_password') or ''

    if not current_password:
        flash("Please enter your current password to delete account.")
        return redirect(url_for('settings'))

    try:
        with get_db_connection() as conn:
            user = conn.execute("SELECT password FROM Users WHERE user_id=?", (user_id,)).fetchone()
            if not user or not check_password_hash(user['password'], current_password):
                flash("Current password is incorrect.")
                return redirect(url_for('settings'))

            _delete_user_account(conn, user_id)
    except Exception as e:
        print("Delete account error:", e)
        flash("Could not delete account right now. Please try again.")
        return redirect(url_for('settings'))

    session.clear()
    flash("Account deleted successfully.")
    return redirect(url_for('login'))

@app.route('/daily_learning')
@login_required
def daily_learning():
    return render_template('shared/daily_learning.html')

@app.route('/api/daily_tip')
@login_required
def api_daily_tip():
    user_id = session['user_id']
    role = session.get('role', 'Student')
    requested_lang = (request.args.get('lang') or session.get('app_language') or session.get('tip_language') or 'en').strip().lower()
    if requested_lang not in ('en', 'hi', 'mr'):
        requested_lang = 'en'
    session['tip_language'] = requested_lang

    with get_db_connection() as conn:
        tip, inferred_subject, inferred_difficulty, final_lang = get_daily_tip_for_user(
            conn, user_id, role, requested_lang
        )
        if not tip:
            return jsonify({"ok": False, "message": "No tip available"}), 404

        today = datetime.date.today().isoformat()
        existing_view = conn.execute("""
            SELECT view_id, reward_points
            FROM UserTipViews
            WHERE user_id=? AND viewed_on=?
        """, (user_id, today)).fetchone()

        just_recorded = False
        if not existing_view:
            conn.execute("""
                INSERT OR IGNORE INTO UserTipViews (user_id, tip_id, viewed_on, reward_points)
                VALUES (?, ?, ?, 0)
            """, (user_id, tip["tip_id"], today))
            just_recorded = True

        streak = _calculate_tip_streak(conn, user_id)
        reward_today = 0
        if just_recorded and streak > 0 and streak % 7 == 0:
            reward_today = 25
            conn.execute("""
                UPDATE UserTipViews
                SET reward_points=?
                WHERE user_id=? AND viewed_on=?
            """, (reward_today, user_id, today))
        elif existing_view:
            reward_today = existing_view["reward_points"] or 0

        total_reward_points = conn.execute("""
            SELECT COALESCE(SUM(reward_points), 0) AS total_points
            FROM UserTipViews
            WHERE user_id=?
        """, (user_id,)).fetchone()["total_points"]

    share_text = f"Daily {tip['content_type'].title()}: {tip['content_text']}"
    share_url = "https://twitter.com/intent/tweet?text=" + urllib.parse.quote(share_text)

    return jsonify({
        "ok": True,
        "tip": {
            "id": tip["tip_id"],
            "text": tip["content_text"],
            "type": tip["content_type"],
            "subject": tip["subject"],
            "difficulty": tip["difficulty_level"],
            "language": tip["language"]
        },
        "meta": {
            "inferred_subject": inferred_subject,
            "adaptive_difficulty": inferred_difficulty,
            "language": final_lang
        },
        "gamification": {
            "streak": streak,
            "reward_today": reward_today,
            "total_reward_points": total_reward_points
        },
        "share": {
            "text": share_text,
            "twitter_intent_url": share_url
        }
    })

@app.route('/join_quiz', methods=['GET', 'POST'])
def join_quiz():
    if request.method == 'POST':
        pin_raw = (request.form.get('pin') or '').strip()
        nickname = (request.form.get('nickname') or '').strip()

        if not pin_raw or not nickname:
            flash("PIN and Nickname are required")
            return redirect('/join_quiz')

        if not pin_raw.isdigit():
            flash("PIN must be numbers only")
            return redirect('/join_quiz')

        try:
            pin = int(pin_raw)
        except ValueError:
            flash("Invalid PIN format")
            return redirect('/join_quiz')

        conn = get_db_connection()

        # Check if PIN exists
        session_data = conn.execute(
            "SELECT session_id, quiz_id FROM live_sessions WHERE pin = ? AND is_active = 1",
            (pin,)
        ).fetchone()

        if not session_data:
            flash("Invalid PIN")
            conn.close()
            return redirect('/join_quiz')

        session_id = session_data['session_id']
        quiz_id = session_data['quiz_id']

        nickname_exists = conn.execute(
            "SELECT 1 FROM participants WHERE session_id=? AND nickname=? LIMIT 1",
            (session_id, nickname)
        ).fetchone()
        if nickname_exists:
            flash("Nickname already taken in this quiz. Please choose another nickname.")
            conn.close()
            return redirect('/join_quiz')

        # Insert student into participants table
        try:
            user_id = session.get('user_id')
            conn.execute(
                "INSERT INTO participants (session_id, nickname, user_id) VALUES (?, ?, ?)",
                (session_id, nickname, user_id)
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            flash("Nickname already taken in this live quiz.")
            return redirect('/join_quiz')
        conn.close()

        # Store student info in session for quiz page
        session['student_nickname'] = nickname
        session['session_id'] = session_id
        session['quiz_id'] = quiz_id

        # Redirect to student quiz page
        return redirect(url_for('student_waiting', session_id=session_id, player=nickname))

    return render_template('student/join_quiz.html')




    return render_template('student/live_quiz.html', quiz_questions=quiz_questions, session_id=session_id)
@app.route('/host_lobby/<int:session_id>')
def host_lobby(session_id):
    if 'role' not in session or session['role'] != 'Teacher':
        flash("Please login as teacher")
        return redirect('/login')

    conn = get_db_connection()

    # Get PIN for the session
    session_data = conn.execute(
        "SELECT pin FROM live_sessions WHERE session_id = ?", 
        (session_id,)
    ).fetchone()
    if not session_data:
        flash("Session not found")
        return redirect('/teacher_dashboard')
    pin = session_data['pin']

    # Get students who joined (empty initially)
    students = conn.execute(
        "SELECT nickname FROM participants WHERE session_id = ?", 
        (session_id,)
    ).fetchall()

    conn.close()

    return render_template('teacher/waiting_room.html', pin=pin, students=students, session_id=session_id)

@app.route('/student_waiting/<int:session_id>')
def student_waiting(session_id):
    player_name = (request.args.get('player') or session.get('student_nickname') or '').strip()
    if not player_name:
        flash("Please enter PIN and nickname first")
        return redirect('/join_quiz')

    conn = get_db_connection()
    
    # Verify session exists
    session_data = conn.execute(
        "SELECT ls.pin, q.quiz_name FROM live_sessions ls "
        "JOIN Quizzes q ON ls.quiz_id = q.quiz_id "
        "WHERE ls.session_id = ? AND ls.is_active = 1",
        (session_id,)
    ).fetchone()
    conn.close()

    if not session_data:
        flash("Invalid or inactive session")
        return redirect('/join_quiz')
    return render_template(
        'student/student_waiting.html',
        session=session,
        player_name=player_name,
        session_id=session_id,
        pin=session_data['pin'],
        quiz_name=session_data['quiz_name']
    )


# Waiting room page
@app.route('/waiting_room/<int:session_id>')
def waiting_room(session_id):
    if 'role' not in session or session['role'] != 'Teacher':
        flash("Please login as teacher")
        return redirect('/login')

    conn = get_db_connection()
    session_data = conn.execute(
        "SELECT ls.pin, q.quiz_name FROM live_sessions ls "
        "JOIN Quizzes q ON ls.quiz_id = q.quiz_id "
        "WHERE ls.session_id = ?", (session_id,)
    ).fetchone()

    students = conn.execute(
        "SELECT nickname FROM participants WHERE session_id = ?", (session_id,)
    ).fetchall()
    conn.close()

    return render_template(
        'teacher/waiting_room.html',
        session_id=session_id,
        pin=session_data['pin'],
        quiz_name=session_data['quiz_name'],
        students=students
    )


# API to fetch joined students
@app.route('/get_students/<int:session_id>')
def get_students(session_id):
    conn = get_db_connection()
    students = conn.execute(
        "SELECT nickname FROM participants WHERE session_id = ?", (session_id,)
    ).fetchall()
    conn.close()
    return {"students": [dict(s) for s in students]}




@app.route('/leave_quiz', methods=['POST'])
def leave_quiz():
    if 'student_nickname' not in session or 'session_id' not in session:
        flash("You are not in any quiz")
        return redirect(url_for('student_dashboard'))  # <-- use url_for

    nickname = session['student_nickname']
    session_id = session['session_id']

    conn = get_db_connection()
    conn.execute(
        "DELETE FROM participants WHERE session_id = ? AND nickname = ?",
        (session_id, nickname)
    )
    conn.commit()
    conn.close()

    # Clear student session
    session.pop('student_nickname', None)
    session.pop('session_id', None)

    flash("You have left the quiz")
    return redirect(url_for('student_dashboard'))  # <-- use url_for



# ---------------- LOGOUT ----------------
@app.route('/logout')
def logout():
    session.clear()
    flash("Logged out successfully")
    return redirect('/login')






@app.route('/teacher_live_quiz/<int:session_id>')
def teacher_live_quiz(session_id):
    if 'role' not in session or session['role'] != 'Teacher':
        flash("Please login as teacher")
        return redirect('/login')

    conn = get_db_connection()
    session_data = conn.execute(
        "SELECT * FROM live_sessions WHERE session_id=?",
        (session_id,)
    ).fetchone()

    if not session_data:
        conn.close()
        flash("Session not found")
        return redirect('/teacher_dashboard')

    live_state = _get_live_question_state(conn, session_data)
    try:
        live_state["scoreboard_released"] = bool(session_data["scoreboard_released"])
    except Exception:
        live_state["scoreboard_released"] = False
    try:
        live_state["scoreboard_released"] = bool(session_data["scoreboard_released"])
    except Exception:
        live_state["scoreboard_released"] = False
    total_questions_row = conn.execute(
        "SELECT COUNT(*) AS total FROM questions WHERE quiz_id=?",
        (session_data["quiz_id"],)
    ).fetchone()
    total_questions = total_questions_row["total"] if total_questions_row else 0
    has_next = (session_data["current_question"] + 1) < total_questions

    options = []
    answer_stats = {}
    question_ranking = {"top3": [], "rows": []}
    has_active_question = live_state.get("started") and not live_state.get("finished")
    if has_active_question:
        options = conn.execute(
            "SELECT option_text, is_correct FROM options WHERE question_id=? ORDER BY option_id",
            (live_state["question_id"],)
        ).fetchall()
        answer_stats = _get_answer_breakdown(conn, session_id, live_state["question_id"])
        question_ranking = _get_question_ranking(conn, session_id, live_state["question_id"])

    conn.close()

    return render_template(
        "teacher/live_question.html",
        session_id=session_id,
        question=live_state if has_active_question else None,
        options=options,
        answer_stats=answer_stats,
        question_ranking=question_ranking,
        has_next=has_next,
        total_questions=total_questions
    )





@app.route('/next_question/<int:session_id>')
def next_question(session_id):
    if 'role' not in session or session['role'] != 'Teacher':
        flash("Please login as teacher")
        return redirect('/login')

    conn = get_db_connection()
    session_data = conn.execute(
        "SELECT * FROM live_sessions WHERE session_id=?",
        (session_id,)
    ).fetchone()
    if not session_data:
        conn.close()
        flash("Session not found")
        return redirect('/teacher_dashboard')

    live_state = _get_live_question_state(conn, session_data)
    if (
        live_state.get("started")
        and not live_state.get("finished")
        and live_state.get("phase") == "question"
        and (live_state.get("time_left") or 0) > 0
    ):
        # Prevent skipping reveal; students should see right/wrong after timer ends.
        flash("Please wait for the question timer to finish.")
        conn.close()
        return redirect(url_for('teacher_live_quiz', session_id=session_id))

    total_row = conn.execute(
        "SELECT COUNT(*) AS total FROM questions WHERE quiz_id=?",
        (session_data["quiz_id"],)
    ).fetchone()
    total_questions = total_row["total"] if total_row else 0
    next_index = (session_data["current_question"] or 0) + 1

    if next_index >= total_questions:
        conn.execute(
            "UPDATE live_sessions SET current_question=?, is_active=0 WHERE session_id=?",
            (next_index, session_id)
        )
        conn.commit()
        conn.close()
        return redirect(url_for('final_podium', session_id=session_id))

    conn.execute("""
        UPDATE live_sessions
        SET current_question = ?,
            question_started_at = datetime('now'),
            scoreboard_released = 0
        WHERE session_id=?
    """, (next_index, session_id))

    conn.commit()
    conn.close()

    return redirect(url_for('teacher_live_quiz', session_id=session_id))


@app.route('/student_live_quiz/<int:session_id>')
def student_live_quiz(session_id):
    player_name = (request.args.get('player') or session.get('student_nickname') or '').strip()
    if not player_name:
        return redirect('/join_quiz')

    return render_template(
        'student/live_question.html',
        session_id=session_id,
        player_name=player_name
    )


@app.route('/student_scoreboard/<int:session_id>')
def student_scoreboard(session_id):
    player_name = (request.args.get('player') or session.get('student_nickname') or '').strip()
    if not player_name:
        return redirect('/join_quiz')

    with get_db_connection() as conn:
        session_data = conn.execute(
            "SELECT * FROM live_sessions WHERE session_id=?",
            (session_id,)
        ).fetchone()
        if not session_data:
            flash("Session not found")
            return redirect('/join_quiz')
        try:
            scoreboard_released = (session_data["scoreboard_released"] == 1)
        except Exception:
            scoreboard_released = False
        if not scoreboard_released:
            return redirect(url_for('student_live_quiz', session_id=session_id, player=player_name))

        quiz_row = conn.execute(
            "SELECT quiz_name FROM quizzes WHERE quiz_id=?",
            (session_data["quiz_id"],)
        ).fetchone()
        total_row = conn.execute(
            "SELECT COUNT(*) AS total FROM questions WHERE quiz_id=?",
            (session_data["quiz_id"],)
        ).fetchone()
        scores = _get_live_leaderboard_rows(conn, session_id)
        resolve_avatar = _get_live_avatar_map(conn, session_id)
        scores = [
            {**row, "avatar": resolve_avatar(row["player_name"])}
            for row in scores
        ]
        resolve_avatar = _get_live_avatar_map(conn, session_id)
        scores = [
            {**row, "avatar": resolve_avatar(row["player_name"])}
            for row in scores
        ]

    total_questions = total_row["total"] if total_row else 0
    current_display = min((session_data["current_question"] or 0) + 1, total_questions if total_questions > 0 else 1)
    return render_template(
        'student/live_scoreboard.html',
        session_id=session_id,
        scores=scores,
        quiz_name=quiz_row["quiz_name"] if quiz_row else "Live Quiz",
        current_question=current_display,
        total_questions=total_questions,
        player_name=player_name
    )


@app.route('/start_live_quiz/<int:session_id>', methods=['POST'])
def start_live_quiz(session_id):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM player_answers WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM PlayerScores WHERE session_id=?", (session_id,))
        conn.execute("""
            UPDATE live_sessions
            SET started = 1,
                current_question = 0,
                start_time = datetime('now'),
                question_started_at = datetime('now'),
                is_active = 1,
                final_released = 0,
                scoreboard_released = 0
            WHERE session_id = ?
        """, (session_id,))
    return jsonify({"success": True})




@app.route('/submit_answer', methods=['POST'])
def submit_answer():
    data = request.json
    session_id = data.get('session_id')
    player_name = (data.get('player_name') or '').strip() or session.get('student_nickname')
    answer = data.get('answer')
    option_id = data.get('option_id')

    if not session_id or not player_name or answer is None:
        return jsonify({"success": False, "message": "Invalid submission"}), 400

    conn = get_db_connection()

    participant_exists = conn.execute(
        "SELECT 1 FROM participants WHERE session_id=? AND nickname=? LIMIT 1",
        (session_id, player_name)
    ).fetchone()
    if not participant_exists:
        conn.close()
        return jsonify({"success": False, "message": "Participant not found in session"}), 403

    # current question
    session_data = conn.execute(
        "SELECT * FROM live_sessions WHERE session_id=?",
        (session_id,)
    ).fetchone()
    if not session_data:
        conn.close()
        return jsonify({"success": False, "message": "Session not found"}), 404

    live_state = _get_live_question_state(conn, session_data)
    if live_state.get("finished") or not live_state.get("started"):
        conn.close()
        return jsonify({"success": False, "message": "Question not active"}), 400

    question_id = live_state["question_id"]
    question_index = live_state["question_index"]
    time_limit = live_state["time_limit"]
    question_started_at = _parse_db_datetime(session_data["question_started_at"]) or datetime.datetime.utcnow()
    # Align scoring window with client timing: exclude intro countdown.
    intro_seconds = 5
    elapsed_seconds = max(0, (datetime.datetime.utcnow() - question_started_at).total_seconds())
    active_elapsed = max(0, elapsed_seconds - intro_seconds)
    response_ms = int(active_elapsed * 1000)

    existing = conn.execute(
        """
        SELECT is_correct, score_awarded, response_ms
        FROM player_answers
        WHERE session_id=? AND question_id=? AND player_name=?
        LIMIT 1
        """,
        (session_id, question_id, player_name)
    ).fetchone()
    if existing:
        rank_details = _get_player_live_rank_details(conn, session_id, player_name)
        conn.close()
        return jsonify({
            "success": True,
            "already_submitted": True,
            "correct": bool(existing["is_correct"]),
            "score": existing["score_awarded"],
            "response_ms": existing["response_ms"],
            "question_id": question_id,
            "rank": rank_details["rank"],
            "total_players": rank_details["total_players"],
            "total_score": rank_details["score"],
            "points_to_next": rank_details["points_to_next"],
            "next_player": rank_details["next_player"]
        })

    normalized_answer = (answer or "").strip()
    raw_correct = 0
    if option_id:
        option_row = conn.execute(
            "SELECT is_correct FROM options WHERE question_id=? AND option_id=?",
            (question_id, option_id)
        ).fetchone()
        raw_correct = 1 if option_row and option_row["is_correct"] == 1 else 0
    else:
        option_row = conn.execute(
            "SELECT MAX(is_correct) AS is_correct FROM options WHERE question_id=? AND TRIM(option_text)=?",
            (question_id, normalized_answer)
        ).fetchone()
        raw_correct = 1 if option_row and option_row["is_correct"] == 1 else 0
    in_time = response_ms <= (time_limit * 1000)
    is_correct = 1 if (raw_correct and in_time) else 0

    if is_correct:
        remaining_ms = max(0, (time_limit * 1000) - response_ms)
        score = int(round(1000 * (remaining_ms / max(1, time_limit * 1000))))
    else:
        score = 0

    conn.execute("""
        INSERT OR IGNORE INTO player_answers (
            session_id, question_id, question_index, player_name, answer,
            is_correct, response_ms, score_awarded
        )
        VALUES (?,?,?,?,?,?,?,?)
    """, (session_id, question_id, question_index, player_name, answer, is_correct, response_ms, score))

    # If ignored due to duplicate (race/refresh), return existing saved row.
    persisted = conn.execute(
        """
        SELECT is_correct, score_awarded, response_ms
        FROM player_answers
        WHERE session_id=? AND question_id=? AND player_name=?
        LIMIT 1
        """,
        (session_id, question_id, player_name)
    ).fetchone()
    final_correct = bool(persisted["is_correct"]) if persisted else bool(is_correct)
    final_score = persisted["score_awarded"] if persisted else score
    final_response_ms = persisted["response_ms"] if persisted else response_ms

    rank_details = _get_player_live_rank_details(conn, session_id, player_name)
    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "correct": final_correct,
        "score": final_score,
        "response_ms": final_response_ms,
        "question_id": question_id,
        "rank": rank_details["rank"],
        "total_players": rank_details["total_players"],
        "total_score": rank_details["score"],
        "points_to_next": rank_details["points_to_next"],
        "next_player": rank_details["next_player"]
    })



@app.route('/check_quiz_started/<int:session_id>')
def check_quiz_started(session_id):
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT started FROM live_sessions WHERE session_id=?",
            (session_id,)
        ).fetchone()

    return jsonify({"started": row["started"] == 1})


@app.route('/get_current_question/<int:session_id>')
def get_current_question(session_id):

    conn = get_db_connection()

    session_data = conn.execute(
        "SELECT * FROM live_sessions WHERE session_id=?",
        (session_id,)
    ).fetchone()

    if not session_data:
        conn.close()
        return jsonify({"finished": True, "started": False})

    live_state = _get_live_question_state(conn, session_data)
    viewer_name = (request.args.get('player_name') or '').strip() or session.get('student_nickname')

    try:
        final_released = (session_data["final_released"] == 1)
    except Exception:
        final_released = False
    try:
        scoreboard_released = (session_data["scoreboard_released"] == 1)
    except Exception:
        scoreboard_released = False

    if final_released:
        final_url = url_for('final_podium', session_id=session_id)
        if viewer_name:
            final_url = url_for('final_podium', session_id=session_id, player=viewer_name)
        live_state["final_podium_url"] = final_url

    if live_state.get("finished"):
        if final_released:
            conn.close()
            return jsonify({
                "finished": True,
                "started": True,
                "leaderboard_url": final_url
            })
        conn.close()
        return jsonify({
            "finished": False,
            "started": True,
            "hold_final": True
        })

    live_state["leaderboard_url"] = url_for('live_leaderboard', session_id=session_id)
    live_state["scoreboard_released"] = scoreboard_released
    if scoreboard_released:
        live_state["scoreboard_url"] = url_for('student_scoreboard', session_id=session_id, player=viewer_name or "")
    if viewer_name and live_state.get("question_id"):
        your_row = _get_player_question_answer(conn, session_id, live_state["question_id"], viewer_name)
        live_state["your_name"] = viewer_name
        live_state["your_submitted"] = bool(your_row)
        live_state["your_answer"] = your_row["answer"] if your_row else None
        live_state["your_correct"] = bool(your_row["is_correct"]) if your_row else None
        live_state["your_response_ms"] = your_row["response_ms"] if your_row else None
        live_state["your_score_awarded"] = your_row["score_awarded"] if your_row else None

    conn.close()
    return jsonify(live_state)


@app.route('/live_leaderboard/<int:session_id>')
def live_leaderboard(session_id):
    if session.get('role') != 'Teacher':
        return redirect('/login')

    with get_db_connection() as conn:
        conn.execute(
            "UPDATE live_sessions SET scoreboard_released=1 WHERE session_id=?",
            (session_id,)
        )
        session_data = conn.execute(
            "SELECT * FROM live_sessions WHERE session_id=?",
            (session_id,)
        ).fetchone()
        if not session_data:
            flash("Session not found")
            return redirect('/teacher_dashboard')

        quiz_row = conn.execute(
            "SELECT quiz_name FROM quizzes WHERE quiz_id=?",
            (session_data["quiz_id"],)
        ).fetchone()
        total_row = conn.execute(
            "SELECT COUNT(*) AS total FROM questions WHERE quiz_id=?",
            (session_data["quiz_id"],)
        ).fetchone()
        scores = _get_live_leaderboard_rows(conn, session_id)
        current_question_row = conn.execute(
            "SELECT question_id FROM questions WHERE quiz_id=? LIMIT 1 OFFSET ?",
            (session_data["quiz_id"], session_data["current_question"] or 0)
        ).fetchone()
        question_ranking = {"top3": [], "rows": []}
        if current_question_row:
            question_ranking = _get_question_ranking(conn, session_id, current_question_row["question_id"])

    total_questions = total_row["total"] if total_row else 0
    current_display = min((session_data["current_question"] or 0) + 1, total_questions if total_questions > 0 else 1)
    role = 'Teacher' if session.get('role') == 'Teacher' else 'Student'
    return render_template(
        'shared/live_leaderboard.html',
        session_id=session_id,
        role=role,
        scores=scores,
        question_ranking=question_ranking,
        quiz_name=quiz_row["quiz_name"] if quiz_row else "Live Quiz",
        current_question=current_display,
        total_questions=total_questions
    )

@app.route('/release_scoreboard/<int:session_id>')
def release_scoreboard(session_id):
    if session.get('role') != 'Teacher':
        return redirect('/login')
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE live_sessions SET scoreboard_released=1 WHERE session_id=?",
            (session_id,)
        )
    return redirect(url_for('live_leaderboard', session_id=session_id))

@app.route('/final_podium/<int:session_id>')
def final_podium(session_id):
    player_from_query = (request.args.get('player') or '').strip()
    is_teacher = session.get('role') == 'Teacher'
    # If the student link includes ?player= (even if empty), force student view.
    force_student_view = ('player' in request.args)
    has_student_identity = bool(
        session.get('role') == 'Student'
        or session.get('student_nickname')
        or ('player' in request.args)
    )

    if not is_teacher and not has_student_identity:
        return redirect('/login')

    with get_db_connection() as conn:
        if is_teacher:
            conn.execute(
                "UPDATE live_sessions SET final_released=1 WHERE session_id=?",
                (session_id,)
            )
        session_row = conn.execute(
            "SELECT quiz_id FROM live_sessions WHERE session_id=?",
            (session_id,)
        ).fetchone()
        if not session_row:
            flash("Session not found")
            return redirect('/student_dashboard')

        scores = _get_live_leaderboard_rows(conn, session_id)
        resolve_avatar = _get_live_avatar_map(conn, session_id)

        podium = []
        for row in scores[:3]:
            podium.append({
                "player_name": row["player_name"],
                "score": row["score"],
            "avatar": resolve_avatar(row["player_name"])
        })

    role = 'Student' if force_student_view else ('Teacher' if is_teacher else 'Student')
    dashboard_url = url_for('teacher_dashboard') if role == 'Teacher' else url_for('student_dashboard')
    student_result = None
    if role == 'Student':
        player_name = (
            player_from_query
            or session.get('student_nickname')
            or session.get('username')
            or ''
        ).strip()
        if player_name:
            target_name = player_name.lower()
            for idx, row in enumerate(scores):
                row_name = (row["player_name"] or "").strip()
                if row_name.lower() == target_name:
                    rank = idx + 1
                    if rank <= 3:
                        message = "Well played!"
                    else:
                        message = "Better luck next time."
                    student_result = {
                        "player_name": row_name or player_name,
                        "rank": rank,
                        "total_players": len(scores),
                        "score": row["score"],
                        "message": message
                    }
                    break
        if not student_result:
            # Keep student result card visible even when nickname mapping fails.
            student_result = {
                "player_name": player_name or "Student",
                "rank": None,
                "total_players": len(scores),
                "score": 0,
                "message": "Well played!"
            }

    return render_template(
        'shared/final_podium.html',
        session_id=session_id,
        podium=podium,
        role=role,
        student_result=student_result,
        dashboard_url=dashboard_url
    )


@app.route('/release_final_podium/<int:session_id>')
def release_final_podium(session_id):
    if session.get('role') != 'Teacher':
        return redirect('/login')
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE live_sessions SET final_released=1, is_active=0 WHERE session_id=?",
            (session_id,)
        )
    return redirect(url_for('final_podium', session_id=session_id))


@app.route('/live_leaderboard_data/<int:session_id>')
def live_leaderboard_data(session_id):
    lite_mode = request.args.get("lite") == "1"
    try:
        limit = int(request.args.get("limit", "0"))
    except ValueError:
        limit = 0
    if limit < 0:
        limit = 0
    limit = min(limit, 100)

    with get_db_connection() as conn:
        session_data = conn.execute(
            "SELECT current_question, quiz_id FROM live_sessions WHERE session_id=?",
            (session_id,)
        ).fetchone()
        if not session_data:
            return jsonify({"success": False, "message": "Session not found"}), 404

        total_row = conn.execute(
            "SELECT COUNT(*) AS total FROM questions WHERE quiz_id=?",
            (session_data["quiz_id"],)
        ).fetchone()
        scores = _get_live_leaderboard_rows(conn, session_id, limit=limit if lite_mode and limit > 0 else None)
        resolve_avatar = _get_live_avatar_map(conn, session_id)
        scores = [
            {**row, "avatar": resolve_avatar(row["player_name"])}
            for row in scores
        ]
        question_ranking = {"top3": [], "rows": []}
        if not lite_mode:
            current_question_row = conn.execute(
                "SELECT question_id FROM questions WHERE quiz_id=? LIMIT 1 OFFSET ?",
                (session_data["quiz_id"], session_data["current_question"] or 0)
            ).fetchone()
            if current_question_row:
                question_ranking = _get_question_ranking(conn, session_id, current_question_row["question_id"])
        total_questions = total_row["total"] if total_row else 0
        current_display = min((session_data["current_question"] or 0) + 1, total_questions if total_questions > 0 else 1)

    return jsonify({
        "success": True,
        "scores": scores,
        "question_ranking": question_ranking,
        "current_question": current_display,
        "total_questions": total_questions
    })

@app.route('/get_answer_counts/<int:session_id>/<int:question_id>')
def get_answer_counts(session_id, question_id):
    with get_db_connection() as conn:
        breakdown = _get_answer_breakdown(conn, session_id, question_id)
    return jsonify(breakdown)

@app.route('/get_question_ranking/<int:session_id>/<int:question_id>')
def get_question_ranking(session_id, question_id):
    with get_db_connection() as conn:
        ranking = _get_question_ranking(conn, session_id, question_id)
    return jsonify(ranking)


_db_initialized = False

@app.before_request
def _ensure_db_initialized():
    global _db_initialized
    if _db_initialized:
        return
    if USE_POSTGRES:
        init_db()
    else:
        migrate_practice_tables()
        init_practice_table()
    _db_initialized = True

# ---------------- RUN APP ----------------
if __name__ == '__main__':
    if USE_POSTGRES:
        init_db()
    else:
        migrate_practice_tables()
        init_practice_table()
    debug_mode = os.environ.get("FLASK_DEBUG", "0").strip().lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=5000, debug=debug_mode)
    

