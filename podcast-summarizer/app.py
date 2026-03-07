import os
import re
import json
import uuid
from datetime import datetime, date
from flask import (
    Flask, render_template, request, jsonify, Response,
    stream_with_context, session,
)
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.formatters import TextFormatter
from werkzeug.security import generate_password_hash, check_password_hash
import anthropic
import urllib.parse
import urllib.request

from database import get_db, init_db

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")

FREE_DAILY_LIMIT = 2

# Claude Sonnet 4 pricing
COST_INPUT_PER_MTOK_USD = 3.0
COST_OUTPUT_PER_MTOK_USD = 15.0
USD_TO_SEK = 10.5


def calc_cost_sek(input_tokens, output_tokens):
    cost_usd = (input_tokens / 1_000_000) * COST_INPUT_PER_MTOK_USD + \
               (output_tokens / 1_000_000) * COST_OUTPUT_PER_MTOK_USD
    return round(cost_usd * USD_TO_SEK, 4)


# --------------- YouTube helpers ---------------

def extract_video_id(url: str) -> str | None:
    patterns = [
        r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})",
        r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def get_video_title(video_id: str) -> str:
    try:
        oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("title", "Unknown title")
    except Exception:
        return "Unknown title"


def get_transcript(video_id: str) -> tuple[str, list[dict]]:
    ytt_api = YouTubeTranscriptApi()
    transcript = ytt_api.fetch(video_id)

    segments = []
    for snippet in transcript:
        segments.append({
            "text": snippet.text,
            "start": snippet.start,
        })

    formatter = TextFormatter()
    full_text = formatter.format_transcript(transcript)
    return full_text, segments


def estimate_duration(segments: list[dict]) -> int:
    if not segments:
        return 0
    last = segments[-1]
    return int((last["start"] + 30) / 60)


# --------------- Prompt builder ---------------

DETAIL_CONFIGS = {
    "short": {
        "summary_length": "2–3 meningar",
        "chapter_count": "3–7 stycken",
        "chapter_summary_length": "1 mening",
    },
    "medium": {
        "summary_length": "3–5 meningar",
        "chapter_count": "5–15 stycken",
        "chapter_summary_length": "1–3 meningar",
    },
    "detailed": {
        "summary_length": "5–8 meningar",
        "chapter_count": "10–20 stycken",
        "chapter_summary_length": "2–4 meningar",
    },
}

SUMMARIZE_PROMPT = """\
Du är en expert på att sammanfatta podcast-avsnitt och videoinnehåll.

Givet följande transkription, gör följande:

1. Ge en sammanfattning ({summary_length}) av hela innehållet. Skriv på {language}.
2. Dela upp innehållet i logiska kapitel/sektioner ({chapter_count} beroende på längd).
3. Varje kapitel ska ha:
   - En kort, beskrivande rubrik på {language}
   - En tidsperiod (t.ex. "0:00\u20135:30") baserat på textens position i transkriptionen
   - En kategori (ett av: introduction, background, analysis, discussion, story, deep-dive, opinion, conclusion, practical, interview)
   - En sammanfattning på {chapter_summary_length} på {language}
   - Den faktiska transkriptionstexten för det avsnittet, organiserad under underrubriker. \
Inkludera de viktigaste delarna av transkriptionstexten \u2014 parafrasera och komprimera där det behövs, \
men behåll viktiga citat ordagrant. Formatera som HTML-fragment med <h4> för underrubriker och <p> för stycken.
4. Plocka ut 3\u20135 av de mest intressanta eller viktiga citaten (ordagrant från transkriptionen).

Svara ENBART med giltig JSON i följande format (ingen markdown, inga kodblock):
{{
  "title": "Titel på avsnittet",
  "summary": "Övergripande sammanfattning...",
  "chapters": [
    {{
      "title": "Kapitelrubrik",
      "time": "0:00\u20135:30",
      "category": "analysis",
      "summary": "Sammanfattning av kapitlet...",
      "transcript_html": "<h4>Underrubrik</h4><p>Text från transkriptionen...</p>"
    }}
  ],
  "key_quotes": [
    {{
      "text": "Det exakta citatet från transkriptionen...",
      "context": "Kort beskrivning av kontexten",
      "time": "3:42"
    }}
  ]
}}

Här är transkriptionen:

---
{transcript}
---

Totallängd på videon: cirka {duration} minuter."""


def build_prompt(transcript_text, duration_minutes, language="svenska", detail_level="medium"):
    config = DETAIL_CONFIGS.get(detail_level, DETAIL_CONFIGS["medium"])
    return SUMMARIZE_PROMPT.format(
        transcript=transcript_text[:100000],
        duration=duration_minutes,
        language=language,
        **config,
    )


def _try_repair_json(text):
    """Attempt to recover valid JSON from truncated/garbled model output."""
    text = (text or "").strip()
    if not text:
        return text

    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass

    first_obj = text.find("{")
    if first_obj == -1:
        return text

    base = text[first_obj:]

    def close_candidate(candidate):
        stripped = candidate.rstrip()

        # If we're mid-string, close it.
        in_string = False
        i = 0
        while i < len(stripped):
            ch = stripped[i]
            if ch == '\\' and in_string:
                i += 2
                continue
            if ch == '"':
                in_string = not in_string
            i += 1
        if in_string:
            stripped += '"'

        # Drop dangling key/value fragments and trailing commas.
        stripped = re.sub(r',\s*"[^"]*"\s*:\s*$', '', stripped)
        stripped = re.sub(r',\s*"[^"]*$', '', stripped)
        stripped = stripped.rstrip().rstrip(',')

        # Close open brackets/braces.
        stack = []
        in_str = False
        i = 0
        while i < len(stripped):
            ch = stripped[i]
            if ch == '\\' and in_str:
                i += 2
                continue
            if ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch in ('{', '['):
                    stack.append('}' if ch == '{' else ']')
                elif ch in ('}', ']') and stack:
                    stack.pop()
            i += 1

        result = stripped
        while stack:
            result += stack.pop()
        return result

    # Progressive trim from the end to recover from trailing garbage.
    max_trim = min(1500, len(base) - 1)
    for trim in range(0, max_trim + 1):
        candidate = base[: len(base) - trim]
        repaired = close_candidate(candidate)
        try:
            json.loads(repaired)
            return repaired
        except json.JSONDecodeError:
            continue

    return text


def summarize_with_claude(transcript_text, duration_minutes, language="svenska", detail_level="medium"):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = build_prompt(transcript_text, duration_minutes, language, detail_level)

    with client.messages.stream(
        model="claude-sonnet-4-20250514",
        max_tokens=32000,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            yield {"type": "text", "text": text}

        final = stream.get_final_message()
        yield {
            "type": "usage",
            "input_tokens": final.usage.input_tokens,
            "output_tokens": final.usage.output_tokens,
            "stop_reason": final.stop_reason,
        }


# --------------- Auth helpers ---------------

def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    db.close()
    return user


def check_rate_limit(user):
    """Returns (allowed, remaining). None user = not logged in."""
    if user is None:
        return False, 0
    if user["is_subscriber"]:
        return True, 999
    today = date.today().isoformat()
    db = get_db()
    if user["daily_folds_date"] != today:
        db.execute("UPDATE users SET daily_folds_used = 0, daily_folds_date = ? WHERE id = ?",
                   (today, user["id"]))
        db.commit()
        used = 0
    else:
        used = user["daily_folds_used"]
    db.close()
    remaining = max(0, FREE_DAILY_LIMIT - used)
    return remaining > 0, remaining


def increment_usage(user_id):
    today = date.today().isoformat()
    db = get_db()
    db.execute(
        "UPDATE users SET daily_folds_used = daily_folds_used + 1, daily_folds_date = ? WHERE id = ?",
        (today, user_id),
    )
    db.commit()
    db.close()


# --------------- Routes ---------------

@app.route("/")
def index():
    user = get_current_user()
    return render_template("index.html", user=user, google_client_id=GOOGLE_CLIENT_ID)


@app.route("/fold/<share_token>")
def shared_fold(share_token):
    db = get_db()
    fold = db.execute("SELECT * FROM folds WHERE share_token = ?", (share_token,)).fetchone()
    db.close()
    if not fold:
        return "Fold not found", 404
    user = get_current_user()
    return render_template("index.html", user=user, shared_fold=dict(fold), google_client_id=GOOGLE_CLIENT_ID)


@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password", "")
    name = (data.get("name") or "").strip()

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        db.close()
        return jsonify({"error": "This email is already registered"}), 400

    pw_hash = generate_password_hash(password)
    cursor = db.execute(
        "INSERT INTO users (email, password_hash, display_name) VALUES (?, ?, ?)",
        (email, pw_hash, name or email.split("@")[0]),
    )
    db.commit()
    user_id = cursor.lastrowid
    db.close()

    session["user_id"] = user_id
    return jsonify({"ok": True, "user": {"id": user_id, "email": email, "name": name, "is_subscriber": False}})


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password", "")

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    db.close()

    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Incorrect email or password"}), 401

    session["user_id"] = user["id"]
    return jsonify({
        "ok": True,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user["display_name"],
            "is_subscriber": bool(user["is_subscriber"]),
        },
    })


@app.route("/api/auth/google", methods=["POST"])
def api_auth_google():
    data = request.get_json()
    credential = data.get("credential", "")
    if not credential:
        return jsonify({"error": "No credential provided"}), 400

    # Verify Google ID token via Google's tokeninfo endpoint
    try:
        token_url = f"https://oauth2.googleapis.com/tokeninfo?id_token={urllib.parse.quote(credential)}"
        req = urllib.request.Request(token_url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            info = json.loads(resp.read().decode())
    except Exception:
        return jsonify({"error": "Could not verify Google token"}), 401

    # Verify audience matches our client ID
    if GOOGLE_CLIENT_ID and info.get("aud") != GOOGLE_CLIENT_ID:
        return jsonify({"error": "Token audience mismatch"}), 401

    google_id = info.get("sub")
    email = info.get("email", "").lower()
    name = info.get("name", "")
    avatar = info.get("picture", "")

    if not email:
        return jsonify({"error": "No email in Google token"}), 400

    db = get_db()

    # Check if user exists by google_id or email
    user = db.execute("SELECT * FROM users WHERE google_id = ?", (google_id,)).fetchone()
    if not user:
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user:
            # Link Google account to existing email user
            db.execute("UPDATE users SET google_id = ?, avatar_url = ? WHERE id = ?",
                       (google_id, avatar, user["id"]))
            db.commit()
        else:
            # Create new user
            cursor = db.execute(
                "INSERT INTO users (email, password_hash, display_name, google_id, avatar_url) "
                "VALUES (?, '', ?, ?, ?)",
                (email, name or email.split("@")[0], google_id, avatar),
            )
            db.commit()
            user = db.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()

    db.close()
    session["user_id"] = user["id"]
    return jsonify({
        "ok": True,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user["display_name"],
            "is_subscriber": bool(user["is_subscriber"]),
            "avatar": user["avatar_url"] or "",
        },
    })


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("user_id", None)
    return jsonify({"ok": True})


@app.route("/api/me")
def api_me():
    user = get_current_user()
    if not user:
        return jsonify({"user": None})
    allowed, remaining = check_rate_limit(user)
    return jsonify({
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user["display_name"],
            "avatar": user["avatar_url"] or "",
            "is_subscriber": bool(user["is_subscriber"]),
            "folds_remaining": remaining if not user["is_subscriber"] else None,
        }
    })


@app.route("/api/history")
def api_history():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not signed in"}), 401
    db = get_db()
    folds = db.execute(
        "SELECT id, video_id, video_title, video_url, cost_sek, share_token, created_at "
        "FROM folds WHERE user_id = ? ORDER BY created_at DESC LIMIT 50",
        (user["id"],),
    ).fetchall()
    db.close()
    return jsonify({"folds": [dict(f) for f in folds]})


@app.route("/api/fold/<int:fold_id>")
def api_fold(fold_id):
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not signed in"}), 401
    db = get_db()
    fold = db.execute(
        "SELECT * FROM folds WHERE id = ? AND user_id = ?", (fold_id, user["id"])
    ).fetchone()
    db.close()
    if not fold:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(fold))


@app.route("/api/summarize", methods=["POST"])
def api_summarize():
    data = request.get_json()
    url = data.get("url", "").strip()
    language = data.get("language", "svenska")
    detail_level = data.get("detail_level", "medium")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "Could not identify YouTube video ID."}), 400

    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY is not configured."}), 500

    user = get_current_user()
    allowed, remaining = check_rate_limit(user)
    if not allowed:
        msg = "Sign in to use Foldly." if user is None else \
              "You've reached today's limit. Upgrade to Foldly Pro for unlimited folds."
        return jsonify({"error": msg}), 429

    try:
        video_title = get_video_title(video_id)
    except Exception:
        video_title = "Unknown title"

    try:
        transcript_text, segments = get_transcript(video_id)
    except Exception as e:
        return jsonify({"error": f"Could not fetch transcript: {e}"}), 400

    duration = estimate_duration(segments)

    def generate():
        yield json.dumps({
            "type": "meta",
            "video_title": video_title,
            "video_id": video_id,
            "duration": duration,
            "segments": segments,
        }) + "\n"

        try:
            full_text = ""
            usage_info = None

            for item in summarize_with_claude(transcript_text, duration, language, detail_level):
                if item["type"] == "text":
                    full_text += item["text"]
                    yield json.dumps({"type": "chunk", "text": item["text"]}) + "\n"
                elif item["type"] == "usage":
                    usage_info = item

            input_tokens = usage_info["input_tokens"] if usage_info else 0
            output_tokens = usage_info["output_tokens"] if usage_info else 0
            stop_reason = usage_info.get("stop_reason") if usage_info else None
            cost = calc_cost_sek(input_tokens, output_tokens)

            # Always try to repair JSON if it's not valid
            # (handles max_tokens, network interruptions, malformed output)
            try:
                json.loads(full_text)
            except (json.JSONDecodeError, ValueError):
                repaired = _try_repair_json(full_text)
                if repaired != full_text:
                    extra = repaired[len(full_text):]
                    yield json.dumps({"type": "chunk", "text": extra}) + "\n"
                    full_text = repaired

            # Save to DB if user is logged in
            share_token = None
            fold_id = None
            if user:
                share_token = uuid.uuid4().hex[:12]
                db = get_db()
                cursor = db.execute(
                    "INSERT INTO folds (user_id, video_id, video_title, video_url, summary_json, "
                    "segments_json, input_tokens, output_tokens, cost_sek, share_token, language, detail_level) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (user["id"], video_id, video_title, url, full_text,
                     json.dumps(segments), input_tokens, output_tokens, cost,
                     share_token, language, detail_level),
                )
                fold_id = cursor.lastrowid
                db.commit()
                db.close()
                increment_usage(user["id"])

            yield json.dumps({
                "type": "done",
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_sek": cost,
                "share_token": share_token,
                "fold_id": fold_id,
                "stop_reason": stop_reason,
            }) + "\n"

        except anthropic.BadRequestError as e:
            yield json.dumps({"type": "error", "error": f"API-fel: {e.message}"}) + "\n"
        except anthropic.AuthenticationError:
            yield json.dumps({"type": "error", "error": "Invalid API key."}) + "\n"
        except anthropic.APIError as e:
            yield json.dumps({"type": "error", "error": f"API-fel: {e.message}"}) + "\n"
        except Exception as e:
            # Catch-all for network errors, timeouts, etc.
            # If we have partial text, try to repair and send it
            if full_text:
                repaired = _try_repair_json(full_text)
                if repaired != full_text:
                    extra = repaired[len(full_text):]
                    yield json.dumps({"type": "chunk", "text": extra}) + "\n"
                yield json.dumps({
                    "type": "done",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_sek": 0,
                    "share_token": None,
                    "fold_id": None,
                    "stop_reason": f"error: {type(e).__name__}",
                    "warning": f"Stream interrupted: {e}",
                }) + "\n"
            else:
                yield json.dumps({"type": "error", "error": f"Unexpected error: {e}"}) + "\n"

    return Response(
        stream_with_context(generate()),
        content_type="application/x-ndjson",
    )


# --------------- Init ---------------

with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
