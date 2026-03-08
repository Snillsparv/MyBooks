import os
import re
import json
import uuid
import time
import math
from datetime import datetime, date
from flask import (
    Flask, render_template, request, jsonify, Response,
    stream_with_context, session,
)
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.formatters import TextFormatter
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.exceptions import HTTPException
import anthropic
import urllib.parse
import urllib.request

from database import get_db, init_db

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
PROXY_URL = os.environ.get("PROXY_URL", "")  # e.g. http://user:pass@proxy:8080

FREE_DAILY_LIMIT = 2
ANON_FREE_FOLDS = 1
ANON_FREE_FOLD_WINDOW_SECONDS = 86400  # 24 hours

TRANSCRIPT_CACHE_TTL_SECONDS = int(os.environ.get("TRANSCRIPT_CACHE_TTL_SECONDS", "21600"))
TRANSCRIPT_FETCH_RETRIES = int(os.environ.get("TRANSCRIPT_FETCH_RETRIES", "3"))
TRANSCRIPT_CACHE = {}

# Claude Sonnet 4 pricing
COST_INPUT_PER_MTOK_USD = 3.0
COST_OUTPUT_PER_MTOK_USD = 15.0
USD_TO_SEK = 10.5

# Chunking threshold: videos longer than this get split into chunks
CHUNK_THRESHOLD_MINUTES = 40
CHUNK_SIZE_MINUTES = 30


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


def _is_temporary_transcript_block(error_text: str) -> bool:
    text = (error_text or "").lower()
    markers = (
        "too many requests",
        "rate limit",
        "requestblocked",
        "ipblocked",
        "forbidden",
        "429",
        "temporarily",
    )
    return any(m in text for m in markers)


def get_transcript(video_id: str) -> tuple[str, list[dict]]:
    now = time.time()
    cached = TRANSCRIPT_CACHE.get(video_id)
    if cached and cached["expires_at"] > now:
        return cached["full_text"], cached["segments"]

    ytt_api = YouTubeTranscriptApi()
    last_error = None

    for attempt in range(1, max(1, TRANSCRIPT_FETCH_RETRIES) + 1):
        try:
            transcript = ytt_api.fetch(video_id)

            segments = []
            for snippet in transcript:
                segments.append({
                    "text": snippet.text,
                    "start": snippet.start,
                })

            formatter = TextFormatter()
            full_text = formatter.format_transcript(transcript)

            TRANSCRIPT_CACHE[video_id] = {
                "full_text": full_text,
                "segments": segments,
                "expires_at": now + max(60, TRANSCRIPT_CACHE_TTL_SECONDS),
            }
            return full_text, segments
        except Exception as e:
            last_error = e
            message = str(e)
            should_retry = _is_temporary_transcript_block(message)
            if should_retry and attempt < max(1, TRANSCRIPT_FETCH_RETRIES):
                backoff_s = min(5, attempt)
                app.logger.warning(
                    "YouTube transcript fetch blocked, retrying (%s/%s) in %ss for video %s",
                    attempt,
                    TRANSCRIPT_FETCH_RETRIES,
                    backoff_s,
                    video_id,
                )
                time.sleep(backoff_s)
                continue
            break

    err_text = str(last_error or "Unknown transcript error")
    if _is_temporary_transcript_block(err_text):
        raise RuntimeError(
            "YouTube is temporarily blocking transcript requests from this server. "
            "Please retry in 1-5 minutes."
        )
    raise RuntimeError(err_text)


def estimate_duration(segments: list[dict]) -> int:
    if not segments:
        return 0
    last = segments[-1]
    return int((last["start"] + 30) / 60)


def split_segments_into_chunks(segments: list[dict], chunk_minutes: int = CHUNK_SIZE_MINUTES) -> list[dict]:
    """Split segments into time-based chunks. Returns list of {start_min, end_min, text}."""
    if not segments:
        return []

    total_seconds = segments[-1]["start"] + 30
    total_minutes = total_seconds / 60
    num_chunks = max(1, math.ceil(total_minutes / chunk_minutes))
    chunk_duration = total_seconds / num_chunks

    chunks = []
    for i in range(num_chunks):
        start_s = i * chunk_duration
        end_s = (i + 1) * chunk_duration
        chunk_segs = [s for s in segments if s["start"] >= start_s and s["start"] < end_s]
        if not chunk_segs and i == num_chunks - 1:
            # Last chunk: grab remaining
            chunk_segs = [s for s in segments if s["start"] >= start_s]
        if chunk_segs:
            text = " ".join(s["text"] for s in chunk_segs)
            chunks.append({
                "start_min": round(start_s / 60, 1),
                "end_min": round(end_s / 60, 1),
                "text": text,
            })
    return chunks


# --------------- Prompt builder ---------------

DETAIL_CONFIGS = {
    "short": {
        "summary_length": "2\u20133 meningar",
        "chapter_count": "3\u20137 stycken",
        "chapter_summary_length": "1 mening",
    },
    "medium": {
        "summary_length": "3\u20135 meningar",
        "chapter_count": "5\u201315 stycken",
        "chapter_summary_length": "1\u20133 meningar",
    },
    "detailed": {
        "summary_length": "5\u20138 meningar",
        "chapter_count": "10\u201320 stycken",
        "chapter_summary_length": "2\u20134 meningar",
    },
}

# Single-pass prompt for short videos
SUMMARIZE_PROMPT = """\
Du \u00e4r en expert p\u00e5 att sammanfatta podcast-avsnitt och videoinneh\u00e5ll.

Givet f\u00f6ljande transkription, g\u00f6r f\u00f6ljande:

1. Ge en sammanfattning ({summary_length}) av hela inneh\u00e5llet. Skriv p\u00e5 {language}.
2. Dela upp inneh\u00e5llet i logiska kapitel/sektioner ({chapter_count} beroende p\u00e5 l\u00e4ngd).
   Tidsintervallen ska tillsammans t\u00e4cka hela videon fr\u00e5n b\u00f6rjan till slut.
3. Varje kapitel ska ha:
   - En kort, beskrivande rubrik p\u00e5 {language}
   - En tidsperiod (t.ex. "0:00\u20135:30") baserat p\u00e5 textens position i transkriptionen
   - En kategori (ett av: introduction, background, analysis, discussion, story, deep-dive, opinion, conclusion, practical, interview)
   - En sammanfattning p\u00e5 {chapter_summary_length} p\u00e5 {language}
   - Den faktiska transkriptionstexten f\u00f6r det avsnittet, organiserad under underrubriker. \
Inkludera de viktigaste delarna av transkriptionstexten \u2014 parafrasera och komprimera d\u00e4r det beh\u00f6vs, \
men beh\u00e5ll viktiga citat ordagrant. Formatera som HTML-fragment med <h4> f\u00f6r underrubriker och <p> f\u00f6r stycken.
4. Plocka ut 3\u20135 av de mest intressanta eller viktiga citaten (ordagrant fr\u00e5n transkriptionen).

Svara ENBART med giltig JSON i f\u00f6ljande format (ingen markdown, inga kodblock):
{{
  "title": "Titel p\u00e5 avsnittet",
  "summary": "\u00d6vergripande sammanfattning...",
  "chapters": [
    {{
      "title": "Kapitelrubrik",
      "time": "0:00\u20135:30",
      "category": "analysis",
      "summary": "Sammanfattning av kapitlet...",
      "transcript_html": "<h4>Underrubrik</h4><p>Text fr\u00e5n transkriptionen...</p>"
    }}
  ],
  "key_quotes": [
    {{
      "text": "Det exakta citatet fr\u00e5n transkriptionen...",
      "context": "Kort beskrivning av kontexten",
      "time": "3:42"
    }}
  ]
}}

H\u00e4r \u00e4r transkriptionen:

---
{transcript}
---

Totall\u00e4ngd p\u00e5 videon: cirka {duration} minuter."""


# Chunk prompt: summarize one part of a longer video
CHUNK_PROMPT = """\
Du \u00e4r en expert p\u00e5 att sammanfatta podcast-avsnitt och videoinneh\u00e5ll.

Detta \u00e4r del {chunk_num} av {total_chunks} fr\u00e5n en video som \u00e4r totalt {total_duration} minuter l\u00e5ng.
Denna del t\u00e4cker tidsintervallet {start_time}\u2013{end_time}.

Sammanfatta denna del i logiska kapitel. Skriv p\u00e5 {language}.

Varje kapitel ska ha:
- En kort, beskrivande rubrik p\u00e5 {language}
- En tidsperiod (t.ex. "{start_time}\u2013XX:XX") baserat p\u00e5 inneh\u00e5llet
- En kategori (ett av: introduction, background, analysis, discussion, story, deep-dive, opinion, conclusion, practical, interview)
- En sammanfattning p\u00e5 {chapter_summary_length} p\u00e5 {language}
- transcript_html: de viktigaste delarna av transkriptionen, parafraserad och komprimerad. \
Beh\u00e5ll viktiga citat ordagrant. Formatera som HTML med <h4> f\u00f6r underrubriker och <p> f\u00f6r stycken.

Plocka ocks\u00e5 ut 1\u20132 av de mest intressanta citaten (ordagrant) fr\u00e5n denna del.

Svara ENBART med giltig JSON (ingen markdown, inga kodblock):
{{
  "chapters": [
    {{
      "title": "Kapitelrubrik",
      "time": "M:SS\u2013M:SS",
      "category": "analysis",
      "summary": "Sammanfattning...",
      "transcript_html": "<h4>Underrubrik</h4><p>Text...</p>"
    }}
  ],
  "key_quotes": [
    {{
      "text": "Citat...",
      "context": "Kontext",
      "time": "M:SS"
    }}
  ]
}}

H\u00e4r \u00e4r transkriptionen f\u00f6r denna del ({start_time}\u2013{end_time}):

---
{transcript}
---"""


# Merge prompt: combine chunk results into overall summary
MERGE_PROMPT = """\
Du har f\u00e5tt kapitelsammanfattningar fr\u00e5n olika delar av en {total_duration}-minuters video.

Skapa en \u00f6vergripande titel och sammanfattning ({summary_length}) p\u00e5 {language}.

Svara ENBART med giltig JSON:
{{
  "title": "Titel p\u00e5 hela avsnittet",
  "summary": "\u00d6vergripande sammanfattning..."
}}

H\u00e4r \u00e4r kapitelrubrikerna och sammanfattningarna:

{chapter_summaries}"""


def build_prompt(transcript_text, duration_minutes, language="svenska", detail_level="medium"):
    config = DETAIL_CONFIGS.get(detail_level, DETAIL_CONFIGS["medium"])
    return SUMMARIZE_PROMPT.format(
        transcript=transcript_text,
        duration=duration_minutes,
        language=language,
        **config,
    )


def build_chunk_prompt(chunk_text, chunk_num, total_chunks, start_time, end_time,
                       total_duration, language="svenska", detail_level="medium"):
    config = DETAIL_CONFIGS.get(detail_level, DETAIL_CONFIGS["medium"])
    return CHUNK_PROMPT.format(
        transcript=chunk_text,
        chunk_num=chunk_num,
        total_chunks=total_chunks,
        start_time=start_time,
        end_time=end_time,
        total_duration=total_duration,
        language=language,
        chapter_summary_length=config["chapter_summary_length"],
    )


def build_merge_prompt(chapter_summaries_text, total_duration, language="svenska", detail_level="medium"):
    config = DETAIL_CONFIGS.get(detail_level, DETAIL_CONFIGS["medium"])
    return MERGE_PROMPT.format(
        chapter_summaries=chapter_summaries_text,
        total_duration=total_duration,
        language=language,
        summary_length=config["summary_length"],
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


def _extract_json(text):
    """Extract valid JSON from text, repairing if needed."""
    text = (text or "").strip()
    text = text.lstrip("`").rstrip("`")
    if text.startswith("json"):
        text = text[4:].strip()

    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        repaired = _try_repair_json(text)
        try:
            return json.loads(repaired)
        except (json.JSONDecodeError, ValueError):
            return None


def _format_time(minutes):
    """Format minutes as M:SS string."""
    m = int(minutes)
    s = int((minutes - m) * 60)
    return f"{m}:{s:02d}"


def _call_claude_with_keepalive(client, prompt, max_tokens=16000):
    """Call Claude, yielding keepalive pings every few seconds, then the result.

    Yields: {"type": "keepalive"} periodically, then {"type": "result", ...} at the end.
    This prevents Railway/proxy timeouts during long blocking calls.
    """
    messages = [{"role": "user", "content": prompt}]
    accumulated = ""
    last_yield_time = time.time()

    with client.messages.stream(
        model="claude-sonnet-4-20250514",
        max_tokens=max_tokens,
        messages=messages,
    ) as stream:
        for text in stream.text_stream:
            accumulated += text
            now = time.time()
            if now - last_yield_time > 5:
                yield {"type": "keepalive"}
                last_yield_time = now

        final = stream.get_final_message()

    yield {
        "type": "result",
        "text": accumulated,
        "input_tokens": final.usage.input_tokens,
        "output_tokens": final.usage.output_tokens,
        "stop_reason": final.stop_reason,
    }


def _call_claude_streaming_yielding(client, prompt, max_tokens=64000):
    """Call Claude, yielding text chunks and finally a usage dict."""
    messages = [{"role": "user", "content": prompt}]
    accumulated = ""

    with client.messages.stream(
        model="claude-sonnet-4-20250514",
        max_tokens=max_tokens,
        messages=messages,
    ) as stream:
        for text in stream.text_stream:
            accumulated += text
            yield {"type": "text", "text": text}

        final = stream.get_final_message()

    yield {
        "type": "usage",
        "input_tokens": final.usage.input_tokens,
        "output_tokens": final.usage.output_tokens,
        "stop_reason": final.stop_reason,
        "full_text": accumulated,
    }


def summarize_with_claude(transcript_text, duration_minutes, language="svenska", detail_level="medium"):
    """Single-pass summarization for short videos."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = build_prompt(transcript_text, duration_minutes, language, detail_level)

    for item in _call_claude_streaming_yielding(client, prompt, max_tokens=64000):
        yield item


def summarize_with_claude_chunked(segments, duration_minutes, language="svenska", detail_level="medium"):
    """Multi-pass chunked summarization for long videos.

    Yields: status messages, then the final combined JSON text, then usage info.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    chunks = split_segments_into_chunks(segments, CHUNK_SIZE_MINUTES)

    total_input_tokens = 0
    total_output_tokens = 0
    all_chapters = []
    all_quotes = []

    for i, chunk in enumerate(chunks):
        start_time = _format_time(chunk["start_min"])
        end_time = _format_time(chunk["end_min"])
        prompt = build_chunk_prompt(
            chunk_text=chunk["text"],
            chunk_num=i + 1,
            total_chunks=len(chunks),
            start_time=start_time,
            end_time=end_time,
            total_duration=duration_minutes,
            language=language,
            detail_level=detail_level,
        )

        yield {"type": "status", "message": f"Analyzing part {i + 1}/{len(chunks)} ({start_time}\u2013{end_time})..."}

        chunk_result = None
        for item in _call_claude_with_keepalive(client, prompt, max_tokens=16000):
            if item["type"] == "keepalive":
                yield {"type": "keepalive"}
            elif item["type"] == "result":
                chunk_result = item

        if chunk_result:
            total_input_tokens += chunk_result["input_tokens"]
            total_output_tokens += chunk_result["output_tokens"]
            chunk_data = _extract_json(chunk_result["text"])
            if chunk_data:
                all_chapters.extend(chunk_data.get("chapters", []))
                all_quotes.extend(chunk_data.get("key_quotes", []))
            else:
                app.logger.warning("Failed to parse chunk %d/%d response", i + 1, len(chunks))

    # Now generate overall title + summary
    yield {"type": "status", "message": "Creating overall summary..."}

    chapter_summaries_text = "\n".join(
        f"- [{ch.get('time', '')}] {ch.get('title', '')}: {ch.get('summary', '')}"
        for ch in all_chapters
    )
    merge_prompt = build_merge_prompt(chapter_summaries_text, duration_minutes, language, detail_level)

    merge_result = None
    for item in _call_claude_with_keepalive(client, merge_prompt, max_tokens=2000):
        if item["type"] == "keepalive":
            yield {"type": "keepalive"}
        elif item["type"] == "result":
            merge_result = item

    merge_text = merge_result["text"] if merge_result else ""
    total_input_tokens += merge_result["input_tokens"] if merge_result else 0
    total_output_tokens += merge_result["output_tokens"] if merge_result else 0

    merge_data = _extract_json(merge_text) or {}

    # Build final combined JSON
    combined = {
        "title": merge_data.get("title", "Untitled"),
        "summary": merge_data.get("summary", ""),
        "chapters": all_chapters,
        "key_quotes": all_quotes[:5],
    }

    combined_text = json.dumps(combined, ensure_ascii=False)

    # Yield the combined result as text chunks (for the frontend)
    yield {"type": "text", "text": combined_text}
    yield {
        "type": "usage",
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "stop_reason": "end_turn",
        "full_text": combined_text,
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
    """Returns (allowed, remaining)."""
    if user is None:
        now = int(time.time())
        window_start = session.get("anon_fold_window_start", 0)
        used = session.get("anon_folds_used", 0)
        if (not window_start) or now - int(window_start) > ANON_FREE_FOLD_WINDOW_SECONDS:
            session["anon_fold_window_start"] = now
            session["anon_folds_used"] = 0
            used = 0
        remaining = max(0, ANON_FREE_FOLDS - int(used))
        return remaining > 0, remaining

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


def increment_usage(user_id=None):
    if user_id is None:
        now = int(time.time())
        window_start = session.get("anon_fold_window_start", 0)
        if (not window_start) or now - int(window_start) > ANON_FREE_FOLD_WINDOW_SECONDS:
            session["anon_fold_window_start"] = now
            session["anon_folds_used"] = 1
        else:
            session["anon_folds_used"] = int(session.get("anon_folds_used", 0)) + 1
        return

    today = date.today().isoformat()
    db = get_db()
    db.execute(
        "UPDATE users SET daily_folds_used = daily_folds_used + 1, daily_folds_date = ? WHERE id = ?",
        (today, user_id),
    )
    db.commit()
    db.close()


# --------------- Routes ---------------


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    if request.path.startswith("/api/"):
        if isinstance(e, HTTPException):
            return jsonify({"error": e.description}), e.code
        app.logger.exception("Unhandled API error: %s", e)
        return jsonify({"error": "Server error. Please try again in a minute."}), 500

    if isinstance(e, HTTPException):
        return e

    app.logger.exception("Unhandled non-API error: %s", e)
    return "Internal Server Error", 500


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
        msg = "You've used your free fold. Sign in to continue." if user is None else \
              "You've reached today's limit. Upgrade to Foldly Pro for unlimited folds."
        return jsonify({"error": msg}), 429

    try:
        video_title = get_video_title(video_id)
    except Exception:
        video_title = "Unknown title"

    try:
        transcript_text, segments = get_transcript(video_id)
    except Exception as e:
        message = str(e)
        status = 503 if "temporarily blocking transcript requests" in message.lower() else 400
        return jsonify({"error": f"Could not fetch transcript: {message}"}), status

    duration = estimate_duration(segments)
    use_chunked = duration > CHUNK_THRESHOLD_MINUTES

    def generate():
        started_at = time.time()
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

            if use_chunked:
                # Chunked approach for long videos
                for item in summarize_with_claude_chunked(segments, duration, language, detail_level):
                    if item["type"] == "text":
                        full_text += item["text"]
                        yield json.dumps({"type": "chunk", "text": item["text"]}) + "\n"
                    elif item["type"] == "status":
                        yield json.dumps({"type": "status", "message": item["message"]}) + "\n"
                    elif item["type"] == "keepalive":
                        yield json.dumps({"type": "keepalive"}) + "\n"
                    elif item["type"] == "usage":
                        usage_info = item
                        full_text = item.get("full_text", full_text)
            else:
                # Single-pass for short videos
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

            increment_usage(user["id"] if user else None)

            elapsed_s = round(time.time() - started_at, 2)
            yield json.dumps({
                "type": "done",
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_sek": cost,
                "share_token": share_token,
                "fold_id": fold_id,
                "stop_reason": stop_reason,
                "debug": {
                    "duration_minutes": duration,
                    "segments_count": len(segments),
                    "transcript_chars": len(transcript_text),
                    "response_chars": len(full_text),
                    "elapsed_seconds": elapsed_s,
                    "chunked": use_chunked,
                },
            }) + "\n"

        except anthropic.BadRequestError as e:
            yield json.dumps({"type": "error", "error": f"API-fel: {e.message}"}) + "\n"
        except anthropic.AuthenticationError:
            yield json.dumps({"type": "error", "error": "Invalid API key."}) + "\n"
        except anthropic.APIError as e:
            yield json.dumps({"type": "error", "error": f"API-fel: {e.message}"}) + "\n"
        except Exception as e:
            if full_text:
                repaired = _try_repair_json(full_text)
                if repaired != full_text:
                    extra = repaired[len(full_text):]
                    yield json.dumps({"type": "chunk", "text": extra}) + "\n"
                elapsed_s = round(time.time() - started_at, 2)
                yield json.dumps({
                    "type": "done",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_sek": 0,
                    "share_token": None,
                    "fold_id": None,
                    "stop_reason": f"error: {type(e).__name__}",
                    "warning": f"Stream interrupted: {e}",
                    "debug": {
                        "duration_minutes": duration,
                        "segments_count": len(segments),
                        "transcript_chars": len(transcript_text),
                        "response_chars": len(repaired),
                        "elapsed_seconds": elapsed_s,
                        "chunked": use_chunked,
                    },
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
