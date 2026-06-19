import os
import json
import re
import time
import logging
import asyncio
import io
import base64
import random

import PyPDF2
import httpx
from bs4 import BeautifulSoup
from youtube_transcript_api import YouTubeTranscriptApi

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)
from groq import Groq

# ======================
# LOGGING
# ======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ======================
# CONFIG
# ======================
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY       = os.getenv("GROQ_API_KEY")
OBG_CHANNEL_ID     = os.getenv("OBG_CHANNEL_ID")      # OBG questions go here
SURGERY_CHANNEL_ID = os.getenv("SURGERY_CHANNEL_ID")  # Surgery questions go here
ADMIN_ID           = 723919716
INTERVAL           = 900          # 15 minutes

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN missing")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY missing")
if not OBG_CHANNEL_ID:
    raise ValueError("OBG_CHANNEL_ID missing")
if not SURGERY_CHANNEL_ID:
    raise ValueError("SURGERY_CHANNEL_ID missing")

groq_client = Groq(api_key=GROQ_API_KEY)

# ======================
# PERSISTENCE HELPERS
# ======================
def load_json(filename, default):
    try:
        with open(filename, "r") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(filename, data):
    try:
        with open(filename, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"save_json error ({filename}): {e}")

# ======================
# STATE  (loaded at startup, kept in memory, mirrored to disk after every write)
# ======================
used_topics       = load_json("used_topics.json",       [])
used_questions    = load_json("used_questions.json",    [])
last_subject      = load_json("last_subject.json",      {"subject": "surgery"})
used_obg_chunks   = load_json("used_obg_chunks.json",   [])
used_surg_chunks  = load_json("used_surg_chunks.json",  [])

# pending_questions: short_id (≤8 chars) → full data dict
pending_questions: dict = load_json("pending_questions.json", {})

def save_pending():
    save_json("pending_questions.json", pending_questions)

OBG_CHUNKS:    list[str] = []
SURGERY_CHUNKS: list[str] = []

# ======================
# SHORT ID  (≤8 chars — fits safely inside 64-byte callback_data)
# ======================
_id_counter = 0

def make_short_id() -> str:
    """Return a collision-resistant 6-char alphanumeric ID."""
    global _id_counter
    _id_counter += 1
    n = (int(time.time()) & 0xFFFF) ^ (_id_counter << 4) ^ random.randint(0, 15)
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    result = ""
    while n:
        result = chars[n % 36] + result
        n //= 36
    return (result or "0").zfill(6)[:8]

# ======================
# PDF LOADER
# ======================
def load_pdf_chunks(filepath: str, chunk_size: int = 3000) -> list[str]:
    chunks = []
    try:
        with open(filepath, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            current = ""
            for page in reader.pages:
                text = page.extract_text() or ""
                current += text + "\n"
                while len(current) >= chunk_size:
                    chunks.append(current[:chunk_size])
                    current = current[chunk_size:]
            if current.strip():
                chunks.append(current)
        logger.info(f"Loaded {len(chunks)} chunks from {filepath}")
    except Exception as e:
        logger.error(f"PDF load error: {e}")
    return chunks

# ======================
# TOPIC POOLS
# ======================
OBG_TOPICS = [
    # Obstetrics
    "Normal labour stages and management",
    "Preeclampsia and eclampsia management",
    "Antepartum haemorrhage placenta praevia vs abruption",
    "Postpartum haemorrhage causes and management",
    "Gestational diabetes mellitus diagnosis and management",
    "Preterm labour tocolysis and antenatal corticosteroids",
    "PROM and PPROM management",
    "Ectopic pregnancy diagnosis and management",
    "Molar pregnancy complete vs partial hydatidiform mole",
    "Rh isoimmunisation and Coombs test",
    "Fetal distress CTG interpretation",
    "Shoulder dystocia manoeuvres McRoberts",
    "Cord prolapse emergency management",
    "Breech presentation types and delivery",
    "Multiple pregnancy twin complications",
    "Polyhydramnios and oligohydramnios causes",
    "IUGR causes diagnosis and management",
    "Amniotic fluid embolism",
    "Anaemia in pregnancy iron deficiency vs megaloblastic",
    "Hypertension in pregnancy classification",
    "Puerperal sepsis common organisms and treatment",
    "Obstetric fistula causes and classification",
    "Deep vein thrombosis and pulmonary embolism in pregnancy",
    "Thyroid disorders in pregnancy",
    "Cardiac disease in pregnancy NYHA classification",
    "Forceps vs vacuum delivery indications",
    "Caesarean section indications and complications",
    "Placenta accreta increta percreta management",
    "HELLP syndrome diagnosis and management",
    "Bishop score and cervical ripening",
    # Gynaecology
    "Menstrual cycle physiology and disorders",
    "Polycystic ovarian syndrome PCOS diagnosis Rotterdam criteria",
    "Primary and secondary amenorrhoea causes",
    "Endometriosis pathophysiology and staging",
    "Uterine fibroids classification and management",
    "Cervical cancer screening colposcopy and FIGO staging",
    "Ovarian cancer CA-125 and FIGO staging",
    "Endometrial cancer risk factors and management",
    "Vaginal discharge causes and treatment",
    "Sexually transmitted infections gonorrhoea chlamydia syphilis",
    "Pelvic inflammatory disease diagnosis Fitz-Hugh-Curtis syndrome",
    "Infertility causes male and female workup",
    "Assisted reproductive techniques IVF ICSI",
    "Contraception OCP IUCD barrier methods",
    "Emergency contraception mechanisms",
    "Menopause HRT indications and contraindications",
    "Lichen sclerosus and vulval disorders",
    "Prolapse uterine vaginal anterior posterior wall",
    "Stress incontinence vs urge incontinence management",
    "Bartholin cyst and abscess management",
]

SURGERY_TOPICS = [
    # General Surgery
    "Acute appendicitis Alvarado score and management",
    "Intestinal obstruction small vs large bowel",
    "Colorectal cancer Duke's and TNM staging",
    "Anal fissure fistula and haemorrhoids management",
    "Hernia inguinal direct vs indirect femoral umbilical",
    "Acute cholecystitis and biliary colic management",
    "Choledocholithiasis ERCP and management",
    "Acute pancreatitis Ranson Balthazar criteria",
    "Chronic pancreatitis complications",
    "Peptic ulcer disease complications perforation bleeding",
    "Gastric cancer Lauren classification and staging",
    "Esophageal cancer squamous vs adenocarcinoma",
    "GERD and hiatus hernia types",
    "Meckel's diverticulum rule of 2s",
    "Intussusception in children and adults",
    "Volvulus sigmoid vs caecal management",
    "Inflammatory bowel disease Crohn's vs UC surgical management",
    "Diverticular disease and diverticulitis",
    "Pilonidal sinus and abscess",
    "Thyroid swelling goitre and malignancy workup",
    "Thyroid cancer papillary follicular medullary anaplastic",
    "Hyperparathyroidism causes and management",
    "Adrenal tumours phaeochromocytoma Cushing Conn",
    "Breast cancer staging management and reconstruction",
    "Breast abscess and mastitis",
    "Fibroadenoma and ANDI spectrum",
    # Vascular Surgery
    "Peripheral arterial disease ABI and management",
    "Aortic aneurysm rupture and repair",
    "Varicose veins CEAP classification and treatment",
    "Acute limb ischaemia 6 Ps and management",
    "Carotid artery stenosis endarterectomy indications",
    "Diabetic foot and Charcot joint",
    # Trauma and Burns
    "ATLS primary survey ABCDE approach",
    "Burns rule of nines and Parkland formula",
    "Tension pneumothorax haemothorax management",
    "Splenic injury grades and FAST scan",
    "Liver trauma and damage control surgery",
    "Urological trauma bladder urethra kidney",
    # Urology
    "Renal calculi composition and management ESWL URS PCNL",
    "Benign prostatic hyperplasia IPSS and treatment",
    "Prostate cancer PSA Gleason grading",
    "Bladder cancer transitional cell carcinoma staging",
    "Testicular torsion vs epididymo-orchitis",
    "Renal cell carcinoma classic triad and management",
    # Orthopaedics (high-yield surgery overlap)
    "Colles fracture vs Smith fracture management",
    "Neck of femur fractures Garden classification",
    "Compartment syndrome causes and fasciotomy",
    "Septic arthritis vs osteomyelitis diagnosis and treatment",
]

# ======================
# HELPERS
# ======================
def escape_md(text: str) -> str:
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text

def extract_json(text: str):
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group())
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            result = json.loads(m.group())
            if isinstance(result, list) and result:
                return result[0]
    except Exception as e:
        logger.error(f"JSON parse error: {e}")
    return None

def make_question_hash(q: str) -> str:
    return q[:80].strip().lower()

def is_question_used(q: str) -> bool:
    return make_question_hash(q) in used_questions

def mark_question_used(q: str):
    h = make_question_hash(q)
    used_questions.append(h)
    if len(used_questions) > 500:
        used_questions.pop(0)
    save_json("used_questions.json", used_questions)

def clean_options(options: list) -> list:
    """Strip any A) / A. prefixes the model may have added inside option text."""
    cleaned = []
    for opt in options:
        opt = opt.strip()
        if len(opt) > 2 and opt[1] in (")", ".") and opt[0].isalpha():
            opt = opt[2:].strip()
        cleaned.append(opt)
    return cleaned

# ======================
# GROQ WRAPPER  (non-blocking retry)
# ======================
async def safe_groq_call(**kwargs):
    for attempt in range(4):
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: groq_client.chat.completions.create(**kwargs)
            )
            return response
        except Exception as e:
            err = str(e).lower()
            if "rate_limit" in err or "429" in err:
                wait = 30 * (attempt + 1)
                logger.warning(f"Rate limit (attempt {attempt+1}), waiting {wait}s…")
                await asyncio.sleep(wait)
            elif any(x in err for x in ("timeout", "connection", "503", "502", "overload")):
                wait = 15 * (attempt + 1)
                logger.warning(f"Transient error (attempt {attempt+1}): {e} — retrying in {wait}s…")
                await asyncio.sleep(wait)
            else:
                logger.error(f"Groq fatal error: {e}")
                return None
    logger.error("safe_groq_call: all attempts exhausted")
    return None

# ======================
# URL / YOUTUBE
# ======================
def extract_youtube_id(url: str):
    for pat in [r"youtube\.com/watch\?v=([^&]+)",
                r"youtu\.be/([^?]+)",
                r"youtube\.com/shorts/([^?]+)"]:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None

async def get_youtube_transcript(video_id: str):
    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
        return " ".join(t["text"] for t in transcript_list)[:4000]
    except Exception as e:
        logger.error(f"YouTube transcript error: {e}")
        return None

async def fetch_url_content(url: str):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=15) as hc:
            r = await hc.get(url, headers=headers, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text[:4000]
    except Exception as e:
        logger.error(f"URL fetch error: {e}")
        return None

# ======================
# SUBJECT ROTATION
# ======================
def get_next_subject() -> str:
    rotation = ["obg", "surgery", "obg_pdf", "surgery_pdf"]
    current  = last_subject.get("subject", "surgery")
    try:
        nxt = rotation[(rotation.index(current) + 1) % len(rotation)]
    except ValueError:
        nxt = "obg"
    last_subject["subject"] = nxt
    save_json("last_subject.json", last_subject)
    return nxt

# ======================
# TOPIC PICKER
# ======================
async def generate_topic(book: str = None) -> str:
    if book == "obg":
        pool = OBG_TOPICS
    elif book == "surgery":
        pool = SURGERY_TOPICS
    else:
        pool = OBG_TOPICS + SURGERY_TOPICS

    available = [t for t in pool if t not in used_topics]
    if not available:
        used_topics[:] = [t for t in used_topics if t not in pool]
        save_json("used_topics.json", used_topics)
        available = pool[:]

    topic = random.choice(available)
    used_topics.append(topic)
    if len(used_topics) > 300:
        used_topics.pop(0)
    save_json("used_topics.json", used_topics)
    return topic

# ======================
# PDF CHUNK PICKERS
# ======================
async def get_obg_chunk() -> str | None:
    if not OBG_CHUNKS:
        return None
    available = [i for i in range(len(OBG_CHUNKS)) if i not in used_obg_chunks]
    if not available:
        used_obg_chunks.clear()
        save_json("used_obg_chunks.json", used_obg_chunks)
        available = list(range(len(OBG_CHUNKS)))
    idx = random.choice(available)
    used_obg_chunks.append(idx)
    if len(used_obg_chunks) > 300:
        used_obg_chunks.pop(0)
    save_json("used_obg_chunks.json", used_obg_chunks)
    return OBG_CHUNKS[idx]

async def get_surgery_chunk() -> str | None:
    if not SURGERY_CHUNKS:
        return None
    available = [i for i in range(len(SURGERY_CHUNKS)) if i not in used_surg_chunks]
    if not available:
        used_surg_chunks.clear()
        save_json("used_surg_chunks.json", used_surg_chunks)
        available = list(range(len(SURGERY_CHUNKS)))
    idx = random.choice(available)
    used_surg_chunks.append(idx)
    if len(used_surg_chunks) > 300:
        used_surg_chunks.pop(0)
    save_json("used_surg_chunks.json", used_surg_chunks)
    return SURGERY_CHUNKS[idx]

# ======================
# MCQ GENERATION
# ======================
async def generate_mcq(content: str, book_context: str = None, retry: int = 0,
                       auto_select_topic: bool = False):
    ctx_map = {
        "obg":          "Based on DC Dutta's Textbook of Obstetrics and Gynaecology. Reference obstetric emergencies, gynaecological oncology, and clinical management protocols.",
        "surgery":      "Based on Bailey & Love's Short Practice of Surgery and SRB's Manual of Surgery. Reference surgical anatomy, operative techniques, and clinical decision-making.",
        "obg_pdf":      "Based on NEET PG OBG high-yield content. Focus on exam-relevant obstetric and gynaecological topics.",
        "surgery_pdf":  "Based on NEET PG Surgery high-yield content. Focus on exam-relevant surgical topics including General, Vascular, and Urology.",
    }
    source_context = ctx_map.get(book_context, "Based on standard NEET PG / USMLE medical curriculum covering OBG and Surgery.")

    if auto_select_topic:
        subject_for_topic = book_context if book_context in ("obg", "surgery") else None
        topic = await generate_topic(book=subject_for_topic)
        mcq_content = topic
    else:
        topic = None
        mcq_content = content

    prompt = (
        "You are a NEET PG / USMLE / FMGE expert examiner specialising in OBG and Surgery.\n\n"
        + source_context + "\n\n"
        "Generate ONE high-yield clinical MCQ based on: " + mcq_content + "\n\n"
        "Rules:\n"
        "- Clinical vignette style with patient scenario\n"
        "- 4 options labeled ONLY as A, B, C, D (no punctuation after letter)\n"
        "- One definitively correct answer\n"
        "- No ambiguous or trick questions\n"
        "- Explanation must cite mechanism / guideline clearly\n"
        "- Explain why each wrong option is incorrect\n\n"
        "Return ONLY this JSON (no markdown, no preamble):\n"
        '{"question": "A patient presents with...", '
        '"options": ["Option text only", "Option text only", "Option text only", "Option text only"], '
        '"answer_index": 0, '
        '"explanation": "Correct: A because... B is wrong because..."}\n\n'
        "CRITICAL: options array must contain plain text strings ONLY. "
        "Do NOT include A) B) C) D) or A. B. C. D. prefixes inside the options array."
    )

    try:
        response = await safe_groq_call(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1024,
        )
        if not response:
            return None

        raw = response.choices[0].message.content
        mcq = extract_json(raw)
        if not mcq:
            logger.error(f"Could not parse MCQ JSON. Raw:\n{raw[:300]}")
            return None

        if not all(k in mcq for k in ("question", "options", "answer_index", "explanation")):
            logger.error("MCQ missing required keys")
            return None
        if len(mcq["options"]) != 4:
            logger.error(f"MCQ has {len(mcq['options'])} options, expected 4")
            return None
        if not (0 <= int(mcq["answer_index"]) <= 3):
            logger.error("answer_index out of range")
            return None

        mcq["options"] = clean_options(mcq["options"])
        mcq["answer_index"] = int(mcq["answer_index"])

        if is_question_used(mcq["question"]):
            logger.warning("Duplicate question, retrying…")
            if retry < 2:
                await asyncio.sleep(5)
                return await generate_mcq(content, book_context, retry=retry + 1,
                                          auto_select_topic=auto_select_topic)
            logger.warning("Still duplicate after retries — using anyway")

        mark_question_used(mcq["question"])
        if auto_select_topic and topic:
            mcq["_selected_topic"] = topic
        return mcq

    except Exception as e:
        logger.error(f"MCQ generation error: {e}")
        return None

# ======================
# REPHRASE FORWARDED MCQ
# ======================
async def rephrase_forwarded_mcq(text: str):
    prompt = (
        "You are a medical MCQ expert specialising in OBG and Surgery.\n\n"
        "Here is a forwarded MCQ:\n\n" + text + "\n\n"
        "Task:\n"
        "1. Slightly rephrase the question stem (keep same meaning)\n"
        "2. Keep the same options\n"
        "3. Identify the correct answer\n"
        "4. Add a detailed explanation\n\n"
        "Return ONLY this JSON (no markdown, no preamble):\n"
        '{"question": "rephrased question...", '
        '"options": ["option1", "option2", "option3", "option4"], '
        '"answer_index": 0, '
        '"explanation": "Correct: A because... B is wrong because..."}\n\n'
        "CRITICAL: options must be plain text only, no A) or A. prefix inside the options array."
    )
    try:
        response = await safe_groq_call(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1024,
        )
        if not response:
            return None
        mcq = extract_json(response.choices[0].message.content)
        if mcq:
            mcq["options"] = clean_options(mcq.get("options", []))
            mcq["answer_index"] = int(mcq.get("answer_index", 0))
        return mcq
    except Exception as e:
        logger.error(f"Rephrase error: {e}")
        return None

# ======================
# IMAGE → MCQ
# ======================
async def generate_mcq_from_image(image_bytes: bytes, mime_type: str = "image/jpeg"):
    try:
        b64 = base64.b64encode(image_bytes).decode()
        vision = await safe_groq_call(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
                    {"type": "text",
                     "text": "Extract all medical/surgical/OBG text from this image. Return raw text only."}
                ]
            }],
            temperature=0.1,
        )
        if not vision:
            return None, "Vision model unavailable"
        extracted = vision.choices[0].message.content
        if not extracted or len(extracted.strip()) < 20:
            return None, "Could not extract text from image"
        extracted = extracted[:4000]
        await asyncio.sleep(10)
        mcq = await generate_mcq(extracted)
        return mcq, extracted[:200]
    except Exception as e:
        logger.error(f"Image MCQ error: {e}")
        return None, str(e)

# ======================
# SEND FOR APPROVAL
# ======================
async def send_for_approval(bot, mcq: dict, source: str,
                             topic_content: str = None, book_context: str = None):
    try:
        qid = make_short_id()
        while qid in pending_questions:
            qid = make_short_id()

        pending_questions[qid] = {
            "mcq":           mcq,
            "source":        source,
            "topic_content": topic_content,
            "book_context":  book_context,
        }
        save_pending()

        options_preview = []
        for i, opt in enumerate(mcq["options"]):
            marker = "✅ " if i == mcq["answer_index"] else ""
            options_preview.append(f"{marker}{chr(65+i)}. {opt}")

        explanation_preview = mcq["explanation"][:800]
        channel_label = "🔪 Surgery Channel" if book_context in ("surgery", "surgery_pdf") else "🤰 OBG Channel"
        text = (
            "📋 NEW MCQ FOR APPROVAL\n\n"
            f"📚 Source: {source}\n"
            f"📢 Will post to: {channel_label}\n\n"
            f"{mcq['question']}\n\n"
            + "\n".join(options_preview)
            + f"\n\n💡 Explanation:\n{explanation_preview}"
        )

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve & Post", callback_data=f"ap_{qid}"),
            InlineKeyboardButton("❌ Reject",         callback_data=f"rj_{qid}"),
        ], [
            InlineKeyboardButton("🔄 Regenerate",     callback_data=f"rg_{qid}"),
        ]])

        await bot.send_message(chat_id=ADMIN_ID, text=text, reply_markup=keyboard)
        logger.info(f"MCQ sent for approval (qid={qid}, source={source})")

    except Exception as e:
        logger.error(f"send_for_approval error: {e}")

# ======================
# POST TO CHANNEL
# ======================
def resolve_channel(book_context: str) -> str:
    """Return the correct channel ID based on subject."""
    surgery_contexts = ("surgery", "surgery_pdf")
    if book_context in surgery_contexts:
        return SURGERY_CHANNEL_ID
    return OBG_CHANNEL_ID   # obg, obg_pdf, or unknown → OBG channel

async def post_to_channel(bot, mcq: dict, book_context: str = None):
    channel_id = resolve_channel(book_context)
    options_text = [f"{chr(65+i)}. {opt}" for i, opt in enumerate(mcq["options"])]
    text_msg = mcq["question"] + "\n\n" + "\n".join(options_text)

    try:
        await bot.send_message(chat_id=channel_id, text=text_msg)
        await asyncio.sleep(2)
    except Exception as e:
        logger.error(f"Failed to send question text: {e}")

    try:
        await bot.send_poll(
            chat_id=channel_id,
            question=mcq["question"][:300],
            options=[opt[:100] for opt in mcq["options"]],
            type="quiz",
            correct_option_id=mcq["answer_index"],
            is_anonymous=True,
        )
        await asyncio.sleep(2)
    except Exception as e:
        logger.error(f"Failed to send poll: {e}")

    try:
        spoiler = "💡 *Explanation:*\n\n||" + escape_md(mcq["explanation"]) + "||"
        await bot.send_message(chat_id=channel_id, text=spoiler, parse_mode="MarkdownV2")
    except Exception as e:
        logger.error(f"Failed to send explanation (MarkdownV2): {e}")
        try:
            await bot.send_message(chat_id=channel_id,
                                   text="💡 Explanation:\n\n" + mcq["explanation"])
        except Exception as e2:
            logger.error(f"Fallback explanation failed: {e2}")

# ======================
# SCHEDULED JOB
# ======================
async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        subject = get_next_subject()
        logger.info(f"Scheduled job — subject: {subject}")

        if subject == "obg_pdf":
            chunk = await get_obg_chunk()
            if not chunk:
                logger.warning("No OBG PDF chunks — falling back to topic list")
                mcq = await generate_mcq("", book_context="obg", auto_select_topic=True)
                subject = "obg"
            else:
                mcq = await generate_mcq(chunk, book_context="obg_pdf")
            if not mcq:
                logger.error("Failed to generate OBG PDF MCQ")
                return
            topic = mcq.get("_selected_topic", "OBG")
            await send_for_approval(context.bot, mcq, "NEET PG OBG (PDF)",
                                    topic_content=chunk or topic, book_context="obg_pdf")

        elif subject == "surgery_pdf":
            chunk = await get_surgery_chunk()
            if not chunk:
                logger.warning("No Surgery PDF chunks — falling back to topic list")
                mcq = await generate_mcq("", book_context="surgery", auto_select_topic=True)
                subject = "surgery"
            else:
                mcq = await generate_mcq(chunk, book_context="surgery_pdf")
            if not mcq:
                logger.error("Failed to generate Surgery PDF MCQ")
                return
            topic = mcq.get("_selected_topic", "Surgery")
            await send_for_approval(context.bot, mcq, "NEET PG Surgery (PDF)",
                                    topic_content=chunk or topic, book_context="surgery_pdf")

        else:
            mcq = await generate_mcq("", book_context=subject, auto_select_topic=True)
            if not mcq:
                logger.error("Failed to generate MCQ")
                return
            topic = mcq.get("_selected_topic", subject)
            logger.info(f"Selected topic: {topic}")
            label = "DC Dutta OBG" if subject == "obg" else "Bailey & Love Surgery"
            await send_for_approval(context.bot, mcq, f"{label}: {topic}",
                                    topic_content=topic, book_context=subject)

    except Exception as e:
        logger.error(f"Scheduled job error: {e}")

# ======================
# CALLBACK HANDLER
# ======================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if "_" not in data:
        await query.edit_message_text("❌ Unknown action.")
        return

    prefix, qid = data.split("_", 1)

    # ── APPROVE ──────────────────────────────────────────────
    if prefix == "ap":
        item = pending_questions.get(qid)
        if not item:
            await query.edit_message_text(
                "❌ Question not found.\n"
                "It may have already been approved/rejected, or the bot restarted.\n"
                "Use /postnow to generate a new one."
            )
            return
        await query.edit_message_text("⏳ Posting to channel…")
        book_ctx = item.get("book_context")
        await post_to_channel(context.bot, item["mcq"], book_context=book_ctx)
        pending_questions.pop(qid, None)
        save_pending()
        channel_label = "🔪 Surgery" if book_ctx in ("surgery", "surgery_pdf") else "🤰 OBG"
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"✅ Posted to {channel_label} channel!")

    # ── REJECT ───────────────────────────────────────────────
    elif prefix == "rj":
        pending_questions.pop(qid, None)
        save_pending()
        await query.edit_message_text("❌ Rejected and discarded.")

    # ── REGENERATE ───────────────────────────────────────────
    elif prefix == "rg":
        old_item = pending_questions.get(qid)
        if not old_item:
            await query.edit_message_text(
                "❌ Original question not found.\n"
                "It may have already been regenerated.\n"
                "Use /postnow to generate a fresh MCQ."
            )
            return

        topic_content = old_item.get("topic_content")
        book          = old_item.get("book_context")
        source        = old_item.get("source", "Unknown Source")

        if not topic_content:
            pending_questions.pop(qid, None)
            save_pending()
            await query.edit_message_text(
                "❌ No topic content stored for this question.\n"
                "Use /postnow to generate a fresh MCQ."
            )
            return

        pending_questions.pop(qid, None)
        save_pending()

        await query.edit_message_text(
            f"🔄 Regenerating MCQ on same topic…\n📚 Source: {source}"
        )

        mcq = None
        for attempt in range(3):
            if attempt > 0:
                await asyncio.sleep(10)
            mcq = await generate_mcq(topic_content, book_context=book)
            if mcq:
                break
            logger.warning(f"Regen attempt {attempt+1} failed")

        if mcq:
            await send_for_approval(context.bot, mcq, source,
                                    topic_content=topic_content, book_context=book)
        else:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "❌ Regeneration failed after 3 attempts (Groq API issue).\n"
                    f"Topic: {source}\n\n"
                    "Wait 1–2 min then try /postnow, /obg, or /surgery."
                )
            )

    else:
        await query.edit_message_text("❌ Unknown action.")

# ======================
# FORWARDED POLL HANDLER
# ======================
async def handle_forwarded_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        poll = update.message.poll
        if not poll:
            return
        text = (
            poll.question + "\n\n"
            + "\n".join(f"{chr(65+i)}) {opt.text}" for i, opt in enumerate(poll.options))
        )
        await update.message.reply_text("📊 Forwarded poll detected! Processing…")
        mcq = await rephrase_forwarded_mcq(text)
        if not mcq:
            await update.message.reply_text("❌ Could not process poll.")
            return
        await send_for_approval(context.bot, mcq, "Forwarded Poll",
                                topic_content=text, book_context=None)
    except Exception as e:
        logger.error(f"Forwarded poll error: {e}")
        await update.message.reply_text("❌ Failed to process poll.")

# ======================
# IMAGE HANDLER
# ======================
async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        await update.message.reply_text("🖼️ Image received. Generating MCQ…")
        if update.message.photo:
            file = await update.message.photo[-1].get_file()
        elif update.message.document:
            file = await update.message.document.get_file()
        else:
            return
        image_bytes = bytes(await file.download_as_bytearray())
        mcq, preview = await generate_mcq_from_image(image_bytes)
        if not mcq:
            await update.message.reply_text(f"❌ Failed. Reason: {preview}")
            return
        await send_for_approval(context.bot, mcq, "Image Upload",
                                topic_content=preview, book_context=None)
    except Exception as e:
        logger.error(f"Image handler error: {e}")
        await update.message.reply_text(f"❌ Image processing failed: {e}")

# ======================
# PDF HANDLER
# ======================
async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        await update.message.reply_text("📄 PDF received. Extracting text…")
        file = await update.message.document.get_file()
        file_bytes = bytes(await file.download_as_bytearray())
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        text = ""
        for page in reader.pages[:10]:
            extracted = page.extract_text()
            if extracted:
                text += extracted + "\n"
        if not text.strip():
            await update.message.reply_text("❌ Could not extract text from PDF.")
            return
        text = text[:4000]
        await update.message.reply_text("⏳ Generating MCQ from PDF…")
        mcq = await generate_mcq(text)
        if not mcq:
            await update.message.reply_text("❌ Failed to generate MCQ.")
            return
        await send_for_approval(context.bot, mcq, "PDF Upload",
                                topic_content=text, book_context=None)
    except Exception as e:
        logger.error(f"PDF handler error: {e}")
        await update.message.reply_text("❌ PDF processing failed.")

# ======================
# TEXT HANDLER
# ======================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    text = update.message.text.strip()

    if text.startswith("http://") or text.startswith("https://"):
        yt_id = extract_youtube_id(text)
        if yt_id:
            await update.message.reply_text("🎥 YouTube link! Fetching transcript…")
            content = await get_youtube_transcript(yt_id)
            if not content:
                await update.message.reply_text("⚠️ No transcript, trying page content…")
                content = await fetch_url_content(text)
            if not content:
                await update.message.reply_text("❌ Could not extract content.")
                return
            source = "YouTube: " + text[:50]
        else:
            await update.message.reply_text("🔗 Article URL! Fetching content…")
            content = await fetch_url_content(text)
            if not content:
                await update.message.reply_text("❌ Could not fetch content.")
                return
            source = "Article: " + text[:50]

        await update.message.reply_text("⏳ Generating MCQ…")
        mcq = await generate_mcq(content)
        if not mcq:
            await update.message.reply_text("❌ Failed to generate MCQ.")
            return
        await send_for_approval(context.bot, mcq, source,
                                topic_content=content, book_context=None)
    else:
        await update.message.reply_text("💬 Forwarded MCQ text detected! Processing…")
        mcq = await rephrase_forwarded_mcq(text)
        if not mcq:
            await update.message.reply_text("❌ Could not process MCQ.")
            return
        await send_for_approval(context.bot, mcq, "Forwarded MCQ",
                                topic_content=text, book_context=None)

# ======================
# COMMANDS
# ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    logger.info(f"/start from user {uid}")
    if uid != ADMIN_ID:
        await update.message.reply_text(f"⛔ Unauthorized. Your ID: {uid}")
        return
    await update.message.reply_text(
        "✅ OBG & Surgery Quiz Bot Running!\n\n"
        "📚 Book Commands:\n"
        "/obg         — MCQ from DC Dutta OBG\n"
        "/surgery     — MCQ from Bailey & Love Surgery\n"
        "/obgpdf      — MCQ from NEET PG OBG PDF\n"
        "/surgerypdf  — MCQ from NEET PG Surgery PDF\n\n"
        "🤖 Other Commands:\n"
        "/postnow        — Generate next alternating MCQ\n"
        "/status         — Bot status\n"
        "/debug          — Debug info\n"
        "/resettopics    — Clear used topics\n"
        "/resetquestions — Clear used questions\n\n"
        "📎 Send anything:\n"
        "📝 Forwarded MCQ text  → rephrase & post\n"
        "📊 Forwarded MCQ poll  → rephrase & post\n"
        "📄 PDF                 → extract & generate MCQ\n"
        "🖼 Image               → analyze & generate MCQ\n"
        "🔗 Article URL         → scrape & generate MCQ\n"
        "🎥 YouTube URL         → transcript & generate MCQ\n\n"
        "🔄 Rotation: OBG → Surgery → OBG PDF → Surgery PDF → …\n"
        "🔄 Regenerate rephrases the SAME topic."
    )

async def obg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("🤰 Generating MCQ from DC Dutta OBG…")
    mcq = await generate_mcq("", book_context="obg", auto_select_topic=True)
    if not mcq:
        await update.message.reply_text("❌ Failed to generate MCQ.")
        return
    topic = mcq.get("_selected_topic", "DC Dutta OBG")
    await update.message.reply_text(f"🏥 Topic: {topic}")
    await send_for_approval(context.bot, mcq, f"DC Dutta OBG: {topic}",
                            topic_content=topic, book_context="obg")

async def surgery_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("🔪 Generating MCQ from Bailey & Love Surgery…")
    mcq = await generate_mcq("", book_context="surgery", auto_select_topic=True)
    if not mcq:
        await update.message.reply_text("❌ Failed to generate MCQ.")
        return
    topic = mcq.get("_selected_topic", "Bailey & Love Surgery")
    await update.message.reply_text(f"🏥 Topic: {topic}")
    await send_for_approval(context.bot, mcq, f"Bailey & Love: {topic}",
                            topic_content=topic, book_context="surgery")

async def obg_pdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not OBG_CHUNKS:
        await update.message.reply_text("❌ OBG PDF not loaded. Check books/neet_obg.pdf.")
        return
    await update.message.reply_text(
        f"🤰 Generating MCQ from NEET PG OBG PDF…\n"
        f"📚 {len(OBG_CHUNKS)} chunks available"
    )
    chunk = await get_obg_chunk()
    mcq = await generate_mcq(chunk, book_context="obg_pdf")
    if not mcq:
        await update.message.reply_text("❌ Failed to generate MCQ.")
        return
    await send_for_approval(context.bot, mcq, "NEET PG OBG (PDF)",
                            topic_content=chunk, book_context="obg_pdf")

async def surgery_pdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not SURGERY_CHUNKS:
        await update.message.reply_text("❌ Surgery PDF not loaded. Check books/neet_surgery.pdf.")
        return
    await update.message.reply_text(
        f"🔪 Generating MCQ from NEET PG Surgery PDF…\n"
        f"📚 {len(SURGERY_CHUNKS)} chunks available"
    )
    chunk = await get_surgery_chunk()
    mcq = await generate_mcq(chunk, book_context="surgery_pdf")
    if not mcq:
        await update.message.reply_text("❌ Failed to generate MCQ.")
        return
    await send_for_approval(context.bot, mcq, "NEET PG Surgery (PDF)",
                            topic_content=chunk, book_context="surgery_pdf")

async def post_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("⏳ Generating MCQ… please wait.")
    await scheduled_job(context)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    rotation = ["obg", "surgery", "obg_pdf", "surgery_pdf"]
    current  = last_subject.get("subject", "surgery")
    try:
        nxt = rotation[(rotation.index(current) + 1) % len(rotation)]
    except ValueError:
        nxt = "obg"
    await update.message.reply_text(
        "✅ Bot is running\n"
        f"📊 Pending approvals:   {len(pending_questions)}\n"
        f"📚 Topics used:         {len(used_topics)}\n"
        f"❓ Questions used:      {len(used_questions)}\n"
        f"🔄 Next subject:        {nxt}\n"
        f"🤰 OBG channel:         {OBG_CHANNEL_ID}\n"
        f"🔪 Surgery channel:     {SURGERY_CHANNEL_ID}\n"
        f"🤰 OBG remaining:       {len([t for t in OBG_TOPICS if t not in used_topics])}/{len(OBG_TOPICS)}\n"
        f"🔪 Surgery remaining:   {len([t for t in SURGERY_TOPICS if t not in used_topics])}/{len(SURGERY_TOPICS)}\n"
        f"📖 OBG PDF:             {len(OBG_CHUNKS)} chunks, {len(used_obg_chunks)} used\n"
        f"📖 Surgery PDF:         {len(SURGERY_CHUNKS)} chunks, {len(used_surg_chunks)} used"
    )

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rotation = ["obg", "surgery", "obg_pdf", "surgery_pdf"]
    current  = last_subject.get("subject", "surgery")
    try:
        nxt = rotation[(rotation.index(current) + 1) % len(rotation)]
    except ValueError:
        nxt = "obg"
    await update.message.reply_text(
        f"🔧 Debug Info\n"
        f"Your ID:            {uid}\n"
        f"Admin ID:           {ADMIN_ID}\n"
        f"Match:              {'✅' if uid == ADMIN_ID else '❌'}\n"
        f"Pending approvals:  {len(pending_questions)}\n"
        f"Topics used:        {len(used_topics)}\n"
        f"Questions used:     {len(used_questions)}\n"
        f"Next subject:       {nxt}\n"
        f"OBG remaining:      {len([t for t in OBG_TOPICS if t not in used_topics])}/{len(OBG_TOPICS)}\n"
        f"Surgery remaining:  {len([t for t in SURGERY_TOPICS if t not in used_topics])}/{len(SURGERY_TOPICS)}\n"
        f"OBG PDF chunks:     {len(OBG_CHUNKS)} loaded, {len(used_obg_chunks)} used\n"
        f"Surgery PDF chunks: {len(SURGERY_CHUNKS)} loaded, {len(used_surg_chunks)} used"
    )

async def reset_topics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    used_topics.clear()
    save_json("used_topics.json", used_topics)
    await update.message.reply_text("✅ Used topics cleared!")

async def reset_questions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    used_questions.clear()
    save_json("used_questions.json", used_questions)
    await update.message.reply_text("✅ Used questions cleared!")

# ======================
# MAIN
# ======================
def main():
    global OBG_CHUNKS, SURGERY_CHUNKS
    OBG_CHUNKS     = load_pdf_chunks("books/neet_obg.pdf")
    SURGERY_CHUNKS = load_pdf_chunks("books/neet_surgery.pdf")
    logger.info(f"OBG PDF chunks: {len(OBG_CHUNKS)}")
    logger.info(f"Surgery PDF chunks: {len(SURGERY_CHUNKS)}")
    logger.info(f"Pending questions restored: {len(pending_questions)}")
    logger.info("Starting OBG & Surgery Quiz Bot…")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",          start))
    app.add_handler(CommandHandler("postnow",        post_now))
    app.add_handler(CommandHandler("status",         status))
    app.add_handler(CommandHandler("obg",            obg_command))
    app.add_handler(CommandHandler("surgery",        surgery_command))
    app.add_handler(CommandHandler("obgpdf",         obg_pdf_command))
    app.add_handler(CommandHandler("surgerypdf",     surgery_pdf_command))
    app.add_handler(CommandHandler("debug",          debug_command))
    app.add_handler(CommandHandler("resettopics",    reset_topics_command))
    app.add_handler(CommandHandler("resetquestions", reset_questions_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.PDF,              handle_pdf))
    app.add_handler(MessageHandler(filters.PHOTO,                     handle_image))
    app.add_handler(MessageHandler(filters.Document.IMAGE,            handle_image))
    app.add_handler(MessageHandler(filters.POLL,                      handle_forwarded_poll))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,   handle_text))

    app.job_queue.run_repeating(scheduled_job, interval=INTERVAL, first=30)

    logger.info(f"Bot started! Interval: {INTERVAL // 60} minutes")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
