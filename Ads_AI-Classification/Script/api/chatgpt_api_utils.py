import base64
import json
import logging
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

from openai import OpenAI

from Script.config import APP_CONFIG, CHATGPT_MODEL, TOKEN_PRICES, api_key
from Script.core.console_bus import emit_log

logger = logging.getLogger(__name__)

client = OpenAI(api_key=api_key)

# ----------------------
# Thread-safe throttling / backoff (per-process)
# ----------------------

_RATE_LOCK = threading.Lock()
_PROGRESS_LOCK = threading.Lock()

_COOLDOWN_UNTIL_TS = 0.0
_LAST_REQUEST_START_TS = 0.0
_MIN_INTERVAL_SEC = 0.0

_MAX_TOKENS = APP_CONFIG.get("params", {}).get("chatgpt_max_tokens", 500)


def set_request_min_interval(seconds: float) -> None:
    global _MIN_INTERVAL_SEC
    try:
        sec = float(seconds or 0.0)
    except Exception: # noqa
        sec = 0.0

    if sec < 0:
        sec = 0.0

    with _RATE_LOCK:
        _MIN_INTERVAL_SEC = sec


def _reserve_request_slot(extra_min_interval_sec: float = 0.0) -> float:
    global _LAST_REQUEST_START_TS

    try:
        extra = float(extra_min_interval_sec or 0.0)
    except Exception: # noqa
        extra = 0.0

    if extra < 0:
        extra = 0.0

    with _RATE_LOCK:
        now = time.time()
        min_interval = max(_MIN_INTERVAL_SEC, extra)
        earliest = max(_COOLDOWN_UNTIL_TS, _LAST_REQUEST_START_TS + min_interval)
        if earliest <= now:
            _LAST_REQUEST_START_TS = now
            return 0.0

        _LAST_REQUEST_START_TS = earliest
        return float(earliest - now)


def _push_cooldown(seconds: float) -> None:
    global _COOLDOWN_UNTIL_TS

    try:
        sec = float(seconds or 0.0)
    except Exception: # noqa
        sec = 0.0

    if sec <= 0:
        return

    with _RATE_LOCK:
        _COOLDOWN_UNTIL_TS = max(_COOLDOWN_UNTIL_TS, time.time() + sec)


def _extract_status_code(exc: Exception) -> Optional[int]:
    for attr in ("status_code", "status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val

    resp = getattr(exc, "response", None)
    if resp is not None:
        for attr in ("status_code", "status"):
            val = getattr(resp, attr, None)
            if isinstance(val, int):
                return val

    return None


def _extract_retry_after(exc: Exception) -> Optional[float]:
    for attr in ("retry_after", "retry_after_seconds"):
        val = getattr(exc, attr, None)
        if isinstance(val, (int, float)) and val > 0:
            return float(val)

    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) if resp is not None else None
    if isinstance(headers, dict):
        ra = headers.get("retry-after") or headers.get("Retry-After")
        if ra is not None:
            try:
                return float(ra)
            except Exception: # noqa
                return None

    return None


def _is_retryable_exception(exc: Exception) -> bool:
    name = exc.__class__.__name__
    status = _extract_status_code(exc)

    if name in {
        "RateLimitError",
        "APITimeoutError",
        "APIConnectionError",
        "APIError",
        "InternalServerError",
        "ServiceUnavailableError",
        "APIStatusError",
    }:
        return True

    if status in {408, 409, 425, 429, 500, 502, 503, 504}:
        return True

    return False


def _model_default_reasoning_effort(model: str) -> Optional[str]:
    m = (model or "").lower().strip()
    if m.startswith("gpt-5"):
        return os.environ.get("OPENAI_REASONING_EFFORT", "minimal")
    return None


def _extract_choice_debug(response: Any) -> dict:
    try:
        ch = response.choices[0]
    except Exception: # noqa
        return {}

    msg = getattr(ch, "message", None)
    debug = {"finish_reason": getattr(ch, "finish_reason", None)}

    if msg is None:
        debug["message_present"] = False
        return debug

    debug["message_present"] = True
    debug["content_is_none"] = getattr(msg, "content", None) is None
    debug["content_len"] = len(getattr(msg, "content", "") or "")
    debug["has_tool_calls"] = bool(getattr(msg, "tool_calls", None))
    debug["has_refusal"] = bool(getattr(msg, "refusal", None))
    return debug


def _get_assistant_text_from_chat_completion(response: Any) -> str:
    try:
        msg = response.choices[0].message
    except Exception: # noqa
        return ""

    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content

    return ""


# ----------------------
# Usage / tokens helpers (universal)
# ----------------------


def _obj_to_dict(obj: Any) -> dict:
    """Convert OpenAI response objects (pydantic) to dict safely."""
    if obj is None:
        return {}

    # OpenAI python SDK objects are pydantic-like
    for method_name in ("model_dump", "to_dict", "dict"):
        method = getattr(obj, method_name, None)
        if callable(method):
            try:
                # model_dump() on pydantic v2
                return method()
            except TypeError:
                try:
                    return method(exclude_none=True)
                except Exception: # noqa
                    return method()
            except Exception: # noqa
                continue

    if isinstance(obj, dict):
        return obj

    # fallback: best-effort via __dict__
    try:
        return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
    except Exception: # noqa
        return {}


def _flatten_numeric(d: Any, prefix: str = "") -> dict[str, int | float]:
    out: dict[str, int | float] = {}

    if isinstance(d, dict):
        items = d.items()
    else:
        d2 = _obj_to_dict(d)
        items = d2.items()

    for k, v in items:
        key = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"

        if isinstance(v, bool):
            continue

        if isinstance(v, int):
            out[key] = v
            continue

        if isinstance(v, float):
            out[key] = v
            continue

        if isinstance(v, dict):
            out.update(_flatten_numeric(v, prefix=key))
            continue

        v_dict = _obj_to_dict(v)
        if v_dict:
            out.update(_flatten_numeric(v_dict, prefix=key))

    return out


def _accumulate_usage(progress: dict, prefix: str, usage_flat: dict[str, int | float]) -> None:
    if not progress:
        return

    safe_prefix = (prefix or "").strip()
    if safe_prefix:
        safe_prefix = f"{safe_prefix}_"

    for k, v in usage_flat.items():
        key = f"{safe_prefix}{k}"
        prev = progress.get(key, 0)

        if isinstance(prev, int) and isinstance(v, int):
            progress[key] = prev + v
            continue

        try:
            progress[key] = float(prev) + float(v)
        except Exception: # noqa
            pass



def _usage_to_flat_dict(response: Any) -> dict[str, float]:
    """Extract all token/cost counters from response.usage + nested details."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}

    usage_dict = _obj_to_dict(usage)
    if not usage_dict:
        return {}

    return _flatten_numeric(usage_dict, prefix="usage")

def _get_price_cfg(model: str) -> dict:
    prices = TOKEN_PRICES or {}
    if model in prices:
        return prices[model]

    # fallback: strip version suffix like "-2025-08-07"
    base = str(model).split("-20")[0]  # cheap & safe heuristic for your model naming
    if base in prices:
        return prices[base]

    return {}

def _call_openai_chat_with_retry(
    messages: list[dict[str, Any]],
    model: str,
    max_tokens: int,
    *,
    request_spacing_sec: float = 0.0,
    max_attempts: int = 6,
) -> Any:
    last_exc: Optional[Exception] = None
    reasoning_effort = _model_default_reasoning_effort(model)

    for attempt in range(max_attempts):
        wait = _reserve_request_slot(request_spacing_sec)
        if wait > 0:
            time.sleep(wait)

        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_completion_tokens": max_tokens,
            }
            if reasoning_effort:
                kwargs["reasoning_effort"] = reasoning_effort

            resp = client.chat.completions.create(**kwargs)

            text = _get_assistant_text_from_chat_completion(resp)
            if not text.strip():
                dbg = _extract_choice_debug(resp)
                emit_log(
                    f"Empty assistant content (model={model}, attempt={attempt + 1}/{max_attempts}) debug={dbg}",
                    level="WARNING",
                )
                raise RuntimeError("Empty assistant content from model")

            return resp

        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not _is_retryable_exception(exc) and not isinstance(exc, RuntimeError):
                raise

            retry_after = _extract_retry_after(exc)
            computed = min(60.0, 1.0 * (2**attempt)) + random.random()
            sleep_sec = float(retry_after) if retry_after else computed

            status = _extract_status_code(exc)
            emit_log(
                f"OpenAI request failed (attempt {attempt + 1}/{max_attempts}, status={status}): {exc}. Backoff {sleep_sec:.1f}s",
                level="WARNING",
            )

            _push_cooldown(sleep_sec)
            time.sleep(sleep_sec)

    if last_exc is not None:
        raise last_exc

    raise RuntimeError("OpenAI request failed with unknown error")


# ----------------------
# Public helpers
# ----------------------


def encode_image(image_path: str) -> str:
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")


def extract_table_from_response(header, response_text, file_names, expected_count: int | None = None):
    if not response_text:
        return None

    table_columns = [c for c in header if c != "FileName"]
    header_pattern = r"\s*" + r"\s*\|\s*".join([re.escape(c) for c in table_columns]) + r"\s*"
    header_match = re.search(header_pattern, response_text, re.IGNORECASE)
    if not header_match:
        return None

    remaining_text = response_text[header_match.end():]
    remaining_text = re.sub(r"^\s*(\|?\s*-+\s*\|?.*)\n", "", remaining_text, flags=re.MULTILINE)

    rows = remaining_text.splitlines()
    formatted_data = []
    column_count = len(table_columns)

    for row in rows:
        if not row or row.strip() == "":
            continue

        if "|" not in row and not re.match(r"^\s*\d+[\.\)]?\s+", row):
            continue

        s = row.strip()
        s = re.sub(r"^\|", "", s)
        s = re.sub(r"\|$", "", s)
        columns = re.split(r"\s*\|\s*", s)

        if len(columns) != column_count:
            continue

        first = columns[0].strip()
        idx_match = re.match(r"^(\d+)$", first)
        if not idx_match:
            continue

        idx = int(idx_match.group(1))
        if idx - 1 < 0 or idx - 1 >= len(file_names):
            continue

        row_values = [c.strip() for c in columns]
        file_name = file_names[idx - 1]
        formatted_data.append(row_values + [file_name])

    if expected_count is not None and len(formatted_data) != expected_count:
        return None

    return formatted_data


def emulate_not_found_response(header: list[str], file_names: list[str], *, missing_value: str = "Not found") -> list[list[str]]:
    safe_header = list(header or [])
    table_columns = [c for c in safe_header if c != "FileName"]
    if not table_columns:
        return []

    data: list[list[str]] = []
    for i, fn in enumerate(file_names or [], start=1):
        row_values: list[str] = []
        for j, _col in enumerate(table_columns):
            row_values.append(str(i) if j == 0 else missing_value)
        row_values.append(str(fn))
        data.append(row_values)

    return data


def main_caller(
    data,
    admin_prompt: str,
    process_type: str = "image",
    model: str = CHATGPT_MODEL,
    max_tokens: int = _MAX_TOKENS,
    progress=None,
    *,
    request_spacing_sec: float = 0.0,
):
    messages: list[dict[str, Any]] = [{"role": "system", "content": admin_prompt}]

    user_content: list[dict[str, Any]] = []
    if process_type == "image":
        data_list = data
        for idx, image_path in enumerate(data, start=1):
            encoded_image = encode_image(image_path)
            user_content.append({"type": "text", "text": f"Image {idx}:"})
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{encoded_image}", "detail": "low"},
                }
            )

    elif process_type == "video":
        data_list = []
        for i, video in enumerate(data, start=1):
            data_list.append(video["video_filename"])
            frames_base64 = video["frame_base64"]

            user_content.append({"type": "text", "text": f"Video {i}:"})
            for idx, frame_base64 in enumerate(frames_base64, start=1):
                user_content.append({"type": "text", "text": f"Frame {idx}:"})
                user_content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{frame_base64}", "detail": "low"},
                    }
                )
    else:
        raise ValueError("process_type must be 'image' or 'video'")

    messages.append({"role": "user", "content": user_content})

    t_start = time.perf_counter()
    response = _call_openai_chat_with_retry(
        messages=messages,
        model=model,
        max_tokens=max_tokens,
        request_spacing_sec=request_spacing_sec,
    )
    t_end = time.perf_counter()
    classify_sec = max(t_end - t_start, 0.0)

    descriptions = _get_assistant_text_from_chat_completion(response)
    descriptions = str(descriptions).replace("`", "").replace("  ", " ").strip()

    usage_flat = _usage_to_flat_dict(response)

    prompt_tokens = int(getattr(getattr(response, "usage", None), "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(getattr(response, "usage", None), "completion_tokens", 0) or 0)
    total_tokens = int(getattr(getattr(response, "usage", None), "total_tokens", prompt_tokens + completion_tokens) or 0)

    details = getattr(getattr(response, "usage", None), "prompt_tokens_details", None)
    cached_tokens = int(getattr(details, "cached_tokens", 0) or 0) if details is not None else 0
    in_uncached_tokens = max(prompt_tokens - cached_tokens, 0)

    price_cfg = (TOKEN_PRICES or {}).get(model, {})
    p_in_unc = float(price_cfg.get("input", 0.0))
    p_in_cached = float(price_cfg.get("cached_input", p_in_unc))
    p_out = float(price_cfg.get("output", 0.0))

    usd_in_unc = in_uncached_tokens * p_in_unc
    usd_in_cached = cached_tokens * p_in_cached
    usd_out = completion_tokens * p_out
    usd_total = usd_in_unc + usd_in_cached + usd_out

    emit_log(
        f"in={in_uncached_tokens} (${usd_in_unc:.6f}), "
        f"cached={cached_tokens} (${usd_in_cached:.6f}), "
        f"out={completion_tokens} (${usd_out:.6f}), "
        f"total={total_tokens} (${usd_total:.6f})"
    )
    if usage_flat:
        #emit_log(f"usage_flat={usage_flat}")
        pass

    if progress is not None:
        with _PROGRESS_LOCK:
            prefix = "img" if process_type == "image" else "vid"

            # --- legacy counters ---
            progress["tok_in_uncached"] = int(progress.get("tok_in_uncached", 0)) + in_uncached_tokens
            progress["tok_in_cached"] = int(progress.get("tok_in_cached", 0)) + cached_tokens
            progress["tok_out"] = int(progress.get("tok_out", 0)) + completion_tokens
            progress["tok_total"] = int(progress.get("tok_total", 0)) + total_tokens

            progress[f"{prefix}_tok_in_uncached"] = int(progress.get(f"{prefix}_tok_in_uncached", 0)) + in_uncached_tokens
            progress[f"{prefix}_tok_in_cached"] = int(progress.get(f"{prefix}_tok_in_cached", 0)) + cached_tokens
            progress[f"{prefix}_tok_out"] = int(progress.get(f"{prefix}_tok_out", 0)) + completion_tokens
            progress[f"{prefix}_tok_total"] = int(progress.get(f"{prefix}_tok_total", 0)) + total_tokens

            progress["usd_in_uncached"] = float(progress.get("usd_in_uncached", 0.0)) + usd_in_unc
            progress["usd_in_cached"] = float(progress.get("usd_in_cached", 0.0)) + usd_in_cached
            progress["usd_out"] = float(progress.get("usd_out", 0.0)) + usd_out
            progress["usd_total"] = float(progress.get("usd_total", 0.0)) + usd_total

            progress[f"{prefix}_usd_in_uncached"] = float(progress.get(f"{prefix}_usd_in_uncached", 0.0)) + usd_in_unc
            progress[f"{prefix}_usd_in_cached"] = float(progress.get(f"{prefix}_usd_in_cached", 0.0)) + usd_in_cached
            progress[f"{prefix}_usd_out"] = float(progress.get(f"{prefix}_usd_out", 0.0)) + usd_out
            progress[f"{prefix}_usd_total"] = float(progress.get(f"{prefix}_usd_total", 0.0)) + usd_total

            # --- universal usage accumulator (all token fields) ---
            _accumulate_usage(progress, prefix=prefix, usage_flat=usage_flat)
            _accumulate_usage(progress, prefix="all", usage_flat=usage_flat)

            progress["classify_time_total_creatives_sec"] = float(progress.get("classify_time_total_creatives_sec", 0.0)) + classify_sec
            if process_type == "image":
                progress["classify_time_total_banner_sec"] = float(progress.get("classify_time_total_banner_sec", 0.0)) + classify_sec
            else:
                progress["classify_time_total_video_sec"] = float(progress.get("classify_time_total_video_sec", 0.0)) + classify_sec

    emit_log(descriptions)
    return descriptions, data_list


# ----------------------
# Local tests (no CLI args)
# ----------------------


def _response_to_pretty_json(resp: Any) -> str:
    d = _obj_to_dict(resp)
    try:
        return json.dumps(d, ensure_ascii=False, indent=2)
    except Exception: # noqa
        return str(d)


def _smoke_test_text_full(model_name: str) -> dict[str, Any]:
    prompt = "Return a markdown table with columns: Index | Label. Provide 3 rows."
    msgs = [{"role": "system", "content": prompt}, {"role": "user", "content": "Go."}]

    try:
        resp = _call_openai_chat_with_retry(
            messages=msgs,
            model=model_name,
            max_tokens=200,
            request_spacing_sec=0.0,
            max_attempts=2,
        )
        text = _get_assistant_text_from_chat_completion(resp).strip()
        usage_flat = _usage_to_flat_dict(resp)
        dbg = _extract_choice_debug(resp)

        return {
            "model": model_name,
            "ok": bool(text),
            "choice_debug": dbg,
            "usage_flat": usage_flat,
            "assistant_preview": (text[:300] + "...") if len(text) > 300 else text,
            "raw_response_json": _response_to_pretty_json(resp),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "model": model_name,
            "ok": False,
            "error": str(exc),
        }


if __name__ == "__main__":
    models = ["gpt-4o", "gpt-5-mini", "gpt-5"]

    emit_log(f"Smoke test models: {models}")

    with ThreadPoolExecutor(max_workers=min(4, len(models))) as ex:
        futs = [ex.submit(_smoke_test_text_full, m) for m in models]

        for f in as_completed(futs):
            result = f.result()
            print("\n" + "=" * 120)
            print(f"MODEL: {result.get('model')}")
            print(f"OK: {result.get('ok')}")
            if "error" in result:
                print(f"ERROR: {result['error']}")
                continue

            print(f"CHOICE_DEBUG: {result.get('choice_debug')}")
            print(f"USAGE_FLAT: {result.get('usage_flat')}")
            print(f"ASSISTANT_PREVIEW:\n{result.get('assistant_preview')}")
            print("\nRAW_RESPONSE_JSON:\n")
            print(result.get("raw_response_json"))
            print("=" * 120)
