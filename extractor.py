"""
extractor.py - AI/LLM Extraction Module with Concept Remembering

Optimized for enterprise speed:
  - Parallel chunk extraction via ThreadPoolExecutor (3-4× faster)
  - Hash-based file caching (instant re-runs)
  - Deterministic smart_merge (no LLM refinement chain)
  - Pydantic-enforced JSON schema for structured extraction
  - Chain of Thought reasoning for accuracy
  - Retry logic with exponential backoff for rate limit handling
"""

import os
import re
import io
import json
import time
import hashlib
import threading
import streamlit as st
from concurrent.futures import ThreadPoolExecutor, as_completed
from pydantic import BaseModel, Field
from langchain_groq import ChatGroq
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from groq import Groq as _GroqClient
import logger as log


# ---------------------------------------------------------------------------
# Cache directory — stores extraction results keyed by file SHA-256 hash
# ---------------------------------------------------------------------------
_CACHE_DIR = os.path.join(os.path.dirname(__file__), ".rfp_cache")
os.makedirs(_CACHE_DIR, exist_ok=True)

def _cache_key(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()[:20]

def load_from_cache(file_bytes: bytes) -> dict | None:
    """Return cached extraction result if it exists for this exact file."""
    path = os.path.join(_CACHE_DIR, f"{_cache_key(file_bytes)}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                result = json.load(f)
            log.info(f"Cache HIT  → {os.path.basename(path)}", ctx="CACHE")
            return result
        except Exception as e:
            log.warning(f"Cache read failed: {e}", ctx="CACHE")
            return None
    log.debug(f"Cache MISS → {_cache_key(file_bytes)}", ctx="CACHE")
    return None

def save_to_cache(file_bytes: bytes, result: dict):
    """Persist extraction result for this file hash."""
    path = os.path.join(_CACHE_DIR, f"{_cache_key(file_bytes)}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        log.info(f"Cache SAVE → {os.path.basename(path)}", ctx="CACHE")
    except Exception as e:
        log.warning(f"Cache write failed: {e}", ctx="CACHE")


# ---------------------------------------------------------------------------
# Pydantic Schema — defines the exact JSON structure we extract
# ---------------------------------------------------------------------------
class BidInformation(BaseModel):
    """Schema for structured bid/RFP information extraction."""
    reasoning: str = Field(
        description="Your step-by-step reasoning for how you found the values for the fields below. Think carefully about each field.",
        default=""
    )
    Bid_Number: str = Field(
        description="The unique identifier or number for the Bid/RFP (e.g., JA-207652, BPM044557). Look closely at the top of the document, headers, or subject lines.",
        default=""
    )
    Title: str = Field(
        description="The title or name of the Bid/RFP.",
        default=""
    )
    Due_Date: str = Field(
        description="The final deadline, due date, and time for bid submission. Include time and timezone if available. If an Addendum changes this, use the LATEST date.",
        default=""
    )
    Bid_Submission_Type: str = Field(
        description="How the bid should be submitted. Specify if Electronic, Physical/hard copy, or both. Include portal name if applicable.",
        default=""
    )
    Term_of_Bid: str = Field(
        description="The duration or term length of the contract/bid, including any renewal options (e.g., '3 years with two 1-year renewals').",
        default=""
    )
    Pre_Bid_Meeting: str = Field(
        description="Extract Date, Time, location/link for Pre-Bid meetings. If there is no meeting, check for and extract any 'Questions Due' or Q&A deadlines.",
        default=""
    )
    Installation: str = Field(
        description="Details regarding installation requirements, deployment, delivery setup, imaging, or related services. If not specified, state 'Not specified in document'.",
        default=""
    )
    Bid_Bond_Requirement: str = Field(
        description="Is a bid bond, surety, or insurance certificate required? Specify the amount or timeframe if mentioned. If not specified, state 'Not specified in document'.",
        default=""
    )
    Delivery_Date: str = Field(
        description="Expected delivery date or timeframe. If not explicitly specified, state 'Not specified in document'.",
        default=""
    )
    Payment_Terms: str = Field(
        description="Terms of payment (e.g., Net 30, Net 45). DO NOT INVENT. If not explicitly stated, state 'Not specified in document'.",
        default=""
    )
    Any_Additional_Documentation_Required: str = Field(
        description="List ALL required additional documents (e.g., Affidavits, CIQ, HB89, W-9, Conflict of Interest, Mercury Affidavit, Contract Affidavit, insurance certificates).",
        default=""
    )
    MFG_for_Registration: str = Field(
        description="Manufacturer registration information or requirements. If not explicitly stated, state 'Not specified in document'.",
        default=""
    )
    Contract_or_Cooperative_to_use: str = Field(
        description="Any specific contract vehicle or cooperative purchasing agreement mentioned (e.g., DIR, NASPO, EPIC6, Central Texas Purchasing Alliance, CATS+).",
        default=""
    )
    Model_no: str = Field(
        description="Specific model numbers requested or referenced. If not specified, state 'Not specified in document'.",
        default=""
    )
    Part_no: str = Field(
        description="Specific part numbers, SKUs, Item Numbers, or SI# requested. If not specified, state 'Not specified in document'.",
        default=""
    )
    Product: str = Field(
        description="General description of the products, computing devices, or services requested.",
        default=""
    )
    contact_info: str = Field(
        description="Contact information (Full Name, Email, Phone number) for ALL listed contacts, including procurement officers, buyers, AND on-site contacts.",
        default=""
    )
    company_name: str = Field(
        description="The name of the issuing company, agency, school district, or department.",
        default=""
    )
    Bid_Summary: str = Field(
        description="A brief but comprehensive summary (2-4 sentences) of what the bid is asking for.",
        default=""
    )
    Product_Specification: str = Field(
        description="Detailed specifications of the products/services. You MUST include the exact Quantities (Qty) requested, along with RAM, storage, processor, screen size, OS requirements, etc.",
        default=""
    )


# Output parser to enforce JSON schema
output_parser = JsonOutputParser(pydantic_object=BidInformation)


# ---------------------------------------------------------------------------
# Token Tracker — estimates token usage for UI visibility
# ---------------------------------------------------------------------------
class TokenTracker:
    """Track estimated token usage across LLM calls."""

    def __init__(self):
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.call_count = 0
        self.errors = []
        self._lock = threading.Lock()

    @property
    def total_tokens(self):
        return self.total_input_tokens + self.total_output_tokens

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Rough estimate: 1 token ≈ 4 characters for English text."""
        return len(text) // 4

    def log_call(self, input_text: str, output_text: str):
        with self._lock:
            self.total_input_tokens += self.estimate_tokens(input_text)
            self.total_output_tokens += self.estimate_tokens(output_text)
            self.call_count += 1

    def log_error(self, file_name: str, chunk_idx: int, error: str):
        with self._lock:
            self.errors.append({
                "file": file_name,
                "chunk": chunk_idx,
                "error": error,
                "timestamp": time.strftime("%H:%M:%S"),
            })

    def get_daily_usage_pct(self, daily_limit: int = 100_000) -> float:
        return (self.total_tokens / daily_limit) * 100


# Singleton tracker stored in session state
def get_tracker() -> TokenTracker:
    if "token_tracker" not in st.session_state:
        st.session_state.token_tracker = TokenTracker()
    return st.session_state.token_tracker


# ---------------------------------------------------------------------------
# LLM Initialization — keyed on model + api_key
# ---------------------------------------------------------------------------
_llm_cache: dict = {}
_llm_lock = threading.Lock()

def get_llm(model_name: str = "llama-3.1-8b-instant"):
    """Initialize and return the ChatGroq LLM instance (thread-safe)."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        log.critical("GROQ_API_KEY is not set — cannot initialize LLM", ctx="LLM")
        raise ValueError("GROQ_API_KEY environment variable is not set.")
    cache_key = (model_name, api_key[:8] + "...")
    with _llm_lock:
        real_key = (model_name, api_key)
        if real_key not in _llm_cache:
            log.info(f"Initializing LLM: {model_name}", ctx="LLM")
            _llm_cache.clear()
            _llm_cache[real_key] = ChatGroq(
                groq_api_key=api_key,
                model_name=model_name,
                temperature=0.0,
                request_timeout=90,
            )
            log.info(f"LLM ready: {model_name}", ctx="LLM")
        else:
            log.debug(f"LLM cache hit: {model_name}", ctx="LLM")
    return _llm_cache[real_key]


# ---------------------------------------------------------------------------
# Field registry — used by the slim thread-worker prompt
# ---------------------------------------------------------------------------
_SCHEMA_INSTRUCTIONS = """
You must return a valid JSON object matching exactly this schema:
{
  "reasoning": "Step-by-step reasoning of where you found the data",
  "Bid_Number": "Unique identifier (e.g., JA-207652, BPM044557)",
  "Title": "The title or name of the Bid/RFP",
  "Due_Date": "Final deadline for bid submission (include time/timezone)",
  "Bid_Submission_Type": "Electronic, Physical/hard copy, or both",
  "Term_of_Bid": "Duration of contract and renewal options",
  "Pre_Bid_Meeting": "Date, Time, location/link, and if mandatory",
  "Installation": "Installation/deployment requirements",
  "Bid_Bond_Requirement": "Is a bid bond/insurance required?",
  "Delivery_Date": "Expected delivery timeframe",
  "Payment_Terms": "Terms of payment (e.g., Net 30)",
  "Any_Additional_Documentation_Required": "List all required forms/affidavits",
  "MFG_for_Registration": "Manufacturer registration info",
  "Contract_or_Cooperative_to_use": "Specific contract vehicle (e.g. DIR, NASPO)",
  "Model_no": "Specific model numbers requested",
  "Part_no": "Specific part numbers, SKUs",
  "Product": "General description of products/services",
  "contact_info": "Contact info (Name, Email, Phone)",
  "company_name": "Issuing company/agency/school district",
  "Bid_Summary": "Brief 2-4 sentence summary of the bid",
  "Product_Specification": "Detailed specs (Qty, RAM, storage, processor, OS)"
}
"""

_SLIM_SYSTEM = """You are an elite procurement data extractor.
Your task is to extract highly specific information from the document.
Return ONLY a valid JSON object. No explanation, no markdown.

CRITICAL RULES:
1. If a field is absent, use the exact string: "Not specified in document"
2. Do not invent or guess information.
"""

_SLIM_USER_TMPL = """{schema}

DOCUMENT:
{text}
"""


# ---------------------------------------------------------------------------
# Retry wrapper — handles 429 rate limits and transient errors
# ---------------------------------------------------------------------------
def _llm_call_with_retry(chain, invoke_args: dict, input_text_for_tracking: str,
                          max_retries: int = 4, status_placeholder=None) -> dict:
    """
    Call an LLM chain with automatic retry and exponential backoff.
    Specifically handles Groq 429 rate-limit errors.
    """
    tracker = get_tracker()
    input_tokens_est = len(input_text_for_tracking) // 4
    log.debug(f"LLM call → ~{input_tokens_est} input tokens", ctx="LLM")

    for attempt in range(max_retries):
        t0 = time.perf_counter()
        try:
            result = chain.invoke(invoke_args)
            elapsed = (time.perf_counter() - t0) * 1000
            tracker.log_call(input_text_for_tracking, json.dumps(result))
            log.info(f"LLM call SUCCESS in {elapsed:.0f}ms (attempt {attempt+1})", ctx="LLM")
            return result

        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            error_str = str(e)

            # Rate limit (429) handling
            if "rate_limit" in error_str.lower() or "429" in error_str:
                match = re.search(r"try again in (?:(\d+)m)?(\d+(?:\.\d+)?)s", error_str)
                if match:
                    minutes = int(match.group(1)) if match.group(1) else 0
                    seconds = float(match.group(2))
                    wait = (minutes * 60) + seconds + 5
                else:
                    wait = (2 ** attempt) * 15
                log.warning(
                    f"RATE LIMIT hit after {elapsed:.0f}ms — sleeping {wait:.0f}s "
                    f"(attempt {attempt+1}/{max_retries})",
                    ctx="LLM"
                )
                if status_placeholder:
                    status_placeholder.warning(
                        f"⏳ Rate limit hit. Waiting {wait:.0f}s (attempt {attempt + 1}/{max_retries})\n\n"
                        f"**Debug:** `{error_str[:300]}`"
                    )
                time.sleep(wait)
                continue

            # Daily quota exhausted
            if "rate_limit_exceeded" in error_str.lower() and "tokens per day" in error_str.lower():
                log.critical("Daily token quota exhausted (100K TPD)", ctx="LLM")
                raise Exception(
                    "🛑 Daily token quota exhausted on Groq Free Tier (100K TPD). "
                    "Please wait until tomorrow or upgrade your Groq plan."
                )

            # Transient / other errors — exponential backoff
            if attempt < max_retries - 1:
                wait = (2 ** attempt) * 5
                log.warning(
                    f"Transient error after {elapsed:.0f}ms — retry in {wait}s: {error_str[:120]}",
                    ctx="LLM"
                )
                if status_placeholder:
                    status_placeholder.warning(
                        f"⚠️ Transient error. Retrying in {wait}s "
                        f"(attempt {attempt + 2}/{max_retries})…"
                    )
                time.sleep(wait)
            else:
                log.error(f"LLM call FAILED after {max_retries} attempts: {error_str[:200]}", ctx="LLM")
                raise


# ---------------------------------------------------------------------------
# Initial Extraction — first chunk
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Thread worker — uses Groq client directly for minimum token overhead
# ---------------------------------------------------------------------------
def _extract_chunk_in_thread(idx: int, chunk: str, model_name: str) -> tuple:
    """
    Thread-safe worker — ZERO Streamlit calls.
    Uses Groq client directly (not LangChain) with:
      - Slim 200-token prompt instead of 2000-token Pydantic schema
      - response_format json_object for guaranteed fast JSON output
      - max_tokens=1500 to prevent runaway responses
    """
    tid = threading.current_thread().name
    char_count = len(chunk)
    token_est = char_count // 4
    log.info(
        f"Thread START chunk[{idx}] | chars={char_count:,} (~{token_est} tokens) | model={model_name}",
        ctx=f"THREAD-{idx}"
    )
    t_start = time.perf_counter()

    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        log.error(f"chunk[{idx}] FAILED — GROQ_API_KEY not set in thread environment", ctx=f"THREAD-{idx}")
        return idx, {}

    # Stagger parallel API requests by 31 seconds per chunk index 
    # to avoid slamming the Groq Free tier (6000 TPM limit).
    # Since 1 chunk is ~3000 tokens, 1 chunk per 30s = 6000 TPM.
    stagger_ms = idx * 31000
    if stagger_ms > 0:
        log.debug(f"chunk[{idx}] staggering start by {stagger_ms}ms to avoid 429s/413s", ctx=f"THREAD-{idx}")
        time.sleep(idx * 31.0)

    client = _GroqClient(api_key=api_key)
    # Give the full chunk text (approx 3000 tokens) to the model so it isn't blinded
    current_chunk = chunk

    for attempt in range(4):
        user_msg = _SLIM_USER_TMPL.format(schema=_SCHEMA_INSTRUCTIONS, text=current_chunk)
        t_call = time.perf_counter()
        try:
            log.debug(f"chunk[{idx}] → Groq API call (attempt {attempt+1}/4)", ctx=f"THREAD-{idx}")
            resp = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": _SLIM_SYSTEM},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0,
                max_tokens=1500,
                response_format={"type": "json_object"},
            )
            call_ms = (time.perf_counter() - t_call) * 1000
            raw = resp.choices[0].message.content
            out_tokens = len(raw) // 4
            result = json.loads(raw)
            total_ms = (time.perf_counter() - t_start) * 1000
            log.info(
                f"chunk[{idx}] SUCCESS | api={call_ms:.0f}ms total={total_ms:.0f}ms "
                f"out_tokens~{out_tokens} fields={len(result)}",
                ctx=f"THREAD-{idx}"
            )
            return idx, result if isinstance(result, dict) else {}
        except Exception as e:
            call_ms = (time.perf_counter() - t_call) * 1000
            error_str = str(e)
            
            # Handle 413 Too Large (requested > TPM limit)
            if "413" in error_str or "too large" in error_str.lower():
                log.warning(f"chunk[{idx}] 413 TOO LARGE — Slicing text in half and retrying instantly", ctx=f"THREAD-{idx}")
                # Slice the chunk in half to fit under the TPM limit
                current_chunk = current_chunk[:len(current_chunk)//2]
                continue

            if "rate_limit" in error_str.lower() or "429" in error_str:
                match = re.search(r"try again in (?:(\d+)m)?(\d+(?:\.\d+)?)s", error_str)
                wait = ((int(match.group(1) or 0) * 60) + float(match.group(2)) + 3) if match else (2 ** attempt) * 15
                log.warning(
                    f"chunk[{idx}] RATE LIMIT after {call_ms:.0f}ms — sleeping {wait:.0f}s | Reason: {error_str}",
                    ctx=f"THREAD-{idx}"
                )
                time.sleep(wait)
                continue
            if attempt < 3:
                wait = (2 ** attempt) * 3
                log.warning(
                    f"chunk[{idx}] ERROR after {call_ms:.0f}ms (attempt {attempt+1}/4) "
                    f"— retry in {wait}s: {error_str[:150]}",
                    ctx=f"THREAD-{idx}"
                )
                time.sleep(wait)
            else:
                log.error(
                    f"chunk[{idx}] FAILED all 4 attempts. Last error: {error_str[:200]}",
                    ctx=f"THREAD-{idx}"
                )
                return idx, {}
    return idx, {}


# Shared LangChain prompt for the main-thread fallback (extract_initial_data)
_EXTRACTION_PROMPT = PromptTemplate(
    template="""You are an elite AI data extractor specializing in government procurement and RFP documents.
Extract the following fields and return ONLY valid JSON.

{format_instructions}

CRITICAL RULES:
1. If a field is absent, use: Not specified in document
2. Extract exact values, do not paraphrase
3. For specifications: include RAM, storage, processor, screen size, OS

TEXT:
{text}
""",
    input_variables=["text"],
    partial_variables={"format_instructions": output_parser.get_format_instructions()},
)


def extract_initial_data(text: str, model_name: str = "llama-3.1-8b-instant",
                          status_placeholder=None) -> dict:
    """Extract baseline data — called from main thread with UI feedback."""
    llm = get_llm(model_name)
    chain = _EXTRACTION_PROMPT | llm | output_parser
    return _llm_call_with_retry(
        chain, {"text": text},
        input_text_for_tracking=text,
        status_placeholder=status_placeholder,
    )


# ---------------------------------------------------------------------------
# Refinement — Concept Remembering (used for multi-file addendum chains)
# ---------------------------------------------------------------------------
def refine_extracted_data(current_state: dict, new_text: str,
                           model_name: str = "llama-3.1-8b-instant",
                           status_placeholder=None) -> dict:
    """
    Concept Remembering: Updates existing state with new addendum/document data.
    Used when processing MULTIPLE FILES sequentially (e.g., RFP + Addendum).
    For chunks of the SAME file, smart_merge is used instead.
    """
    llm = get_llm(model_name)

    prompt = PromptTemplate(
        template="""You are an elite AI data extractor. You are processing a Bid/RFP chronologically using "Concept Remembering".
You already extracted data from previous documents. Now you are reading a NEW associated document chunk (such as an Addendum or specification sheet).
Your job is to UPDATE the current state with any new information found in the new text.

CRITICAL RULES FOR CONCEPT REMEMBERING:
1. **OVERWRITE:** If the new document explicitly changes something (e.g., extending a Due Date, changing a Pre-Bid Meeting time, modifying specifications), completely OVERWRITE the old value with the new one.
2. **APPEND/ENHANCE:** If the new document adds details (e.g., new Product Specs, additional required documentation, contact info), APPEND them to existing values.
3. **PRESERVE:** If a field is not mentioned in the new text, KEEP the existing value unchanged. Do NOT change a populated value to "Not specified in document".
4. **Chain of Thought:** Use the `reasoning` field to explain what changes you are making and why.

{format_instructions}

CURRENT STATE (JSON):
{current_state}

NEW DOCUMENT TEXT:
{new_text}
""",
        input_variables=["current_state", "new_text"],
        partial_variables={"format_instructions": output_parser.get_format_instructions()},
    )

    chain = prompt | llm | output_parser
    state_str = json.dumps(current_state, indent=2)
    combined = state_str + new_text

    return _llm_call_with_retry(
        chain,
        {"current_state": state_str, "new_text": new_text},
        input_text_for_tracking=combined,
        status_placeholder=status_placeholder,
    )


# ---------------------------------------------------------------------------
# Smart Merge — deterministic conflict resolution across parallel results
# ---------------------------------------------------------------------------
def smart_merge(partial_results: list) -> dict:
    """
    Merge N partial JSONs extracted from parallel chunks.

    Priority rules (highest to lowest):
      1. Real specific value  (len > 20 chars, no 'not specified')
      2. Short real value     (len <= 20, no 'not specified')
      3. 'Not specified in document'
      4. Empty string / None
    """
    def _score(value) -> int:
        if not value or str(value).strip() == "":
            return 0
        v = str(value).strip()
        if "not specified" in v.lower():
            return 1
        if len(v) <= 20:
            return 2
        return 3  # Long, specific real value

    merged = {}
    for partial in partial_results:
        if not isinstance(partial, dict):
            continue
        for field, value in partial.items():
            if field == "reasoning":
                continue
            if field not in merged:
                merged[field] = value
            elif _score(value) > _score(merged[field]):
                merged[field] = value
            elif _score(value) == _score(merged[field]) == 3:
                # Both have real long values — prefer longer/more detailed
                if len(str(value)) > len(str(merged[field])) * 1.2:
                    merged[field] = value

    return merged


# ---------------------------------------------------------------------------
# Parallel Extraction — main entry point for fast processing
# ---------------------------------------------------------------------------
_thread_lock = threading.Lock()

def extract_parallel(chunks: list, model_name: str = "llama-3.1-8b-instant",
                     status_placeholder=None, live_update_fn=None) -> dict:
    """
    Process all chunks SIMULTANEOUSLY using ThreadPoolExecutor.
    Worker threads use _extract_chunk_in_thread which has ZERO Streamlit calls.
    All UI updates (status_placeholder, live_update_fn) happen in the main
    thread inside the as_completed() loop — fully Streamlit-safe.
    """
    n = len(chunks)
    log.info(
        f"extract_parallel START | n_chunks={n} model={model_name}",
        ctx="PARALLEL"
    )
    t_parallel_start = time.perf_counter()

    if n == 0:
        log.warning("extract_parallel called with 0 chunks — returning empty", ctx="PARALLEL")
        return {}
    if n == 1:
        log.info("Single chunk — skipping thread pool, direct call", ctx="PARALLEL")
        _, result = _extract_chunk_in_thread(0, chunks[0], model_name)
        tracker = get_tracker()  # safe: main thread
        tracker.log_call(chunks[0], json.dumps(result))
        return result or {}

    max_workers = min(4, n)
    partial_results = [{}] * n
    done_count = [0]
    tracker = get_tracker()  # called from main thread — safe

    log.info(f"Spawning {max_workers} workers for {n} chunks", ctx="PARALLEL")

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rfp_worker") as executor:
        futures = {
            executor.submit(_extract_chunk_in_thread, i, chunk, model_name): i
            for i, chunk in enumerate(chunks)
        }
        log.debug(f"All {n} futures submitted — waiting for as_completed", ctx="PARALLEL")

        # as_completed() loop runs in main thread — all st.* calls here are safe
        for future in as_completed(futures):
            idx, result = future.result()
            partial_results[idx] = result or {}

            if result:
                tracker.log_call(chunks[idx], json.dumps(result))

            done_count[0] += 1
            done = done_count[0]
            bar = "▓" * done + "░" * (n - done)
            elapsed = (time.perf_counter() - t_parallel_start) * 1000
            log.info(
                f"Future resolved: chunk[{idx}] | {done}/{n} done | elapsed={elapsed:.0f}ms",
                ctx="PARALLEL"
            )

            if status_placeholder:
                status_placeholder.info(
                    f"**⚡ Parallel Extraction:** {done}/{n} chunks complete  `{bar}`"
                )

            if live_update_fn:
                current_merged = smart_merge([r for r in partial_results if r])
                if current_merged:
                    live_update_fn(current_merged)

    total_ms = (time.perf_counter() - t_parallel_start) * 1000
    final = smart_merge([r for r in partial_results if r])
    log.info(
        f"extract_parallel COMPLETE | total={total_ms:.0f}ms | merged_fields={len(final)}",
        ctx="PARALLEL"
    )
    return final
