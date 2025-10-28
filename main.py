"""Multi-chat Gradio UI with synchronized controls and responsive layout."""

import hashlib
import csv
import datetime as _dt
import io
import os
import tempfile
import uuid
import threading
import time
from queue import Queue, Empty
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

# --- Stream cancel registry (per chat index) ---
STREAM_CANCEL: Dict[int, threading.Event] = {}
# Global stop flag to handle early Stop clicks (before per-chat events exist)
GLOBAL_STOP = threading.Event()

def _begin_stream_cancel(idx: int) -> threading.Event:
    ev = threading.Event()
    # If a global stop was requested before this stream registered, honor it immediately.
    if GLOBAL_STOP.is_set():
        ev.set()
    STREAM_CANCEL[idx] = ev
    return ev

def _end_stream_cancel(idx: int) -> None:
    try:
        STREAM_CANCEL.pop(idx, None)
    except Exception:
        pass

def _should_cancel(idx: int) -> bool:
    ev = STREAM_CANCEL.get(idx)
    return bool(ev.is_set()) if ev is not None else False

def stop_generation(idx: int) -> bool:
    """Signal stop for a specific chat index. Returns True if a stream was active."""
    ev = STREAM_CANCEL.get(idx)
    if ev is not None:
        ev.set()
        return True
    return False

# --- Helper to stop ALL active streams ---
def stop_all_generation() -> int:
    """Signal stop for all active chat indices. Returns the number of streams signaled."""
    # Raise the global stop flag first to catch streams that haven't registered yet
    GLOBAL_STOP.set()
    indices = list(STREAM_CANCEL.keys())
    count = 0
    for i in indices:
        try:
            ev = STREAM_CANCEL.get(i)
            if ev is not None:
                ev.set()
                count += 1
        except Exception:
            pass
    return count

from dotenv import load_dotenv, set_key
import gradio as gr
import random

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore

ENV_FILE = ".env"
DEFAULT_API_BASE = "https://openrouter.ai/api/v1"

ENV_FILE_LOADED = load_dotenv(ENV_FILE)
if ENV_FILE_LOADED:
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY") or ""
    OPENROUTER_API_BASE = os.getenv("OPENROUTER_API_BASE") or DEFAULT_API_BASE
else:
    OPENROUTER_API_KEY = ""
    OPENROUTER_API_BASE = DEFAULT_API_BASE

DEFAULT_SYSTEM_PROMPT = "You are a concise, friendly assistant."
FALLBACK_MODEL_CHOICES = [
    "openrouter/auto",
    "anthropic/claude-3.5-sonnet",
    "openai/gpt-4o-mini",
]
DEFAULT_TEMPERATURE = 0.7
MAX_CHAT_SECTIONS = 10
CSV_FIELDNAMES = [
    "interaction_id",
    "request_hash",
    "chat_index",
    "message_index",
    "timestamp",
    "sync_mode",
    "system_prompt",
    "temperature",
    "model",
    "user_message",
    "assistant_response",
    "like",
]
STATE_DEFAULT_VISIBLE = [True] + [False] * (MAX_CHAT_SECTIONS - 1)


def _sanitize_log_entries(entries: Any) -> List[Dict[str, Any]]:
    """Normalize arbitrary log payloads into CSV-friendly dictionaries."""
    sanitized: List[Dict[str, Any]] = []
    if not isinstance(entries, list):
        return sanitized

    for item in entries:
        if not isinstance(item, dict):
            continue
        record = {field: item.get(field, "") for field in CSV_FIELDNAMES}
        record["like"] = bool(item.get("like", False))
        sanitized.append(record)

    return sanitized


def _build_openrouter_client() -> Optional["OpenAI"]:
    if not OPENROUTER_API_KEY or OpenAI is None:
        return None
    try:
        headers: Dict[str, str] = {}

        client_kwargs: Dict[str, Any] = {
            "api_key": OPENROUTER_API_KEY,
            "base_url": OPENROUTER_API_BASE,
        }
        if headers:
            client_kwargs["default_headers"] = headers

        return OpenAI(**client_kwargs)
    except Exception:
        return None


OPENROUTER_CLIENT = _build_openrouter_client()


# --- OpenRouter warmup helper ---
def _warm_openrouter():
    """Fire a tiny no-op request in the background to reduce first-token latency on the first message."""
    if OPENROUTER_CLIENT is None:
        return
    try:
        # Use the default (randomized) model to warm the connection. Keep it extremely small and fast.
        model = random.choice(MODEL_CHOICES) if MODEL_CHOICES else random.choice(FALLBACK_MODEL_CHOICES)
        # We intentionally avoid logging or touching UI state here.
        # A short timeout ensures we never block the UI on cold start.
        OPENROUTER_CLIENT.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            temperature=0,
            max_tokens=1,
            timeout=8
        )
    except Exception:
        # Best-effort warmup; ignore all errors.
        pass

# Kick off warmup in the background so app load isn't blocked.
threading.Thread(target=_warm_openrouter, daemon=True).start()


def _load_model_choices() -> List[str]:
    if OPENROUTER_CLIENT is None:
        return FALLBACK_MODEL_CHOICES
    try:
        response = OPENROUTER_CLIENT.models.list()
        raw_names = []
        for item in getattr(response, "data", []):
            identifier = getattr(item, "id", None)
            if not isinstance(identifier, str):
                continue
            raw_names.append(identifier)
        choices = sorted(set(raw_names))
        return choices or FALLBACK_MODEL_CHOICES
    except Exception:
        return FALLBACK_MODEL_CHOICES


MODEL_CHOICES = _load_model_choices()
DEFAULT_MODEL = (random.choice(MODEL_CHOICES) if MODEL_CHOICES else random.choice(FALLBACK_MODEL_CHOICES))


def _apply_openrouter_settings(api_key: str, api_base: str) -> Tuple[str, List[str]]:
    """Persist OpenRouter credentials and refresh model choices."""
    global OPENROUTER_API_KEY
    global OPENROUTER_API_BASE
    global OPENROUTER_CLIENT
    global MODEL_CHOICES
    global DEFAULT_MODEL

    sanitized_key = (api_key or "").strip()
    sanitized_base = (api_base or "").strip() or DEFAULT_API_BASE

    OPENROUTER_API_KEY = sanitized_key
    OPENROUTER_API_BASE = sanitized_base

    os.environ["OPENROUTER_API_KEY"] = sanitized_key
    os.environ["OPENROUTER_API_BASE"] = sanitized_base

    env_exists = os.path.exists(ENV_FILE)
    persistence_notes: List[str] = []

    if env_exists:
        try:
            set_key(ENV_FILE, "OPENROUTER_API_KEY", sanitized_key)
        except Exception as exc:  # noqa: BLE001
            persistence_notes.append(
                f"Could not update OPENROUTER_API_KEY in .env ({exc})."
            )

        try:
            set_key(ENV_FILE, "OPENROUTER_API_BASE", sanitized_base)
        except Exception as exc:  # noqa: BLE001
            persistence_notes.append(
                f"Could not update OPENROUTER_API_BASE in .env ({exc})."
            )

        if not persistence_notes:
            persistence_notes.append("Settings saved to .env.")
    else:
        persistence_notes.append(
            "No .env file detected; settings kept in-memory for this session."
        )

    OPENROUTER_CLIENT = _build_openrouter_client()
    MODEL_CHOICES = _load_model_choices()
    DEFAULT_MODEL = (random.choice(MODEL_CHOICES) if MODEL_CHOICES else random.choice(FALLBACK_MODEL_CHOICES))

    # Warm the newly configured client to reduce first-token latency on the next send
    threading.Thread(target=_warm_openrouter, daemon=True).start()

    if OPENROUTER_CLIENT is None:
        status_prefix = (
            "Settings applied, but OpenRouter client is unavailable. Verify the API key."
        )
    else:
        status_prefix = "Settings applied and OpenRouter client refreshed."

    status_message = " ".join([status_prefix, *persistence_notes]).strip()
    return status_message, MODEL_CHOICES


def update_openrouter_settings(
    api_key: str,
    api_base: str,
    *current_models: str,
):
    """Callback hooked to the Settings tab to refresh credentials and dropdowns."""
    status_message, choices = _apply_openrouter_settings(api_key, api_base)
    choices = list(choices or FALLBACK_MODEL_CHOICES)
    fallback_choice = (random.choice(choices) if choices else "")

    dropdown_updates = []
    for selected in current_models:
        new_value = selected if selected in choices else fallback_choice
        dropdown_updates.append(gr.update(choices=choices, value=new_value))

    key_update = gr.update(value="")
    base_update = gr.update(value=OPENROUTER_API_BASE)

    return (key_update, base_update, *dropdown_updates, status_message)


CSS = """
html, body {height: 100%; margin: 0; background: #f3f4f6;}
#root, .gradio-container {min-height: 100vh; display: flex;}
.app-shell {flex: 1; display: flex; flex-direction: column; gap: 1rem; padding-bottom: 1rem;}
.control-row {display: flex; gap: 0.75rem;}
.control-row button {flex: 1 1 0;}
.chat-grid {flex: 1 1 auto; display: flex; flex-wrap: wrap; gap: 1rem; align-content: flex-start;}
.chat-section {flex: 1 1 320px; min-width: 280px; border: 1px solid #e5e7eb; border-radius: 8px; padding: 1rem; background: #ffffff; display: flex; flex-direction: column; gap: 0.75rem;}
.chat-section h4 {margin: 0;}
.chat-section .gradio-chatbot, .chat-section [data-testid="chatbot"] {flex: 1 1 auto;}
.full-width {width: 100%;}
.latency { font-size: 10px; color: #9ca3af; margin-left: .4rem; }
"""

BIND_LABEL = "Bind chats"
UNBIND_LABEL = "Unbind chats"
BOUND_STATUS = "🔗 Chats are bound. Messages and clears apply to every visible chat."
UNBOUND_STATUS = (
    "🔓 Chats are unbound. Messages and clears affect only the originating chat."
)


def _prepare_chat_messages(
    message: str,
    history: Sequence[Tuple[str, str]],
    instruction: str,
) -> List[dict]:
    messages: List[dict] = []
    if instruction:
        messages.append({"role": "system", "content": instruction})
    for user_turn, assistant_turn in history:
        if user_turn:
            messages.append({"role": "user", "content": user_turn})
        if assistant_turn:
            messages.append({"role": "assistant", "content": assistant_turn})
    messages.append({"role": "user", "content": message})
    return messages


def _generate_openrouter_reply(
    message: str,
    history: Sequence[Tuple[str, str]],
    instruction: str,
    temperature: float,
    model_name: str,
) -> str:
    if OPENROUTER_CLIENT is None:
        raise RuntimeError("OpenRouter client is not available.")

    messages = _prepare_chat_messages(message, history, instruction)
    response = OPENROUTER_CLIENT.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=temperature,
        timeout=30
    )
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise RuntimeError("OpenRouter returned no choices.")

    first_choice = choices[0]
    message_obj = getattr(first_choice, "message", None)
    content = getattr(message_obj, "content", None)
    if not content:
        content = getattr(first_choice, "text", "")

    if isinstance(content, list):
        fragments = []
        for part in content:
            if isinstance(part, dict):
                text_value = part.get("text")
                if text_value:
                    fragments.append(text_value)
        content = "\n".join(fragments)

    if not content:
        raise RuntimeError("OpenRouter returned empty content.")

    return str(content).strip()


def _stream_openrouter_reply(
    message: str,
    history: Sequence[Tuple[str, str]],
    instruction: str,
    temperature: float,
    model_name: str,
) -> Iterator[str]:
    if OPENROUTER_CLIENT is None:
        raise RuntimeError("OpenRouter client is not available.")

    messages = _prepare_chat_messages(message, history, instruction)
    stream = OPENROUTER_CLIENT.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=temperature,
        stream=True,
        timeout=30
    )

    fragments: List[str] = []
    for chunk in stream:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        first_choice = choices[0]
        delta = getattr(first_choice, "delta", None)
        text_piece: Any = None
        if delta is not None:
            text_piece = getattr(delta, "content", None)
        if not text_piece:
            text_piece = getattr(first_choice, "text", None)

        if isinstance(text_piece, list):
            collected: List[str] = []
            for part in text_piece:
                if isinstance(part, dict):
                    text_value = part.get("text")
                    if text_value:
                        collected.append(str(text_value))
                elif isinstance(part, str):
                    collected.append(part)
            text_piece = "".join(collected)

        if not text_piece:
            continue

        fragments.append(str(text_piece))
        yield "".join(fragments)

    if not fragments:
        raise RuntimeError("OpenRouter returned no streamed content.")


def _stream_basic_reply(
    message: str,
    history: Sequence[Tuple[str, str]],
) -> Iterator[str]:
    yield "[no response]"


def generate_reply_stream(
    message: str,
    history: Sequence[Tuple[str, str]],
    system_prompt: str,
    temperature: float,
    model_name: str,
) -> Iterator[str]:
    """Return a streaming iterator for the reply."""
    instruction = system_prompt.strip() or DEFAULT_SYSTEM_PROMPT
    try:
        return _stream_openrouter_reply(
            message,
            history,
            instruction,
            temperature,
            model_name,
        )
    except Exception:
        return _stream_basic_reply(message, history)


def generate_reply(
    message: str,
    history: Sequence[Tuple[str, str]],
    system_prompt: str,
    temperature: float,
    model_name: str,
) -> str:
    """Compose a response that highlights the active model and instructions."""
    instruction = system_prompt.strip() or DEFAULT_SYSTEM_PROMPT
    try:
        return _generate_openrouter_reply(
            message,
            history,
            instruction,
            temperature,
            model_name,
        )
    except Exception:
        return "[no response]"


def _history_to_messages(history: Sequence[Tuple[str, str]]) -> List[Dict[str, str]]:
    """Convert tuple-based history into Chatbot message dictionaries."""
    messages: List[Dict[str, str]] = []
    for user_text, assistant_text in history:
        if user_text is not None:
            messages.append({"role": "user", "content": user_text})
        if assistant_text is not None:
            messages.append({"role": "assistant", "content": assistant_text})
    return messages


# --- Helper functions for stopped/latency tags ---
def _append_stopped_tag(a_text: str) -> str:
    """
    Append ' [stopped]' to assistant text, keeping any latency badge on a new line intact.
    If a latency badge exists, add the tag before the badge.
    """
    a_text = str(a_text or "")
    marker = 'class="latency"'
    if not a_text:
        return "[stopped]"
    if marker in a_text:
        # Keep the badge on the next line; only modify the first (main) line.
        parts = a_text.split('\n', 1)
        main_txt = parts[0]
        rest = ('\n' + parts[1]) if len(parts) > 1 else ''
        if "[stopped]" not in main_txt:
            main_txt = f"{main_txt} [stopped]"
        return main_txt + rest
    if "[stopped]" not in a_text:
        return f"{a_text} [stopped]"
    return a_text


def _inject_latency(a_text: str, ms: Optional[int]) -> str:
    """
    Ensure a latency badge is present exactly once at the end of the message (on a new line).
    If ms is None, return the text unchanged.
    """
    if ms is None:
        return str(a_text or "")
    a_text = str(a_text or "")
    badge = f'<span class="latency">{ms} ms</span>'
    if 'class="latency"' in a_text:
        return a_text
    # Always put the badge on a new line for readability.
    if a_text.endswith('\n'):
        return a_text + badge
    return a_text + '\n' + badge


def _handle_feedback_event(
    event: gr.LikeData,
    feedback_store: Optional[Dict[str, Any]],
    log_entries: Optional[Sequence[Dict[str, Any]]],
    chat_index: int,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Persist feedback selections and refresh existing log entries."""
    store: Dict[str, Any] = dict(feedback_store or {})
    entries: List[Dict[str, Any]] = [dict(entry) for entry in (log_entries or [])]

    def finalize(payload: Sequence[Dict[str, Any]]) -> Tuple[
        Dict[str, Any], List[Dict[str, Any]]
    ]:
        sanitized_payload = _sanitize_log_entries(payload)
        return store, sanitized_payload

    raw_index = getattr(event, "index", None)
    if raw_index is None:
        return finalize(entries)

    if isinstance(raw_index, int):
        message_index = max(raw_index // 2, 0)
    else:
        try:
            message_index = max(int(raw_index) // 2, 0)
        except (TypeError, ValueError):
            return finalize(entries)

    selected = getattr(event, "selected", None)
    raw_value = getattr(event, "value", None)
    if selected is None:
        if isinstance(raw_value, dict):
            selected = bool(raw_value.get("selected"))
        elif raw_value is None:
            selected = False
        else:
            selected = bool(raw_value)
    like_flag = bool(selected)

    key = f"{chat_index}:{message_index}"
    store[key] = like_flag

    like_value = store.get(key, False)
    for entry in entries:
        if (
            entry.get("chat_index") == chat_index
            and entry.get("message_index") == message_index
        ):
            entry["like"] = like_value

    return finalize(entries)


def add_chat(visible_flags: List[bool]) -> tuple:
    """Reveal the next hidden chat section and randomize its model selection.
    Ensures that existing visible chats occupy indices 0..k-1 (compacted),
    and the newly added chat appears at index k (append-to-end).
    """
    flags = list(visible_flags)

    # Compact: move all True flags to the left in order
    active_indices = [i for i, f in enumerate(flags) if f]
    k = len(active_indices)  # count of currently visible chats

    compact_flags = [False] * MAX_CHAT_SECTIONS
    for i in range(k):
        compact_flags[i] = True

    # Append new chat at the end if capacity allows
    next_index = None
    if k < MAX_CHAT_SECTIONS:
        compact_flags[k] = True
        next_index = k
        k += 1

    # Build UI updates
    section_updates = [gr.update(visible=f) for f in compact_flags]
    can_add = not all(compact_flags)

    # Model updates: only set the newly revealed section to a random model
    model_updates: List[Any] = []
    for idx in range(MAX_CHAT_SECTIONS):
        if next_index is not None and idx == next_index:
            random_model = random.choice(MODEL_CHOICES) if MODEL_CHOICES else random.choice(FALLBACK_MODEL_CHOICES)
            model_updates.append(gr.update(value=random_model))
        else:
            model_updates.append(gr.update())

    return (
        compact_flags,
        *section_updates,
        gr.update(interactive=can_add),
        *model_updates,
    )


def toggle_sync(sync_enabled: bool) -> Tuple[bool, Dict[str, Any], Dict[str, Any]]:
    """Flip synchronized controls on or off."""
    new_state = not sync_enabled
    button_text = UNBIND_LABEL if new_state else BIND_LABEL
    status_text = BOUND_STATUS if new_state else UNBOUND_STATUS
    return new_state, gr.update(value=button_text), gr.update(value=status_text)




def close_chat(index: int, visible_flags: List[bool], *args):
    """Hide a specific chat section, then compact remaining chats left."""
    size = MAX_CHAT_SECTIONS

    # Unpack state lists in fixed-size blocks
    histories = [list(args[i]) for i in range(size)]
    system_values = list(args[size : 2 * size])
    temperature_values = list(args[2 * size : 3 * size])
    model_values = list(args[3 * size : 4 * size])
    user_values = list(args[4 * size : 5 * size])

    # Copy flags and mark target index invisible if valid
    flags = list(visible_flags)
    if 0 <= index < size and flags[index]:
        flags[index] = False

    # Build list of remaining active indices in order
    active_src = [i for i, f in enumerate(flags) if f]

    # Prepare fresh containers for compacted state
    new_histories = [[] for _ in range(size)]
    new_system = [DEFAULT_SYSTEM_PROMPT for _ in range(size)]
    new_temp = [DEFAULT_TEMPERATURE for _ in range(size)]
    new_model = [DEFAULT_MODEL for _ in range(size)]
    new_user = ["" for _ in range(size)]
    new_flags = [False] * size

    # Move remaining visible chats to the left (0..k-1)
    for dst, src in enumerate(active_src):
        new_histories[dst] = list(histories[src])
        new_system[dst] = system_values[src]
        new_temp[dst] = temperature_values[src]
        new_model[dst] = model_values[src]
        new_user[dst] = user_values[src]
        new_flags[dst] = True

    # UI updates
    section_updates = [gr.update(visible=f) for f in new_flags]
    can_add = not all(new_flags)

    return (
        new_flags,
        *section_updates,
        gr.update(interactive=can_add),
        *[_history_to_messages(h) for h in new_histories],
        *new_histories,
        *new_system,
        *new_temp,
        *new_model,
        *new_user,
    )


def _pump_stream(idx: int, iterator: Iterator[str], out_q: "Queue[Tuple[int, Optional[str]]]", stop_event: threading.Event) -> None:
    """
    Consume a streaming iterator in a background thread and push partials into a shared queue.
    Sends (idx, None) once the stream is exhausted to signal completion.
    """
    try:
        for partial in iterator:
            if stop_event.is_set():
                break
            try:
                out_q.put((idx, str(partial)), block=False)
            except Exception:
                # Best effort; if queue is full/non-blocking, skip to keep thread moving.
                pass
    except Exception:
        # Swallow errors; the main thread will swap to fallback if needed.
        pass
    finally:
        try:
            out_q.put((idx, None), block=False)
        except Exception:
            pass


def dispatch_message(
    message: str,
    visible_flags: List[bool],
    sync_enabled: bool,
    origin_index: int,
    *args,
):
    """Send a message, either broadcast to all chats or targeted to one, streaming replies."""
    size = MAX_CHAT_SECTIONS
    histories = [list(args[i]) for i in range(size)]
    system_prompts = list(args[size : 2 * size])
    temperatures = list(args[2 * size : 3 * size])
    models = list(args[3 * size : 4 * size])
    user_values = list(args[4 * size : 5 * size])
    feedback_store = (
        {k: bool(v) for k, v in dict(args[5 * size]).items()}
        if len(args) > 5 * size and isinstance(args[5 * size], dict)
        else {}
    )
    existing_logs = (
        [
            {**entry, "like": bool(entry.get("like", False))}
            for entry in list(args[5 * size + 1])
        ]
        if len(args) > 5 * size + 1 and isinstance(args[5 * size + 1], list)
        else []
    )
    log_entries: List[Dict[str, Any]] = _sanitize_log_entries(existing_logs)

    def package_outputs(
        current_inputs: Sequence[str], current_logs: Sequence[Dict[str, Any]]
    ):
        display_histories = [_history_to_messages(hist) for hist in histories]
        log_payload = [dict(entry) for entry in current_logs]
        return (
            *display_histories,
            *current_inputs,
            *histories,
            log_payload,
        )

    trimmed_message = message.strip()
    if not trimmed_message:
        yield package_outputs(user_values, log_entries)
        return

    if sync_enabled:
        target_indices = [idx for idx, visible in enumerate(visible_flags) if visible]
    else:
        target_indices = [origin_index]

    valid_indices: List[int] = [
        idx for idx in target_indices if 0 <= idx < size and visible_flags[idx]
    ]

    if not valid_indices:
        yield package_outputs(user_values, log_entries)
        return

    cleared_inputs = [
        "" if idx in valid_indices else user_values[idx] for idx in range(size)
    ]

    # Measure first token latency per active chat
    start_times: Dict[int, float] = {}
    first_token_ms: Dict[int, Optional[int]] = {}

    active_generators: List[Dict[str, Any]] = []
    for idx in valid_indices:
        previous_history = histories[idx][:]
        stream_iterator = iter(
            generate_reply_stream(
                message,
                previous_history,
                system_prompts[idx],
                temperatures[idx],
                models[idx],
            )
        )
        histories[idx] = previous_history + [(message, "")]
        start_times[idx] = time.monotonic()
        first_token_ms[idx] = None
        active_generators.append(
            {
                "idx": idx,
                "iterator": stream_iterator,
                "history": previous_history,
            }
        )

    # --- Streaming section: prioritize fastest responder ---
    out_q: "Queue[Tuple[int, Optional[str]]]" = Queue()
    active_set = set()
    for entry in active_generators:
        idx = entry["idx"]
        iterator = entry["iterator"]
        stop_ev = _begin_stream_cancel(idx)
        t = threading.Thread(target=_pump_stream, args=(idx, iterator, out_q, stop_ev), daemon=True)
        t.start()
        active_set.add(idx)

    yield package_outputs(cleared_inputs, log_entries)

    while active_set:
        got_item = False
        try:
            idx, payload = out_q.get(timeout=0.1)
            got_item = True
        except Empty:
            pass
        if got_item:
            # Ignore any late/stale chunks for streams that have already been removed
            if idx not in active_set:
                continue
            if payload is None:
                if idx in active_set:
                    active_set.remove(idx)
                    _end_stream_cancel(idx)
            else:
                if first_token_ms.get(idx) is None:
                    try:
                        first_token_ms[idx] = int((time.monotonic() - start_times[idx]) * 1000)
                    except Exception:
                        first_token_ms[idx] = None
                histories[idx][-1] = (message, _inject_latency(str(payload), first_token_ms.get(idx)))
                yield package_outputs(cleared_inputs, log_entries)

        # Check for explicit stop signals even if no chunks arrive
        for j in list(active_set):
            if _should_cancel(j):
                active_set.remove(j)
                _end_stream_cancel(j)
                # Mark the latest assistant message as stopped for this chat
                try:
                    msg_idx = len(histories[j]) - 1
                    if msg_idx >= 0:
                        u, a = histories[j][msg_idx]
                        histories[j][msg_idx] = (u, _append_stopped_tag(a))
                except Exception:
                    pass
                # Flush an immediate UI update after stop so users see the change
                yield package_outputs(cleared_inputs, log_entries)

    for idx in list(active_set):
        _end_stream_cancel(idx)
    # All current streams are done; lower the global stop flag for future sessions
    GLOBAL_STOP.clear()

    # Ensure latency badge is present in the final message text
    for idx in valid_indices:
        msg_idx = len(histories[idx]) - 1
        if msg_idx >= 0:
            u, a = histories[idx][msg_idx]
            histories[idx][msg_idx] = (u, _inject_latency(str(a or ""), first_token_ms.get(idx)))

    if valid_indices:
        timestamp = _dt.datetime.now(_dt.timezone.utc).isoformat()
        base_id = uuid.uuid4().hex
        sync_mode = "broadcast" if sync_enabled else "single"
        new_entries: List[Dict[str, Any]] = []
        for idx in valid_indices:
            message_index = len(histories[idx]) - 1
            if message_index < 0:
                continue
            user_text, assistant_text = histories[idx][message_index]
            like_key = f"{idx}:{message_index}"
            request_hash_input = (
                f"{models[idx]}|{temperatures[idx]}|{system_prompts[idx]}"
            )
            request_hash = hashlib.md5(request_hash_input.encode("utf-8")).hexdigest()
            new_entries.append(
                {
                    "interaction_id": base_id,
                    "request_hash": request_hash,
                    "chat_index": idx,
                    "message_index": message_index,
                    "timestamp": timestamp,
                    "sync_mode": sync_mode,
                    "system_prompt": system_prompts[idx],
                    "temperature": temperatures[idx],
                    "model": models[idx],
                    "user_message": user_text,
                    "assistant_response": assistant_text,
                    "like": bool(feedback_store.get(like_key, False)),
                }
            )

        if new_entries:
            log_entries.extend(new_entries)

    yield package_outputs(cleared_inputs, log_entries)


def send_or_stop(
    message: str,
    visible_flags: List[bool],
    sync_enabled: bool,
    origin_index: int,
    is_streaming: bool,
    *args,
):
    """
    Single-button handler per chat:
    - In bind (sync) mode: starting a send will flip ALL visible chats' buttons to 'Stop' and mark them streaming=True.
    - Pressing Stop stops ONLY the clicked chat (individual stop), even in bind mode. Other chats keep streaming and keep 'Stop'.
    - Outputs now include updates for ALL action buttons and ALL streaming state flags.
    """
    size = MAX_CHAT_SECTIONS
    histories = [list(args[i]) for i in range(size)]
    system_prompts = list(args[size : 2 * size])
    temperatures = list(args[2 * size : 3 * size])
    models = list(args[3 * size : 4 * size])
    user_values = list(args[4 * size : 5 * size])
    feedback_state = (
        {k: bool(v) for k, v in dict(args[5 * size]).items()}
        if len(args) > 5 * size and isinstance(args[5 * size], dict)
        else {}
    )
    existing_logs = (
        [
            {**entry, "like": bool(entry.get("like", False))}
            for entry in list(args[5 * size + 1])
        ]
        if len(args) > 5 * size + 1 and isinstance(args[5 * size + 1], list)
        else []
    )

    # Stop ALL ongoing streams (global stop), regardless of bind state or origin
    stopped_count = stop_all_generation()
    did_signal_stop = stopped_count > 0

    # Helper to package a no-op output (used when stopping)
    def package_current(button_updates: List[Any], streaming_values: List[bool]):
        display_histories = [_history_to_messages(hist) for hist in histories]
        log_payload = existing_logs
        return (
            *display_histories,
            *user_values,
            *histories,
            log_payload,
            *button_updates,
            *streaming_values,
        )

    if is_streaming or did_signal_stop:
        # Global stop: apply [stopped] tag to ALL chats that have at least one assistant message
        size_local = len(histories)
        # Apply [stopped] tag to ALL chats that have at least one assistant message
        target_indices = list(range(size_local))

        for i in target_indices:
            try:
                if 0 <= i < size_local and histories[i]:
                    u, a = histories[i][-1]
                    histories[i][-1] = (u, _append_stopped_tag(a))
            except Exception:
                pass

        # After a global stop, ALL buttons return to Send and streaming becomes False
        button_updates = []
        streaming_values = []
        for i in range(size_local):
            # After a global stop, ALL buttons return to Send and streaming becomes False
            button_updates.append(gr.update(value="Send", variant="primary"))
            streaming_values.append(False)
        yield (*package_current(button_updates, streaming_values),)
        return

    # Starting a new stream: delegate to dispatch_message and append our button/state updates
    GLOBAL_STOP.clear()
    stream = dispatch_message(
        message,
        visible_flags,
        sync_enabled,
        origin_index,
        *args,
    )
    # Determine which chats should show as streaming
    if sync_enabled:
        start_targets = [i for i in range(len(histories)) if i < len(visible_flags) and bool(visible_flags[i])]
    else:
        start_targets = [origin_index]
    any_yield = False
    for out in stream:
        any_yield = True
        button_updates = []
        streaming_values = []
        for i in range(len(histories)):
            if i in start_targets:
                button_updates.append(gr.update(value="Stop", variant="secondary"))
                streaming_values.append(True)
            else:
                button_updates.append(gr.update())
                streaming_values.append(False)
        yield (*out, *button_updates, *streaming_values)
    # After streaming completes, ensure all targets return to 'Send' and streaming False
    if any_yield:
        button_updates = []
        streaming_values = []
        for i in range(len(histories)):
            if i in start_targets:
                button_updates.append(gr.update(value="Send", variant="primary"))
                streaming_values.append(False)
            else:
                button_updates.append(gr.update())
                streaming_values.append(False)
        yield (*out, *button_updates, *streaming_values)

def download_logs(log_entries: Sequence[Dict[str, Any]]) -> Optional[str]:
    """Serialize the collected logs into a CSV payload for download."""
    if not log_entries:
        return None

    temp_dir = tempfile.mkdtemp(prefix="multichat-logs-")
    file_path = os.path.join(temp_dir, "chat_logs.csv")

    with open(file_path, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for entry in log_entries:
            writer.writerow({field: entry.get(field, "") for field in CSV_FIELDNAMES})

    return file_path


def _sanitize_temperature(value: Any) -> float:
    """Ensure temperature values remain within the 0-1 slider bounds."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TEMPERATURE
    return min(max(numeric, 0.0), 1.0)


def _sanitize_model(value: Any) -> str:
    """Clamp model choices to known entries, defaulting to a fresh random choice each time."""
    if isinstance(value, str) and value in MODEL_CHOICES:
        return value
    return random.choice(MODEL_CHOICES) if MODEL_CHOICES else random.choice(FALLBACK_MODEL_CHOICES)

# --- Propagate helpers ---
def propagate_system(value: Any, visible_flags: List[bool], origin_index: int):
    """Propagate the system prompt to all other VISIBLE chats (excluding origin)."""
    text = str(value or "")
    updates: List[Any] = []
    for i in range(MAX_CHAT_SECTIONS):
        if i != origin_index and i < len(visible_flags) and visible_flags[i]:
            updates.append(gr.update(value=text))
        else:
            updates.append(gr.update())
    return tuple(updates)


def propagate_temperature(value: Any, visible_flags: List[bool], origin_index: int):
    """Propagate the temperature to all other VISIBLE chats (excluding origin)."""
    temp = _sanitize_temperature(value)
    updates: List[Any] = []
    for i in range(MAX_CHAT_SECTIONS):
        if i != origin_index and i < len(visible_flags) and visible_flags[i]:
            updates.append(gr.update(value=temp))
        else:
            updates.append(gr.update())
    return tuple(updates)


def propagate_model(value: Any, visible_flags: List[bool], origin_index: int):
    """Propagate the model to all other VISIBLE chats (excluding origin)."""
    model = _sanitize_model(value)
    updates: List[Any] = []
    for i in range(MAX_CHAT_SECTIONS):
        if i != origin_index and i < len(visible_flags) and visible_flags[i]:
            updates.append(gr.update(value=model))
        else:
            updates.append(gr.update())
    return tuple(updates)


def _reconstruct_session_from_logs(
    logs: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Derive chat histories and control states from persisted logs."""
    histories: List[List[Tuple[str, str]]] = [[] for _ in range(MAX_CHAT_SECTIONS)]
    system_prompts = [DEFAULT_SYSTEM_PROMPT for _ in range(MAX_CHAT_SECTIONS)]
    temperatures = [DEFAULT_TEMPERATURE for _ in range(MAX_CHAT_SECTIONS)]
    models = [
        (random.choice(MODEL_CHOICES) if MODEL_CHOICES else random.choice(FALLBACK_MODEL_CHOICES))
        for _ in range(MAX_CHAT_SECTIONS)
    ]
    visible_flags = [False for _ in range(MAX_CHAT_SECTIONS)]
    feedback_state: Dict[str, bool] = {}
    sync_enabled = True

    for entry in logs:
        try:
            chat_index = int(entry.get("chat_index", 0))
        except (TypeError, ValueError):
            continue
        if not (0 <= chat_index < MAX_CHAT_SECTIONS):
            continue

        try:
            message_index = int(entry.get("message_index", 0))
        except (TypeError, ValueError):
            message_index = len(histories[chat_index])

        while len(histories[chat_index]) <= message_index:
            histories[chat_index].append(("", ""))

        user_text = entry.get("user_message", "")
        assistant_text = entry.get("assistant_response", "")
        histories[chat_index][message_index] = (
            str(user_text or ""),
            str(assistant_text or ""),
        )

        prompt_value = entry.get("system_prompt")
        if isinstance(prompt_value, str) and prompt_value.strip():
            system_prompts[chat_index] = prompt_value

        temperatures[chat_index] = _sanitize_temperature(entry.get("temperature"))
        models[chat_index] = _sanitize_model(entry.get("model"))
        visible_flags[chat_index] = True

        feedback_key = f"{chat_index}:{message_index}"
        feedback_state[feedback_key] = bool(entry.get("like", False))

        sync_mode = entry.get("sync_mode")
        if isinstance(sync_mode, str):
            sync_enabled = sync_mode == "broadcast"

    if not any(visible_flags):
        visible_flags = list(STATE_DEFAULT_VISIBLE)

    chatbot_payloads = [_history_to_messages(history) for history in histories]

    return {
        "visible_flags": visible_flags,
        "histories": histories,
        "chatbot_payloads": chatbot_payloads,
        "system_prompts": system_prompts,
        "temperatures": temperatures,
        "models": models,
        "feedback_state": feedback_state,
        "sync_enabled": sync_enabled,
    }


def load_cached_session(
    cached_logs: Optional[Any] = None,
) -> Tuple[Any, ...]:
    """Hydrate the UI state with cached logs and reconstructed session data."""
    sanitized_logs = _sanitize_log_entries(cached_logs)
    session = _reconstruct_session_from_logs(sanitized_logs)

    histories = session["histories"]
    chatbot_payloads = session["chatbot_payloads"]

    user_inputs = ["" for _ in range(MAX_CHAT_SECTIONS)]
    section_updates = [gr.update(visible=flag) for flag in session["visible_flags"]]
    can_add = not all(session["visible_flags"])
    add_button_state = gr.update(interactive=can_add)

    return (
        session["visible_flags"],
        session["sync_enabled"],
        session["feedback_state"],
        sanitized_logs,
        *section_updates,
        add_button_state,
        *chatbot_payloads,
        *user_inputs,
        *histories,
        *session["system_prompts"],
        *session["temperatures"],
        *session["models"],
        *[gr.update(value="Send", variant="primary") for _ in range(MAX_CHAT_SECTIONS)],
        *[False for _ in range(MAX_CHAT_SECTIONS)],
    )


def dispatch_clear(
    visible_flags: List[bool],
    sync_enabled: bool,
    origin_index: int,
    *args,
) -> tuple:
    """Clear chat history for either all visible chats or a single chat."""
    size = MAX_CHAT_SECTIONS
    histories = [list(args[i]) for i in range(size)]
    user_values = list(args[size:])

    if sync_enabled:
        target_indices = [idx for idx, visible in enumerate(visible_flags) if visible]
    else:
        target_indices = [origin_index]

    cleared_histories = [
        [] if idx in target_indices else histories[idx] for idx in range(size)
    ]
    cleared_inputs = [
        "" if idx in target_indices else user_values[idx] for idx in range(size)
    ]

    return (
        *[_history_to_messages(history) for history in cleared_histories],
        *cleared_inputs,
        *cleared_histories,
    )


def build_demo() -> gr.Blocks:
    """Assemble the Gradio Blocks demo with synchronized, horizontal chats."""
    with gr.Blocks(title="MultiChat Assistant", css=CSS) as demo:
        visible_state = gr.State([True] + [False] * (MAX_CHAT_SECTIONS - 1))
        history_states = [gr.State([]) for _ in range(MAX_CHAT_SECTIONS)]
        sync_state = gr.State(True)
        index_states = [gr.State(i) for i in range(MAX_CHAT_SECTIONS)]
        feedback_state = gr.State({})
        log_state = gr.State([])

        model_dropdowns: List[gr.Dropdown] = []

        with gr.Tabs():
            with gr.TabItem("Chat"):
                with gr.Column(elem_classes="app-shell"):
                    gr.Markdown(
                        "### MultiChat Assistant\nManage multiple synchronized chat sessions side-by-side."
                    )

                    with gr.Row(elem_classes="control-row"):
                        add_button = gr.Button("Add chat session", variant="primary")
                        sync_toggle = gr.Button(UNBIND_LABEL, variant="secondary")
                        download_button = gr.DownloadButton(
                            label="Download CSV logs",
                            variant="secondary",
                        )

                    sync_indicator = gr.Markdown(BOUND_STATUS)

                    chat_sections: List[gr.Column] = []
                    chatbots: List[gr.Chatbot] = []
                    system_inputs: List[gr.Textbox] = []
                    temperature_sliders: List[gr.Slider] = []
                    user_inputs: List[gr.Textbox] = []
                    action_buttons: List[gr.Button] = []
                    streaming_states = [gr.State(False) for _ in range(MAX_CHAT_SECTIONS)]
                    clear_buttons: List[gr.Button] = []
                    close_buttons: List[gr.Button] = []

                    with gr.Row(elem_classes="chat-grid"):
                        for idx in range(MAX_CHAT_SECTIONS):
                            with gr.Column(
                                visible=(idx == 0),
                                elem_classes="chat-section",
                                scale=1,
                                min_width=320,
                            ) as section:
                                chat_sections.append(section)
                                # gr.Markdown(f"#### Chat {idx + 1}")
                                close_button = gr.Button("Close Chat", variant="primary")
                                close_buttons.append(close_button)

                                system_prompt = gr.Textbox(
                                    label="System prompt",
                                    value=DEFAULT_SYSTEM_PROMPT,
                                    lines=2,
                                    placeholder="Describe how this assistant should behave.",
                                    elem_classes="full-width",
                                )
                                system_inputs.append(system_prompt)
                                # --- Propagate system prompt button ---
                                sys_propagate_btn = gr.Button("Propagate", variant="secondary")
                                sys_propagate_btn.click(
                                    fn=propagate_system,
                                    inputs=[system_prompt, visible_state, index_states[idx]],
                                    outputs=system_inputs,
                                    show_progress=False,
                                )

                                temperature = gr.Slider(
                                    label="Temperature",
                                    minimum=0.0,
                                    maximum=1.0,
                                    value=DEFAULT_TEMPERATURE,
                                    step=0.05,
                                )
                                temperature_sliders.append(temperature)
                                # --- Propagate temperature button ---
                                temp_propagate_btn = gr.Button("Propagate", variant="secondary")
                                temp_propagate_btn.click(
                                    fn=propagate_temperature,
                                    inputs=[temperature, visible_state, index_states[idx]],
                                    outputs=temperature_sliders,
                                    show_progress=False,
                                )

                                model_selector = gr.Dropdown(
                                    label="Model selection",
                                    choices=MODEL_CHOICES,
                                    value=(random.choice(MODEL_CHOICES) if MODEL_CHOICES else random.choice(FALLBACK_MODEL_CHOICES)),
                                )
                                model_dropdowns.append(model_selector)
                                # --- Propagate model button ---
                                model_propagate_btn = gr.Button("Propagate", variant="secondary")
                                model_propagate_btn.click(
                                    fn=propagate_model,
                                    inputs=[model_selector, visible_state, index_states[idx]],
                                    outputs=model_dropdowns,
                                    show_progress=False,
                                )

                                chatbot = gr.Chatbot(
                                    label="Conversation",
                                    height=360,
                                    type="messages",
                                    feedback_options=("Like",),
                                )
                                chatbots.append(chatbot)

                                user_input = gr.Textbox(
                                    label="Your message",
                                    placeholder="Send a message",
                                    elem_classes="full-width",
                                )
                                user_inputs.append(user_input)

                                action_button = gr.Button(
                                    "Send",
                                    variant="primary",
                                    elem_classes="full-width",
                                )
                                action_buttons.append(action_button)

                                clear_button = gr.Button(
                                    "Clear conversation",
                                    variant="secondary",
                                    elem_classes="full-width",
                                )
                                clear_buttons.append(clear_button)

                                chatbot.like(
                                    fn=_handle_feedback_event,
                                    inputs=[feedback_state, log_state, index_states[idx]],
                                    outputs=[feedback_state, log_state],
                                    queue=False,
                                )

                    # Wire up close button click handlers after all components have been created
                    for idx in range(MAX_CHAT_SECTIONS):
                        close_buttons[idx].click(
                            fn=close_chat,
                            inputs=[
                                index_states[idx],
                                visible_state,
                                *history_states,
                                *system_inputs,
                                *temperature_sliders,
                                *model_dropdowns,
                                *user_inputs,
                            ],
                            outputs=[
                                visible_state,
                                *chat_sections,
                                add_button,
                                *chatbots,
                                *history_states,
                                *system_inputs,
                                *temperature_sliders,
                                *model_dropdowns,
                                *user_inputs,
                            ],
                            show_progress=False,
                        )

                    add_button.click(
                        fn=add_chat,
                        inputs=visible_state,
                        outputs=[visible_state, *chat_sections, add_button, *model_dropdowns],
                        show_progress=False,
                    )

                    sync_toggle.click(
                        fn=toggle_sync,
                        inputs=sync_state,
                        outputs=[sync_state, sync_toggle, sync_indicator],
                        show_progress=False,
                    )

                    download_button.click(
                        fn=download_logs,
                        inputs=[log_state],
                        outputs=download_button,
                        show_progress=False,
                    )

                    for idx in range(MAX_CHAT_SECTIONS):
                        action_send_inputs = [
                            user_inputs[idx],
                            visible_state,
                            sync_state,
                            index_states[idx],
                            streaming_states[idx],
                            *history_states,
                            *system_inputs,
                            *temperature_sliders,
                            *model_dropdowns,
                            *user_inputs,
                            feedback_state,
                            log_state,
                        ]
                        action_send_outputs = [
                            *chatbots,
                            *user_inputs,
                            *history_states,
                            log_state,
                            *action_buttons,
                            *streaming_states,
                        ]

                        action_buttons[idx].click(
                            fn=send_or_stop,
                            inputs=action_send_inputs,
                            outputs=action_send_outputs,
                            show_progress=False,
                        )

                        user_inputs[idx].submit(
                            fn=send_or_stop,
                            inputs=action_send_inputs,
                            outputs=action_send_outputs,
                            show_progress=False,
                        )

                        clear_inputs = [
                            visible_state,
                            sync_state,
                            index_states[idx],
                            *history_states,
                            *user_inputs,
                        ]
                        clear_outputs = [*chatbots, *user_inputs, *history_states]
                        clear_outputs_extended = [*clear_outputs, action_buttons[idx], streaming_states[idx]]
                        def _clear_and_reset(*cargs):
                            # cargs mirrors clear_inputs; we just call dispatch_clear and then append resets for button/state
                            gen = dispatch_clear(*cargs)
                            out = tuple(gen) if isinstance(gen, tuple) else gen
                            # dispatch_clear returns a tuple; ensure it's concrete
                            if isinstance(out, tuple):
                                return (*out, gr.update(value="Send", variant="primary"), False)
                            return (*out, gr.update(value="Send", variant="primary"), False)

                        clear_buttons[idx].click(
                            fn=_clear_and_reset,
                            inputs=clear_inputs,
                            outputs=clear_outputs_extended,
                            show_progress=False,
                        )

            with gr.TabItem("Settings"):
                with gr.Column(elem_classes="app-shell"):
                    gr.Markdown("### Settings")
                    gr.Markdown(
                        "Update the OpenRouter credentials used for chat completions."
                    )

                    api_key_input = gr.Textbox(
                        label="OpenRouter API key",
                        value=OPENROUTER_API_KEY or "",
                        placeholder="sk-or-...",
                        type="password",
                        elem_classes="full-width",
                    )

                    api_base_input = gr.Textbox(
                        label="API base URL",
                        value=OPENROUTER_API_BASE or DEFAULT_API_BASE,
                        placeholder=DEFAULT_API_BASE,
                        elem_classes="full-width",
                    )

                    save_button = gr.Button("Save settings", variant="primary")
                    settings_status = gr.Markdown("")

                    save_button.click(
                        fn=update_openrouter_settings,
                        inputs=[api_key_input, api_base_input, *model_dropdowns],
                        outputs=[
                            api_key_input,
                            api_base_input,
                            *model_dropdowns,
                            settings_status,
                        ],
                        show_progress=False,
                    )

        demo.load(
            fn=load_cached_session,
            inputs=None,
            outputs=[
                visible_state,
                sync_state,
                feedback_state,
                log_state,
                *chat_sections,
                add_button,
                *chatbots,
                *user_inputs,
                *history_states,
                *system_inputs,
                *temperature_sliders,
                *model_dropdowns,
                *action_buttons,
                *streaming_states,
            ],
            show_progress=False,
            queue=False,
        )

    return demo


if __name__ == "__main__":
    build_demo().launch(share=False)
