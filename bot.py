import asyncio
import difflib
import html
import io
import logging
import math
import os
import re
import shutil
import smtplib
import sqlite3
import time
import unicodedata
import uuid
import zipfile
from email.message import EmailMessage
from pathlib import Path

import mobi
from dotenv import load_dotenv
from ebooklib import epub
from PIL import Image
from rapidocr_onnxruntime import RapidOCR
from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError, MessageNotModifiedError
from telethon.tl.types import PeerChannel

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

API_ID = int(os.getenv("TELEGRAM_API_ID") or "0")
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_PASS = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")
SEND_DELAY = int(os.getenv("SEND_DELAY_SECONDS") or "5")

ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID") or os.getenv("ALLOWED_TELEGRAM_ID") or "0")
ADMIN_NAME = os.getenv("ADMIN_NAME", "Admin")
ADMIN_KINDLE_EMAIL = os.getenv("KINDLE_EMAIL", "")

SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "")
SECONDARY_SOURCE_BOT = os.getenv("SECONDARY_SOURCE_BOT", "")

raw_ids = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = [int(x.strip()) for x in raw_ids.split(",") if x.strip().isdigit()]
if ADMIN_USER_ID and ADMIN_USER_ID not in ALLOWED_USER_IDS:
    ALLOWED_USER_IDS.append(ADMIN_USER_ID)

DB_PATH = BASE_DIR / "library.db"
SESSION_USER_PATH = str(BASE_DIR / "kindle_user")
SESSION_BOT_PATH = str(BASE_DIR / "kindle_bot_token")

SUPPORTED_EXTENSIONS = (
    ".epub", ".pdf", ".mobi", ".azw", ".azw3", ".txt", ".doc", ".docx", ".rtf"
)
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")

user_client = TelegramClient(SESSION_USER_PATH, API_ID, API_HASH)
bot_client = TelegramClient(SESSION_BOT_PATH, API_ID, API_HASH)
user_client.flood_sleep_threshold = 60
bot_client.flood_sleep_threshold = 60

ocr_engine = RapidOCR()

queue = asyncio.Queue()
search_sessions = {}
batch_trackers = {}
user_states = {}
temp_configs = {}
batch_incoming = {}

# ==================== UTILITÁRIOS DE FORMATAÇÃO E MENSAGENS SEGURAS ====================

def h(val) -> str:
    """Escapa texto dinâmico para uso seguro em HTML do Telegram."""
    return html.escape(str(val or ""))

def strip_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)

async def safe_edit(msg, text: str, buttons=None):
    """Edita mensagem com proteção total contra erros de formatação, MessageNotModified e FloodWait."""
    if not msg:
        return None
    for attempt in range(2):
        try:
            return await msg.edit(text, buttons=buttons, parse_mode="html")
        except MessageNotModifiedError:
            return msg
        except FloodWaitError as e:
            wait_s = min(e.seconds + 1, 30)
            logging.warning(f"FloodWait ao editar mensagem: aguardando {wait_s}s...")
            await asyncio.sleep(wait_s)
        except Exception as e:
            logging.warning(f"Fallback em safe_edit ({e}): enviando sem parse_mode")
            try:
                return await msg.edit(strip_html_tags(text), buttons=buttons, parse_mode=None)
            except Exception:
                return msg
    return msg

async def safe_reply(target, text: str, buttons=None):
    """Responde mensagem com proteção total contra erros de formatação e FloodWait."""
    if not target:
        return None
    for attempt in range(2):
        try:
            return await target.reply(text, buttons=buttons, parse_mode="html")
        except FloodWaitError as e:
            wait_s = min(e.seconds + 1, 30)
            logging.warning(f"FloodWait ao responder mensagem: aguardando {wait_s}s...")
            await asyncio.sleep(wait_s)
        except Exception as e:
            logging.warning(f"Fallback em safe_reply ({e}): enviando sem parse_mode")
            try:
                return await target.reply(strip_html_tags(text), buttons=buttons, parse_mode=None)
            except Exception:
                return None
    return None

# ==================== BANCO DE DADOS E TRAVA DE DUPLICIDADE INTELIGENTE ====================

NUM_MAP = {
    'um': '1', 'uma': '1', 'dois': '2', 'duas': '2', 'tres': '3', 'quatro': '4',
    'cinco': '5', 'seis': '6', 'sete': '7', 'oito': '8', 'nove': '9', 'dez': '10',
    '01': '1', '02': '2', '03': '3', '04': '4', '05': '5',
    '06': '6', '07': '7', '08': '8', '09': '9',
    'i': '1', 'ii': '2', 'iii': '3', 'iv': '4', 'v': '5', 'vi': '6', 'vii': '7', 'viii': '8'
}

STOP_WORDS = {
    'by', 'de', 'do', 'da', 'dos', 'das', 'e', 'a', 'o', 'os', 'as',
    'em', 'para', 'com', 'por', 'vol', 'volume', 'serie', 'livro', 'edicao'
}

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            kindle_email TEXT,
            sender_email TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS user_books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            raw_title TEXT,
            normalized_title TEXT,
            source TEXT,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, normalized_title)
        )
        """)
        if ADMIN_USER_ID and ADMIN_KINDLE_EMAIL and GMAIL_USER:
            conn.execute("""
            INSERT OR IGNORE INTO users (user_id, name, kindle_email, sender_email)
            VALUES (?, ?, ?, ?)
            """, (ADMIN_USER_ID, ADMIN_NAME, ADMIN_KINDLE_EMAIL, GMAIL_USER))
        conn.commit()

def get_user(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id, name, kindle_email, sender_email FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row:
            return {"user_id": row[0], "name": row[1], "kindle_email": row[2], "sender_email": row[3]}
    return None

def save_user(user_id: int, name: str, kindle_email: str, sender_email: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
        INSERT INTO users (user_id, name, kindle_email, sender_email)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            name = excluded.name,
            kindle_email = excluded.kindle_email,
            sender_email = excluded.sender_email
        """, (user_id, name, kindle_email.strip().lower(), sender_email.strip().lower()))
        conn.commit()

def _clean_base_title(title: str) -> str:
    t = title.lower()
    t = unicodedata.normalize('NFKD', t).encode('ASCII', 'ignore').decode('utf-8')
    t = re.sub(r'\.(epub|pdf|mobi|azw3?|docxf?|docx|txt|rtf)$', '', t)
    t = t.replace('_', ' ')
    t = re.sub(r'^\(tm\)\s*', '', t)
    t = re.sub(r'\s*\([a-z0-9\-]+\.org\)', '', t)
    return t.strip()

def extract_core_title(title: str) -> str:
    """Extrai apenas o título principal da obra (removendo autor e sufixo de série após ' - ' ou ' by ')."""
    t = _clean_base_title(title)
    t = re.sub(r'\[.*?\]|\(.*?\)|\{.*?\}', ' ', t)
    parts = [p.strip() for p in re.split(r'\s+-\s+|\s+by\s+', t) if p.strip()]
    if len(parts) >= 2:
        first_clean = re.sub(r'[^a-z0-9\s]', ' ', parts[0]).strip()
        if first_clean.startswith('serie ') and len(parts) >= 2:
            core = parts[0] + ' ' + parts[1]
        else:
            core = parts[0]
    else:
        core = t
    words = [NUM_MAP.get(w, w) for w in re.findall(r'[a-z0-9]+', core) if w not in STOP_WORDS]
    return ' '.join(words)

def normalize_title(title: str) -> str:
    t = _clean_base_title(title)
    t = re.sub(r'\[.*?\]|\(.*?\)|\{.*?\}', ' ', t)
    words = [NUM_MAP.get(w, w) for w in re.findall(r'[a-z0-9]+', t) if w not in STOP_WORDS]
    return ' '.join(words)

def _extract_volume_numbers(norm_str: str) -> set:
    """Extrai números de volume ignorando anos de 4 dígitos (ex: 2022, 2025, 2026)."""
    return {w for w in norm_str.split() if w.isdigit() and not (len(w) == 4 and w.startswith(('19', '20')))}

def check_duplicate(user_id: int, candidate_title: str) -> tuple[bool, str]:
    norm_new = normalize_title(candidate_title)
    core_new = extract_core_title(candidate_title)
    if not norm_new or len(norm_new) < 3:
        return False, ""

    nums_new = _extract_volume_numbers(norm_new)
    core_words_new = core_new.split()
    core_set_new = set(core_words_new)

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT raw_title, normalized_title FROM user_books WHERE user_id = ?", (user_id,))
        records = cur.fetchall()

    for raw_title, _ in records:
        if not raw_title:
            continue
        norm_stored = normalize_title(raw_title)
        core_stored = extract_core_title(raw_title)
        if not norm_stored:
            continue

        nums_stored = _extract_volume_numbers(norm_stored)
        # Se ambos têm números de volume explícitos e diferentes (ex: vol 1 vs vol 2), nunca são duplicados
        if nums_new and nums_stored and nums_new != nums_stored:
            continue

        # 1. Igualdade exata do título completo normalizado
        if norm_new == norm_stored:
            return True, raw_title

        # 2. Igualdade exata do núcleo do título (sem autor/série)
        if core_new and core_stored and len(core_new) >= 4 and core_new == core_stored:
            return True, raw_title

        # 3. Comparação estrutural do núcleo do título (ex: "Cidade dos Anjos Caídos Os Instrumentos Mortais 4" vs "Cidade dos Anjos Caídos 4")
        core_words_stored = core_stored.split()
        core_set_stored = set(core_words_stored)
        if len(core_set_new) >= 2 and len(core_set_stored) >= 2:
            if core_set_new == core_set_stored:
                return True, raw_title
            # Se começam exatamente com as mesmas 2 primeiras palavras do título e um é subconjunto completo do outro
            if core_words_new[:2] == core_words_stored[:2]:
                if core_set_stored.issubset(core_set_new) or core_set_new.issubset(core_set_stored):
                    return True, raw_title

    return False, ""

def add_book_to_db(user_id: int, raw_title: str, source: str = "telegram_bot"):
    norm = normalize_title(raw_title)
    if not norm:
        return
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO user_books (user_id, raw_title, normalized_title, source) VALUES (?, ?, ?, ?)",
                (user_id, raw_title, norm, source)
            )
            conn.commit()
    except Exception as e:
        logging.error(f"Erro ao salvar livro no banco: {e}")

def get_user_books_summary(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM user_books WHERE user_id = ?", (user_id,))
        total = cur.fetchone()[0]
        cur.execute("SELECT raw_title FROM user_books WHERE user_id = ? ORDER BY id DESC LIMIT 5", (user_id,))
        recent = [r[0] for r in cur.fetchall()]
    return total, recent

# ==================== FORMATAÇÃO DE NOMES, CONVERSÃO E METADADOS ====================

def beautify_filename(file_name: str) -> str:
    ext = Path(file_name).suffix.lower() or ".epub"
    stem = Path(file_name).stem
    stem = stem.replace('_', ' ')
    stem = re.sub(r'^\(tm\)\s*', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'\s*\([a-z0-9\-]+\.org\)', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'\s*-\s*', ' - ', stem)
    stem = re.sub(r'\s+', ' ', stem).strip()
    stem = stem.strip('- ')
    if not stem:
        stem = "Livro"
    return f"{stem}{ext}"

def clean_synopsis_text(raw_text: str) -> str:
    if not raw_text:
        return "Sinopse não disponível no canal."
    # Remove marcadores markdown injetados pelo Telethon (** e __)
    cleaned = raw_text.replace("**", "").replace("__", "")
    lines = cleaned.split('\n')
    clean_lines = []
    for l in lines:
        stripped = l.strip()
        if stripped.startswith(('#', '👉', '📥', 'Link:', 'Canal:', 'Postado por', 'Parceria:', 'http://', 'https://')):
            continue
        clean_lines.append(l)

    text = '\n'.join(clean_lines).strip()
    if len(text) > 650:
        cut = text[:647]
        last_space = cut.rfind(' ')
        if last_space > 500:
            cut = cut[:last_space]
        text = cut + "..."
    return text if text else "Sinopse disponível no canal."

def score_title_match(query: str, filename: str, synopsis: str = "") -> int:
    def get_words(t):
        t = t.lower()
        t = unicodedata.normalize('NFKD', t).encode('ASCII', 'ignore').decode('utf-8')
        stop = {'o', 'a', 'os', 'as', 'de', 'do', 'da', 'dos', 'das', 'e', 'em', 'um', 'uma', 'por', 'com'}
        return set(re.findall(r'[a-z0-9]+', t)) - stop

    q_words = get_words(query)
    f_words = get_words(filename)
    if not q_words or not f_words:
        return 0

    score = 0
    intersection = q_words.intersection(f_words)
    score += len(intersection) * 15

    # Tolerância fuzzy para palavras em fontes góticas/cursivas lidas com 1-2 letras trocadas pelo OCR (ex: 'uicksiluer' -> 'quicksilver')
    unmatched_q = q_words - intersection
    unmatched_f = f_words - intersection
    fuzzy_matched_q = set()
    for qw in unmatched_q:
        if len(qw) < 4:
            continue
        best_ratio = 0.0
        for fw in unmatched_f:
            if len(fw) < 4:
                continue
            r = difflib.SequenceMatcher(None, qw, fw).ratio()
            if (len(qw) >= 5 and qw in fw) or (len(fw) >= 5 and fw in qw):
                r = max(r, 0.82)
            if r > best_ratio:
                best_ratio = r
        if best_ratio >= 0.70:
            fuzzy_matched_q.add(qw)
            score += int(best_ratio * 45)

    effective_matched = intersection | fuzzy_matched_q

    if effective_matched == q_words:
        score += 50

    core_f_words = get_words(extract_core_title(filename))
    if q_words == core_f_words:
        score += 120
    elif q_words.issubset(core_f_words):
        score += 40
    elif fuzzy_matched_q:
        for qw in fuzzy_matched_q:
            for cfw in core_f_words:
                if difflib.SequenceMatcher(None, qw, cfw).ratio() >= 0.70:
                    score += 60

    if not effective_matched and synopsis:
        score -= 50

    if filename.lower().endswith(".epub"):
        score += 15

    return score

def html_to_epub(html_path: Path, output_epub_path: Path, title: str):
    book = epub.EpubBook()
    book.set_identifier("encha_seu_kindle_" + str(int(time.time())))
    book.set_title(title)
    book.set_language("pt")

    with open(html_path, "r", encoding="utf-8", errors="ignore") as f:
        html_content = f.read()

    if not html_content.strip():
        html_content = f"<html><body><h1>{h(title)}</h1></body></html>"

    c1 = epub.EpubHtml(title=title, file_name="chap_1.xhtml", lang="pt")
    c1.content = html_content
    book.add_item(c1)

    for folder_name in ("images", "Images", "HDImages"):
        img_dir = html_path.parent / folder_name
        if img_dir.exists() and img_dir.is_dir():
            for img_file in img_dir.iterdir():
                if img_file.is_file():
                    try:
                        ext = img_file.suffix.lower()
                        mtype = "image/png" if ext == ".png" else "image/gif" if ext == ".gif" else "image/jpeg"
                        with open(img_file, "rb") as img_f:
                            epub_img = epub.EpubItem(
                                uid=f"{folder_name}_{img_file.name}",
                                file_name=f"images/{img_file.name}",
                                media_type=mtype,
                                content=img_f.read()
                            )
                            book.add_item(epub_img)
                    except Exception:
                        pass

    book.toc = (epub.Link("chap_1.xhtml", title, "intro"),)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", c1]
    epub.write_epub(str(output_epub_path), book)

def convert_to_epub_if_needed(file_path: Path, display_name: str) -> tuple[Path, str]:
    ext = file_path.suffix.lower()
    if ext not in ('.azw3', '.azw', '.mobi'):
        return file_path, display_name

    new_stem = Path(display_name).stem
    new_display_name = f"{new_stem}.epub"
    target_epub = file_path.with_name(f"converted_{int(time.time())}_{uuid.uuid4().hex[:6]}.epub")

    tempdir = None
    try:
        tempdir, extracted_path_str = mobi.extract(str(file_path))
        extracted_path = Path(extracted_path_str)
        if extracted_path.exists() and extracted_path.suffix.lower() == ".epub":
            shutil.copy2(extracted_path, target_epub)
            return target_epub, new_display_name
        elif extracted_path.exists() and extracted_path.suffix.lower() in (".html", ".htm"):
            html_to_epub(extracted_path, target_epub, new_stem)
            return target_epub, new_display_name
        else:
            # Procura qualquer .epub ou .html dentro do diretório extraído
            for root, _, files in os.walk(tempdir):
                for f in files:
                    if f.lower().endswith(".epub"):
                        shutil.copy2(Path(root) / f, target_epub)
                        return target_epub, new_display_name
            for root, _, files in os.walk(tempdir):
                for f in files:
                    if f.lower().endswith((".html", ".htm")):
                        html_to_epub(Path(root) / f, target_epub, new_stem)
                        return target_epub, new_display_name
        raise RuntimeError(f"Não foi possível extrair estrutura EPUB/HTML de {file_path.name}")
    except Exception as e:
        logging.error(f"Erro na conversão automática de {file_path.name} para EPUB: {e}", exc_info=True)
        raise RuntimeError(f"Falha ao converter arquivo {ext} para EPUB: {e}")
    finally:
        if tempdir and os.path.exists(tempdir):
            shutil.rmtree(tempdir, ignore_errors=True)

def _compress_image_bytes(filename: str, data: bytes, max_dim: tuple[int, int] = (900, 1300), quality: int = 60) -> bytes:
    if len(data) < 80_000:
        return data
    ext = Path(filename).suffix.lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.webp'):
        return data
    try:
        img = Image.open(io.BytesIO(data))
        img.thumbnail(max_dim)
        buf = io.BytesIO()
        if ext in ('.jpg', '.jpeg'):
            if img.mode in ('RGBA', 'P', 'LA'):
                img = img.convert('RGB')
            img.save(buf, format='JPEG', quality=quality, optimize=True)
        elif ext == '.png':
            if img.mode not in ('RGB', 'RGBA', 'P'):
                img = img.convert('RGB')
            q_img = img.quantize(colors=48)
            q_img.save(buf, format='PNG', optimize=True)
        elif ext == '.webp':
            img.save(buf, format='WEBP', quality=quality)
        new_data = buf.getvalue()
        if len(new_data) < len(data):
            return new_data
    except Exception:
        pass
    return data

def clean_epub_internal_metadata(epub_path: Path):
    if not epub_path.exists() or epub_path.suffix.lower() != ".epub":
        return

    is_oversized = epub_path.stat().st_size > 16 * 1024 * 1024
    temp_epub = epub_path.with_suffix('.tmp.epub')
    try:
        with zipfile.ZipFile(epub_path, 'r') as zin:
            opf_names = [n for n in zin.namelist() if n.endswith('.opf')]
            if not opf_names and not is_oversized:
                return

            with zipfile.ZipFile(temp_epub, 'w') as zout:
                if 'mimetype' in zin.namelist():
                    zout.writestr('mimetype', b'application/epub+zip', compress_type=zipfile.ZIP_STORED)

                seen_names = {'mimetype'} if 'mimetype' in zin.namelist() else set()
                for item in zin.infolist():
                    if item.filename in seen_names:
                        continue
                    seen_names.add(item.filename)
                    data = zin.read(item.filename)
                    if item.filename in opf_names:
                        try:
                            text = data.decode('utf-8')
                            def replace_title(m):
                                t = m.group(1).replace('_', ' ')
                                t = re.sub(r'\s+', ' ', t).strip()
                                return f"<dc:title>{t}</dc:title>"
                            text = re.sub(r'<dc:title[^>]*>(.*?)</dc:title>', replace_title, text, flags=re.DOTALL)
                            data = text.encode('utf-8')
                        except Exception:
                            pass
                    elif is_oversized:
                        data = _compress_image_bytes(item.filename, data)
                    zout.writestr(item, data, compress_type=zipfile.ZIP_DEFLATED)

        temp_epub.replace(epub_path)
        if is_oversized:
            logging.info(f"EPUB grande otimizado: novo tamanho = {epub_path.stat().st_size / (1024*1024):.2f} MB")
    except Exception as e:
        logging.warning(f"Aviso ao limpar metadados: {e}")
        if temp_epub.exists():
            try:
                temp_epub.unlink()
            except Exception:
                pass

# ==================== ENVIO VIA GMAIL SMTP COM RETRY ====================

def send_email_to_kindle(file_path: Path, display_name: str, target_kindle_email: str):
    if not file_path.exists():
        raise FileNotFoundError(f"Arquivo temporário não encontrado: {file_path}")

    file_size_mb = file_path.stat().st_size / (1024 * 1024)
    if file_size_mb > 24.5:
        raise ValueError(f"O arquivo tem {file_size_mb:.1f} MB (limite máximo do Gmail/Kindle é 25 MB).")

    ext = Path(display_name).suffix.lower() or ".epub"
    if ext in (".azw3", ".azw", ".mobi"):
        raise ValueError(f"Formato {ext} não suportado pela Amazon e não pôde ser convertido.")

    stem = Path(display_name).stem
    clean_stem = unicodedata.normalize('NFKD', stem).encode('ASCII', 'ignore').decode('utf-8')
    clean_stem = re.sub(r'[^a-zA-Z0-9_\-\s]', '', clean_stem).strip()
    clean_stem = re.sub(r'\s+', ' ', clean_stem)
    if not clean_stem:
        clean_stem = "livro"

    safe_filename = f"{clean_stem}{ext}"

    msg = EmailMessage()
    msg["From"] = GMAIL_USER
    msg["To"] = target_kindle_email
    msg["Subject"] = f"Livro: {safe_filename}"
    msg.set_content(f"Envio automatico do livro '{display_name}' para o Kindle.")

    with open(file_path, "rb") as f:
        file_data = f.read()

    msg.add_attachment(
        file_data,
        maintype="application",
        subtype="octet-stream",
        filename=safe_filename
    )

    last_err = None
    for attempt in range(1, 4):
        try:
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=90) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(GMAIL_USER, GMAIL_PASS)
                server.send_message(msg)
            return
        except Exception as e:
            last_err = e
            logging.warning(f"Tentativa SMTP {attempt}/3 falhou para {safe_filename}: {e}")
            if attempt < 3:
                time.sleep(3 * attempt)
    raise RuntimeError(f"Falha no envio SMTP após 3 tentativas: {last_err}")

# ==================== MOTORES DE BUSCA INTELIGENTES ====================

async def search_primary_channel(query: str, scoring_query: str = ""):
    """Busca no canal principal de acervo configurado e ranqueia resultados por relevância."""
    candidates = []
    seen_doc_ids = set()
    if not SOURCE_CHANNEL:
        return candidates
    rank_q = scoring_query or query
    try:
        ch = await user_client.get_entity(SOURCE_CHANNEL)
        clean_terms = " ".join([w for w in re.findall(r'[a-zA-Z0-9Á-ú]+', query) if len(w) > 2])
        if not clean_terms:
            clean_terms = query.strip()

        async for m in user_client.iter_messages(ch, search=clean_terms, limit=25):
            doc_msg = None
            synopsis_msg = None

            if m.file and m.file.name and m.file.name.lower().endswith(SUPPORTED_EXTENSIONS):
                doc_msg = m
                prev_msgs = await user_client.get_messages(ch, ids=[m.id - 1, m.id - 2])
                for pm in prev_msgs:
                    if pm and (pm.raw_text or pm.text) and len(pm.raw_text or pm.text) > 40:
                        synopsis_msg = pm
                        break
            elif (m.raw_text or m.text) and len(m.raw_text or m.text) > 40:
                synopsis_msg = m
                next_msgs = await user_client.get_messages(ch, ids=[m.id + 1, m.id + 2, m.id + 3])
                for nm in next_msgs:
                    if nm and nm.file and nm.file.name and nm.file.name.lower().endswith(SUPPORTED_EXTENSIONS):
                        doc_msg = nm
                        break

            if doc_msg and doc_msg.id not in seen_doc_ids:
                seen_doc_ids.add(doc_msg.id)
                syn_raw = (synopsis_msg.raw_text or synopsis_msg.text) if synopsis_msg else ""
                score = score_title_match(rank_q, doc_msg.file.name, syn_raw)
                synopsis = clean_synopsis_text(syn_raw)
                candidates.append({
                    'source': 'Acervo Principal (Canal)',
                    'doc_id': doc_msg.id,
                    'title': doc_msg.file.name,
                    'synopsis': synopsis,
                    'doc_msg': doc_msg,
                    'score': score,
                    'downloader': user_client
                })

        candidates.sort(key=lambda x: x['score'], reverse=True)
    except Exception as e:
        logging.error(f"Erro ao buscar no canal principal: {e}", exc_info=True)
    return candidates

async def search_secondary_bot(query: str):
    """Busca o livro na fonte secundária configurada escolhendo a melhor opção (preferindo EPUB e PT-BR)."""
    if not SECONDARY_SOURCE_BOT:
        return []
    try:
        ext_bot = await user_client.get_entity(SECONDARY_SOURCE_BOT)
        clean_query = query.strip()
        sent = await user_client.send_message(ext_bot, clean_query)
        for _ in range(12):
            await asyncio.sleep(1)
            async for m in user_client.iter_messages(ext_bot, limit=5):
                if m.id <= sent.id:
                    continue
                raw = m.raw_text or m.text or ""
                if any(phrase in raw.lower() for phrase in ("nothing found", "no books", "nenhum livro", "not found")):
                    return []
                if '/book_' in raw:
                    entries = []
                    curr_title = clean_query
                    curr_info = []
                    for line in raw.split('\n'):
                        line_s = line.strip().replace('**', '').replace('__', '')
                        if '📚' in line_s:
                            curr_title = line_s.replace('📚', '').strip()
                            curr_info = []
                        elif line_s.startswith(('Year:', '🌐', 'Author:', 'Publisher:')) or (line_s and not line_s.startswith(('/book_', 'Good news'))):
                            if len(line_s) < 80:
                                curr_info.append(line_s)
                        if line_s.startswith('/book_'):
                            cmd = line_s.split()[0]
                            is_epub = 'epub' in line_s.lower()
                            info_str = " • ".join(curr_info)
                            is_pt = any(lang in info_str.lower() for lang in ('brazilian', 'portuguese', 'português'))
                            entry_score = score_title_match(clean_query, curr_title)
                            if is_epub:
                                entry_score += 30
                            if is_pt:
                                entry_score += 25
                            entries.append({
                                'cmd': cmd,
                                'title': curr_title,
                                'info': info_str,
                                'score': entry_score
                            })

                    if not entries:
                        return []
                    entries.sort(key=lambda x: x['score'], reverse=True)
                    best = entries[0]
                    req = await user_client.send_message(ext_bot, best['cmd'])
                    for _ in range(20):
                        await asyncio.sleep(1)
                        async for reply in user_client.iter_messages(ext_bot, limit=4):
                            if reply.id > req.id and reply.file and reply.file.name:
                                syn_desc = f"Título localizado: {best['title']}"
                                if best['info']:
                                    syn_desc += f"\n{best['info']}"
                                return [{
                                    'source': 'Acervo Secundário',
                                    'doc_id': reply.id,
                                    'title': reply.file.name,
                                    'synopsis': syn_desc,
                                    'doc_msg': reply,
                                    'score': 100,
                                    'downloader': user_client
                                }]
                    return []
    except Exception as e:
        logging.error(f"Erro ao buscar na fonte secundária: {e}", exc_info=True)
    return []

PUBLISHERS_AND_NOISE = {
    'alt', 'arqueiro', 'intrinseca', 'seguinte', 'rocco', 'record', 'verus',
    'galera', 'planeta', 'suma', 'darkside', 'harper', 'companhia', 'presenca',
    'best', 'seller', 'bestseller', 'edicao', 'volume', 'vol', 'livro', 'trilogia',
    'duologia', 'serie', 'new', 'york', 'times', 'tiktok', 'booktok', 'autora', 'autor',
    'traducao', 'prefacio', 'introducao', 'digital'
}

def extract_cover_queries(image_path: Path) -> list[str]:
    """
    Analisa a geometria das caixas de texto da capa (altura da fonte, largura, orientação)
    para separar o TÍTULO PRINCIPAL de frases promocionais pequenas, selos de editora e textos verticais.
    """
    try:
        res, _ = ocr_engine(str(image_path))
        if not res:
            return []

        max_y = max((max(p[1] for p in box) for box, _, _ in res), default=1000.0)
        valid_boxes = []
        for box, text, conf in res:
            raw = text.strip()
            if not raw or conf < 0.78:
                continue
            w = math.hypot(box[1][0] - box[0][0], box[1][1] - box[0][1])
            h = math.hypot(box[3][0] - box[0][0], box[3][1] - box[0][1])
            yc = sum(p[1] for p in box) / 4.0

            # 1. Ignora textos verticais/rotacionados (ex: plaquinhas laterais onde altura > largura)
            if w < h * 1.15:
                continue

            # 2. Ignora citações e frases promocionais de topo ("Prepare-se..., —REBECCA ROSS)
            if raw.startswith(('"', '“', '”', "'", '—', '-', '«', '<', '(')):
                continue
            raw_lower = raw.lower()
            if any(p in raw_lower for p in ('autora de', 'autor de', 'best-seller', 'prepare-se', 'obsessao', 'obsessão', 'melodia da', 'divinos rivais')):
                continue

            cleaned = re.sub(r'[^a-zA-Z0-9\sÁ-ú]', ' ', raw)
            words = [
                w_str for w_str in cleaned.split()
                if len(w_str) >= 3 and w_str.lower() not in PUBLISHERS_AND_NOISE
            ]
            if not words:
                continue

            is_exclam = '!' in raw and len(words) == 1
            valid_boxes.append({
                'text': ' '.join(words),
                'words': words,
                'h': h,
                'w': w,
                'yc': yc,
                'conf': conf,
                'is_exclam': is_exclam
            })

        if not valid_boxes:
            return []

        max_h = max(b['h'] for b in valid_boxes)
        max_w = max(b['w'] for b in valid_boxes)

        # Filtra linhas minúsculas de blurb/review (menores que 3.2% da altura da capa e < 30% da fonte principal)
        significant_boxes = [
            b for b in valid_boxes
            if b['h'] >= max_h * 0.16 and not (b['h'] < max_y * 0.032 and b['h'] < max_h * 0.32)
        ]
        if not significant_boxes:
            significant_boxes = valid_boxes

        # Camada 1: TÍTULO PRINCIPAL (apenas as maiores fontes da capa: h >= 50% da maior fonte)
        primary_title_boxes = [
            b for b in significant_boxes
            if b['h'] >= max_h * 0.50 and not b['is_exclam']
        ]
        if len(primary_title_boxes) < 2:
            primary_title_boxes = [
                b for b in significant_boxes
                if b['h'] >= max_h * 0.36 and not (b['is_exclam'] and b['h'] < max_h * 0.50)
            ]
        primary_title_boxes.sort(key=lambda b: b['yc'])

        # Camada 2: TÍTULO + AUTOR (fontes médias-grandes de até 3 palavras por linha)
        title_author_boxes = [
            b for b in significant_boxes
            if b['w'] >= max_w * 0.22 and not b['is_exclam'] and len(b['words']) <= 3
        ]
        title_author_boxes.sort(key=lambda b: b['yc'])

        queries = []
        if primary_title_boxes:
            q1 = ' '.join(b['text'] for b in primary_title_boxes).strip()
            if q1:
                queries.append(q1)

        if title_author_boxes:
            q2 = ' '.join(b['text'] for b in title_author_boxes).strip()
            if q2 and q2 not in queries:
                queries.append(q2)

        by_size = sorted(significant_boxes, key=lambda b: b['h'], reverse=True)
        q3 = ' '.join(b['text'] for b in sorted(by_size[:2], key=lambda x: x['yc'])).strip()
        if q3 and q3 not in queries:
            queries.append(q3)

        if by_size:
            for b in by_size:
                if b['text'] and b['text'] not in queries:
                    queries.append(b['text'])

        return queries
    except Exception as e:
        logging.warning(f"Erro no OCR geométrico: {e}")
        return []

def extract_text_from_cover(image_path: Path) -> str:
    qs = extract_cover_queries(image_path)
    return qs[0] if qs else ""

# ==================== NAVEGAÇÃO DE RESULTADOS DE BUSCA ====================

def make_search_buttons(session_id: str, current_idx: int, total_results: int, current_source: str):
    btns = []
    btns.append([Button.inline("✅ Confirmar e Enviar para o Kindle", data=f"conf_{session_id}_{current_idx}".encode())])

    nav_row = []
    if current_idx > 0:
        nav_row.append(Button.inline("⬅️ Anterior", data=f"prev_{session_id}".encode()))
    if current_idx < total_results - 1:
        nav_row.append(Button.inline(f"➡️ Próximo ({current_idx + 2}/{total_results})", data=f"next_{session_id}".encode()))
    if nav_row:
        btns.append(nav_row)

    action_row = []
    if current_source != 'Acervo Secundário':
        action_row.append(Button.inline("🌐 Buscar na Fonte Secundária", data=f"alt_{session_id}".encode()))
    else:
        action_row.append(Button.inline("🔙 Voltar ao Canal", data=f"canal_{session_id}".encode()))
    action_row.append(Button.inline("❌ Cancelar", data=f"cancel_{session_id}".encode()))
    btns.append(action_row)

    return btns

def render_search_card(session: dict):
    idx = session["current_index"]
    item = session["results"][idx]
    total = len(session["results"])
    clean_name = beautify_filename(item["title"])
    clean_syn = clean_synopsis_text(item.get("synopsis", ""))

    pos_indicator = f" ({idx + 1} de {total})" if total > 1 else ""

    text = (
        f"📖 <b>{h(clean_name)}</b>\n"
        f"🌐 <b>Fonte:</b> {h(item['source'])}{h(pos_indicator)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Sinopse:</b>\n<i>{h(clean_syn)}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Deseja enviar este livro para o seu Kindle?"
    )
    buttons = make_search_buttons(session["id"], idx, total, item["source"])
    return text, buttons

# ==================== PROGRESSO DE LOTES (DASHBOARD) ====================

def render_progress_bar(current: int, total: int, length: int = 10) -> str:
    percent = int((current / total) * length) if total > 0 else 0
    percent = max(0, min(length, percent))
    return "🟩" * percent + "⬜" * (length - percent)

async def update_batch_dashboard(user_id: int, final: bool = False):
    tracker = batch_trackers.get(user_id)
    if not tracker or not tracker.get("status_msg"):
        return

    now = time.time()
    # Evita FloodWait no Telegram limitando edições intermediárias a 1 a cada 1.8s
    if not final and (now - tracker.get("last_edit_ts", 0)) < 1.8:
        return
    tracker["last_edit_ts"] = now

    total = tracker["total"]
    processed = tracker["processed"]
    success = tracker["success"]
    dup = tracker["dup"]
    fail = tracker["fail"]
    current_title = tracker.get("current_title", "...")
    clean_title = current_title.replace('_', ' ').replace('*', '').strip()

    bar = render_progress_bar(processed, total)
    pct = int((processed / total) * 100) if total > 0 else 0

    if not final:
        text = (
            f"📦 <b>Processando Lote de Livros ({total} livros)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ <b>Progresso:</b> {bar} {processed}/{total} ({pct}%)\n"
            f"📖 <b>Livro atual:</b> <code>{h(clean_title)}</code>\n\n"
            f"✅ <b>Enviados para o Kindle:</b> {success}\n"
            f"⚠️ <b>Já estavam no Kindle:</b> {dup}\n"
            f"❌ <b>Não encontrados/Falhas:</b> {fail}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱️ <i>Atualizando em tempo real a cada envio...</i>"
        )
    else:
        text = (
            f"🎉 <b>Lote de {total} livros concluído com sucesso!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ <b>{success} livros enviados</b> direto para o seu Kindle!\n"
            f"⚠️ <b>{dup} livros ignorados</b> (já estavam na sua biblioteca).\n"
            f"❌ <b>{fail} livros não encontrados</b> no canal.\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📚 Aproveite suas leituras!"
        )

    await safe_edit(tracker["status_msg"], text)

# ==================== WORKER DE FILA BLINDADO ====================

async def resolve_channel_messages(channel_ref, candidate_ids: list[int]):
    """Busca mensagens do canal com fallback seguro caso o ID do canal não esteja no cache."""
    try:
        peer = PeerChannel(channel_ref) if isinstance(channel_ref, int) and channel_ref > 0 else channel_ref
        return await user_client.get_messages(peer, ids=candidate_ids)
    except Exception:
        ch = await user_client.get_entity(SOURCE_CHANNEL)
        return await user_client.get_messages(ch, ids=candidate_ids)

async def worker():
    logging.info("Worker v7 (Blindado) ativo.")
    while True:
        try:
            task = await queue.get()
            email_was_sent = False
            try:
                user_id, source_msg, status_msg, channel, orig_msg_id, direct_doc, file_name, is_batch = task

                user = get_user(user_id)
                if not user or not user.get("kindle_email"):
                    if not is_batch:
                        if status_msg:
                            await safe_edit(status_msg, "❌ Erro: Configure seu Kindle antes com /start.")
                        elif source_msg:
                            await safe_reply(source_msg, "❌ Erro: Configure seu Kindle antes com /start.")
                    continue

                target_kindle = user["kindle_email"]
                clean_display_name = beautify_filename(file_name)

                if is_batch and user_id in batch_trackers:
                    batch_trackers[user_id]["current_title"] = clean_display_name
                    await update_batch_dashboard(user_id)

                temp_path = None
                try:
                    target_doc_msg = None
                    if direct_doc:
                        target_doc_msg = source_msg
                    else:
                        candidate_ids = list(range(orig_msg_id, orig_msg_id + 7))
                        channel_msgs = await resolve_channel_messages(channel, candidate_ids)
                        for m in channel_msgs:
                            if m and m.document and m.file and m.file.name:
                                if m.file.name.lower().endswith(SUPPORTED_EXTENSIONS):
                                    target_doc_msg = m
                                    file_name = m.file.name
                                    clean_display_name = beautify_filename(file_name)
                                    if is_batch and user_id in batch_trackers:
                                        batch_trackers[user_id]["current_title"] = clean_display_name
                                        await update_batch_dashboard(user_id)
                                    break

                    if not target_doc_msg:
                        if is_batch and user_id in batch_trackers:
                            batch_trackers[user_id]["processed"] += 1
                            batch_trackers[user_id]["fail"] += 1
                            await update_batch_dashboard(user_id)
                        elif status_msg:
                            await safe_edit(status_msg, f"⚠️ Não encontrei o arquivo do livro no post <code>{h(orig_msg_id)}</code>.")
                        continue

                    # Trava de duplicidade inteligente
                    is_dup, matched_title = check_duplicate(user_id, clean_display_name)
                    if is_dup:
                        if is_batch and user_id in batch_trackers:
                            batch_trackers[user_id]["processed"] += 1
                            batch_trackers[user_id]["dup"] += 1
                            await update_batch_dashboard(user_id)
                        elif status_msg:
                            await safe_edit(
                                status_msg,
                                f"⚠️ <b>{h(clean_display_name)}</b> não foi enviado porque já consta no seu Kindle (<code>{h(matched_title)}</code>)!"
                            )
                        continue

                    if not is_batch and status_msg:
                        await safe_edit(status_msg, f"⏳ Baixando e enviando: <b>{h(clean_display_name)}</b>...")

                    # Download
                    safe_local = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', clean_display_name)
                    temp_path = BASE_DIR / f"temp_{int(time.time())}_{uuid.uuid4().hex[:4]}_{safe_local}"
                    downloader = user_client if not direct_doc else (
                        user_client if ADMIN_USER_ID and getattr(source_msg, 'chat_id', None) == ADMIN_USER_ID and getattr(source_msg, 'out', False) else bot_client
                    )
                    await downloader.download_media(target_doc_msg, file=str(temp_path))

                    if not temp_path.exists():
                        # Fallback com o outro client caso necessário
                        alt_downloader = bot_client if downloader == user_client else user_client
                        await alt_downloader.download_media(target_doc_msg, file=str(temp_path))

                    # Conversão automática em thread separada (não trava o loop do Telegram)
                    temp_converted, clean_display_name = await asyncio.to_thread(
                        convert_to_epub_if_needed, temp_path, clean_display_name
                    )
                    if temp_converted != temp_path:
                        try:
                            temp_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        temp_path = temp_converted

                    await asyncio.to_thread(clean_epub_internal_metadata, temp_path)

                    # Envia via Gmail SMTP com retry
                    await asyncio.to_thread(send_email_to_kindle, temp_path, clean_display_name, target_kindle)
                    email_was_sent = True
                    add_book_to_db(user_id, clean_display_name, source="telegram_bot")
                    logging.info(f"Livro '{clean_display_name}' enviado para {target_kindle} (User {user_id}) com sucesso!")

                    if is_batch and user_id in batch_trackers:
                        batch_trackers[user_id]["processed"] += 1
                        batch_trackers[user_id]["success"] += 1
                        await update_batch_dashboard(user_id)
                    elif status_msg:
                        await safe_edit(
                            status_msg,
                            f"✅ <b>{h(clean_display_name)}</b> enviado com sucesso para o seu Kindle (<code>{h(target_kindle)}</code>)!"
                        )

                except Exception as e:
                    logging.error(f"Erro ao processar livro '{file_name}': {e}", exc_info=True)
                    if is_batch and user_id in batch_trackers:
                        batch_trackers[user_id]["processed"] += 1
                        batch_trackers[user_id]["fail"] += 1
                        await update_batch_dashboard(user_id)
                    elif status_msg:
                        await safe_edit(status_msg, f"❌ Erro ao enviar <b>{h(clean_display_name)}</b>:\n<code>{h(str(e))}</code>")
                finally:
                    if temp_path and temp_path.exists():
                        try:
                            temp_path.unlink()
                        except Exception:
                            pass

                    if is_batch and user_id in batch_trackers:
                        if batch_trackers[user_id]["processed"] >= batch_trackers[user_id]["total"]:
                            await update_batch_dashboard(user_id, final=True)
                            batch_trackers.pop(user_id, None)

                    await asyncio.sleep(SEND_DELAY if email_was_sent else 0.3)
            finally:
                queue.task_done()
        except Exception as err:
            logging.error(f"Erro inesperado no loop do worker: {err}", exc_info=True)
            await asyncio.sleep(2)

# ==================== INTERFACE INTERATIVA DO BOT ====================

def get_menu_buttons(user_configured: bool):
    if user_configured:
        return [
            [Button.inline("⚙️ Alterar Meus E-mails", data=b"setup_emails")],
            [Button.inline("📚 Minha Biblioteca", data=b"view_library"), Button.inline("❓ Como Usar", data=b"help_info")]
        ]
    else:
        return [
            [Button.inline("⚙️ Configurar Meu Kindle Agora", data=b"setup_emails")],
            [Button.inline("❓ Como Funciona", data=b"help_info")]
        ]

async def show_user_dashboard(event, user_id: int, user_name: str, is_callback: bool = False):
    user = get_user(user_id)
    if user and user.get("kindle_email"):
        total, _ = get_user_books_summary(user_id)
        msg_text = (
            f"📱 <b>Encha Seu Kindle</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Usuário: <b>{h(user_name)}</b>\n"
            f"📥 <b>E-mail do seu Kindle:</b> <code>{h(user['kindle_email'])}</code>\n"
            f"📧 <b>E-mail cadastrado na Amazon:</b> <code>{h(user['sender_email'])}</code>\n"
            f"📚 <b>Livros no seu Kindle:</b> <b>{total} livros</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 <b>Como enviar livros:</b>\n"
            f"• Envie qualquer arquivo <code>.epub</code>, <code>.pdf</code>, <code>.azw3</code> ou <code>.mobi</code>.\n"
            f"• Ou encaminhe um lote de capas (com painel em tempo real).\n"
            f"• Ou envie uma foto ou nome do livro para ver a sinopse e confirmar!"
        )
        buttons = get_menu_buttons(True)
    else:
        msg_text = (
            f"👋 Olá, <b>{h(user_name)}</b>! Bem-vindo ao <b>Encha Seu Kindle</b>!\n\n"
            f"Para começar a enviar livros direto para o seu Kindle sem autorizações manuais por e-mail, "
            f"configure seu e-mail do Kindle abaixo:"
        )
        buttons = get_menu_buttons(False)

    if is_callback:
        await safe_edit(event, msg_text, buttons=buttons)
    else:
        await safe_reply(event, msg_text, buttons=buttons)

@bot_client.on(events.CallbackQuery)
async def handle_callback(event):
    user_id = event.sender_id
    if user_id not in ALLOWED_USER_IDS:
        await event.answer("Acesso restrito.", alert=True)
        return

    data = event.data.decode()

    # Confirmação de envio de busca (normal ou forçada)
    if data.startswith(("conf_", "forceconf_")):
        force_send = data.startswith("forceconf_")
        parts = data.split("_")
        session_id = parts[1]
        idx = int(parts[2])

        session = search_sessions.get(session_id)
        if not session or idx >= len(session["results"]):
            await event.answer("Sessão expirada.", alert=True)
            await safe_edit(event, "⚠️ Sessão de busca expirada. Digite o nome do livro novamente.")
            return

        item = session["results"][idx]
        target_msg = item["doc_msg"]
        file_name = item["title"]
        clean_name = beautify_filename(file_name)

        user = get_user(user_id)
        if not user or not user.get("kindle_email"):
            await event.answer("Configure seu Kindle primeiro.", alert=True)
            await safe_edit(event, "❌ Configure seu Kindle primeiro com /start.")
            return

        if not force_send:
            is_dup, matched = check_duplicate(user_id, clean_name)
            if is_dup:
                await event.answer("Livro já consta na biblioteca!")
                btns = [
                    [Button.inline("🚀 Enviar Mesmo Assim", data=f"forceconf_{session_id}_{idx}".encode())],
                    [Button.inline("❌ Cancelar", data=f"cancel_{session_id}".encode())]
                ]
                await safe_edit(
                    event,
                    f"⚠️ <b>{h(clean_name)}</b> já consta como enviado para o seu Kindle (<code>{h(matched)}</code>)!\n\n"
                    f"Deseja forçar o reenvio mesmo assim?",
                    buttons=btns
                )
                return

        search_sessions.pop(session_id, None)
        await event.answer("Iniciando envio para o Kindle...")
        await safe_edit(event, f"⏳ Baixando e enviando <b>{h(clean_name)}</b> para o seu Kindle...")

        safe_local = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', clean_name)
        temp_path = BASE_DIR / f"temp_{int(time.time())}_{uuid.uuid4().hex[:4]}_{safe_local}"
        try:
            downloader = item.get("downloader", user_client)
            await downloader.download_media(target_msg, file=str(temp_path))

            temp_converted, clean_name = await asyncio.to_thread(convert_to_epub_if_needed, temp_path, clean_name)
            if temp_converted != temp_path:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:
                    pass
                temp_path = temp_converted

            await asyncio.to_thread(clean_epub_internal_metadata, temp_path)
            await asyncio.to_thread(send_email_to_kindle, temp_path, clean_name, user["kindle_email"])
            add_book_to_db(user_id, clean_name, source="search_confirmed")
            await safe_edit(event, f"✅ <b>{h(clean_name)}</b> enviado com sucesso para o seu Kindle (<code>{h(user['kindle_email'])}</code>)!")
        except Exception as e:
            logging.error(f"Erro no envio confirmado: {e}", exc_info=True)
            await safe_edit(event, f"❌ Erro ao enviar <b>{h(clean_name)}</b>:\n<code>{h(str(e))}</code>")
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
        return

    # Navegação entre resultados
    elif data.startswith("next_"):
        session_id = data.replace("next_", "")
        session = search_sessions.get(session_id)
        if session and session["current_index"] < len(session["results"]) - 1:
            session["current_index"] += 1
            text, btns = render_search_card(session)
            await safe_edit(event, text, buttons=btns)
        await event.answer()

    elif data.startswith("prev_"):
        session_id = data.replace("prev_", "")
        session = search_sessions.get(session_id)
        if session and session["current_index"] > 0:
            session["current_index"] -= 1
            text, btns = render_search_card(session)
            await safe_edit(event, text, buttons=btns)
        await event.answer()

    # Alternar para Fonte Secundária
    elif data.startswith("alt_"):
        session_id = data.replace("alt_", "")
        session = search_sessions.get(session_id)
        if not session:
            await event.answer("Sessão expirada.", alert=True)
            return

        await event.answer("Consultando fonte secundária...")
        await safe_edit(event, f"🔍 Buscando <b>{h(session['query'])}</b> na fonte secundária...")
        alt_results = await search_secondary_bot(session["query"])
        if not alt_results:
            await safe_edit(
                event,
                f"❌ Não encontrei resultados na fonte secundária para <b>{h(session['query'])}</b>.",
                buttons=[[Button.inline("🔙 Voltar aos Resultados", data=f"canal_{session_id}".encode())]]
            )
            return

        if "channel_results" not in session:
            session["channel_results"] = session["results"]
        session["results"] = alt_results
        session["current_index"] = 0
        text, btns = render_search_card(session)
        await safe_edit(event, text, buttons=btns)

    elif data.startswith("canal_"):
        session_id = data.replace("canal_", "")
        session = search_sessions.get(session_id)
        if session and "channel_results" in session:
            session["results"] = session["channel_results"]
            session["current_index"] = 0
            text, btns = render_search_card(session)
            await safe_edit(event, text, buttons=btns)
        await event.answer()

    elif data.startswith("cancel_"):
        session_id = data.replace("cancel_", "")
        search_sessions.pop(session_id, None)
        await safe_edit(event, "❌ Busca cancelada.")
        await event.answer()

    # Menu Callbacks
    elif data == "setup_emails":
        user_states[user_id] = "WAITING_KINDLE_EMAIL"
        temp_configs[user_id] = {}
        await safe_edit(
            event,
            "⚙️ <b>Configuração do Encha Seu Kindle</b> (Passo 1 de 2):\n\n"
            "Digite o seu <b>e-mail do Kindle</b> para receber os livros (ex: <code>seunome@kindle.com</code>):",
            buttons=[[Button.inline("❌ Cancelar", data=b"cancel_setup")]]
        )
        await event.answer()

    elif data == "cancel_setup":
        user_states.pop(user_id, None)
        temp_configs.pop(user_id, None)
        user = await event.get_sender()
        name = getattr(user, "first_name", None) or "Leitor"
        await show_user_dashboard(event, user_id, name, is_callback=True)
        await event.answer("Cancelado.")

    elif data == "view_library":
        total, recent = get_user_books_summary(user_id)
        if total == 0:
            text = "📚 Sua biblioteca ainda não tem livros enviados por este bot."
        else:
            text = f"📚 <b>Sua Biblioteca no Kindle:</b>\nTotal: <b>{total} livros enviados</b>\n\nÚltimos livros enviados:\n"
            for r in recent:
                text += f"• <code>{h(r)}</code>\n"
        await safe_edit(event, text, buttons=[[Button.inline("🔙 Voltar ao Menu", data=b"back_menu")]])
        await event.answer()

    elif data == "help_info":
        await safe_edit(
            event,
            "❓ <b>Como funciona o envio para o Kindle:</b>\n\n"
            "1. <b>Adicione nosso e-mail na Amazon:</b>\n"
            f"👉 <code>{h(GMAIL_USER)}</code> em <i>amazon.com.br/mycd &gt; Preferências &gt; Documentos pessoais</i>.\n\n"
            "2. <b>Busca Inteligente:</b>\n"
            "Digite o nome de qualquer livro ou envie a foto da capa. Ele mostrará a sinopse real, opção de ver outros volumes ou alternar entre as fontes de acervo!\n\n"
            "3. <b>Conversão Automática:</b>\n"
            "Arquivos <code>.azw3</code> e <code>.mobi</code> são convertidos automaticamente para <code>.epub</code>.\n\n"
            "4. <b>Envio em Lote de Capas:</b>\n"
            "Encaminhe várias capas juntas. Ele processará uma por uma mostrando a barra de progresso em tempo real.",
            buttons=[[Button.inline("🔙 Voltar ao Menu", data=b"back_menu")]]
        )
        await event.answer()

    elif data == "back_menu":
        user = await event.get_sender()
        name = getattr(user, "first_name", None) or "Leitor"
        await show_user_dashboard(event, user_id, name, is_callback=True)
        await event.answer()

async def process_batch_after_delay(user_id: int):
    await asyncio.sleep(2.0)
    items = batch_incoming.pop(user_id, [])
    if not items:
        return

    user = get_user(user_id)
    if not user or not user.get("kindle_email"):
        first_msg = items[0][4]
        await safe_reply(first_msg, "❌ Configure seu Kindle primeiro com /start.")
        return

    is_batch = len(items) > 1

    if is_batch:
        status_msg = await bot_client.send_message(
            user_id,
            f"📦 <b>Lote de {len(items)} livros recebido!</b>\nIniciando processamento com barra de progresso...",
            parse_mode="html"
        )
        batch_trackers[user_id] = {
            "status_msg": status_msg,
            "total": len(items),
            "processed": 0,
            "success": 0,
            "dup": 0,
            "fail": 0,
            "current_title": "Iniciando...",
            "last_edit_ts": 0
        }
        await update_batch_dashboard(user_id, final=True)
        for item in items:
            channel, orig_msg_id, direct_doc, file_name, msg_obj = item
            await queue.put((user_id, msg_obj, None, channel, orig_msg_id, direct_doc, file_name, True))
    else:
        channel, orig_msg_id, direct_doc, file_name, msg_obj = items[0]
        label = "Arquivo recebido" if direct_doc else "Capa recebida"
        ack_msg = await safe_reply(msg_obj, f"📥 <b>{label}!</b> Localizando e preparando envio para o Kindle...")
        await queue.put((user_id, msg_obj, ack_msg, channel, orig_msg_id, direct_doc, file_name, False))

@bot_client.on(events.NewMessage)
async def handle_bot_message(event):
    sender = await event.get_sender()
    user_id = event.sender_id
    user_name = getattr(sender, "first_name", None) or "Leitor"

    if user_id not in ALLOWED_USER_IDS:
        await safe_reply(event, "⛔ Acesso restrito. Este bot é privado.")
        return

    msg = event.message

    # Tratamento de máquina de estados de configuração
    state = user_states.get(user_id)
    if state == "WAITING_KINDLE_EMAIL" and msg.text and not msg.text.startswith("/"):
        text = msg.text.strip()
        if "@" not in text:
            await safe_reply(msg, "⚠️ Digite um e-mail válido (ex: <code>seunome@kindle.com</code>):")
            return
        temp_configs[user_id]["kindle_email"] = text
        user_states[user_id] = "WAITING_SENDER_EMAIL"
        await safe_reply(msg, f"✅ Kindle: <code>{h(text)}</code>\n\nAgora digite o seu e-mail pessoal cadastrado na Amazon (ex: <code>voce@gmail.com</code>):")
        return

    elif state == "WAITING_SENDER_EMAIL" and msg.text and not msg.text.startswith("/"):
        text = msg.text.strip()
        if "@" not in text:
            await safe_reply(msg, "⚠️ Digite um e-mail válido (ex: <code>voce@gmail.com</code>):")
            return
        kindle_email = temp_configs[user_id]["kindle_email"]
        save_user(user_id, user_name, kindle_email, text)
        user_states.pop(user_id, None)
        temp_configs.pop(user_id, None)
        await safe_reply(
            msg,
            f"🎉 <b>Configuração Concluída!</b>\n📱 Kindle: <code>{h(kindle_email)}</code>\n📧 Remetente: <code>{h(text)}</code>\n\n"
            f"⚠️ <b>Importante</b>: Na sua conta Amazon (<i>amazon.com.br/mycd</i>), adicione <code>{h(GMAIL_USER)}</code> na lista de e-mails aprovados!",
            buttons=get_menu_buttons(True)
        )
        return

    # Comandos
    if msg.text and msg.text.strip().lower() in ("/start", "/menu", "/config"):
        user_states.pop(user_id, None)
        await show_user_dashboard(event, user_id, user_name, is_callback=False)
        return

    if msg.text and msg.text.strip().lower() in ("/biblioteca", "/livros"):
        total, recent = get_user_books_summary(user_id)
        text = f"📚 <b>Sua Biblioteca no Kindle:</b> {total} livros\n" + "\n".join([f"• <code>{h(r)}</code>" for r in recent])
        await safe_reply(msg, text)
        return

    if msg.text and msg.text.strip().lower() in ("/reenviar_lote", "/reenviar_pendentes", "/resgatar"):
        if not ADMIN_USER_ID or user_id != ADMIN_USER_ID:
            await safe_reply(msg, "Comando exclusivo para o administrador.")
            return

        raw_posts = os.getenv("RESEND_BATCH_POSTS", "")
        batch_posts = [int(x.strip()) for x in raw_posts.split(",") if x.strip().isdigit()]
        if not batch_posts:
            await safe_reply(msg, "ℹ️ Nenhum lote pendente configurado em <code>RESEND_BATCH_POSTS</code>.")
            return

        status_msg = await bot_client.send_message(
            user_id,
            f"📦 <b>Resgatando lote de {len(batch_posts)} livros!</b>\nIniciando envio com barra de progresso...",
            parse_mode="html"
        )
        batch_trackers[user_id] = {
            "status_msg": status_msg,
            "total": len(batch_posts),
            "processed": 0,
            "success": 0,
            "dup": 0,
            "fail": 0,
            "current_title": "Iniciando...",
            "last_edit_ts": 0
        }
        await update_batch_dashboard(user_id, final=True)

        try:
            ch = await user_client.get_entity(SOURCE_CHANNEL)
            for pid in batch_posts:
                await queue.put((user_id, msg, None, ch.id, pid, False, f"Livro #{pid}", True))
        except Exception as e:
            await safe_edit(status_msg, f"❌ Erro ao enfileirar lote: <code>{h(e)}</code>")
        return

    # Caso 1: Envio de Documento Direto (.epub / .pdf / .azw3 / .mobi)
    if msg.document and msg.file and msg.file.name:
        fname = msg.file.name
        if fname.lower().endswith(SUPPORTED_EXTENSIONS):
            if user_id not in batch_incoming:
                batch_incoming[user_id] = []
                asyncio.create_task(process_batch_after_delay(user_id))
            batch_incoming[user_id].append((None, None, True, fname, msg))
            return

    # Caso 2: Foto encaminhada de canal com post ID
    if msg.photo and msg.fwd_from and msg.fwd_from.channel_post:
        fwd = msg.fwd_from
        orig_msg_id = fwd.channel_post
        channel = fwd.from_id.channel_id if isinstance(fwd.from_id, PeerChannel) else fwd.chat

        if channel and orig_msg_id:
            if user_id not in batch_incoming:
                batch_incoming[user_id] = []
                asyncio.create_task(process_batch_after_delay(user_id))

            book_label = f"Livro #{orig_msg_id}"
            batch_incoming[user_id].append((channel, orig_msg_id, False, book_label, msg))
            return

    # Caso 3: Foto avulsa de capa ou imagem enviada como arquivo (Busca inteligente com OCR Geométrico)
    is_image_doc = (msg.document and msg.file and msg.file.name and msg.file.name.lower().endswith(IMAGE_EXTENSIONS))
    if msg.photo or is_image_doc:
        status_search = await safe_reply(msg, "🔍 Analisando capa com OCR inteligente...")
        temp_img = BASE_DIR / f"ocr_{int(time.time())}_{uuid.uuid4().hex[:4]}.jpg"
        try:
            await bot_client.download_media(msg, file=str(temp_img))
            cover_queries = await asyncio.to_thread(extract_cover_queries, temp_img)

            if not cover_queries:
                await safe_edit(status_search, "⚠️ Não consegui ler o título na capa. Envie o nome do livro em texto para eu buscar.")
                return

            primary_query = cover_queries[0]
            full_ocr_context = " ".join(cover_queries[:2])
            await safe_edit(status_search, f"🔍 Título detectado: <b>{h(primary_query)}</b>\nBuscando nos acervos configurados...")

            results = []
            for q in cover_queries:
                cand = await search_primary_channel(q, scoring_query=full_ocr_context)
                good_cand = [c for c in cand if c["score"] > 0]
                if good_cand:
                    results = good_cand
                    break
                if cand and not results:
                    results = cand

            if not results:
                for q in cover_queries:
                    results = await search_secondary_bot(q)
                    if results:
                        break

            if not results:
                await safe_edit(status_search, f"❌ Não encontrei o livro <b>{h(primary_query)}</b> nos acervos configurados.")
                return

            sess_id = str(uuid.uuid4())[:8]
            search_sessions[sess_id] = {
                "id": sess_id,
                "user_id": user_id,
                "query": primary_query,
                "results": results,
                "current_index": 0
            }
            text, btns = render_search_card(search_sessions[sess_id])
            await safe_edit(status_search, text, buttons=btns)

        except Exception as e:
            logging.error(f"Erro na busca inteligente por capa: {e}", exc_info=True)
            await safe_edit(status_search, f"❌ Erro ao buscar: <code>{h(e)}</code>")
        finally:
            if temp_img.exists():
                try:
                    temp_img.unlink()
                except Exception:
                    pass
        return

    # Caso 4: Busca direta por texto
    if msg.text and not msg.text.startswith(("/", "⏳", "📥", "📤", "✅", "❌", "⚠️", "📚", "📦", "🎉", "👋", "📱")):
        query = msg.text.strip()
        status_search = await safe_reply(msg, f"🔍 Buscando <b>{h(query)}</b>...")
        try:
            results = await search_primary_channel(query)
            if not results:
                await safe_edit(status_search, f"🔍 Não encontrado no canal principal. Buscando <b>{h(query)}</b> na fonte secundária...")
                results = await search_secondary_bot(query)

            if not results:
                await safe_edit(status_search, f"❌ Não encontrei o livro <b>{h(query)}</b> nos acervos configurados.")
                return

            sess_id = str(uuid.uuid4())[:8]
            search_sessions[sess_id] = {
                "id": sess_id,
                "user_id": user_id,
                "query": query,
                "results": results,
                "current_index": 0
            }
            text, btns = render_search_card(search_sessions[sess_id])
            await safe_edit(status_search, text, buttons=btns)
        except Exception as e:
            logging.error(f"Erro na busca por texto: {e}", exc_info=True)
            await safe_edit(status_search, f"❌ Erro ao buscar: <code>{h(e)}</code>")
        return

# ==================== MONITOR DE MENSAGENS SALVAS (ADMIN) ====================

@user_client.on(events.NewMessage(chats="me"))
async def handle_admin_saved_messages(event):
    if not ADMIN_USER_ID:
        return
    msg = event.message
    if msg.text and msg.text.startswith(("⏳", "📥", "📤", "✅", "❌", "⚠️", "📚", "📱", "📦", "🎉")):
        return

    admin_id = ADMIN_USER_ID

    if msg.document and msg.file and msg.file.name:
        fname = msg.file.name
        if fname.lower().endswith(SUPPORTED_EXTENSIONS):
            clean_name = beautify_filename(fname)
            is_dup, matched_title = check_duplicate(admin_id, clean_name)
            if is_dup:
                await safe_reply(msg, f"⚠️ <b>{h(clean_name)}</b> já consta no seu Kindle (<code>{h(matched_title)}</code>)!")
                return
            ack = await safe_reply(msg, f"📥 <b>{h(clean_name)}</b> na fila de envio!")
            await queue.put((admin_id, msg, ack, None, None, True, fname, False))
            return

    if msg.photo and msg.fwd_from and msg.fwd_from.channel_post:
        fwd = msg.fwd_from
        orig_msg_id = fwd.channel_post
        channel = fwd.from_id.channel_id if isinstance(fwd.from_id, PeerChannel) else fwd.chat

        if channel and orig_msg_id:
            book_label = f"Livro #{orig_msg_id}"
            ack = await safe_reply(msg, "📥 Capa recebida! Buscando no canal...")
            await queue.put((admin_id, msg, ack, channel, orig_msg_id, False, book_label, False))
            return

# ==================== EXECUÇÃO PRINCIPAL DUAL ====================

async def main():
    init_db()

    await user_client.start()
    me_user = await user_client.get_me()
    logging.info(f"User Client iniciado: {me_user.first_name} (ID: {me_user.id})")

    await bot_client.start(bot_token=BOT_TOKEN)
    me_bot = await bot_client.get_me()
    logging.info(f"Bot Client iniciado: {me_bot.first_name} (@{me_bot.username})")

    asyncio.create_task(worker())
    logging.info("Encha Seu Kindle v7 (Blindado + HTML Safe + Anti-Dup v2 + Auto-EPUB) ativo!")

    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected()
    )

if __name__ == "__main__":
    asyncio.run(main())
