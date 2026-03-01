"""
ArkainBrain — Context Window Guardian v3

Prevents context_length_exceeded AND tool_calls pairing errors.

v3 fixes over v2:
  - Image-aware token estimation (base64 images → ~1,105 tokens, not len/3)
  - List content handling in truncation (multimodal messages)
  - Sliding-window group dropping (works even with few messages)
  - Broader model limits (gpt-4.1, gemini, claude, o-series)
  - Conversation compressor for inter-stage use
  - Emergency pass that always succeeds

Usage:
    import config.context_guard  # auto-registers on import
"""

import json
import logging
import os
import re

logger = logging.getLogger("arkainbrain.context_guard")

# ── Model context limits (safe input budget ~75% of total) ──
MODEL_LIMITS = {
    # GPT-5 family
    "gpt-5": 200_000, "gpt-5-mini": 200_000,
    "gpt-5.1": 200_000, "gpt-5.2": 200_000,
    # GPT-4.1 family (1M context)
    "gpt-4.1": 750_000, "gpt-4.1-mini": 750_000, "gpt-4.1-nano": 750_000,
    # GPT-4o family (128K context)
    "gpt-4o": 96_000, "gpt-4o-mini": 96_000, "gpt-4-turbo": 96_000,
    # o-series
    "o1": 150_000, "o1-mini": 96_000, "o1-pro": 150_000,
    "o3": 150_000, "o3-mini": 150_000, "o4-mini": 150_000,
    # Anthropic via litellm
    "claude-3": 150_000, "claude-3.5": 150_000, "claude-sonnet": 150_000,
    "claude-opus": 150_000, "claude-haiku": 150_000,
    # Google via litellm (1M-2M context)
    "gemini": 800_000, "gemini-2": 800_000, "gemini-1.5": 800_000,
    "gemini-pro": 800_000, "gemini-flash": 800_000,
}
DEFAULT_LIMIT = 120_000

_LIMIT_OVERRIDE = int(os.getenv("CONTEXT_BUDGET", "0")) or None


# ═══════════════════════════════════════════════════════════
# Token estimation — IMAGE-AWARE (v3 fix)
# ═══════════════════════════════════════════════════════════

# OpenAI vision token costs (from docs):
#   "low" detail: 85 tokens flat
#   "high" detail: 85 + 170 * tiles (max ~1105 for a 2048x2048)
#   "auto": treat as high
_IMAGE_TOKEN_ESTIMATE = {
    "low": 85,
    "high": 1105,  # worst case for large images
    "auto": 1105,
}

def _is_base64_image(s):
    """Check if a string is a base64 data URI for an image."""
    if not isinstance(s, str):
        return False
    return s.startswith("data:image/") and ";base64," in s[:50]


def _est_tokens(text):
    if not text:
        return 0
    s = text if isinstance(text, str) else str(text)
    # Don't count base64 image data as text tokens
    if _is_base64_image(s):
        return _IMAGE_TOKEN_ESTIMATE["high"]
    return len(s) // 3


def _content_block_tokens(block):
    """Estimate tokens for a single content block (text or image_url)."""
    if isinstance(block, str):
        return _est_tokens(block)
    if not isinstance(block, dict):
        return _est_tokens(str(block))

    btype = block.get("type", "")

    if btype == "text":
        return _est_tokens(block.get("text", ""))

    if btype == "image_url":
        img = block.get("image_url", {})
        url = img.get("url", "") if isinstance(img, dict) else ""
        detail = img.get("detail", "auto") if isinstance(img, dict) else "auto"
        # Base64 images: use fixed token estimate, NOT base64 length
        if _is_base64_image(url):
            return _IMAGE_TOKEN_ESTIMATE.get(detail, 1105)
        # URL images: same fixed cost
        return _IMAGE_TOKEN_ESTIMATE.get(detail, 1105)

    # Unknown block type
    return _est_tokens(str(block))


def _msg_tokens(msg):
    total = 4  # per-message overhead
    if isinstance(msg, dict):
        content = msg.get("content") or ""
        tc = msg.get("tool_calls")
    elif hasattr(msg, "content"):
        content = getattr(msg, "content", "") or ""
        tc = getattr(msg, "tool_calls", None)
    else:
        return _est_tokens(str(msg))

    # Content: string or list of blocks
    if isinstance(content, str):
        total += _est_tokens(content)
    elif isinstance(content, list):
        for block in content:
            total += _content_block_tokens(block)
    else:
        total += _est_tokens(str(content))

    # Tool calls
    if tc:
        tc_str = json.dumps(tc) if isinstance(tc, (list, dict)) else str(tc)
        # Don't let tool_calls estimation go crazy either
        total += min(_est_tokens(tc_str), 10000)

    return total


def _total_tokens(messages):
    return sum(_msg_tokens(m) for m in messages) if messages else 0


def _get_limit(model):
    if _LIMIT_OVERRIDE:
        return _LIMIT_OVERRIDE
    if not model:
        return DEFAULT_LIMIT
    name = str(model).split("/")[-1] if "/" in str(model) else str(model)
    for key, lim in MODEL_LIMITS.items():
        if key == name:
            return lim
    for key, lim in MODEL_LIMITS.items():
        if key in name:
            return lim
    return DEFAULT_LIMIT


# ═══════════════════════════════════════════════════════════
# Message helpers
# ═══════════════════════════════════════════════════════════

def _truncate_content(content, max_chars):
    if not isinstance(content, str) or len(content) <= max_chars:
        return content
    half = max_chars // 2
    cut = len(content) - max_chars
    return content[:half] + f"\n\n[...{cut:,} chars truncated...]\n\n" + content[-half:]


def _summarize_content(content, max_chars=300):
    """Aggressively summarize to a brief description."""
    if not isinstance(content, str) or len(content) <= max_chars:
        return content
    first_line = content[:200].split("\n")[0][:150]
    return f"{first_line}... [{len(content):,} chars summarized]"


def _truncate_list_content(content_list, max_text_chars=3000):
    """Truncate a multimodal content list. Preserve images, truncate text."""
    if not isinstance(content_list, list):
        return content_list
    result = []
    for block in content_list:
        if isinstance(block, dict):
            btype = block.get("type", "")
            if btype == "text":
                text = block.get("text", "")
                if len(text) > max_text_chars:
                    block = dict(block)
                    block["text"] = _truncate_content(text, max_text_chars)
            # image_url blocks: keep as-is (they're fixed-cost tokens)
        elif isinstance(block, str) and len(block) > max_text_chars:
            block = _truncate_content(block, max_text_chars)
        result.append(block)
    return result


def _to_dict(msg):
    if isinstance(msg, dict):
        return dict(msg)
    try:
        if hasattr(msg, "model_dump"):
            return msg.model_dump()
        if hasattr(msg, "dict"):
            return msg.dict()
    except Exception:
        pass
    d = {}
    for key in ("role", "content", "tool_calls", "tool_call_id", "name", "function_call"):
        val = getattr(msg, key, None)
        if val is not None:
            d[key] = val
    return d if "role" in d else {"role": "user", "content": str(msg)}


def _has_tool_calls(msg):
    tc = msg.get("tool_calls") if isinstance(msg, dict) else getattr(msg, "tool_calls", None)
    return bool(tc)


# ═══════════════════════════════════════════════════════════
# Atomic group identification
# ═══════════════════════════════════════════════════════════

def _identify_groups(msgs):
    groups = []
    i = 0
    n = len(msgs)
    while i < n:
        msg = msgs[i]
        role = msg.get("role", "")
        if role == "assistant" and _has_tool_calls(msg):
            group = [i]
            j = i + 1
            while j < n and msgs[j].get("role") == "tool":
                group.append(j)
                j += 1
            groups.append(group)
            i = j
        else:
            groups.append([i])
            i += 1
    return groups


# ═══════════════════════════════════════════════════════════
# Core truncation (v3)
# ═══════════════════════════════════════════════════════════

def truncate_messages(messages, limit):
    """
    Truncate message list to fit within token limit.

    v3 strategy:
    1. Shrink TEXT in all content (string or list) > 4K → 2K
    2. Shrink tool results to summaries
    3. Sliding window: keep system + first user + last N groups
    4. Nuclear: summarize ALL text > 500 chars
    5. Emergency: system + last 3 groups only
    """
    if not messages:
        return messages

    msgs = [_to_dict(m) for m in messages]
    total = _total_tokens(msgs)
    if total <= limit:
        return msgs

    overshoot = total / max(limit, 1)
    logger.warning(f"~{total:,} tokens > {limit:,} limit ({overshoot:.1f}x). "
                   f"Truncating {len(msgs)} msgs...")

    # ── Pass 1: Shrink text content (handles both str and list) ──
    for msg in msgs:
        content = msg.get("content")
        if isinstance(content, str) and len(content) > 4000:
            msg["content"] = _truncate_content(content, 2000)
        elif isinstance(content, list):
            msg["content"] = _truncate_list_content(content, 2000)

    total = _total_tokens(msgs)
    if total <= limit:
        return _validate_structure(msgs)

    # ── Pass 2: Summarize tool results ──
    for msg in msgs:
        if msg.get("role") == "tool":
            content = msg.get("content") or ""
            if isinstance(content, str) and len(content) > 800:
                msg["content"] = _summarize_content(content, 300)

    # Also cap tool_calls JSON
    for msg in msgs:
        tc = msg.get("tool_calls")
        if tc and isinstance(tc, list) and len(json.dumps(tc)) > 5000:
            # Keep structure but truncate argument values
            for call in tc:
                if isinstance(call, dict):
                    fn = call.get("function", {})
                    if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                        args = fn["arguments"]
                        if len(args) > 500:
                            fn["arguments"] = args[:500] + "..."

    total = _total_tokens(msgs)
    if total <= limit:
        return _validate_structure(msgs)

    # ── Pass 3: Sliding window ──
    groups = _identify_groups(msgs)
    n_groups = len(groups)

    system_groups = []
    first_user_group = None
    other_groups = []

    for gi, group in enumerate(groups):
        role = msgs[group[0]].get("role", "")
        if role == "system":
            system_groups.append(gi)
        elif role == "user" and first_user_group is None:
            first_user_group = gi
        else:
            other_groups.append(gi)

    # Binary search for how many tail groups fit
    keep_n = len(other_groups)
    while keep_n > 2:
        keep_set = set(system_groups)
        if first_user_group is not None:
            keep_set.add(first_user_group)
        for gi in other_groups[-keep_n:]:
            keep_set.add(gi)

        trial = []
        for gi, group in enumerate(groups):
            if gi in keep_set:
                for idx in group:
                    trial.append(msgs[idx])
        if _total_tokens(trial) <= limit:
            break
        keep_n = max(keep_n * 2 // 3, 2)

    keep_set = set(system_groups)
    if first_user_group is not None:
        keep_set.add(first_user_group)
    for gi in other_groups[-max(keep_n, 2):]:
        keep_set.add(gi)

    dropped = n_groups - len(keep_set)
    msgs_new = []
    for gi, group in enumerate(groups):
        if gi in keep_set:
            for idx in group:
                msgs_new.append(msgs[idx])

    if dropped > 0:
        marker_pos = 1 if msgs_new and msgs_new[0].get("role") == "system" else 0
        if first_user_group is not None:
            marker_pos = min(marker_pos + 1, len(msgs_new))
        msgs_new.insert(marker_pos, {
            "role": "user",
            "content": f"[Context compressed: {dropped} conversation groups trimmed]",
        })

    msgs = msgs_new
    total = _total_tokens(msgs)
    logger.info(f"Pass 3 (sliding window): kept {len(keep_set)}/{n_groups} groups, "
                f"dropped {dropped}, ~{total:,} tokens")

    if total <= limit:
        return _validate_structure(msgs)

    # ── Pass 4: Nuclear — everything > 500 chars ──
    for msg in msgs:
        content = msg.get("content")
        if isinstance(content, str) and len(content) > 500:
            if msg.get("role") == "system":
                msg["content"] = _truncate_content(content, 1500)
            else:
                msg["content"] = _summarize_content(content, 300)
        elif isinstance(content, list):
            msg["content"] = _truncate_list_content(content, 500)

    total = _total_tokens(msgs)
    logger.warning(f"Pass 4 (nuclear): ~{total:,} tokens, {len(msgs)} msgs")

    if total <= limit:
        return _validate_structure(msgs)

    # ── Pass 5: Emergency — system + last 3 ──
    groups = _identify_groups(msgs)
    emergency = []
    for gi, group in enumerate(groups):
        if msgs[group[0]].get("role") == "system":
            for idx in group:
                emergency.append(msgs[idx])
    for group in groups[-3:]:
        if msgs[group[0]].get("role") != "system":
            for idx in group:
                emergency.append(msgs[idx])

    for msg in emergency:
        content = msg.get("content")
        if isinstance(content, str) and len(content) > 1000:
            msg["content"] = _summarize_content(content, 500)
        elif isinstance(content, list):
            msg["content"] = _truncate_list_content(content, 500)

    total = _total_tokens(emergency)
    logger.warning(f"Pass 5 (emergency): ~{total:,} tokens, {len(emergency)} msgs")
    return _validate_structure(emergency)


# ═══════════════════════════════════════════════════════════
# Conversation compressor (inter-stage)
# ═══════════════════════════════════════════════════════════

def compress_conversation(messages, target_tokens=None, model=None):
    """Compress conversation for carrying between CrewAI stages."""
    if not messages:
        return messages
    limit = _get_limit(model)
    target = target_tokens or (limit // 5)
    msgs = [_to_dict(m) for m in messages]
    total = _total_tokens(msgs)
    if total <= target:
        return msgs

    compressed = []
    for msg in msgs:
        if msg.get("role") == "system":
            c = msg.get("content", "")
            if isinstance(c, str) and len(c) > 3000:
                msg["content"] = _truncate_content(c, 3000)
            compressed.append(msg)

    user_msgs = [m for m in msgs if m.get("role") == "user"]
    asst_msgs = [m for m in msgs if m.get("role") == "assistant"]
    tool_count = sum(1 for m in msgs if m.get("role") == "tool")

    summary = (f"[Conversation summary: {len(user_msgs)} user, "
               f"{len(asst_msgs)} assistant, {tool_count} tool results]")
    compressed.append({"role": "user", "content": summary})

    if user_msgs:
        last = dict(user_msgs[-1])
        c = last.get("content", "")
        if isinstance(c, str) and len(c) > 2000:
            last["content"] = _truncate_content(c, 2000)
        compressed.append(last)
    if asst_msgs:
        last = dict(asst_msgs[-1])
        c = last.get("content", "")
        if isinstance(c, str) and len(c) > 2000:
            last["content"] = _truncate_content(c, 2000)
        last.pop("tool_calls", None)
        compressed.append(last)

    return compressed


# ═══════════════════════════════════════════════════════════
# Structure validation
# ═══════════════════════════════════════════════════════════

def _validate_structure(msgs):
    result = []
    i = 0
    n = len(msgs)
    while i < n:
        msg = msgs[i]
        role = msg.get("role", "")
        if role == "assistant" and _has_tool_calls(msg):
            tc = msg.get("tool_calls", [])
            expected_ids = set()
            if isinstance(tc, list):
                for call in tc:
                    cid = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
                    if cid:
                        expected_ids.add(cid)

            tool_msgs = []
            j = i + 1
            while j < n and msgs[j].get("role") == "tool":
                tool_msgs.append(msgs[j])
                j += 1

            found_ids = {tm.get("tool_call_id") for tm in tool_msgs if tm.get("tool_call_id")}

            if expected_ids and not expected_ids.issubset(found_ids):
                logger.warning(f"Dropping broken tool group: missing {expected_ids - found_ids}")
                i = j
                continue

            result.append(msg)
            result.extend(tool_msgs)
            i = j
        elif role == "tool":
            if result and result[-1].get("role") == "assistant" and _has_tool_calls(result[-1]):
                result.append(msg)
            else:
                logger.warning("Dropping orphan tool message")
            i += 1
        else:
            result.append(msg)
            i += 1
    return result


# ═══════════════════════════════════════════════════════════
# Error detection
# ═══════════════════════════════════════════════════════════

def _is_context_error(exc):
    s = str(exc).lower()
    return any(k in s for k in [
        "context_length_exceeded", "maximum context length",
        "input tokens exceed", "too many tokens",
    ])

def _is_tool_pairing_error(exc):
    s = str(exc).lower()
    return any(k in s for k in [
        "tool_calls", "tool_call_id",
        "must be followed by tool messages",
        "an assistant message with",
    ])


# ═══════════════════════════════════════════════════════════
# OpenAI SDK monkey-patch
# ═══════════════════════════════════════════════════════════

_patched = False

def _patch_openai():
    global _patched
    if _patched:
        return
    _patched = True

    try:
        import openai
    except ImportError:
        logger.debug("openai not installed — skipped")
        return

    try:
        from openai.resources.chat.completions import Completions
        _orig_create = Completions.create

        def _guarded_create(self, *args, **kwargs):
            messages = kwargs.get("messages")
            model = str(kwargs.get("model", ""))
            if messages:
                limit = _get_limit(model)
                est = _total_tokens(messages)
                if est > limit * 0.80:
                    logger.warning(f"Pre-flight: ~{est:,} tokens for {model} (limit {limit:,})")
                    kwargs["messages"] = truncate_messages(messages, limit)
                else:
                    kwargs["messages"] = _validate_structure(
                        [_to_dict(m) for m in messages])
            try:
                return _orig_create(self, *args, **kwargs)
            except Exception as e:
                if _is_context_error(e):
                    logger.error(f"Context overflow for {model}. Retrying at 50%...")
                    msgs = kwargs.get("messages", [])
                    kwargs["messages"] = truncate_messages(msgs, _get_limit(model) // 2)
                    return _orig_create(self, *args, **kwargs)
                if _is_tool_pairing_error(e):
                    logger.error("Tool pairing error. Re-validating...")
                    msgs = kwargs.get("messages", [])
                    kwargs["messages"] = _validate_structure([_to_dict(m) for m in msgs])
                    return _orig_create(self, *args, **kwargs)
                raise

        Completions.create = _guarded_create
        logger.info("Patched openai Completions.create (v3)")
    except Exception as e:
        logger.warning(f"Failed to patch sync: {e}")

    try:
        from openai.resources.chat.completions import AsyncCompletions
        _orig_acreate = AsyncCompletions.create

        async def _guarded_acreate(self, *args, **kwargs):
            messages = kwargs.get("messages")
            model = str(kwargs.get("model", ""))
            if messages:
                limit = _get_limit(model)
                est = _total_tokens(messages)
                if est > limit * 0.80:
                    kwargs["messages"] = truncate_messages(messages, limit)
                else:
                    kwargs["messages"] = _validate_structure(
                        [_to_dict(m) for m in messages])
            try:
                return await _orig_acreate(self, *args, **kwargs)
            except Exception as e:
                if _is_context_error(e):
                    msgs = kwargs.get("messages", [])
                    kwargs["messages"] = truncate_messages(msgs, _get_limit(model) // 2)
                    return await _orig_acreate(self, *args, **kwargs)
                if _is_tool_pairing_error(e):
                    msgs = kwargs.get("messages", [])
                    kwargs["messages"] = _validate_structure([_to_dict(m) for m in msgs])
                    return await _orig_acreate(self, *args, **kwargs)
                raise

        AsyncCompletions.create = _guarded_acreate
        logger.info("Patched openai AsyncCompletions.create (v3)")
    except Exception as e:
        logger.warning(f"Failed to patch async: {e}")


def _patch_litellm():
    try:
        import litellm
        _orig = litellm.completion

        def _guarded(*args, **kwargs):
            messages = kwargs.get("messages") or (args[1] if len(args) > 1 else None)
            model = str(kwargs.get("model") or (args[0] if args else ""))
            if messages and isinstance(messages, list):
                limit = _get_limit(model)
                est = _total_tokens(messages)
                if est > limit * 0.80:
                    truncated = truncate_messages(messages, limit)
                    kwargs["messages"] = truncated
                    if len(args) > 1:
                        args = (args[0], truncated) + args[2:]
                else:
                    clean = _validate_structure([_to_dict(m) for m in messages])
                    kwargs["messages"] = clean
                    if len(args) > 1:
                        args = (args[0], clean) + args[2:]
            try:
                return _orig(*args, **kwargs)
            except Exception as e:
                if _is_context_error(e):
                    msgs = kwargs.get("messages", [])
                    kwargs["messages"] = truncate_messages(msgs, _get_limit(model) // 2)
                    return _orig(*args, **kwargs)
                if _is_tool_pairing_error(e):
                    msgs = kwargs.get("messages", [])
                    kwargs["messages"] = _validate_structure([_to_dict(m) for m in msgs])
                    return _orig(*args, **kwargs)
                raise

        litellm.completion = _guarded
        logger.info("Patched litellm.completion (v3)")
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"Failed to patch litellm: {e}")


# Auto-register
_patch_openai()
_patch_litellm()
