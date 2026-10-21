"""ESP32 AI 控制台的笔记本网关。

本模块是整条链路的"大脑"，负责在浏览器、本地 Ollama 模型和 ESP32 之间中转：

    网页(index.html) --HTTP--> 本网关 --HTTP--> 本地 Ollama 拿决策
                                  \\--HTTP--> ESP32 /api/action 控制 RGB 灯与雾化器
                                  \\--HTTP--> ESP32 /api/display 显示 OLED 文字

设计要点：
  * 模型输出不可全信：先做本地解析，再严格校验 JSON；
    只有合法颜色/亮度或布尔开关状态才会下发硬件指令；
  * 设备地址只允许局域网 IPv4，避免把控制接口暴露到公网；
  * OLED 文字渲染依赖 Pillow 和 Windows 中文字体，渲染与发送在独立线程里做，
    不阻塞聊天请求。

启动方式见项目根目录 BRIDGE_SETUP.md。
"""

import argparse
import ipaddress
import json
import os
import queue
import re
import ssl
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
    # Pillow 只用于把文字渲染成 OLED 位图，属于可选依赖
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None


# 静态网页目录，do_GET 时从这里读取 index.html
WEB_ROOT = Path(__file__).parent / "web"
# 单个请求体上限（字节），防止超大请求
MAX_BODY_BYTES = 4096
# 每个会话保留最近多少条消息作为上下文
MAX_SESSION_MESSAGES = 12
# 最多同时保留多少个会话（超出后丢弃最早的）
MAX_SESSIONS = 64
STEPPER_MIN_STEPS = 1
STEPPER_MAX_STEPS = 10000
STEPPER_MIN_INTERVAL_MS = 5
STEPPER_MAX_INTERVAL_MS = 100
STEPPER_DEFAULT_INTERVAL_MS = 20
HUMIDITY_ON_BELOW_MIN = 20
HUMIDITY_ON_BELOW_MAX = 75
HUMIDITY_OFF_AT_MIN = 25
HUMIDITY_OFF_AT_MAX = 80
HUMIDITY_MIN_THRESHOLD_GAP = 3
HUMIDITY_MAX_THRESHOLD_GAP = 15
# Ollama 对话接口与默认模型，可用环境变量覆盖
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5-2b-local")
# OLED 屏幕尺寸与一帧的字节数（128*64/8）
OLED_WIDTH = 128
OLED_HEIGHT = 64
OLED_FRAME_BYTES = OLED_WIDTH * OLED_HEIGHT // 8
# 每屏显示几行文字
OLED_LINES_PER_PAGE = 3
# 每屏停留秒数
OLED_PAGE_DELAY = 2.0

@lru_cache(maxsize=2)
def _oled_font(size):
    """加载 Windows 自带的中文字体，供 OLED 渲染使用。

    按优先级尝试微软雅黑、宋体、黑体；结果做了缓存，避免重复加载。
    找不到时抛 RuntimeError，由上层捕获并提示。
    """
    if ImageFont is None:
        raise RuntimeError("OLED text rendering requires Pillow; install host_gateway/requirements.txt.")
    font_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for filename in ("msyh.ttc", "simsun.ttc", "simhei.ttf"):
        path = font_dir / filename
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    raise RuntimeError("No Windows Chinese font was found for OLED text rendering.")


def _wrap_oled_text(text, font):
    """按屏幕像素宽度把文本折行（中英文混排按实际渲染宽度计算）。

    返回行列表；保留原文中的换行符，空文本返回一个空行。
    """
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
        # 再加一个字符就超宽时先断行
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
    """把 Pillow 的 1 位图像转换成 SSD1306 的页面(page)格式字节流。

    SSD1306 显存按 8 行一页、先横向后纵向排列，每字节的 bit0~bit7
    对应同一列上的 8 个像素，因此需要按这个顺序重新打包成 1024 字节。
    """
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
    """把一段文字渲染成若干帧 OLED 位图。

    role 显示在顶部（例如 "YOU" / "AI"），下面有一条分隔线，
    正文每 OLED_LINES_PER_PAGE 行分一屏，返回帧列表。
    """
    if Image is None or ImageDraw is None or ImageFont is None:
        raise RuntimeError("OLED text rendering requires Pillow; install host_gateway/requirements.txt.")

    font = _oled_font(16)        # 正文 16px
    header_font = _oled_font(10)  # 标题 10px
    lines = _wrap_oled_text(text[:500], font)
    frames = []
    for start in range(0, len(lines), OLED_LINES_PER_PAGE):
        image = Image.new("1", (OLED_WIDTH, OLED_HEIGHT), 0)
        draw = ImageDraw.Draw(image)
        # 顶部角色标签 + 分隔线
        draw.text((2, 0), role, font=header_font, fill=1)
        draw.line((0, 11, OLED_WIDTH - 1, 11), fill=1)

        # 逐字符绘制，用 getbbox 修正基线，保证中英文对齐
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
    """把一帧 1024 字节位图 POST 给 ESP32 的 /api/display 接口。

    设备返回非 ok 时抛 ValueError，由调用方记录日志。
    """
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
    """OLED 显示后台线程：从队列取 (设备地址, 角色, 文本) 并逐屏播放。

    单独开线程是为了不拖慢 HTTP 请求；每屏之间有固定停顿，
    显示失败只打印日志，不影响聊天主流程。
    """
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
    """把一条待显示消息放入 OLED 队列（非阻塞）。

    队列满时丢弃最旧的一条再放入，保证最新的对话能显示；
    缺少设备地址或队列时直接忽略。
    """
    if display_queue is None or not device_url or not text:
        return
    item = (device_url, role, text[:500])
    try:
        display_queue.put_nowait(item)
    except queue.Full:
        # 丢最旧的，腾出位置
        try:
            display_queue.get_nowait()
            display_queue.task_done()
        except queue.Empty:
            pass
        try:
            display_queue.put_nowait(item)
        except queue.Full:
            pass


# 预设颜色到 RGB 分量的映射（yellow 用 96 的绿，观感更接近暖黄）
COLOR_RGB = {
    "off": (0, 0, 0),
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 96, 0),
    "white": (255, 255, 255),
}
# 各颜色在中文/英文里可能出现的说法，用于本地正则识别
COLOR_TERMS = {
    "red": ("红色", "红灯", "红光", "red"),
    "green": ("绿色", "绿灯", "绿光", "green"),
    "blue": ("蓝色", "蓝灯", "蓝光", "blue"),
    "yellow": ("黄色", "黄灯", "黄光", "yellow"),
    "white": ("白色", "白灯", "白光", "white"),
}
# 回复用户时使用的中文颜色名
COLOR_LABELS = {
    "off": "关闭",
    "red": "红色",
    "green": "绿色",
    "blue": "蓝色",
    "yellow": "黄色",
    "white": "白色",
}
LIGHT_STATE_PALETTE = (
    ("红色", COLOR_RGB["red"]),
    ("绿色", COLOR_RGB["green"]),
    ("蓝色", COLOR_RGB["blue"]),
    ("黄色", COLOR_RGB["yellow"]),
    ("黄色", (255, 255, 0)),
    ("青色", (0, 255, 255)),
    ("紫色", (255, 0, 255)),
    ("白色", COLOR_RGB["white"]),
)

# 要求模型输出严格符合的 JSON Schema（Ollama 的 format 参数会据此约束解码）
DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["chat", "set_light", "set_humidifier", "set_curtain", "stop_curtain", "query_curtain", "query_environment", "unsupported"],
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
        "humidifier_on": {"type": "boolean"},
        "curtain_position_pct": {"type": "integer", "minimum": 0, "maximum": 100},
        "reply": {"type": "string"},
    },
    "required": ["intent", "colors", "brightness", "humidifier_on", "curtain_position_pct", "reply"],
    "additionalProperties": False,
}

# 系统提示词：把模型限制成聊天助手和有限设备意图分类器
SYSTEM_PROMPT = (
    "你是中文聊天助手、ESP32 RGB灯、雾化器和窗帘控制助手。"
    "只返回符合JSON Schema的JSON，不要输出JSON以外的文字。"
    "目前已接入一颗RGB灯、一个雾化器开关和一幅由步进电机驱动的窗帘。用户明确要求控制灯时，intent=set_light，colors列出全部指定颜色，"
    "brightness填写0到100的亮度百分比；没有说亮度时为100。开灯但没指定颜色时使用white，关灯时使用off。"
    "颜色只允许off/red/green/blue/yellow/white。"
    "用户明确要求打开/关闭雾化器、加湿器时，intent=set_humidifier，并用humidifier_on=true/false表示目标状态；colors=[]、brightness=100。"
    "讨论雾化器、询问如何使用、否定或不确定的表达都不能执行开关，使用intent=chat或unsupported，humidifier_on=false。"
    "用户明确要求打开、关闭或调整窗帘时使用intent=set_curtain，curtain_position_pct为目标开合百分比，0全关、100全开。"
    "只说打开/拉开/开窗帘时目标为100，只说关闭/合上/关窗帘时为0；一半或半开为50，明确百分比按用户数值填写。"
    "只说调整窗帘但没有目标开合度时使用intent=chat并追问，不要默认百分比。"
    "用户明确要求停止窗帘运动时使用intent=stop_curtain。"
    "用户询问当前开合度时使用intent=query_curtain，不要猜测，网关会读取设备位置。"
    "如果窗帘位置未校准或不确定，必须说明需要校准且不执行动作；不要把步数、角度或圈数换算成窗帘开合度。"
    "询问、否定或讨论窗帘都不能触发动作。非窗帘意图统一使用curtain_position_pct=0。"
    "普通问候、聊天和知识问答使用intent=chat、colors=[]、brightness=100、humidifier_on=false。"
    "用户询问目前/现在的温度、相对湿度或是否干燥时，使用intent=query_environment；不要猜测读数，网关会提供传感器数据。"
    "用户要求控制空调或其他未接入设备时，使用intent=unsupported、colors=[]、brightness=100、humidifier_on=false。"
    "reply使用简短自然的中文。不要询问RGB数值。"
)

class OllamaError(Exception):
    """调用本地 Ollama 失败时抛出（连接不上、返回非法数据等）。"""
    pass


class DeviceConflictError(ValueError):
    """设备当前运行模式不允许执行该手动动作。"""
    pass


def validate_device_url(value):
    """校验并规范化 ESP32 设备地址。

    只接受 http 协议、无路径/查询串、端口为 80（或省略）的地址，
    且主机部分必须是局域网 IPv4（或 .local 主机名）。
    返回去掉结尾斜杠的规范地址；空输入返回空字符串。
    """
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
        # mDNS 名称允许直接放行
        return f"http://{hostname}"
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise ValueError("请使用 ESP32 的局域网 IPv4 地址，例如 http://192.168.1.50。") from exc
    if address.version != 4 or not address.is_private:
        raise ValueError("ESP32 地址必须是局域网 IPv4 地址。")
    return f"http://{address}"


def is_negated_light_request(text):
    """判断是否是"否定式"的灯光指令（如"不要把灯调成蓝色"）。

    同时出现控制动词和否定词才算，用于在进入模型前直接短路，
    避免把否定句误判成控制命令。
    """
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


def is_negated_humidifier_request(text):
    normalized = text.strip().lower()
    has_device = any(term in normalized for term in ("雾化器", "加湿器", "雾化"))
    has_control = any(term in normalized for term in ("打开", "开启", "启动", "开", "关闭", "关", "停止", "停"))
    has_negation = any(term in normalized for term in ("不要", "别", "不需要", "不想", "don't", "do not"))
    return has_device and has_control and has_negation


def is_negated_curtain_request(text):
    normalized = text.strip().lower()
    has_device = any(term in normalized for term in ("窗帘", "窗簾", "帘子"))
    has_control = any(term in normalized for term in ("打开", "拉开", "关闭", "合上", "开窗帘", "关窗帘", "调整", "调到", "开到", "开", "关"))
    has_negation = any(term in normalized for term in ("不要", "别", "不需要", "不想", "don't", "do not"))
    return has_device and has_control and has_negation


def explicit_curtain_query_from_text(text):
    normalized = " ".join(text.strip().lower().split())
    if not any(term in normalized for term in ("窗帘", "窗簾", "帘子")):
        return False
    return any(
        term in normalized
        for term in (
            "多少",
            "几成",
            "开合度",
            "开合状态",
            "开度",
            "开到哪",
            "拉开多少",
            "开了吗",
            "开了没",
            "开了没有",
            "有没有开",
            "有没有打开",
            "现在怎么样",
            "目前怎样",
        )
    )


def explicit_humidifier_state_from_text(text):
    """Return an explicit on/off request, or None for chat and ambiguous text."""
    normalized = " ".join(text.strip().lower().split())
    if not any(term in normalized for term in ("雾化器", "加湿器", "雾化")):
        return None
    if is_negated_humidifier_request(normalized):
        return None
    if any(term in normalized for term in ("怎么", "如何", "是什么", "什么意思", "原理", "介绍", "吗", "？", "?")):
        return None

    wants_on = any(term in normalized for term in ("打开", "开启", "启动", "开一下", "开起来", "开始雾化", "开"))
    wants_off = any(term in normalized for term in ("关闭", "关掉", "关上", "停止", "停掉", "关一下", "关起来", "关"))
    if wants_on == wants_off:
        return None
    return wants_on


def _brightness_from_text(normalized):
    """从文本中抽取亮度百分比，抽不到返回 None。

    支持"百分之50""50%""亮度调到50""一半""最亮""最暗"等说法，
    多个数值时取最后一个；超出 0~100 直接抛 ValueError。
    """
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
    """用本地规则解析明确的灯光指令，识别不出则返回 None。

    返回 {"colors": [...], "brightness": int}。这一步优先于模型：
    正则能确定的指令直接执行，既快又不会出现模型幻觉。
    注意刻意排除疑问句（"蓝色是什么"）和否定句，让它们继续走模型/聊天分支。
    """
    normalized = " ".join(text.strip().lower().split())
    if any(term in normalized for term in ("窗帘", "窗簾", "帘子", "雾化器", "加湿器", "湿度", "温度", "步进电机")):
        return None
    # 问句式表达不是控制命令，交给模型回答
    if any(word in normalized for word in ("是什么", "什么意思", "为什么", "原理", "解释", "介绍一下", "教程")):
        return None
    if is_negated_light_request(normalized):
        return None

    # 可能表示"要操作灯"的动词/短语
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
    # 命中的颜色按 COLOR_TERMS 的键顺序返回（红/绿/蓝/黄/白）
    colors = [
        color
        for color, terms in COLOR_TERMS.items()
        if any(term in normalized for term in terms)
    ]
    has_action = any(word in normalized for word in action_terms)
    # "绿色和红色"这类多色混光
    combined_colors = len(colors) > 1 and any(word in normalized for word in ("和", "与", "、", "+", "and"))
    # "亮红灯"这类以"亮+颜色"开头的说法
    bright_color_phrase = bool(colors) and any(
        normalized.startswith(prefix) for prefix in ("亮红", "亮绿", "亮蓝", "亮黄", "亮白", "点亮")
    )
    # 整句只有一个颜色词，如"红灯"
    exact_color_command = any(
        normalized == term for terms in COLOR_TERMS.values() for term in terms
    )

    # 关灯优先处理
    if any(word in normalized for word in off_terms) and (has_action or exact_color_command):
        return {"colors": ["off"], "brightness": 100}
    # 以上信号都没有，视为不是灯光指令
    if not (
        has_action
        or combined_colors
        or bright_color_phrase
        or exact_color_command
        or (colors and brightness is not None)
    ):
        return None
    if not colors and brightness is None:
        # "开灯"默认白光；"调亮"之类没有具体颜色时留空，由上层沿用当前颜色
        if any(word in normalized for word in ("开灯", "打开灯", "亮灯", "turn on")):
            colors = ["white"]
        else:
            return None
    return {"colors": colors, "brightness": 100 if brightness is None else brightness}


def compose_light_rgb(colors, brightness, current_rgb=None, remembered_rgb=None):
    """把颜色列表和亮度换算成最终要下发的 RGB。

    做三件事：
      1. 多颜色时把各分量相加并截断到 255（红+绿=黄）；
      2. 没有指定颜色（只调亮度）时，沿用设备当前颜色，设备已是黑的
         则用最近一次点亮的颜色记忆恢复；
      3. 按亮度百分比缩放，+50 是为了四舍五入更均匀。

    返回 (输出 RGB, 未缩放的基色) —— 基色用于更新"记忆颜色"，
    这样关灯后再"调亮度"仍能恢复原来的色。
    """
    if "off" in colors:
        # 关灯：输出黑色，记忆色保持不变
        return (0, 0, 0), remembered_rgb

    if colors:
        base = tuple(
            min(255, sum(COLOR_RGB[color][channel] for color in colors))
            for channel in range(3)
        )
    else:
        # 无颜色：优先用设备当前颜色，其次用记忆色，最后兜底白光
        source = current_rgb if current_rgb and max(current_rgb) else remembered_rgb
        if source and max(source):
            peak = max(source)
            base = tuple(round(value * 255 / peak) for value in source)
        else:
            base = COLOR_RGB["white"]

    output = tuple((value * brightness + 50) // 100 for value in base)
    return output, base


def summarize_light_state(status):
    """Derive an on/off state, requested brightness, and color name from ESP32 RGB."""
    rgb = status.get("rgb") if isinstance(status, dict) else None
    if not isinstance(rgb, dict):
        raise ValueError("ESP32 状态中缺少 RGB 颜色值。")

    values = tuple(rgb.get(channel) for channel in ("r", "g", "b"))
    if any(
        isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255
        for value in values
    ):
        raise ValueError("ESP32 返回的 RGB 颜色值无效。")

    peak = max(values)
    is_on = peak > 0
    if is_on:
        normalized = tuple((value * 255 + peak // 2) // peak for value in values)
        color, reference = min(
            LIGHT_STATE_PALETTE,
            key=lambda item: sum(abs(actual - expected) for actual, expected in zip(normalized, item[1])),
        )
        if sum(abs(actual - expected) for actual, expected in zip(normalized, reference)) > 24:
            color = "自定义颜色"
    else:
        color = "无"

    return {
        "on": is_on,
        "brightness": (peak * 100 + 127) // 255,
        "color": color,
        "rgb": dict(zip(("r", "g", "b"), values)),
    }


def parse_manual_light_settings(payload):
    """Validate a direct RGB + brightness request from the manual controls."""
    if not isinstance(payload, dict) or not isinstance(payload.get("rgb"), dict):
        raise ValueError("请求需要包含 RGB 颜色值。")

    rgb = payload["rgb"]
    values = tuple(rgb.get(channel) for channel in ("r", "g", "b"))
    if any(
        isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255
        for value in values
    ):
        raise ValueError("RGB 颜色值必须是 0 到 255 的整数。")

    brightness = payload.get("brightness")
    if isinstance(brightness, bool) or not isinstance(brightness, int) or not 0 <= brightness <= 100:
        raise ValueError("亮度必须是 0 到 100 的整数。")

    return values, brightness


def scale_rgb_brightness(rgb, brightness):
    """Scale an RGB tuple using the same integer rounding as chat controls."""
    return tuple((value * brightness + 50) // 100 for value in rgb)


def parse_humidifier_request(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("on"), bool):
        raise ValueError("请求需要包含 on 布尔值。")
    return payload["on"]


def parse_curtain_request(payload):
    if not isinstance(payload, dict):
        raise ValueError("请求格式不正确。")
    operation = payload.get("operation")
    if operation == "stop":
        return {"operation": "stop"}
    if operation == "set_closed":
        direction = payload.get("open_direction")
        if direction not in ("forward", "reverse"):
            raise ValueError("请选择电机正转或反转中，哪一个方向会打开窗帘。")
        return {"operation": operation, "open_direction": direction}
    if operation == "set_open":
        return {"operation": operation}
    if operation == "set_position":
        position_pct = payload.get("position_pct")
        interval_ms = payload.get("interval_ms")
        if isinstance(position_pct, bool) or not isinstance(position_pct, int) or not 0 <= position_pct <= 100:
            raise ValueError("窗帘开合度必须是0到100的整数百分比。")
        if isinstance(interval_ms, bool) or not isinstance(interval_ms, int) or not STEPPER_MIN_INTERVAL_MS <= interval_ms <= STEPPER_MAX_INTERVAL_MS:
            raise ValueError(f"速度间隔必须是{STEPPER_MIN_INTERVAL_MS}到{STEPPER_MAX_INTERVAL_MS}毫秒。")
        return {"operation": operation, "position_pct": position_pct, "interval_ms": interval_ms}
    if operation == "jog":
        direction = payload.get("direction")
        steps = payload.get("steps")
        interval_ms = payload.get("interval_ms")
        if direction not in ("open", "close"):
            raise ValueError("点动方向必须是打开或关闭。")
        if isinstance(steps, bool) or not isinstance(steps, int) or not STEPPER_MIN_STEPS <= steps <= STEPPER_MAX_STEPS:
            raise ValueError(f"点动步数必须是{STEPPER_MIN_STEPS}到{STEPPER_MAX_STEPS}的整数。")
        if isinstance(interval_ms, bool) or not isinstance(interval_ms, int) or not STEPPER_MIN_INTERVAL_MS <= interval_ms <= STEPPER_MAX_INTERVAL_MS:
            raise ValueError(f"速度间隔必须是{STEPPER_MIN_INTERVAL_MS}到{STEPPER_MAX_INTERVAL_MS}毫秒。")
        return {"operation": operation, "direction": direction, "steps": steps, "interval_ms": interval_ms}
    raise ValueError("不支持的窗帘操作。")


def parse_environment_config(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("auto_mode"), bool):
        raise ValueError("请求需要包含 auto_mode 布尔值。")

    on_below = payload.get("on_below_pct")
    off_at = payload.get("off_at_pct")
    if isinstance(on_below, bool) or not isinstance(on_below, int) or not HUMIDITY_ON_BELOW_MIN <= on_below <= HUMIDITY_ON_BELOW_MAX:
        raise ValueError(f"开启湿度阈值必须是 {HUMIDITY_ON_BELOW_MIN}% 到 {HUMIDITY_ON_BELOW_MAX}% 的整数。")
    if isinstance(off_at, bool) or not isinstance(off_at, int) or not HUMIDITY_OFF_AT_MIN <= off_at <= HUMIDITY_OFF_AT_MAX:
        raise ValueError(f"关闭湿度阈值必须是 {HUMIDITY_OFF_AT_MIN}% 到 {HUMIDITY_OFF_AT_MAX}% 的整数。")
    if not HUMIDITY_MIN_THRESHOLD_GAP <= off_at - on_below <= HUMIDITY_MAX_THRESHOLD_GAP:
        raise ValueError(f"开启和关闭阈值之间需相差 {HUMIDITY_MIN_THRESHOLD_GAP}% 到 {HUMIDITY_MAX_THRESHOLD_GAP}%。")

    return {
        "auto_mode": payload["auto_mode"],
        "on_below_pct": on_below,
        "off_at_pct": off_at,
    }


def parse_model_decision(content):
    """解析并严格校验模型返回的 JSON 决策。

    任何不合法的地方（JSON 解析失败、intent 不认识、颜色不在白名单、
    off 与其它颜色混用、亮度不是 0~100 的整数）都抛 ValueError，
    从而保证不会把脏指令发给硬件。返回统一的 dict。
    """
    try:
        data = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("本地模型没有返回有效的 JSON 指令。") from exc
    if not isinstance(data, dict):
        raise ValueError("本地模型返回的数据格式不正确。")

    intent = data.get("intent")
    colors = data.get("colors")
    brightness = data.get("brightness")
    humidifier_on = data.get("humidifier_on")
    curtain_position_pct = data.get("curtain_position_pct")
    reply = data.get("reply")
    if intent not in ("chat", "set_light", "set_humidifier", "set_curtain", "stop_curtain", "query_curtain", "query_environment", "unsupported"):
        raise ValueError("本地模型返回了不支持的操作类型。")
    if not isinstance(reply, str):
        reply = ""
    # 去掉推理模型可能带上的 <think> 段，并限制长度
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()[:500]

    if intent == "set_light":
        # 颜色必须是非空列表、最多 5 个、全部在白名单内，且 off 不能与其它颜色共存
        if (
            not isinstance(colors, list)
            or not colors
            or len(colors) > 5
            or any(not isinstance(color, str) or color not in COLOR_RGB for color in colors)
            or ("off" in colors and len(colors) != 1)
        ):
            raise ValueError("本地模型提出了无效颜色，未执行硬件操作。")
        # 亮度必须是非 bool 的整数且落在 0~100
        if isinstance(brightness, bool) or not isinstance(brightness, int) or not 0 <= brightness <= 100:
            raise ValueError("本地模型提出了无效亮度，未执行硬件操作。")
        # 去重但保持顺序
        colors = list(dict.fromkeys(colors))
        humidifier_on = False
        curtain_position_pct = 0
    elif intent == "set_humidifier":
        if not isinstance(humidifier_on, bool):
            raise ValueError("本地模型没有给出有效的雾化器开关状态，未执行硬件操作。")
        colors = []
        brightness = 100
        curtain_position_pct = 0
    elif intent == "set_curtain":
        if (
            isinstance(curtain_position_pct, bool)
            or not isinstance(curtain_position_pct, int)
            or not 0 <= curtain_position_pct <= 100
        ):
            raise ValueError("窗帘开合度必须是0到100的整数百分比，未执行硬件操作。")
        colors = []
        brightness = 100
        humidifier_on = False
    elif intent == "query_curtain":
        colors = []
        brightness = 100
        humidifier_on = False
        curtain_position_pct = 0
    elif intent == "stop_curtain":
        colors = []
        brightness = 100
        humidifier_on = False
        curtain_position_pct = 0
    else:
        # 聊天/不支持的动作强制清空硬件参数
        colors = []
        brightness = 100
        humidifier_on = False
        curtain_position_pct = 0
    return {
        "intent": intent,
        "colors": colors,
        "brightness": brightness,
        "humidifier_on": humidifier_on,
        "curtain_position_pct": curtain_position_pct,
        "reply": reply,
    }


def call_ollama(messages, ollama_url, model):
    """调用本地 Ollama 的 /api/chat，返回校验后的决策 dict。

    参数固定为不流式、温度 0、限制上下文和输出长度，并传入 JSON Schema
    约束输出格式；任何异常统一包装成 OllamaError 抛出。
    """
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
    """会话 ID 必须是不超过 80 位的字母/数字/下划线/连字符，防止被注入脏数据。"""
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) is not None


def configure_https(server, certfile=None, keyfile=None):
    """Optionally wrap the gateway socket in TLS for browser microphone access."""
    if bool(certfile) != bool(keyfile):
        raise ValueError("HTTPS 需要同时提供证书文件和私钥文件。")
    if not certfile:
        return "http"

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return "https"


class GatewayHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器：对外暴露网页和 /api/* 接口。

    每个请求由 ThreadingHTTPServer 分配一个线程处理，
    共享状态（设备地址、会话历史、记忆颜色、OLED 队列）挂在 server 对象上。
    """

    server_version = "ESP32Bridge/1.0"

    def _send_json(self, status, payload):
        """统一发送 JSON 响应。

        额外做一件事：如果是 /api/chat 且带 reply，就把助手的回复
        投递到 OLED 队列，让设备屏幕也显示出来。
        """
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
        """读取并解析请求体 JSON，长度非法或格式错误时抛 ValueError。"""
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
        """处理 GET 请求：首页、配置查询、设备状态、健康检查。"""
        if self.path == "/":
            # 返回前端单页
            page = (WEB_ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return

        if self.path == "/api/config":
            # 供前端回填设备地址和模型名
            self._send_json(
                200,
                {
                    "device_url": self.server.device_url,
                    "model": self.server.ollama_model,
                },
            )
            return

        if self.path == "/api/device":
            # 代理查询 ESP32 的 /api/status，供页面展示
            if not self.server.device_url:
                self._send_json(503, {"ok": False, "error": "请先填写 ESP32 的局域网地址。"})
                return
            try:
                status = self._get_device_status()
                status["light"] = summarize_light_state(status)
                status["humidifier_on"] = status.get("humidifier_on") is True
                self._send_json(200, status)
            except (OSError, ValueError, TimeoutError) as exc:
                self._send_json(502, {"ok": False, "error": str(exc)})
            return

        if self.path == "/health":
            # 简单的进程存活探针
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
        """处理设备配置、手动控灯与聊天请求。"""
        if self.path == "/api/environment/config":
            self._handle_environment_config()
            return

        if self.path == "/api/humidifier":
            self._handle_humidifier_request()
            return

        if self.path == "/api/light":
            self._handle_manual_light()
            return

        if self.path == "/api/curtain":
            self._handle_curtain_request()
            return

        if self.path == "/api/device/config":
            # 保存/校验前端填写的 ESP32 地址
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

        # ---- 以下是 /api/chat 的处理流程 ----
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
        # 会话 ID 非法就新开一个，保证多轮上下文可用
        session_id = payload.get("session_id")
        if not session_id_is_valid(session_id):
            session_id = uuid.uuid4().hex
        # 用户的输入同步推到 OLED 显示
        queue_oled_message(
            getattr(self.server, "display_queue", None),
            self.server.device_url,
            "YOU",
            text,
        )

        # 否定句直接短路，避免误操作灯光
        if is_negated_light_request(text):
            reply = "好的，我不会更改灯光。"
            self._remember_turn(session_id, text, reply)
            self._send_json(200, {"ok": True, "reply": reply, "action": None, "session_id": session_id})
            return
        if is_negated_humidifier_request(text):
            reply = "好的，我不会操作雾化器。"
            self._remember_turn(session_id, text, reply)
            self._send_json(200, {"ok": True, "reply": reply, "action": None, "session_id": session_id})
            return
        if is_negated_curtain_request(text):
            reply = "好的，我不会操作窗帘。"
            self._remember_turn(session_id, text, reply)
            self._send_json(200, {"ok": True, "reply": reply, "action": None, "session_id": session_id})
            return

        # 先尝试本地规则解析
        try:
            explicit_settings = explicit_light_settings_from_text(text)
        except ValueError as exc:
            self._send_json(400, {"ok": False, "reply": str(exc), "error": str(exc), "session_id": session_id})
            return
        explicit_humidifier_on = explicit_humidifier_state_from_text(text)
        explicit_curtain_query = explicit_curtain_query_from_text(text)

        # 本地规则命中就直接执行；否则交给模型判断意图
        try:
            decision = (
                {
                    "intent": "set_light",
                    **explicit_settings,
                    "humidifier_on": False,
                    "reply": "",
                }
                if explicit_settings is not None
                else {
                    "intent": "set_humidifier",
                    "colors": [],
                    "brightness": 100,
                    "humidifier_on": explicit_humidifier_on,
                    "reply": "",
                }
                if explicit_humidifier_on is not None
                else {
                    "intent": "query_curtain",
                    "colors": [],
                    "brightness": 100,
                    "humidifier_on": False,
                    "curtain_position_pct": 0,
                    "reply": "",
                }
                if explicit_curtain_query
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

        if decision["intent"] == "set_humidifier":
            if not self.server.device_url:
                self._send_json(
                    503,
                    {
                        "ok": False,
                        "reply": "先连接 ESP32，再控制雾化器。",
                        "action": None,
                        "session_id": session_id,
                    },
                )
                return

            on = decision["humidifier_on"]
            try:
                action, device_result = self._set_humidifier_state(on)
            except DeviceConflictError as exc:
                self._send_json(
                    409,
                    {"ok": False, "reply": str(exc), "error": str(exc), "session_id": session_id},
                )
                return
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                self._send_json(
                    502,
                    {
                        "ok": False,
                        "reply": "ESP32 没有确认执行雾化器开关。",
                        "action": {"action": "set_humidifier", "on": on},
                        "error": str(exc),
                        "session_id": session_id,
                    },
                )
                return

            reply = "已开启雾化器。" if on else "已关闭雾化器。"
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

        if decision["intent"] == "set_curtain":
            if not self.server.device_url:
                self._send_json(
                    503,
                    {"ok": False, "reply": "先连接 ESP32，再控制窗帘。", "session_id": session_id},
                )
                return

            position_pct = decision["curtain_position_pct"]
            action = {
                "request_id": uuid.uuid4().hex,
                "action": "curtain_set_position",
                "position_pct": position_pct,
                "interval_ms": STEPPER_DEFAULT_INTERVAL_MS,
            }
            try:
                device_result = self._post_device_action(action)
            except DeviceConflictError as exc:
                self._send_json(409, {"ok": False, "reply": str(exc), "error": str(exc), "session_id": session_id})
                return
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                self._send_json(
                    502,
                    {"ok": False, "reply": "ESP32 没有确认窗帘动作。", "error": str(exc), "session_id": session_id},
                )
                return

            curtain_state = device_result.get("state", {}).get("curtain", {})
            reply = (
                f"已开始将窗帘调整到 {position_pct}% 开合度。"
                if curtain_state.get("busy") is True
                else f"窗帘当前开合度约 {position_pct}%。"
            )
            self._remember_turn(session_id, text, reply)
            self._send_json(
                200,
                {"ok": True, "reply": reply, "action": action, "device": device_result,
                 "curtain": curtain_state, "session_id": session_id},
            )
            return

        if decision["intent"] == "stop_curtain":
            if not self.server.device_url:
                self._send_json(503, {"ok": False, "reply": "先连接 ESP32，再停止窗帘。", "session_id": session_id})
                return
            action = {"request_id": uuid.uuid4().hex, "action": "stepper_stop"}
            try:
                device_result = self._post_device_action(action)
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                self._send_json(502, {"ok": False, "reply": "ESP32 没有确认停止请求。", "error": str(exc), "session_id": session_id})
                return
            curtain_state = device_result.get("state", {}).get("curtain", {})
            reply = "已请求窗帘停止。" if curtain_state.get("busy") else "窗帘当前没有移动。"
            self._remember_turn(session_id, text, reply)
            self._send_json(200, {"ok": True, "reply": reply, "action": action, "curtain": curtain_state, "session_id": session_id})
            return

        if decision["intent"] == "query_curtain":
            if not self.server.device_url:
                self._send_json(503, {"ok": False, "reply": "先连接 ESP32，再读取窗帘状态。", "session_id": session_id})
                return
            try:
                status = self._get_device_status()
            except (OSError, ValueError, TimeoutError) as exc:
                self._send_json(502, {"ok": False, "reply": str(exc), "error": str(exc), "session_id": session_id})
                return

            curtain_state = status.get("curtain", {})
            if not isinstance(curtain_state, dict) or curtain_state.get("calibrated") is not True:
                reply = "窗帘行程还没有校准，请先在控制台设置全关和全开端点。"
            elif curtain_state.get("busy") is True:
                position_pct = curtain_state.get("position_pct")
                direction = "打开" if curtain_state.get("moving_toward_open") is True else "关闭"
                reply = f"窗帘正在{direction}，当前位置估算约 {position_pct}%。"
            elif curtain_state.get("position_valid") is True:
                reply = f"窗帘当前开合度约 {curtain_state.get('position_pct')}%。"
            else:
                reply = "窗帘位置不确定，请重新校准全关和全开端点。"
            self._remember_turn(session_id, text, reply)
            self._send_json(200, {"ok": True, "reply": reply, "curtain": curtain_state, "session_id": session_id})
            return

        if decision["intent"] == "query_environment":
            if not self.server.device_url:
                self._send_json(
                    503,
                    {
                        "ok": False,
                        "reply": "先连接 ESP32，再读取温湿度。",
                        "session_id": session_id,
                    },
                )
                return
            try:
                status = self._get_device_status()
            except (OSError, ValueError, TimeoutError) as exc:
                self._send_json(502, {"ok": False, "reply": str(exc), "error": str(exc), "session_id": session_id})
                return

            if not status.get("sensor_ok"):
                reply = "目前没有有效的 DHT11 温湿度读数，请检查传感器接线。"
            else:
                temperature = status.get("temperature_c")
                humidity = status.get("humidity_pct")
                if not all(
                    isinstance(value, (int, float)) and not isinstance(value, bool)
                    for value in (temperature, humidity)
                ):
                    reply = "DHT11 返回的数据格式无效，请检查传感器。"
                else:
                    reply = f"当前温度 {temperature:.1f}°C，相对湿度 {humidity:.1f}%。"
                    if status.get("auto_mode") is True:
                        reply += (
                            f"自动控湿已开启，湿度低于 {status.get('on_below_pct')}% 时开启雾化器，"
                            f"达到 {status.get('off_at_pct')}% 时关闭。"
                        )
            self._remember_turn(session_id, text, reply)
            self._send_json(
                200,
                {
                    "ok": True,
                    "reply": reply,
                    "environment": {
                        "sensor_ok": status.get("sensor_ok") is True,
                        "temperature_c": status.get("temperature_c"),
                        "humidity_pct": status.get("humidity_pct"),
                        "sensor_age_ms": status.get("sensor_age_ms"),
                        "auto_mode": status.get("auto_mode") is True,
                        "on_below_pct": status.get("on_below_pct"),
                        "off_at_pct": status.get("off_at_pct"),
                        "humidifier_on": status.get("humidifier_on") is True,
                    },
                    "session_id": session_id,
                },
            )
            return

        if decision["intent"] == "set_light":
            # 没连设备就不能控灯
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
                # 只调亮度时需要先读设备当前颜色
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

            # 换算成最终 RGB，并保留基色用于记忆
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
            # 下发到 ESP32
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

            # 更新"最近一次点亮颜色"记忆（关灯时 base 为 None 不会覆盖）
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

        # 非控灯分支：不支持的动作给固定话术，普通聊天用模型回复
        if decision["intent"] == "unsupported":
            reply = "目前接入了 RGB 灯、雾化器和窗帘控制，暂时不能操作其他设备。"
        else:
            reply = decision["reply"] or "你好，有什么我可以帮你？"

        self._remember_turn(session_id, text, reply)
        self._send_json(
            200,
            {"ok": True, "reply": reply, "action": None, "session_id": session_id},
        )

    def _handle_manual_light(self):
        """Apply validated manual RGB and brightness settings without invoking Ollama."""
        try:
            payload = self._read_json()
            base_rgb, brightness = parse_manual_light_settings(payload)
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return

        if not self.server.device_url:
            self._send_json(503, {"ok": False, "error": "请先连接 ESP32。"})
            return

        output_rgb = scale_rgb_brightness(base_rgb, brightness)
        action = {
            "request_id": uuid.uuid4().hex,
            "action": "set_rgb",
            "r": output_rgb[0],
            "g": output_rgb[1],
            "b": output_rgb[2],
        }
        try:
            device_result = self._post_device_action(action)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            self._send_json(502, {"ok": False, "error": f"ESP32 没有确认执行：{exc}"})
            return

        if any(base_rgb):
            self.server.last_lit_rgb = base_rgb
        self._send_json(
            200,
            {
                "ok": True,
                "light": summarize_light_state({"rgb": dict(zip(("r", "g", "b"), output_rgb))}),
                "device": device_result,
            },
        )

    def _handle_humidifier_request(self):
        try:
            on = parse_humidifier_request(self._read_json())
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return

        if not self.server.device_url:
            self._send_json(503, {"ok": False, "error": "请先连接 ESP32。"})
            return

        try:
            action, device_result = self._set_humidifier_state(on)
        except DeviceConflictError as exc:
            self._send_json(409, {"ok": False, "error": str(exc)})
            return
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            self._send_json(502, {"ok": False, "error": f"ESP32 没有确认执行：{exc}"})
            return

        self._send_json(
            200,
            {"ok": True, "humidifier_on": on, "action": action, "device": device_result},
        )

    def _handle_curtain_request(self):
        try:
            settings = parse_curtain_request(self._read_json())
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return

        if not self.server.device_url:
            self._send_json(503, {"ok": False, "error": "请先连接 ESP32。"})
            return

        action_name = {
            "stop": "stepper_stop",
            "set_closed": "curtain_set_closed",
            "set_open": "curtain_set_open",
            "set_position": "curtain_set_position",
            "jog": "curtain_jog",
        }[settings["operation"]]
        action = {"request_id": uuid.uuid4().hex, "action": action_name}
        action.update({key: value for key, value in settings.items() if key != "operation"})
        try:
            device_result = self._post_device_action(action)
        except DeviceConflictError as exc:
            self._send_json(409, {"ok": False, "error": str(exc)})
            return
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            self._send_json(502, {"ok": False, "error": f"ESP32 没有确认窗帘操作：{exc}"})
            return

        state = device_result.get("state", {}).get("curtain", {})
        self._send_json(200, {"ok": True, "curtain": state, "action": action})

    def _handle_environment_config(self):
        try:
            settings = parse_environment_config(self._read_json())
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return

        if not self.server.device_url:
            self._send_json(503, {"ok": False, "error": "请先连接 ESP32。"})
            return

        try:
            result = self._post_environment_config(settings)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            self._send_json(502, {"ok": False, "error": f"ESP32 没有确认自动控湿设置：{exc}"})
            return
        self._send_json(200, result)

    def _set_humidifier_state(self, on):
        action = {
            "request_id": uuid.uuid4().hex,
            "action": "set_humidifier",
            "on": on,
        }
        return action, self._post_device_action(action)

    def _post_environment_config(self, settings):
        request = urllib.request.Request(
            f"{self.server.device_url}/api/humidity/config",
            data=json.dumps(settings).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code == 409:
                raise DeviceConflictError("自动控湿正在运行，请先关闭自动模式再手动控制雾化器。") from exc
            raise ValueError(f"ESP32 返回 HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise urllib.error.URLError(
                f"无法连接 ESP32：{exc.reason}。请确认同一 Wi-Fi 且设备 IP 正确。"
            ) from exc
        if not isinstance(result, dict) or not result.get("ok"):
            raise ValueError(f"ESP32 未确认自动控湿设置：{result}")
        return result

    def _get_model_decision(self, session_id, text):
        """把该会话的历史消息 + 当前输入一起发给模型，返回决策 dict。"""
        with self.server.sessions_lock:
            history = list(self.server.sessions.get(session_id, []))
        messages = [*history, {"role": "user", "content": text}]
        return call_ollama(messages, self.server.ollama_url, self.server.ollama_model)

    def _remember_turn(self, session_id, user_text, assistant_text):
        """记录一轮对话到会话历史，并做两件清理：

        每个会话只保留最近 MAX_SESSION_MESSAGES 条；会话总数超过 MAX_SESSIONS
        时丢弃最早创建的会话，防止内存无限增长。
        """
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
        """向 ESP32 的 /api/action 下发动作，返回设备响应 dict。

        连接失败、HTTP 错误码或设备未确认（ok 不为真）都会抛异常。
        """
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
            if exc.code == 409:
                raise DeviceConflictError("窗帘当前无法执行此操作；请检查是否正在移动或尚未完成行程校准。") from exc
            raise ValueError(f"ESP32 返回 HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise urllib.error.URLError(
                f"无法连接 ESP32：{exc.reason}。请确认同一 Wi-Fi 且设备 IP 正确。"
            ) from exc

        if not isinstance(result, dict) or not result.get("ok"):
            raise ValueError(f"ESP32 未确认执行：{result}")
        return result

    def _get_device_status(self):
        """读取 ESP32 的 /api/status，返回其响应 dict。"""
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
        """覆盖默认日志格式，统一加上 [gateway] 前缀。"""
        print(f"[gateway] {self.address_string()} - {fmt % args}")


def main():
    """命令行入口：解析参数、初始化共享状态、启动 HTTP(S) 服务。"""
    parser = argparse.ArgumentParser(description="Laptop gateway for ESP32 and local Ollama")
    parser.add_argument("--device-url", default="", help="ESP32 URL, e.g. http://192.168.1.50")
    parser.add_argument("--host", default="127.0.0.1", help="Web bind address")
    parser.add_argument("--port", type=int, default=8080, help="Web port")
    parser.add_argument("--tls-certfile", help="PEM certificate for HTTPS")
    parser.add_argument("--tls-keyfile", help="PEM private key for HTTPS")
    parser.add_argument("--ollama-url", default=OLLAMA_URL, help="Local Ollama chat endpoint")
    parser.add_argument("--model", default=OLLAMA_MODEL, help="Installed local Ollama model")
    args = parser.parse_args()

    # 启动参数里的设备地址同样要走校验
    try:
        device_url = validate_device_url(args.device_url)
    except ValueError as exc:
        parser.error(str(exc))

    # 出于安全考虑，Ollama 只允许本机地址
    ollama_parts = urlsplit(args.ollama_url)
    if ollama_parts.scheme != "http" or ollama_parts.hostname not in ("127.0.0.1", "localhost"):
        parser.error("--ollama-url must point to a local HTTP Ollama server")
    if not args.model.strip():
        parser.error("--model cannot be empty")

    server = ThreadingHTTPServer((args.host, args.port), GatewayHandler)
    try:
        scheme = configure_https(server, args.tls_certfile, args.tls_keyfile)
    except (OSError, ssl.SSLError, ValueError) as exc:
        server.server_close()
        parser.error(f"HTTPS 配置失败：{exc}")

    # 共享状态挂在 server 实例上，供各请求线程访问
    server.device_url = device_url
    server.ollama_url = args.ollama_url
    server.ollama_model = args.model.strip()
    server.sessions = {}
    server.sessions_lock = threading.Lock()
    server.last_lit_rgb = None  # 最近一次点亮的基色，用于关灯后恢复
    server.display_queue = queue.Queue(maxsize=8)
    # OLED 播放线程设为 daemon，随主进程退出而结束
    threading.Thread(
        target=_oled_display_worker,
        args=(server.display_queue,),
        name="oled-display",
        daemon=True,
    ).start()
    display_host = args.host if args.host not in ("0.0.0.0", "::") else "127.0.0.1"
    print(f"网页地址：{scheme}://{display_host}:{args.port}/")
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
