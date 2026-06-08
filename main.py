import os
import json
import re
import time
import logging
import asyncio

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MEDICINE_GROUP_ID = os.getenv("MEDICINE_GROUP_ID")
ADMIN_ID = 723919716
INTERVAL = 900
QUESTIONS_PER_BATCH = 2

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN missing")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY missing")
if not MEDICINE_GROUP_ID:
    raise ValueError("MEDICINE_GROUP_ID missing")

client = Groq(api_key=GROQ_API_KEY)
pending_batches = {}
used_topics = []

HARRISON_TOPICS = [
    "Approach to the patient with chest pain",
    "Acute coronary syndrome STEMI NSTEMI",
    "Heart failure systolic diastolic management",
    "Atrial fibrillation management anticoagulation",
    "Hypertensive emergency urgency",
    "Aortic stenosis clinical features management",
    "Infective endocarditis diagnosis Duke criteria",
    "Pericarditis and cardiac tamponade",
    "Pulmonary embolism diagnosis management",
    "Deep vein thrombosis anticoagulation",
    "Community acquired pneumonia management",
    "Tuberculosis diagnosis treatment",
    "COPD exacerbation management",
    "Asthma acute severe management",
    "Pleural effusion causes diagnosis",
    "Acute respiratory distress syndrome",
    "Pneumothorax types management",
    "Peptic ulcer disease H pylori",
    "Inflammatory bowel disease Crohn ulcerative colitis",
    "Acute pancreatitis severity management",
    "Liver cirrhosis complications management",
    "Hepatitis B C diagnosis treatment",
    "Acute liver failure causes management",
    "Acute kidney injury causes management",
    "Chronic kidney disease complications",
    "Nephrotic syndrome causes management",
    "Nephritic syndrome glomerulonephritis",
    "Diabetic ketoacidosis management",
    "Hyperosmolar hyperglycemic state",
    "Hypothyroidism hyperthyroidism management",
    "Adrenal insufficiency Addison disease",
    "Cushing syndrome diagnosis",
    "Diabetes mellitus type 1 type 2 complications",
    "Hyponatremia hypernatremia management",
    "Hypokalemia hyperkalemia ECG changes",
    "Hypercalcemia hypocalcemia causes",
    "Metabolic acidosis alkalosis approach",
    "Respiratory acidosis alkalosis approach",
    "Anemia approach iron deficiency",
    "Megaloblastic anemia B12 folate",
    "Hemolytic anemia causes workup",
    "Sickle cell disease complications",
    "Thrombocytopenia causes ITP TTP",
    "Disseminated intravascular coagulation",
    "Leukemia acute chronic types",
    "Lymphoma Hodgkin non Hodgkin",
    "Multiple myeloma diagnosis treatment",
    "Rheumatoid arthritis diagnosis management",
    "Systemic lupus erythematosus criteria",
    "Sepsis septic shock management",
    "Meningitis bacterial viral management",
    "Stroke ischemic hemorrhagic management",
    "Seizures epilepsy management",
    "Guillain Barre syndrome",
    "Myasthenia gravis diagnosis treatment",
    "Parkinson disease management",
    "HIV AIDS opportunistic infections",
    "Malaria diagnosis treatment",
    "Typhoid fever diagnosis treatment",
    "Dengue fever management",
    "Approach to fever of unknown origin",
]

def escape_md(text):
    for ch in ["_", "*", "[", "]", "(", ")", "~", "`", ">",
               "#", "+", "-", "=", "|", "{", "}", ".", "!"]:
        text = text.replace(ch, f"\\{ch}")
    return text

def extract_json(text):
    try:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            result = json.loads(match.group())
            if isinstance(result, list):
                return result
        return []
    except Exception as e:
        logger.error("JSON parse error: " + str(e))
        return []

async def generate_topic():
    used = ", ".join(used_topics[-20:]) if used_topics else "none"
    prompt = (
        "You are a Harrison Internal Medicine expert.\n\n"
        "Suggest ONE specific high-yield Internal Medicine topic.\n\n"
        "Already used (avoid repeating): " + used + "\n\n"
        "Must be:\n"
        "- From Harrison Principles of Internal Medicine\n"
        "- High yield for NEET PG / USMLE / FMGE\n"
        "- Specific clinical topic\n\n"
        'Return ONLY JSON: {"topic": "Acute coronary syndrome management"}'
    )
    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9
        )
        text = response.choices[0].message.content
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            result = json.loads(match.group())
            topic = result.get("topic", "Internal medicine high yield topic")
            used_topics.append(topic)
            if len(used_topics) > 100:
                used_topics.pop(0)
            return topic
        return "Internal medicine high yield topic"
    except Exception as e:
        logger.error("Topic generation error: " + str(e))
        import random
        return random.choice(HARRISON_TOPICS)

async def generate_questions(topic):
    prompt = (
        "You are a Harrison Internal Medicine expert examiner.\n\n"
        "Generate EXACTLY 2 high-yield clinical MCQs about: " + topic + "\n\n"
        "Rules:\n"
        "- Harrison Principles of Internal Medicine style\n"
        "- Clinical vignette with patient scenario\n"
        "- 4 options, one definitively correct\n"
        "- Detailed explanation citing Harrison\n"
        "- Explain why each wrong option is incorrect\n"
        "- NEET PG / USMLE standard\n\n"
        "Return ONLY JSON array:\n"
        "[\n"
        "  {\n"
        '    "question": "A 55-year-old patient presents with...",\n'
        '    "options": ["A) ...", "B) ...", "C) ...", "D) ..."],\n'
        '    "answer_index": 0,\n'
        '    "explanation": "Correct: A because... B is wrong because..."\n'
        "  }\n"
        "]"
    )
    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        return extract_json(response.choices[0].message.content)
    except Exception as e:
        logger.error("Question generation error: " + str(e))
        return []

async def send_for_approval(bot, questions, topic):
    try:
        qid = str(int(time.time()))
        pending_batches[qid] = {"questions": questions, "topic": topic}

        preview = "📋 HARRISON MCQ FOR APPROVAL\n\n"
        preview += "📚 Topic: " + topic + "\n\n"

        for i, q in enumerate(questions):
            preview += "Q" + str(i + 1) + ": " + q["question"] + "\n\n"
            preview += "\n".join(q["options"]) + "\n\n"
            preview += "✅ Correct: " + q["options"][q["answer_index"]] + "\n\n"
            preview += "💡 " + q["explanation"] + "\n\n"
            preview += "─────────────────\n\n"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve & Post", callback_data="approve_" + qid),
                InlineKeyboardButton("❌ Reject", callback_data="reject_" + qid)
            ],
            [
                InlineKeyboardButton("🔄 Regenerate", callback_data="regen_" + qid)
            ]
        ])

        if len(preview) > 4000:
            preview = preview[:4000] + "...\n\n[Truncated - tap Approve to post full version]"

        await bot.send_message(
            chat_id=ADMIN_ID,
            text=preview,
            reply_markup=keyboard
        )
        logger.info("Sent for approval: " + topic)
    except Exception as e:
        logger.error("Send for approval error: " + str(e))

async def post_to_group(bot, questions, topic):
    try:
        header = "🏥 HARRISON INTERNAL MEDICINE MCQ\n📚 Topic: " + topic + "\n\n"
        await bot.send_message(
            chat_id=MEDICINE_GROUP_ID,
            text=header
        )
        await asyncio.sleep(1)

        for q in questions:
            text_msg = q["question"] + "\n\n" + "\n".join(q["options"])
            await bot.send_message(
                chat_id=MEDICINE_GROUP_ID,
                text=text_msg
            )
            await asyncio.sleep(1)

            clean_options = []
            for opt in q["options"]:
                if len(opt) > 2 and opt[1] == ")":
                    clean_options.append(opt[3:].strip())
                else:
                    clean_options.append(opt)

            await bot.send_poll(
                chat_id=MEDICINE_GROUP_ID,
                question=q["question"][:300],
                options=clean_options,
                type="quiz",
                correct_option_id=int(q["answer_index"]),
                is_anonymous=True
            )
            await asyncio.sleep(2)

            explanation_escaped = escape_md(q["explanation"])
            spoiler = "💡 Explanation:\n\n||" + explanation_escaped + "||"
            await bot.send_message(
                chat_id=MEDICINE_GROUP_ID,
                text=spoiler,
                parse_mode="MarkdownV2"
            )
            await asyncio.sleep(2)

        logger.info("Posted to medicine group: " + topic)
    except Exception as e:
        logger.error("Post to group error: " + str(e))

async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        logger.info("Running scheduled job...")
        topic = await generate_topic()
        logger.info("Topic: " + topic)
        questions = await generate_questions(topic)
        if not questions:
            logger.error("Failed to generate questions")
            return
        await send_for_approval(context.bot, questions, topic)
    except Exception as e:
        logger.error("Scheduled job error: " + str(e))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("approve_"):
        qid = data.replace("approve_", "")
        item = pending_batches.get(qid)
        if item:
            await post_to_group(context.bot, item["questions"], item["topic"])
            pending_batches.pop(qid, None)
            await query.edit_message_text("✅ Posted to medicine group!")
        else:
            await query.edit_message_text("❌ Questions expired.")

    elif data.startswith("reject_"):
        qid = data.replace("reject_", "")
        pending_batches.pop(qid, None)
        await query.edit_message_text("❌ Rejected.")

    elif data.startswith("regen_"):
        qid = data.replace("regen_", "")
        pending_batches.pop(qid, None)
        await query.edit_message_text("🔄 Regenerating...")
        topic = await generate_topic()
        questions = await generate_questions(topic)
        if questions:
            await send_for_approval(context.bot, questions, topic)
        else:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text="❌ Failed to regenerate. Try /postnow"
            )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "✅ MedHacker Bot Running!\n\n"
        "Harrison Internal Medicine MCQs\n"
        "2 questions every 15 minutes\n\n"
        "Commands:\n"
        "/postnow - Generate immediately\n"
        "/status - Check bot status"
    )

async def post_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("⏳ Generating Harrison MCQs... please wait")
    await scheduled_job(context)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "✅ Bot is running\n"
        "📊 Pending approvals: " + str(len(pending_batches)) + "\n"
        "📚 Topics used: " + str(len(used_topics))
    )

async def error_handler(update, context):
    logger.error("Update error: " + str(context.error))

def main():
    logger.info("Starting MedHacker Bot...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("postnow", post_now))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(error_handler)

    app.job_queue.run_repeating(
        scheduled_job,
        interval=INTERVAL,
        first=10
    )

    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
