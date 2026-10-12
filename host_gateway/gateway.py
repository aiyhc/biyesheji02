import argparse
import ipaddress
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None


WEB_ROOT = Path(__file__).parent / "web"
MAX_BODY_BYTES = 4096
MAX_SESSION_MESSAGES = 12
MAX_SESSIONS = 64
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5-2b-local")
OLED_WIDTH = 128
OLED_HEIGHT = 64
OLED_FRAME_BYTES = OLED_WIDTH * OLED_HEIGHT // 8
OLED_LINES_PER_PAGE = 3
OLED_PAGE_DELAY = 2.0

@lru_cache(maxsize=2)
def _oled_font(size):
    if ImageFont is None:
        raise RuntimeError("OLED text rendering requires Pillow; install host_gateway/requirements.txt.")
    font_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for filename in ("msyh.ttc", "simsun.ttc", "simhei.ttf"):
        path = font_dir / filename
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    raise RuntimeError("No Windows Chinese font was found for OLED text rendering.")


def _wrap_oled_text(text, font):
    lines = []
    line = ""
    width = 0
    for char in text.replace("\r\n", "\n").replace("\r", "\n"):
        if char == "\n":
            lines.append(line)
            line = ""
            width = 0
            continue
        char_width = font.getlength(char)
        if line and width + char_width > OLED_WIDTH:
            lines.append(line)
            line = ""
            width = 0
        line += char
        width += char_width
    if line or not lines:
        lines.append(line)
    return lines


def _image_to_ssd1306_frame(image):
    pixels = image.convert("1").load()
    frame = bytearray(OLED_FRAME_BYTES)
    offset = 0
    for page in range(OLED_HEIGHT // 8):
        for x in range(OLED_WIDTH):
            value = 0
            for bit in range(8):
                if pixels[x, page * 8 + bit]:
                    value |= 1 << bit
            frame[offset] = value
            offset += 1
    return bytes(frame)


def render_display_frames(text, role):
    if Image is None or ImageDraw is None or ImageFont is None:
        raise RuntimeError("OLED text rendering requires Pillow; install host_gateway/requirements.txt.")

    font = _oled_font(16)
    header_font = _oled_font(10)
    lines = _wrap_oled_text(text[:500], font)
    frames = []
    for start in range(0, len(lines), OLED_LINES_PER_PAGE):
        image = Image.new("1", (OLED_WIDTH, OLED_HEIGHT), 0)
        draw = ImageDraw.Draw(image)
        draw.text((2, 0), role, font=header_font, fill=1)
        draw.line((0, 11, OLED_WIDTH - 1, 11), fill=1)

        for row, line in enumerate(lines[start:start + OLED_LINES_PER_PAGE]):
            x = 0
            y = 13 + row * 16
            for char in line:
                bounds = font.getbbox(char)
                draw.text((round(x), y - bounds[1]), char, font=font, fill=1)
                x += font.getlength(char)
        frames.append(_image_to_ssd1306_frame(image))
    return frames


def _post_oled_frame(device_url, frame):
    request = urllib.request.Request(
        f"{device_url}/api/display",
        data=frame,
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=4) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict) or not result.get("ok"):
        raise ValueError(f"ESP32 rejected OLED frame: {result}")


def _oled_display_worker(display_queue):
    while True:
        device_url, role, text = display_queue.get()
        try:
            for frame in render_display_frames(text, role):
                _post_oled_frame(device_url, frame)
                time.sleep(OLED_PAGE_DELAY)
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"[gateway] OLED display unavailable: {exc}")
        finally:
            display_queue.task_done()


def queue_oled_message(display_queue, device_url, role, text):
    if display_queue is None or not device_url or not text:
        return
    item = (device_url, role, text[:500])
    try:
        display_queue.put_nowait(item)
    except queue.Full:
        try:
            display_queue.get_nowait()
            display_queue.task_done()
        except queue.Empty:
            pass
        try:
            display_queue.put_nowait(item)
        except queue.Full:
            pass


COLOR_RGB = {
    "off": (0, 0, 0),
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 96, 0),
    "white": (255, 255, 255),
}
COLOR_TERMS = {
    "red": ("红色", "红灯", "红光", "red"),
    "green": ("绿色", "绿灯", "绿光", "green"),
    "blue": ("蓝色", "蓝灯", "蓝光", "blue"),
    "yellow": ("黄色", "黄灯", "黄光", "yellow"),
    "white": ("白色", "白灯", "白光", "white"),
}
COLOR_LABELS = {
    "off": "关闭",
    "red": "红色",
    "green": "绿色",
    "blue": "蓝色",
    "yellow": "黄色",
    "white": "白色",
}

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["chat", "set_light", "unsupported"],
        },
        "colors": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["off", "red", "green", "blue", "yellow", "white"],
            },
            "maxItems": 5,
        },
        "brightness": {"type": "integer", "minimum": 0, "maximum": 100},
        "reply": {"type": "string"},
    },
    "required": ["intent", "colors", "brightness", "reply"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "你是中文聊天助手和ESP32 RGB灯控制助手。"
    "只返回符合JSON Schema的JSON，不要输出JSON以外的文字。"
    "目前只能控制一颗RGB灯。用户明确要求控制灯时，intent=set_light，colors列出全部指定颜色，"
    "brightness填写0到100的亮度百分比；没有说亮度时为100。开灯但没指定颜色时使用white，关灯时使用off。"
    "颜色只允许off/red/green/blue/yellow/white。"
    "普通问候、聊天和知识问答使用intent=chat、colors=[]、brightness=100。"
    "用户要求控制空调、电机或其他未接入设备时，使用intent=unsupported、colors=[]、brightness=100。"
    "reply使用简短自然的中文。不要询问RGB数值。"
)

class OllamaError(Exception):
    pass


def validate_device_url(value):
    candidate = (value or "").strip().rstrip("/")
    if not candidate:
        return ""
    parsed = urlsplit(candidate)
    if parsed.scheme != "http" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("请输入 http://192.168.x.x 形式的 ESP32 地址。")
    if parsed.username or parsed.password:
        raise ValueError("设备地址不能包含用户名或密码。")
    if parsed.port not in (None, 80):
        raise ValueError("此起步工程只支持 ESP32 的 HTTP 80 端口。")

    hostname = parsed.hostname.lower()
    if hostname.endswith(".local"):
        return f"http://{hostname}"
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise ValueError("请使用 ESP32 的局域网 IPv4 地址，例如 http://192.168.1.50。") from exc
    if address.version != 4 or not address.is_private:
        raise ValueError("ESP32 地址必须是局域网 IPv4 地址。")
    return f"http://{address}"


def is_negated_light_request(text):
    normalized = text.strip().lower()
    has_control_verb = any(
        word in normalized
        for word in (
            "把灯",
            "开灯",
            "关灯",
            "关闭",
            "打开",
            "熄灭",
            "调成",
            "改成",
            "换成",
            "设为",
            "点亮",
            "亮度",
        )
    )
    has_negation = any(
        word in normalized
        for word in ("不要", "别把", "别开", "别关", "不需要开", "不想开", "don't", "do not")
    )
    return has_control_verb and has_negation


def _brightness_from_text(normalized):
    matches = re.findall(r"百分之\s*(-?\d{1,3})|(?<!\d)(-?\d{1,3})\s*(?:%|％)", normalized)
    values = [int(first or second) for first, second in matches]
    if not values:
        match = re.search(
            r"亮度\s*(?:(?:调到|调整到|设置为|设为|改为|到|为)\s*)?(-?\d{1,3})(?!\d)",
            normalized,
        )
        if match:
            values.append(int(match.group(1)))
    if values:
        brightness = values[-1]
    elif any(word in normalized for word in ("一半", "半亮", "一半亮")):
        brightness = 50
    elif any(word in normalized for word in ("最亮", "满亮", "全亮")):
        brightness = 100
    elif any(word in normalized for word in ("最暗", "最低亮度")):
        brightness = 0
    else:
        return None

    if not 0 <= brightness <= 100:
        raise ValueError("亮度范围是 0% 到 100%，请重新输入。")
    return brightness


def explicit_light_settings_from_text(text):
    normalized = " ".join(text.strip().lower().split())
    if any(word in normalized for word in ("是什么", "什么意思", "为什么", "原理", "解释", "介绍一下", "教程")):
        return None
    if is_negated_light_request(normalized):
        return None

    action_terms = (
        "把灯",
        "让灯",
        "给灯",
        "调成",
        "调为",
        "改成",
        "换成",
        "设为",
        "设置",
        "变成",
        "打开",
        "开灯",
        "点亮",
        "turn on",
        "turn off",
        "关灯",
        "关闭",
        "熄灭",
        "亮度",
        "亮一点",
        "暗一点",
        "调亮",
        "调暗",
        "半亮",
        "一半",
    )
    off_terms = ("关灯", "关掉灯", "熄灭", "关闭灯", "灭灯", "turn off")
    brightness = _brightness_from_text(normalized)
    colors = [
        color
        for color, terms in COLOR_TERMS.items()
        if any(term in normalized for term in terms)
    ]
    has_action = any(word in normalized for word in action_terms)
    combined_colors = len(colors) > 1 and any(word in normalized for word in ("和", "与", "、", "+", "and"))
    bright_color_phrase = bool(colors) and any(
        normalized.startswith(prefix) for prefix in ("亮红", "亮绿", "亮蓝", "亮黄", "亮白", "点亮")
    )
    exact_color_command = any(
        normalized == term for terms in COLOR_TERMS.values() for term in terms
    )

    if any(word in normalized for word in off_terms) and (has_action or exact_color_command):
        return {"colors": ["off"], "brightness": 100}
    if not (
        has_action
        or combined_colors
        or bright_color_phrase
        or exact_color_command
        or (colors and brightness is not None)
    ):
        return None
    if not colors and brightness is None:
        if any(word in normalized for word in ("开灯", "打开灯", "亮灯", "turn on")):
            colors = ["white"]
        else:
            return None
    return {"colors": colors, "brightness": 100 if brightness is None else brightness}


def compose_light_rgb(colors, brightness, current_rgb=None, remembered_rgb=None):
    if "off" in colors:
        return (0, 0, 0), remembered_rgb

    if colors:
        base = tuple(
            min(255, sum(COLOR_RGB[color][channel] for color in colors))
            for channel in range(3)
        )
    else:
        source = current_rgb if current_rgb and max(current_rgb) else remembered_rgb
        if source and max(source):
            peak = max(source)
            base = tuple(round(value * 255 / peak) for value in source)
        else:
            base = COLOR_RGB["white"]

    output = tuple((value * brightness + 50) // 100 for value in base)
    return output, base


def parse_model_decision(content):
    try:
        data = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("本地模型没有返回有效的 JSON 指令。") from exc
    if not isinstance(data, dict):
        raise ValueError("本地模型返回的数据格式不正确。")

    intent = data.get("intent")
    colors = data.get("colors")
    brightness = data.get("brightness")
    reply = data.get("reply")
    if intent not in ("chat", "set_light", "unsupported"):
        raise ValueError("本地模型返回了不支持的操作类型。")
    if not isinstance(reply, str):
        reply = ""
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()[:500]

    if intent == "set_light":
        if (
            not isinstance(colors, list)
            or not colors
            or len(colors) > 5
            or any(not isinstance(color, str) or color not in COLOR_RGB for color in colors)
            or ("off" in colors and len(colors) != 1)
        ):
            raise ValueError("本地模型提出了无效颜色，未执行硬件操作。")
        if isinstance(brightness, bool) or not isinstance(brightness, int) or not 0 <= brightness <= 100:
            raise ValueError("本地模型提出了无效亮度，未执行硬件操作。")
        colors = list(dict.fromkeys(colors))
    else:
        colors = []
        brightness = 100
    return {"intent": intent, "colors": colors, "brightness": brightness, "reply": reply}


def call_ollama(messages, ollama_url, model):
    payload = {
        "model": model,
        "stream": False,
        "think": False,
        "format": DECISION_SCHEMA,
        "options": {
            "temperature": 0,
            "num_ctx": 2048,
            "num_predict": 128,
        },
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
    }
    request = urllib.request.Request(
        ollama_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise OllamaError(f"Ollama 返回 HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OllamaError(
            "无法连接本机 Ollama。请确认 Ollama 正在运行，并且模型已安装。"
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OllamaError("Ollama 返回的数据不是有效JSON。") from exc

    message = result.get("message")
    if not isinstance(message, dict):
        raise OllamaError("Ollama 响应中缺少 message。")
    try:
        return parse_model_decision(message.get("content"))
    except ValueError as exc:
        raise OllamaError(str(exc)) from exc


def session_id_is_valid(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) is not None


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "ESP32Bridge/1.0"

    def _send_json(self, status, payload):
        if (
            self.path == "/api/chat"
            and isinstance(payload, dict)
            and isinstance(payload.get("reply"), str)
        ):
            queue_oled_message(
                getattr(self.server, "display_queue", None),
                self.server.device_url,
                "AI",
                payload["reply"],
            )
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效。") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("请求内容为空或超过大小限制。")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求不是有效的 UTF-8 JSON。") from exc

    def do_GET(self):
        if self.path == "/":
            page = (WEB_ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return

        if self.path == "/api/config":
            self._send_json(
                200,
                {
                    "device_url": self.server.device_url,
                    "model": self.server.ollama_model,
                },
            )
            return

        if self.path == "/api/device":
            if not self.server.device_url:
                self._send_json(503, {"ok": False, "error": "请先填写 ESP32 的局域网地址。"})
                return
            try:
                self._send_json(200, self._get_device_status())
            except (OSError, ValueError, TimeoutError) as exc:
                self._send_json(502, {"ok": False, "error": str(exc)})
            return

        if self.path == "/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "device_url": self.server.device_url,
                    "model": self.server.ollama_model,
                },
            )
            return

        self._send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        if self.path == "/api/device/config":
            try:
                payload = self._read_json()
                if not isinstance(payload, dict) or not isinstance(payload.get("device_url"), str):
                    raise ValueError("请求需要包含 device_url 字符串。")
                self.server.device_url = validate_device_url(payload["device_url"])
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            self._send_json(200, {"ok": True, "device_url": self.server.device_url})
            return

        if self.path != "/api/chat":
            self._send_json(404, {"ok": False, "error": "Not found"})
            return

        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return

        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            self._send_json(400, {"ok": False, "error": "请求需要包含 text 字符串。"})
            return

        text = payload["text"].strip()[:500]
        if not text:
            self._send_json(400, {"ok": False, "error": "请输入一句话。"})
            return
        session_id = payload.get("session_id")
        if not session_id_is_valid(session_id):
            session_id = uuid.uuid4().hex
        queue_oled_message(
            getattr(self.server, "display_queue", None),
            self.server.device_url,
            "YOU",
            text,
        )

        if is_negated_light_request(text):
            reply = "好的，我不会更改灯光。"
            self._remember_turn(session_id, text, reply)
            self._send_json(200, {"ok": True, "reply": reply, "action": None, "session_id": session_id})
            return

        try:
            explicit_settings = explicit_light_settings_from_text(text)
        except ValueError as exc:
            self._send_json(400, {"ok": False, "reply": str(exc), "error": str(exc), "session_id": session_id})
            return

        try:
            decision = (
                {
                    "intent": "set_light",
                    **explicit_settings,
                    "reply": "",
                }
                if explicit_settings is not None
                else self._get_model_decision(session_id, text)
            )
        except OllamaError as exc:
            self._send_json(
                502,
                {
                    "ok": False,
                    "reply": str(exc),
                    "error": str(exc),
                    "session_id": session_id,
                },
            )
            return

        if decision["intent"] == "set_light":
            if not self.server.device_url:
                self._send_json(
                    503,
                    {
                        "ok": False,
                        "reply": "先连接 ESP32，再执行灯光控制。",
                        "action": None,
                        "session_id": session_id,
                    },
                )
                return

            colors = decision["colors"]
            brightness = decision["brightness"]
            current_rgb = None
            if not colors:
                try:
                    status = self._get_device_status()
                except (OSError, ValueError, TimeoutError) as exc:
                    self._send_json(502, {"ok": False, "reply": str(exc), "error": str(exc), "session_id": session_id})
                    return
                rgb = status.get("rgb")
                if isinstance(rgb, dict):
                    values = (rgb.get("r"), rgb.get("g"), rgb.get("b"))
                    if all(
                        isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 255
                        for value in values
                    ):
                        current_rgb = values

            rgb_values, base_rgb = compose_light_rgb(
                colors,
                brightness,
                current_rgb=current_rgb,
                remembered_rgb=getattr(self.server, "last_lit_rgb", None),
            )
            action = {
                "request_id": uuid.uuid4().hex,
                "action": "set_rgb",
                "r": rgb_values[0],
                "g": rgb_values[1],
                "b": rgb_values[2],
            }
            try:
                device_result = self._post_device_action(action)
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                self._send_json(
                    502,
                    {
                        "ok": False,
                        "reply": "ESP32 没有确认执行灯光设置。",
                        "action": action,
                        "error": str(exc),
                        "session_id": session_id,
                    },
                )
                return

            if any(base_rgb):
                self.server.last_lit_rgb = base_rgb
            if colors == ["off"]:
                reply = "已关灯。"
            elif colors:
                labels = "和".join(COLOR_LABELS[color] for color in colors)
                reply = f"已调成{labels}混色，亮度 {brightness}%。"
            else:
                reply = f"已将当前灯色亮度调到 {brightness}%。"
            self._remember_turn(session_id, text, reply)
            self._send_json(
                200,
                {
                    "ok": True,
                    "reply": reply,
                    "action": action,
                    "device": device_result,
                    "session_id": session_id,
                },
            )
            return

        if decision["intent"] == "unsupported":
            reply = "目前只接入了 RGB 灯控制，暂时不能操作其他设备。"
        else:
            reply = decision["reply"] or "你好，有什么我可以帮你？"

        self._remember_turn(session_id, text, reply)
        self._send_json(
            200,
            {"ok": True, "reply": reply, "action": None, "session_id": session_id},
        )

    def _get_model_decision(self, session_id, text):
        with self.server.sessions_lock:
            history = list(self.server.sessions.get(session_id, []))
        messages = [*history, {"role": "user", "content": text}]
        return call_ollama(messages, self.server.ollama_url, self.server.ollama_model)

    def _remember_turn(self, session_id, user_text, assistant_text):
        with self.server.sessions_lock:
            history = self.server.sessions.setdefault(session_id, [])
            history.extend(
                [
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": assistant_text},
                ]
            )
            self.server.sessions[session_id] = history[-MAX_SESSION_MESSAGES:]
            while len(self.server.sessions) > MAX_SESSIONS:
                oldest = next(iter(self.server.sessions))
                self.server.sessions.pop(oldest, None)

    def _post_device_action(self, action):
        url = f"{self.server.device_url}/api/action"
        body = json.dumps(action).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise ValueError(f"ESP32 返回 HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise urllib.error.URLError(
                f"无法连接 ESP32：{exc.reason}。请确认同一 Wi-Fi 且设备 IP 正确。"
            ) from exc

        if not isinstance(result, dict) or not result.get("ok"):
            raise ValueError(f"ESP32 未确认执行：{result}")
        return result

    def _get_device_status(self):
        if not self.server.device_url:
            raise ValueError("请先填写 ESP32 的局域网地址。")
        request = urllib.request.Request(f"{self.server.device_url}/api/status")
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise ValueError(f"ESP32 返回 HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise urllib.error.URLError(f"无法连接 ESP32：{exc.reason}") from exc
        if not isinstance(result, dict) or not result.get("ok"):
            raise ValueError(f"ESP32 状态响应无效：{result}")
        return result

    def log_message(self, fmt, *args):
        print(f"[gateway] {self.address_string()} - {fmt % args}")


def main():
    parser = argparse.ArgumentParser(description="Laptop gateway for ESP32 and local Ollama")
    parser.add_argument("--device-url", default="", help="ESP32 URL, e.g. http://192.168.1.50")
    parser.add_argument("--host", default="127.0.0.1", help="Web bind address")
    parser.add_argument("--port", type=int, default=8080, help="Web port")
    parser.add_argument("--ollama-url", default=OLLAMA_URL, help="Local Ollama chat endpoint")
    parser.add_argument("--model", default=OLLAMA_MODEL, help="Installed local Ollama model")
    args = parser.parse_args()

    try:
        device_url = validate_device_url(args.device_url)
    except ValueError as exc:
        parser.error(str(exc))

    ollama_parts = urlsplit(args.ollama_url)
    if ollama_parts.scheme != "http" or ollama_parts.hostname not in ("127.0.0.1", "localhost"):
        parser.error("--ollama-url must point to a local HTTP Ollama server")
    if not args.model.strip():
        parser.error("--model cannot be empty")

    server = ThreadingHTTPServer((args.host, args.port), GatewayHandler)
    server.device_url = device_url
    server.ollama_url = args.ollama_url
    server.ollama_model = args.model.strip()
    server.sessions = {}
    server.sessions_lock = threading.Lock()
    server.last_lit_rgb = None
    server.display_queue = queue.Queue(maxsize=8)
    threading.Thread(
        target=_oled_display_worker,
        args=(server.display_queue,),
        name="oled-display",
        daemon=True,
    ).start()
    print(f"网页地址：http://127.0.0.1:{args.port}/")
    print(f"ESP32 地址：{device_url or '尚未设置，请在网页中填写'}")
    print(f"本地模型：{server.ollama_model} ({server.ollama_url})")
    print("请保持 Ollama 和本机模型运行。按 Ctrl+C 停止网关。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止网关。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
