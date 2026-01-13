#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GhostShell — Production-ready AI-driven Windows automation agent
Enhanced with adaptive smart waiting, visual stability checks, and production hardening.

Key improvements in this version:
- OCR coordinate system is corrected by offsetting to the window's screen position
- Deterministic rule-based planner as a strong fallback when AI is unavailable or unhelpful
- Safer Gemini API key handling via GEMINI_API_KEY (no hardcoded key)
- Smarter merging between OCR text and UI controls to avoid "blind" actions

Requirements:
  pip install pywinauto pillow pytesseract
  pip install google-generativeai   # optional for Gemini integration

Environment variables:
  TESSERACT_CMD        - path to the tesseract executable (optional)
  GEMINI_API_KEY       - API key for Google Generative AI (optional)
  GHOSTSHELL_LOG       - path to log file (default: ghostshell_production.log)
  GHOSTSHELL_WORKERS   - number of thread workers (default: 3)
  GHOSTSHELL_TESS_CONF - OCR confidence threshold (default: 35.0)
  GHOSTSHELL_VISUAL_STABLE - enable visual stability check (0/1; default 0)
  GHOSTSHELL_FAILSAFE  - ask confirmation before executing actions (0/1; default 0)
  GHOSTSHELL_GEM_MODEL - Gemini model name (default: gemini-2.5-flash-lite)
  GHOSTSHELL_LAUNCH_TIMEOUT - app launch wait timeout (s; default: 10.0)
  GHOSTSHELL_STATE_TIMEOUT  - UI stability wait timeout (s; default: 4.0)
  GHOSTSHELL_MIN_STABLE     - minimum stable time to consider UI stable (s; default: 0.5)
  GHOSTSHELL_POLL_INTERVAL  - polling interval (s; default: 0.25)
  GHOSTSHELL_ACTION_RETRIES - per-action retries (default: 1)
  GHOSTSHELL_AI_RETRIES     - AI retries (default: 2)
  GHOSTSHELL_START_WAIT     - wait after opening Start menu (s; default: 0.5)
"""

import os
import sys
import time
import re
import json
import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from typing import Any, Dict, List, Optional, Tuple

# UI and image
try:
    from pywinauto import Desktop
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto import findwindows
except ImportError as e:
    raise SystemExit("pywinauto is required. Install with: pip install pywinauto") from e

try:
    from PIL import Image
except ImportError:
    raise SystemExit("Pillow is required. Install with: pip install pillow")

try:
    import pytesseract
except ImportError:
    raise SystemExit("pytesseract is required. Install with: pip install pytesseract")

# Optional Gemini client (google.generativeai)
try:
    import google.generativeai as genai  # type: ignore
    HAS_GENAI = True
except Exception:
    HAS_GENAI = False

# --- Tesseract config override ---
if os.getenv("TESSERACT_CMD"):
    pytesseract.pytesseract.tesseract_cmd = os.getenv("TESSERACT_CMD")

# --- Logging Setup ---
LOG_FILE = os.getenv("GHOSTSHELL_LOG", "ghostshell_production.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("ghostshell")

# --- Config Constants (override via env) ---
MAX_WORKERS = int(os.getenv("GHOSTSHELL_WORKERS", "3"))
OCR_CONF_THRESHOLD = float(os.getenv("GHOSTSHELL_TESS_CONF", "35.0"))
OCR_TOP_N = int(os.getenv("GHOSTSHELL_OCR_TOP", "30"))
UI_MAX_DEPTH = int(os.getenv("GHOSTSHELL_UI_DEPTH", "5"))
STEP_TIMEOUT = float(os.getenv("GHOSTSHELL_STEP_TIMEOUT", "1.2"))
AI_TEMPERATURE = float(os.getenv("GHOSTSHELL_AI_TEMP", "0.05"))
FAILSAFE_ENABLED = os.getenv("GHOSTSHELL_FAILSAFE", "0") == "1"
GEMINI_MODEL = os.getenv("GHOSTSHELL_GEM_MODEL", "gemini-2.5-flash-lite")
VISUAL_STABILITY_ENABLED = os.getenv("GHOSTSHELL_VISUAL_STABLE", "0") == "1"

# --- Enhanced Smart waiting constants ---
APP_LAUNCH_TIMEOUT = float(os.getenv("GHOSTSHELL_LAUNCH_TIMEOUT", "10.0"))
STATE_CHANGE_TIMEOUT = float(os.getenv("GHOSTSHELL_STATE_TIMEOUT", "4.0"))
MIN_STABLE_TIME = float(os.getenv("GHOSTSHELL_MIN_STABLE", "0.5"))
POLL_INTERVAL = float(os.getenv("GHOSTSHELL_POLL_INTERVAL", "0.25"))
ACTION_RETRY_COUNT = int(os.getenv("GHOSTSHELL_ACTION_RETRIES", "1"))
AI_RETRY_COUNT = int(os.getenv("GHOSTSHELL_AI_RETRIES", "2"))
START_MENU_WAIT = float(os.getenv("GHOSTSHELL_START_WAIT", "0.5"))

_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# -------------------------
# Small helpers
# -------------------------
def timer_ms() -> int:
    return int(time.time() * 1000)

def md5_hex(s: Any) -> str:
    if isinstance(s, str):
        return hashlib.md5(s.encode("utf-8")).hexdigest()
    elif isinstance(s, bytes):
        return hashlib.md5(s).hexdigest()
    return hashlib.md5(str(s).encode("utf-8")).hexdigest()

def safe_json_loads(s: str) -> Optional[Any]:
    try:
        return json.loads(s)
    except Exception:
        return None

# -------------------------
# UI Utilities
# -------------------------
def _get_foreground_wrapper_via_desktop() -> Optional[UIAWrapper]:
    try:
        desktop = Desktop(backend="uia")
        return desktop.window(top_level_only=True, active_only=True).wrapper_object()
    except Exception as e:
        logger.debug("Desktop active window fetch failed: %s", e)
        return None

def get_foreground_window() -> Optional[UIAWrapper]:
    # Try foreground handle first for better accuracy
    try:
        hwnd = findwindows.get_foreground()
        if hwnd:
            desktop = Desktop(backend="uia")
            return desktop.window(handle=hwnd).wrapper_object()
    except Exception as e:
        logger.debug("Foreground by handle failed: %s", e)
    return _get_foreground_wrapper_via_desktop()

def get_foreground_rect(wrapper: Optional[UIAWrapper]) -> Optional[Tuple[int, int, int, int]]:
    try:
        w = wrapper or get_foreground_window()
        if not w:
            return None
        r = w.element_info.rectangle
        return (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        return None

def capture_foreground_window_image(wrapper: Optional[UIAWrapper]) -> Optional[Image.Image]:
    if not wrapper:
        return None
    try:
        img = wrapper.capture_as_image()
        return img.convert("RGB")
    except Exception as e:
        logger.debug("Window image capture failed: %s", e)
        return None


def _collect_ui(control: UIAWrapper, depth: int = 0, max_depth: int = 3) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    try:
        elem_info = getattr(control, "element_info", None)
        if not elem_info:
            return items
        name = (control.window_text() or "").strip()
        ctrl_type = getattr(elem_info, "control_type", "") or ""
        rect = elem_info.rectangle
        cx, cy = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
        automation_id = getattr(elem_info, "automation_id", "") or ""
        class_name = getattr(elem_info, "class_name", "") or ""
        items.append({
            "name": name,
            "type": ctrl_type,
            "center": [int(cx), int(cy)],
            "depth": depth,
            "automation_id": automation_id,
            "class_name": class_name,
            "rect": [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)],
            "control": control
        })
        if depth < max_depth:
            for child in control.children():
                try:
                    items.extend(_collect_ui(child, depth + 1, max_depth))
                except Exception:
                    continue
    except Exception as e:
        logger.debug("_collect_ui error: %s", e)
    return items


def get_active_window_ui_tree(max_depth: int = UI_MAX_DEPTH) -> List[Dict[str, Any]]:
    wrapper = get_foreground_window()
    if not wrapper:
        return []
    try:
        return _collect_ui(wrapper, 0, max_depth)
    except Exception as e:
        logger.debug("UI tree collection failed: %s", e)
        return []

# -------------------------
# OCR
# -------------------------
def ocr_extract_text_items(
    img: Image.Image,
    conf_threshold: float = OCR_CONF_THRESHOLD,
    top_n: int = OCR_TOP_N,
    x_offset: int = 0,
    y_offset: int = 0,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if img is None:
        return out
    try:
        config = "--oem 1 --psm 6"
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, config=config)
        n = len(data.get("text", []))
        for i in range(n):
            txt = (data["text"][i] or "").strip()
            if not txt:
                continue
            try:
                conf = float(data["conf"][i])
            except Exception:
                conf = -1.0
            if conf < conf_threshold:
                continue
            left, top, w, h = (
                data.get("left", [0]*n)[i],
                data.get("top", [0]*n)[i],
                data.get("width", [0]*n)[i],
                data.get("height", [0]*n)[i],
            )
            cx, cy = int(left + w // 2 + x_offset), int(top + h // 2 + y_offset)
            out.append({"text": txt, "x": cx, "y": cy, "conf": float(conf)})
        out.sort(key=lambda x: -x["conf"])
        return out[:top_n]
    except Exception as e:
        logger.debug("OCR extraction failed: %s", e)
        return []

# -------------------------
# Enhanced Smart waiting / visual stability
# -------------------------
def get_window_title(wrapper: Optional[UIAWrapper] = None) -> str:
    if wrapper is None:
        wrapper = get_foreground_window()
    try:
        return wrapper.window_text() if wrapper else ""
    except Exception:
        return ""

def get_window_class(wrapper: Optional[UIAWrapper] = None) -> str:
    if wrapper is None:
        wrapper = get_foreground_window()
    try:
        return wrapper.element_info.class_name if wrapper else ""
    except Exception:
        return ""

def get_window_pid(wrapper: Optional[UIAWrapper] = None) -> int:
    if wrapper is None:
        wrapper = get_foreground_window()
    try:
        return int(wrapper.element_info.process_id) if wrapper else 0
    except Exception:
        return 0

def get_ui_state_signature() -> str:
    try:
        wrapper = get_foreground_window()
        if not wrapper:
            return ""
        elem = wrapper.element_info
        title = wrapper.window_text() or ""
        class_name = elem.class_name or ""
        pid = int(elem.process_id or 0)
        rect_str = f"{elem.rectangle.left},{elem.rectangle.top},{elem.rectangle.right},{elem.rectangle.bottom}"
        controls = []
        try:
            children = wrapper.children()
            for child in children[:10]:
                ctrl_name = (child.window_text() or "")[:50]
                ctrl_type = getattr(child.element_info, "control_type", "")
                ctrl_class = getattr(child.element_info, "class_name", "")
                controls.append(f"{ctrl_type}:{ctrl_class}:{ctrl_name}")
        except Exception:
            pass
        state_data = f"{title}|{class_name}|{pid}|{rect_str}|{'|'.join(controls)}"
        return md5_hex(state_data)
    except Exception:
        return ""


def get_image_signature(wrapper: Optional[UIAWrapper]) -> str:
    try:
        img = capture_foreground_window_image(wrapper)
        if not img:
            return ""
        try:
            # Pillow >= 10
            small = img.resize((64, 64), Image.Resampling.LANCZOS)
        except Exception:
            # Older Pillow fallback
            small = img.resize((64, 64), Image.LANCZOS)
        return md5_hex(small.tobytes())
    except Exception:
        return ""


def wait_for_window_change(initial_title: str, initial_class: str, initial_pid: int, timeout: float = APP_LAUNCH_TIMEOUT) -> bool:
    logger.info(f"Waiting for window change from: '{initial_title}' (class: {initial_class}, pid: {initial_pid})")
    start_time = time.time()
    while time.time() - start_time < timeout:
        wrapper = get_foreground_window()
        current_title = get_window_title(wrapper)
        current_class = get_window_class(wrapper)
        current_pid = get_window_pid(wrapper)
        if (current_title != initial_title or current_class != initial_class or current_pid != initial_pid) and current_title.strip():
            logger.info(f"Window changed to: '{current_title}' (class: {current_class}, pid: {current_pid}) after {time.time() - start_time:.1f}s")
            return True
        time.sleep(POLL_INTERVAL)
    logger.warning(f"No window change detected within {timeout}s")
    return False


def wait_for_ui_stability(timeout: float = STATE_CHANGE_TIMEOUT) -> bool:
    logger.debug("Waiting for UI to stabilize...")
    visual = VISUAL_STABILITY_ENABLED
    start_time = time.time()
    last_sig = ""
    last_img_sig = ""
    stable_since = time.time()
    while time.time() - start_time < timeout:
        wrapper = get_foreground_window()
        current_sig = get_ui_state_signature()
        current_img_sig = get_image_signature(wrapper) if visual else ""
        current_time = time.time()
        changed = current_sig != last_sig or (visual and current_img_sig != last_img_sig)
        if changed:
            stable_since = current_time
            last_sig = current_sig
            last_img_sig = current_img_sig
        elif current_time - stable_since >= MIN_STABLE_TIME:
            logger.debug(f"UI stabilized after {current_time - start_time:.1f}s (visual: {visual})")
            return True
        time.sleep(POLL_INTERVAL)
    logger.debug(f"UI did not fully stabilize within {timeout}s (continuing)")
    return True


def smart_wait_after_action(action_type: str, action_data: Dict[str, Any], **kwargs) -> None:
    app_name = kwargs.get("app_name", "").lower()
    if action_type == "app_launch":
        initial_title = get_window_title().lower()
        initial_class = get_window_class()
        initial_pid = get_window_pid()
        if app_name and app_name in initial_title:
            logger.info("App appears to be already open; performing short wait")
            time.sleep(0.5)
            wait_for_ui_stability(STATE_CHANGE_TIMEOUT)
        else:
            changed = wait_for_window_change(initial_title, initial_class, initial_pid, APP_LAUNCH_TIMEOUT)
            if changed:
                new_title = get_window_title().lower()
                if app_name and app_name not in new_title:
                    logger.warning(f"New window title '{new_title}' does not contain app name '{app_name}'")
                time.sleep(0.3)
                wait_for_ui_stability(STATE_CHANGE_TIMEOUT)
            else:
                time.sleep(1.0)
                time.sleep(2.0)
    elif action_type == "click":
        time.sleep(0.12)
        wait_for_ui_stability(2.0)
    elif action_type == "type":
        text_len = len(action_data.get("text", ""))
        time.sleep(0.03 * text_len + 0.12)
        wait_for_ui_stability(0.9)
    else:
        time.sleep(0.3)
        wait_for_ui_stability(1.0)

# -------------------------
# Unification: combine OCR + UI tree into unified elements
# -------------------------
def create_unified_ui_elements(
    ocr_items: List[Dict[str, Any]],
    ui_tree: List[Dict[str, Any]],
    screen_rect: Optional[Tuple[int, int, int, int]] = None,
) -> List[Dict[str, Any]]:
    unified: List[Dict[str, Any]] = []

    # Add OCR items (already in screen coordinates if x/y offsets were applied)
    for it in ocr_items:
        unified.append({
            "name": "text",
            "text": it.get("text", ""),
            "x": int(it.get("x", 0)),
            "y": int(it.get("y", 0)),
            "type": "text",
            "confidence": float(it.get("conf", 0.0)),
            "control": None
        })

    # Map UI control types
    map_type = {
        "Button": "btn", "MenuItem": "menu", "ListItem": "item", "TabItem": "tab",
        "CheckBox": "checkbox", "RadioButton": "radio", "ComboBox": "dropdown",
        "Edit": "input", "Text": "text", "Hyperlink": "link", "Image": "image",
        "Slider": "slider", "ScrollBar": "scroll"
    }

    for node in ui_tree:
        if "error" in node:
            continue
        center = node.get("center", [0, 0])
        if not center or len(center) != 2:
            continue
        x, y = int(center[0]), int(center[1])
        name = (node.get("name") or node.get("automation_id") or node.get("class_name") or "").strip() or "element"
        ui_type = node.get("type", "")
        short = map_type.get(ui_type, "element")
        unified.append({
            "name": short,
            "text": node.get("name", ""),
            "x": x,
            "y": y,
            "type": short,
            "ui_type": ui_type,
            "automation_id": node.get("automation_id", ""),
            "class_name": node.get("class_name", ""),
            "control": node.get("control"),
            "rect": node.get("rect", [0, 0, 0, 0])
        })

    # Merge items that are very close (now coordinates are in the same space)
    merged: List[Dict[str, Any]] = []
    PROXIMITY = 22
    for e in unified:
        found = False
        for ex in merged:
            if abs(e.get("x", 0) - ex.get("x", 0)) <= PROXIMITY and abs(e.get("y", 0) - ex.get("y", 0)) <= PROXIMITY:
                # Merge heuristics: attach OCR text to interactive elements
                if ex.get("type") != "text" and e.get("type") == "text" and not ex.get("text") and e.get("text"):
                    ex["text"] = e.get("text")
                    ex["confidence"] = e.get("confidence", 0.0)
                elif ex.get("type") == "text" and e.get("type") != "text" and not e.get("text") and ex.get("text"):
                    e["text"] = ex.get("text")
                    e["confidence"] = ex.get("confidence", 0.0)
                    merged.remove(ex)
                found = True
                break
        if not found:
            merged.append(e)

    def priority(el: Dict[str, Any]) -> float:
        conf = float(el.get("confidence", 0.0))
        has_text = 1.0 if el.get("text") else 0.0
        is_interactive = 1.0 if el.get("name") in ("btn", "menu", "link", "tab", "dropdown", "input", "item") else 0.0
        return is_interactive * 100 + has_text * 50 + conf

    merged.sort(key=priority, reverse=True)
    return merged[:40]

# -------------------------
# Gemini AI integration (wrap)
# -------------------------
SYSTEM_PROMPT = """
You are GhostShell, a precise Windows UI automation assistant.
Input: User goal, current window title, UI elements (name, text, x, y, type, ui_type, rect), and screen size.
Output: JSON array of actions. Allowed actions:
  {"action":"click","control_id":int,"x":int,"y":int}
  {"action":"type","text":"string","control_id":int}
  {"action":"press","key":"string"}
  {"action":"press_combo","keys":["string"]}
  {"action":"done"}
Rules:
- Use control_id (index in elements list) for clicks and typing to target specific controls.
- Use x,y only as fallback if control_id is unavailable or invalid.
- Prefer clicking interactive elements (btn, menu, link, tab, dropdown, input).
- Use rect to ensure clicks are within control bounds.
- Return valid JSON only (no explanations).
"""


def call_gemini(
    user_goal: str,
    unified_elements: List[Dict[str, Any]],
    screen_size: Tuple[int, int],
    timeout: float = STEP_TIMEOUT,
) -> List[Dict[str, Any]]:
    """
    Call Gemini / generative model to convert user_goal + UI snapshot into actions.
    If Gemini is not available or fails, return an empty list (so rule-based fallback can run).
    """
    if not HAS_GENAI:
        logger.warning("Gemini client not installed; skipping AI planning.")
        return []

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        logger.warning("GEMINI_API_KEY is not set; cannot call Gemini.")
        return []

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(GEMINI_MODEL)
        elements_for_prompt = [
            {
                "id": idx,
                "name": e.get("name"),
                "text": (e.get("text") or "")[:60],
                "x": e.get("x"),
                "y": e.get("y"),
                "type": e.get("type"),
                "ui_type": e.get("ui_type", ""),
                "rect": e.get("rect", [0, 0, 0, 0]),
            }
            for idx, e in enumerate(unified_elements[:22])
        ]
        current_title = get_window_title() or "Unknown"
        prompt = "\n".join([
            f"User goal: {user_goal}",
            f"Current window title: {current_title}",
            f"Screen size: {screen_size[0]}x{screen_size[1]}",
            f"UI elements: {json.dumps(elements_for_prompt, ensure_ascii=False)}",
            "Return JSON array of actions.",
        ])
        generation_config = {"temperature": AI_TEMPERATURE, "max_output_tokens": 300}
        for attempt in range(max(1, AI_RETRY_COUNT)):
            try:
                res = model.generate_content([SYSTEM_PROMPT, prompt], generation_config=generation_config)
                reply_text = getattr(res, "text", "") or str(res)
                cleaned = re.sub(r"```(?:json)?", "", reply_text, flags=re.IGNORECASE).strip()
                m = re.search(r"(\[[\s\S]*\])", cleaned)
                blob = m.group(1) if m else cleaned
                parsed = safe_json_loads(blob)
                if not isinstance(parsed, list):
                    logger.warning("Gemini returned non-list JSON: %s", cleaned)
                    return []
                validated: List[Dict[str, Any]] = []
                for item in parsed:
                    if not isinstance(item, dict) or "action" not in item:
                        continue
                    act = item.get("action")
                    control_id = item.get("control_id")
                    if act == "click" and (isinstance(control_id, int) or ("x" in item and "y" in item)):
                        x, y = int(item.get("x", 0)), int(item.get("y", 0))
                        validated.append({"action": "click", "control_id": control_id, "x": x, "y": y})
                    elif act == "type" and isinstance(item.get("text"), str):
                        validated.append({"action": "type", "text": item.get("text", ""), "control_id": control_id})
                    elif act == "press" and isinstance(item.get("key"), str):
                        validated.append({"action": "press", "key": item.get("key", "")})
                    elif act == "press_combo" and isinstance(item.get("keys"), list):
                        validated.append({"action": "press_combo", "keys": list(item.get("keys", []))})
                    elif act == "done":
                        validated.append({"action": "done"})
                return validated
            except Exception as e:
                logger.warning("Gemini attempt %d failed: %s", attempt + 1, e)
                if attempt < AI_RETRY_COUNT - 1:
                    time.sleep(2 ** attempt)
                else:
                    return []
    except Exception as e:
        logger.error("call_gemini setup failed: %s", e)
        return []

# -------------------------
# Deterministic fallback planner (rule-based)
# -------------------------

def _normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _text_similarity(a: str, b: str) -> float:
    a, b = _normalize_text(a), _normalize_text(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return min(len(a), len(b)) / max(len(a), len(b))
    # simple token overlap
    aset, bset = set(a.split()), set(b.split())
    if not aset or not bset:
        return 0.0
    return len(aset & bset) / len(aset | bset)


def _find_best_element_by_text(query: str, unified_elements: List[Dict[str, Any]]) -> Optional[int]:
    best_id: Optional[int] = None
    best_score = 0.0
    q = _normalize_text(query)
    for idx, el in enumerate(unified_elements):
        el_text = _normalize_text((el.get("text") or ""))
        el_name = _normalize_text((el.get("name") or ""))
        score = max(_text_similarity(q, el_text), _text_similarity(q, el_name))
        # prefer interactive controls
        if el.get("name") in ("btn", "menu", "link", "tab", "dropdown", "input", "item"):
            score += 0.1
        if score > best_score:
            best_score = score
            best_id = idx
    if best_score >= 0.45:
        return best_id
    return None


def _parse_key_combo(text: str) -> Optional[List[str]]:
    # support "press ctrl+s" or "press ctrl + s"
    t = _normalize_text(text)
    m = re.search(r"press\s+([a-z+\s]+)$", t)
    if not m:
        return None
    combo = m.group(1)
    # split on + or spaces
    parts = [p.strip() for p in re.split(r"\+|\s+", combo) if p.strip()]
    return parts or None


def rule_based_plan(user_goal: str, unified_elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    goal = _normalize_text(user_goal)
    actions: List[Dict[str, Any]] = []

    # Open / launch app via Start menu
    m = re.match(r"(open|launch|start)\s+(.+)$", goal)
    if m:
        app = m.group(2).strip()
        if app:
            return [
                {"action": "press", "key": "win"},
                {"action": "type", "text": app, "control_id": None},
                {"action": "press", "key": "enter"},
            ]

    # Click by text
    m = re.match(r"(click|select|open|press)\s+'?\"?(.+?)\"?'?$", goal)
    if m:
        label = m.group(2).strip()
        idx = _find_best_element_by_text(label, unified_elements)
        if idx is not None:
            return [{"action": "click", "control_id": idx}]

    # Type text (focus an input field first if available)
    m = re.match(r"type\s+(.+)$", goal)
    if m:
        text = m.group(1).strip()
        input_field_id = next((i for i, e in enumerate(unified_elements) if e.get("name") == "input" or e.get("ui_type") == "Edit"), None)
        if input_field_id is not None:
            return [
                {"action": "click", "control_id": input_field_id},
                {"action": "type", "text": text, "control_id": input_field_id},
            ]
        return [{"action": "type", "text": text, "control_id": None}]

    # Press key combo
    keys = _parse_key_combo(goal)
    if keys:
        return [{"action": "press_combo", "keys": keys}]

    # If "search for X" shortcut: click an input box if present then type
    m = re.match(r"(search\s+for|search)\s+(.+)$", goal)
    if m:
        query = m.group(2).strip()
        input_field_id = next((i for i, e in enumerate(unified_elements) if e.get("name") == "input" or e.get("ui_type") == "Edit" or "search" in _normalize_text(e.get("text") or "")), None)
        if input_field_id is not None:
            return [
                {"action": "click", "control_id": input_field_id},
                {"action": "type", "text": query, "control_id": input_field_id},
                {"action": "press", "key": "enter"},
            ]

    return []

# -------------------------
# Action Execution
# -------------------------

def is_app_launch_sequence(actions: List[Dict[str, Any]]) -> Tuple[bool, str]:
    if len(actions) >= 3:
        if (
            actions[0].get("action") == "press"
            and actions[0].get("key") == "win"
            and actions[1].get("action") == "type"
            and actions[2].get("action") == "press"
            and actions[2].get("key") == "enter"
        ):
            return True, actions[1].get("text", "").lower()
    return False, ""


def clamp_coords(x: int, y: int) -> Tuple[int, int]:
    try:
        desktop = Desktop(backend="uia")
        rect = desktop.rectangle()
        x = max(rect.left, min(rect.right - 1, x))
        y = max(rect.top, min(rect.bottom - 1, y))
        return int(x), int(y)
    except Exception:
        return int(x), int(y)


def execute_action_sequence(
    actions: List[Dict[str, Any]],
    unified_elements: List[Dict[str, Any]],
    confirmation_if_failsafe: bool = FAILSAFE_ENABLED,
) -> bool:
    """
    Executes a sequence of actions. Returns True to continue (agent may continue),
    or False to stop (for example, when action had 'done' or user declined).
    """
    if not actions:
        return True

    if confirmation_if_failsafe:
        print("About to execute actions:", json.dumps(actions, ensure_ascii=False, indent=2))
        resp = input("Confirm execute? (y/N) ").strip().lower()
        if resp not in ("y", "yes"):
            logger.info("User declined action execution")
            return True

    try:
        from pywinauto import mouse, keyboard  # imported here to avoid import error on non-Windows

        KEY_CODE_MAP = {
            "win": "{VK_LWIN}",
            "windows": "{VK_LWIN}",
            "enter": "{ENTER}",
            "tab": "{TAB}",
            "esc": "{ESC}",
            "escape": "{ESC}",
            "ctrl": "^",
            "control": "^",
            "alt": "%",
            "shift": "+",
        }

        is_launching_app, app_name = is_app_launch_sequence(actions)

        for i, action in enumerate(actions):
            act = action.get("action")
            for retry in range(ACTION_RETRY_COUNT + 1):
                try:
                    logger.info("Executing action %d (retry %d): %s", i + 1, retry, action)
                    if act == "done":
                        logger.info("Action sequence finished with 'done'")
                        return False
                    elif act == "click":
                        control_id = action.get("control_id")
                        if isinstance(control_id, int) and 0 <= control_id < len(unified_elements):
                            el = unified_elements[control_id]
                            control = el.get("control")
                            rect = el.get("rect") or None
                            if control and hasattr(control, "click_input"):
                                try:
                                    control.click_input()
                                    smart_wait_after_action("click", action)
                                    break
                                except Exception:
                                    pass
                            if rect and isinstance(rect, (list, tuple)) and len(rect) == 4:
                                x = (rect[0] + rect[2]) // 2
                                y = (rect[1] + rect[3]) // 2
                                x, y = clamp_coords(x, y)
                                mouse.click(coords=(x, y))
                                smart_wait_after_action("click", action)
                                break
                        # fallback to given coords
                        x, y = clamp_coords(int(action.get("x", 0)), int(action.get("y", 0)))
                        mouse.click(coords=(x, y))
                        smart_wait_after_action("click", action)
                        break
                    elif act == "type":
                        control_id = action.get("control_id")
                        text = action.get("text", "")
                        if isinstance(control_id, int) and 0 <= control_id < len(unified_elements):
                            control = unified_elements[control_id].get("control")
                            if control and hasattr(control, "type_keys"):
                                control.type_keys(text, with_spaces=True, pause=0.01)
                                smart_wait_after_action("type", action)
                                break
                        from pywinauto import keyboard as kbd
                        kbd.send_keys(text, with_spaces=True, pause=0.01)
                        smart_wait_after_action("type", action)
                        break
                    elif act == "press":
                        key = action.get("key", "")
                        if key:
                            mapped = KEY_CODE_MAP.get(key.lower())
                            if mapped:
                                keyboard.send_keys(mapped)
                            else:
                                logger.warning(f"Unknown key code for '{key}', sending as-is.")
                                keyboard.send_keys(key)
                            if key.lower() == "win":
                                time.sleep(START_MENU_WAIT)
                                wait_for_ui_stability(2.0)
                            else:
                                time.sleep(0.05)
                            if is_launching_app and i == len(actions) - 1 and key.lower() == "enter":
                                smart_wait_after_action("app_launch", action, app_name=app_name)
                        break
                    elif act == "press_combo":
                        keys = action.get("keys", [])
                        if not isinstance(keys, list):
                            break
                        combo = ""
                        for k in keys:
                            mapped = KEY_CODE_MAP.get(k.lower(), k)
                            combo += mapped
                        keyboard.send_keys(combo)
                        time.sleep(0.1)
                        break
                    else:
                        logger.warning("Unknown action type: %s", action)
                        break
                except Exception as e:
                    logger.error("Execution of action %d failed (retry %d): %s", i + 1, retry, e)
                    if retry < ACTION_RETRY_COUNT:
                        time.sleep(0.5 * (2 ** retry))
                    else:
                        logger.debug("Giving up on action after retries.")
                        break
        return True
    except Exception as e:
        logger.error("Execution environment error: %s", e)
        return True

# -------------------------
# Agent orchestration and caching
# -------------------------
_last_state_sig: Optional[str] = None
_last_actions_cache: Optional[List[Dict[str, Any]]] = None
_prev_image_hash: Optional[str] = None
_state_lock = threading.Lock()


def compact_state_signature(unified_elements: List[Dict[str, Any]]) -> str:
    arr = [(e.get("name"), (e.get("text") or "")[:20], e.get("x"), e.get("y")) for e in unified_elements[:12]]
    s = json.dumps({"els": arr}, ensure_ascii=False, sort_keys=True)
    return md5_hex(s)


def ghostshell_decide_and_act(user_goal: str, use_ai: bool = True) -> bool:
    global _last_state_sig, _last_actions_cache, _prev_image_hash
    step_start = timer_ms()

    wrapper = get_foreground_window()
    if not wrapper:
        logger.error("No active window detected; aborting step")
        return False

    rect = get_foreground_rect(wrapper)
    img = capture_foreground_window_image(wrapper)
    if img is None:
        logger.error("No image captured; aborting step")
        return False

    img_hash = md5_hex(img.tobytes()[:2048]) if img else None
    screen_size = (img.width, img.height) if img else (1920, 1080)

    # small pause to stabilize capture
    time.sleep(0.12)

    x_off = rect[0] if rect else 0
    y_off = rect[1] if rect else 0

    ocr_future = _executor.submit(ocr_extract_text_items, img, OCR_CONF_THRESHOLD, OCR_TOP_N, x_off, y_off)
    ui_future = _executor.submit(get_active_window_ui_tree)
    done, not_done = wait([ocr_future, ui_future], timeout=0.9, return_when=FIRST_COMPLETED)

    ocr_items = ocr_future.result(timeout=0.2) if ocr_future.done() else []
    ui_tree = ui_future.result(timeout=0.2) if ui_future.done() else []

    unified = create_unified_ui_elements(ocr_items, ui_tree, screen_rect=rect)
    logger.info("Unified elements count=%d", len(unified))

    sig = compact_state_signature(unified)
    goal_sig = md5_hex(user_goal.strip().lower())
    cache_sig = f"{sig}_{goal_sig}"

    actions: List[Dict[str, Any]]

    with _state_lock:
        if cache_sig == _last_state_sig and _last_actions_cache and img_hash == _prev_image_hash:
            logger.info("State and goal unchanged; reusing cached actions")
            actions = _last_actions_cache
        else:
            # Try AI first (if enabled), then rule-based fallback
            actions = []
            if use_ai:
                ai_actions = call_gemini(user_goal, unified, screen_size)
                if ai_actions:
                    actions = ai_actions
            if not actions or (len(actions) == 1 and actions[0].get("action") == "done"):
                rb = rule_based_plan(user_goal, unified)
                if rb:
                    actions = rb
            if not actions:
                actions = [{"action": "done"}]
            _last_state_sig = cache_sig
            _last_actions_cache = actions
            _prev_image_hash = img_hash

    cont = execute_action_sequence(actions, unified)
    step_elapsed = timer_ms() - step_start
    logger.info("Step completed in %d ms", step_elapsed)
    return cont

# -------------------------
# Goal decomposition (simple fallback + optional planner)
# -------------------------

def plan_steps_for_goal(user_goal: str) -> List[str]:
    if HAS_GENAI:
        try:
            api_key = os.getenv("GEMINI_API_KEY", "").strip()
            if api_key:
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel(GEMINI_MODEL)
                plan_prompt = (
                    "You are an expert assistant for UI automation.\n"
                    "Given a user goal, break it down into a minimal ordered list of atomic actions, "
                    "each as a single sentence that can be executed step by step by an automation agent.\n"
                    "ONLY return the JSON list of steps, no explanations.\n"
                    f"Goal: '{user_goal}'"
                )
                res = model.generate_content(plan_prompt, generation_config={"temperature": 0.16, "max_output_tokens": 256})
                reply_text = getattr(res, "text", "") or str(res)
                cleaned = re.sub(r"```(?:json)?", "", reply_text, flags=re.IGNORECASE).strip()
                m = re.search(r"(\[[\s\S]*\])", cleaned)
                blob = m.group(1) if m else cleaned
                parsed = safe_json_loads(blob)
                if isinstance(parsed, list) and all(isinstance(s, str) for s in parsed):
                    return [s.strip() for s in parsed if s.strip()]
                logger.warning("AI step planner returned malformed: %s", cleaned)
        except Exception as e:
            logger.warning("AI planning failed: %s", e)
    # fallback split heuristics
    delimiters = [r"\band then\b", r"\bthen\b", ",", r"\band\b"]
    pattern = "|".join(delimiters)
    steps = [x.strip().capitalize() for x in re.split(pattern, user_goal, flags=re.IGNORECASE) if x.strip()]
    return steps or [user_goal]

# -------------------------
# REPL / CLI
# -------------------------
import argparse


def run_repl():
    parser = argparse.ArgumentParser(prog="ghostshell", description="AI-driven Windows UI automation agent")
    parser.add_argument("--benchmark", action="store_true", help="Run lightweight benchmark")
    parser.add_argument("--iterations", type=int, default=6, help="Benchmark iterations")
    parser.add_argument("--step-delay", type=float, default=0.3, help="Delay (seconds) between REPL steps")
    args = parser.parse_args()
    if args.benchmark:
        run_benchmark(args.iterations)
        return
    if not HAS_GENAI:
        logger.warning("Gemini client not available; REPL will still run with deterministic planner.")
    print("GhostShell — AI-driven automation (Enhanced for production with adaptive waiting)")
    print("Type a goal (or 'exit')")
    try:
        while True:
            goal = input("GhostShell Goal > ").strip()
            if not goal:
                continue
            if goal.lower() in ("exit", "quit", "q"):
                break
            sub_goals = plan_steps_for_goal(goal)
            print(f"Planned {len(sub_goals)} step(s):")
            for i, sg in enumerate(sub_goals, 1):
                print(f"  Step {i}: {sg}")
            for i, subgoal in enumerate(sub_goals):
                print(f"\n--- Executing step {i+1}/{len(sub_goals)}: {subgoal} ---")
                cont = ghostshell_decide_and_act(subgoal, use_ai=True)
                if not cont:
                    logger.info("Agent stopped step %d", i+1)
                    break
                if i < len(sub_goals) - 1:
                    print("Pausing before next step...")
                    time.sleep(args.step_delay)
                    wait_for_ui_stability(1.0)
    except KeyboardInterrupt:
        logger.info("User interrupted; exiting")
    except Exception:
        logger.exception("Unhandled error in REPL loop")
    finally:
        logger.info("Shutting down")
        _executor.shutdown(wait=True, cancel_futures=True)

# -------------------------
# Benchmark
# -------------------------

def run_benchmark(iterations: int = 6):
    logger.info("Running benchmark (%d iterations)", iterations)
    timings = []
    for i in range(iterations):
        t0 = timer_ms()
        wrapper = get_foreground_window()
        img = capture_foreground_window_image(wrapper)
        rect = get_foreground_rect(wrapper)
        x_off = rect[0] if rect else 0
        y_off = rect[1] if rect else 0
        t1 = timer_ms()
        ocr = ocr_extract_text_items(img, OCR_CONF_THRESHOLD, OCR_TOP_N, x_off, y_off) if img else []
        t2 = timer_ms()
        ui = get_active_window_ui_tree()
        t3 = timer_ms()
        unified = create_unified_ui_elements(ocr, ui, rect)
        t4 = timer_ms()
        logger.info("Iter %d timings: capture=%d ocr=%d ui=%d unify=%d ms", i+1, t1 - t0, t2 - t1, t3 - t2, t4 - t3)
        timings.append((t1 - t0, t2 - t1, t3 - t2, t4 - t3))
        time.sleep(0.12)
    if timings:
        sums = [sum(col)/len(timings) for col in zip(*timings)]
        logger.info("Average timings: capture=%.1f ocr=%.1f ui=%.1f unify=%.1f ms", *sums)

# -------------------------
# Entry point
# -------------------------
if __name__ == "__main__":
    run_repl()