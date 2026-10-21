"""gateway.py 的单元测试。

覆盖四类逻辑：
  1. 模型输出解析与非法指令拒绝（ModelDecisionTests）；
  2. 中文灯光指令的本地解析、RGB 混色与亮度换算（LightCommandTests）；
  3. OLED 文字渲染与帧格式（OledRenderingTests）；
  4. 设备地址合法性校验（DeviceUrlTests）。

运行方式（在 host_gateway 目录下）：
    python -m unittest discover -s tests
"""

import importlib.util
import json
import queue
import unittest
from pathlib import Path


# 以文件路径方式导入 gateway.py（gateway 不是包，避免额外改动 sys.path）
MODULE_PATH = Path(__file__).resolve().parents[1] / "gateway.py"
SPEC = importlib.util.spec_from_file_location("gateway", MODULE_PATH)
gateway = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gateway)


class ModelDecisionTests(unittest.TestCase):
    """测试 parse_model_decision：模型返回内容的校验与归一化。"""

    def test_set_light_decision(self):
        """合法的控灯指令应被完整解析出来。"""
        decision = gateway.parse_model_decision(
            '{"intent":"set_light","colors":["red","green"],"brightness":30,"humidifier_on":false,"reply":"好的。"}'
        )
        self.assertEqual(decision["intent"], "set_light")
        self.assertEqual(decision["colors"], ["red", "green"])
        self.assertEqual(decision["brightness"], 30)
        self.assertFalse(decision["humidifier_on"])

    def test_set_humidifier_decision(self):
        decision = gateway.parse_model_decision(
            '{"intent":"set_humidifier","colors":[],"brightness":100,"humidifier_on":true,"reply":"好的。"}'
        )
        self.assertEqual(decision["intent"], "set_humidifier")
        self.assertTrue(decision["humidifier_on"])
        self.assertEqual(decision["colors"], [])

        off = gateway.parse_model_decision(
            '{"intent":"set_humidifier","colors":[],"brightness":100,"humidifier_on":false,"reply":"好的。"}'
        )
        self.assertFalse(off["humidifier_on"])

    def test_curtain_target_and_stop_decisions(self):
        decision = gateway.parse_model_decision(
            '{"intent":"set_curtain","colors":[],"brightness":100,"humidifier_on":false,'
            '"curtain_position_pct":35,"reply":"打开到35%。"}'
        )
        self.assertEqual(decision["curtain_position_pct"], 35)

        stop = gateway.parse_model_decision(
            '{"intent":"stop_curtain","colors":[],"brightness":100,"humidifier_on":false,'
            '"curtain_position_pct":0,"reply":"停止窗帘。"}'
        )
        self.assertEqual(stop["intent"], "stop_curtain")

        for pct in (-1, 101, True, 25.5):
            content = (
                '{"intent":"set_curtain","colors":[],"brightness":100,"humidifier_on":false,'
                f'"curtain_position_pct":{json.dumps(pct)},"reply":"调整窗帘。"}}'
            )
            with self.subTest(pct=pct), self.assertRaises(ValueError):
                gateway.parse_model_decision(content)

    def test_query_curtain_is_non_actuating(self):
        decision = gateway.parse_model_decision(
            '{"intent":"query_curtain","colors":["red"],"brightness":10,"humidifier_on":true,'
            '"curtain_position_pct":90,"reply":"查询窗帘。"}'
        )
        self.assertEqual(decision["intent"], "query_curtain")
        self.assertEqual(decision["curtain_position_pct"], 0)
        self.assertFalse(decision["humidifier_on"])

    def test_query_environment_is_non_actuating(self):
        decision = gateway.parse_model_decision(
            '{"intent":"query_environment","colors":["red"],"brightness":5,"humidifier_on":true,"reply":"查看当前湿度。"}'
        )
        self.assertEqual(decision["intent"], "query_environment")
        self.assertEqual(decision["colors"], [])
        self.assertEqual(decision["brightness"], 100)
        self.assertFalse(decision["humidifier_on"])

    def test_non_action_fields_are_ignored(self):
        """聊天意图下，模型多给的颜色/亮度字段必须被清空，避免误控灯。"""
        decision = gateway.parse_model_decision(
            '{"intent":"chat","colors":["white"],"brightness":45,"humidifier_on":true,"reply":"你好。"}'
        )
        self.assertEqual(decision["intent"], "chat")
        self.assertEqual(decision["colors"], [])
        self.assertEqual(decision["brightness"], 100)
        self.assertFalse(decision["humidifier_on"])

    def test_invalid_light_settings_are_rejected(self):
        """非法颜色、超范围亮度、off 与其它颜色混用都应抛 ValueError。"""
        invalid = (
            '{"intent":"set_light","colors":["purple"],"brightness":30,"humidifier_on":false,"reply":"好的。"}',
            '{"intent":"set_light","colors":["red"],"brightness":101,"humidifier_on":false,"reply":"好的。"}',
            '{"intent":"set_light","colors":["off","red"],"brightness":50,"humidifier_on":false,"reply":"好的。"}',
            '{"intent":"set_humidifier","colors":[],"brightness":100,"humidifier_on":"true","reply":"好的。"}',
        )
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(ValueError):
                gateway.parse_model_decision(content)


class LightCommandTests(unittest.TestCase):
    """测试本地中文指令解析（不经模型）与 RGB 合成。"""

    def test_explicit_color_and_mixed_color_commands(self):
        """单色、混色 + 亮度、单色改亮度、开灯默认白光等常见说法。"""
        self.assertEqual(
            gateway.explicit_light_settings_from_text("把灯调成蓝色"),
            {"colors": ["blue"], "brightness": 100},
        )
        self.assertEqual(
            gateway.explicit_light_settings_from_text("亮绿色和红色，亮度 30%"),
            {"colors": ["red", "green"], "brightness": 30},
        )
        self.assertEqual(
            gateway.explicit_light_settings_from_text("把红灯亮度调到一半"),
            {"colors": ["red"], "brightness": 50},
        )

        self.assertEqual(
            gateway.explicit_light_settings_from_text("开灯"),
            {"colors": ["white"], "brightness": 100},
        )

    def test_brightness_only_keeps_current_color(self):
        """只说亮度时 colors 为空，表示沿用设备当前颜色。"""
        self.assertEqual(
            gateway.explicit_light_settings_from_text("把灯亮度调到30%"),
            {"colors": [], "brightness": 30},
        )

    def test_off_negation_and_questions(self):
        """关灯可识别；否定句和疑问句必须不被当成控制指令。"""
        self.assertEqual(
            gateway.explicit_light_settings_from_text("关灯"),
            {"colors": ["off"], "brightness": 100},
        )
        self.assertIsNone(
            gateway.explicit_light_settings_from_text("不要把灯调成蓝色")
        )
        self.assertIsNone(gateway.explicit_light_settings_from_text("别关灯"))
        self.assertIsNone(
            gateway.explicit_light_settings_from_text("蓝色是什么")
        )
        self.assertTrue(gateway.is_negated_light_request("不要把灯调成蓝色"))
        self.assertFalse(gateway.is_negated_light_request("把灯调成蓝色"))

    def test_brightness_out_of_range_is_rejected(self):
        """亮度超过 100% 应直接报错。"""
        with self.assertRaises(ValueError):
            gateway.explicit_light_settings_from_text("红灯亮度调到120%")

    def test_rgb_mixing_and_brightness_scaling(self):
        """红+绿混成黄色；30% 亮度时各分量按比例缩小（四舍五入）。"""
        self.assertEqual(
            gateway.compose_light_rgb(["red", "green"], 100),
            ((255, 255, 0), (255, 255, 0)),
        )
        self.assertEqual(
            gateway.compose_light_rgb(["red", "green"], 30),
            ((77, 77, 0), (255, 255, 0)),
        )

    def test_brightness_only_preserves_current_or_remembered_color(self):
        """只调亮度时：优先按设备当前颜色归一化，其次用记忆颜色恢复。"""
        self.assertEqual(
            gateway.compose_light_rgb([], 50, current_rgb=(80, 0, 0)),
            ((128, 0, 0), (255, 0, 0)),
        )
        self.assertEqual(
            gateway.compose_light_rgb([], 40, current_rgb=(0, 0, 0), remembered_rgb=(0, 255, 0)),
            ((0, 102, 0), (0, 255, 0)),
        )


class LightStateSummaryTests(unittest.TestCase):
    def test_off_state(self):
        self.assertEqual(
            gateway.summarize_light_state({"rgb": {"r": 0, "g": 0, "b": 0}}),
            {
                "on": False,
                "brightness": 0,
                "color": "无",
                "rgb": {"r": 0, "g": 0, "b": 0},
            },
        )

    def test_dimmed_color_reports_intended_brightness(self):
        result = gateway.summarize_light_state({"rgb": {"r": 77, "g": 29, "b": 0}})
        self.assertTrue(result["on"])
        self.assertEqual(result["brightness"], 30)
        self.assertEqual(result["color"], "黄色")

    def test_mixed_color_and_custom_rgb(self):
        cyan = gateway.summarize_light_state({"rgb": {"r": 0, "g": 128, "b": 128}})
        custom = gateway.summarize_light_state({"rgb": {"r": 128, "g": 64, "b": 20}})
        self.assertEqual(cyan["color"], "青色")
        self.assertEqual(custom["color"], "自定义颜色")

    def test_invalid_rgb_is_rejected(self):
        with self.assertRaises(ValueError):
            gateway.summarize_light_state({"rgb": {"r": 0, "g": 0}})
        with self.assertRaises(ValueError):
            gateway.summarize_light_state({"rgb": {"r": True, "g": 0, "b": 0}})


class ManualLightControlTests(unittest.TestCase):
    def test_manual_settings_and_brightness_scaling(self):
        rgb, brightness = gateway.parse_manual_light_settings(
            {"rgb": {"r": 255, "g": 96, "b": 0}, "brightness": 30}
        )
        self.assertEqual(rgb, (255, 96, 0))
        self.assertEqual(brightness, 30)
        self.assertEqual(gateway.scale_rgb_brightness(rgb, brightness), (77, 29, 0))

    def test_manual_settings_reject_invalid_rgb_or_brightness(self):
        invalid = (
            {"rgb": {"r": 256, "g": 0, "b": 0}, "brightness": 50},
            {"rgb": {"r": 0, "g": 0, "b": 0}, "brightness": -1},
            {"rgb": {"r": 0, "g": 0, "b": 0}, "brightness": 101},
            {"rgb": {"r": False, "g": 0, "b": 0}, "brightness": 50},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                gateway.parse_manual_light_settings(payload)


class HumidifierControlTests(unittest.TestCase):
    def test_explicit_on_off_phrases(self):
        self.assertIsNone(gateway.explicit_light_settings_from_text("打开雾化器"))
        self.assertIs(gateway.explicit_humidifier_state_from_text("打开雾化器"), True)
        self.assertIs(gateway.explicit_humidifier_state_from_text("开启加湿器"), True)
        self.assertIs(gateway.explicit_humidifier_state_from_text("停止雾化"), False)
        self.assertIs(gateway.explicit_humidifier_state_from_text("关闭雾化器"), False)

    def test_questions_and_negations_do_not_trigger(self):
        self.assertIsNone(gateway.explicit_humidifier_state_from_text("雾化器怎么使用"))
        self.assertIsNone(gateway.explicit_humidifier_state_from_text("雾化器开关是什么"))
        self.assertTrue(gateway.is_negated_humidifier_request("不要打开雾化器"))
        self.assertIsNone(gateway.explicit_humidifier_state_from_text("不要打开雾化器"))

    def test_manual_switch_requires_boolean(self):
        self.assertTrue(gateway.parse_humidifier_request({"on": True}))
        self.assertFalse(gateway.parse_humidifier_request({"on": False}))
        for payload in ({}, {"on": "true"}, {"on": 1}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                gateway.parse_humidifier_request(payload)

    def test_environment_thresholds_are_ordered_and_bounded(self):
        self.assertEqual(
            gateway.parse_environment_config({"auto_mode": True, "on_below_pct": 45, "off_at_pct": 50}),
            {"auto_mode": True, "on_below_pct": 45, "off_at_pct": 50},
        )
        invalid = (
            {"auto_mode": True, "on_below_pct": 52, "off_at_pct": 50},
            {"auto_mode": False, "on_below_pct": 45, "off_at_pct": 47},
            {"auto_mode": True, "on_below_pct": 50, "off_at_pct": 68},
            {"auto_mode": "yes", "on_below_pct": 45, "off_at_pct": 50},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                gateway.parse_environment_config(payload)


class CurtainControlTests(unittest.TestCase):
    def test_curtain_position_questions_use_device_state(self):
        self.assertTrue(gateway.explicit_curtain_query_from_text("窗帘现在开到多少了"))
        self.assertTrue(gateway.explicit_curtain_query_from_text("窗帘大概开几成"))
        self.assertFalse(gateway.explicit_curtain_query_from_text("打开窗帘"))

    def test_curtain_commands_are_not_parsed_as_light_brightness(self):
        self.assertIsNone(gateway.explicit_light_settings_from_text("把窗帘打开到30%"))

    def test_manual_curtain_operations(self):
        self.assertEqual(
            gateway.parse_curtain_request(
                {"operation": "set_position", "position_pct": 45, "interval_ms": 20}
            ),
            {"operation": "set_position", "position_pct": 45, "interval_ms": 20},
        )
        self.assertEqual(
            gateway.parse_curtain_request({"operation": "set_closed", "open_direction": "reverse"}),
            {"operation": "set_closed", "open_direction": "reverse"},
        )
        self.assertEqual(
            gateway.parse_curtain_request({"operation": "jog", "direction": "open", "steps": 100, "interval_ms": 30}),
            {"operation": "jog", "direction": "open", "steps": 100, "interval_ms": 30},
        )
        self.assertEqual(gateway.parse_curtain_request({"operation": "stop"}), {"operation": "stop"})

        invalid = (
            {"operation": "set_position", "position_pct": -1, "interval_ms": 20},
            {"operation": "set_position", "position_pct": 101, "interval_ms": 20},
            {"operation": "set_position", "position_pct": True, "interval_ms": 20},
            {"operation": "set_position", "position_pct": 50, "interval_ms": 4},
            {"operation": "jog", "direction": "sideways", "steps": 10, "interval_ms": 20},
            {"operation": "jog", "direction": "close", "steps": 10001, "interval_ms": 20},
            {"operation": "set_closed", "open_direction": "sideways"},
            {"operation": "unknown"},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                gateway.parse_curtain_request(payload)

    def test_negated_curtain_request_is_not_an_action(self):
        self.assertTrue(gateway.is_negated_curtain_request("不要打开窗帘"))
        self.assertFalse(gateway.is_negated_curtain_request("把窗帘打开到一半"))


@unittest.skipIf(gateway.Image is None, "Pillow is not installed")
class OledRenderingTests(unittest.TestCase):
    """测试 OLED 渲染，未安装 Pillow 时整类跳过。"""

    def test_user_message_precedes_assistant_reply(self):
        """消息入队顺序必须是先用户后助手，保证屏幕上先显示提问。"""
        messages = queue.Queue(maxsize=4)
        gateway.queue_oled_message(messages, "http://192.168.1.50", "YOU", "user text")
        gateway.queue_oled_message(messages, "http://192.168.1.50", "AI", "assistant reply")

        self.assertEqual(messages.get_nowait()[1:], ("YOU", "user text"))
        self.assertEqual(messages.get_nowait()[1:], ("AI", "assistant reply"))

    def test_frame_uses_ssd1306_page_byte_order(self):
        """验证像素到 SSD1306 页面格式字节的映射顺序（低 bit 对应上方像素）。"""
        image = gateway.Image.new("1", (128, 64), 0)
        image.putpixel((3, 2), 1)
        image.putpixel((4, 15), 1)

        frame = gateway._image_to_ssd1306_frame(image)

        self.assertEqual(len(frame), 1024)
        self.assertEqual(frame[3], 1 << 2)        # 第 0 页，x=3，y=2
        self.assertEqual(frame[128 + 4], 1 << 7)  # 第 1 页，x=4，y=15

    def test_long_text_is_paginated_once(self):
        """长文本应被切成多屏，且每屏都是 1024 字节、至少有一屏有内容。"""
        frames = gateway.render_display_frames("中文内容" * 12, "YOU")

        self.assertGreater(len(frames), 1)
        self.assertTrue(all(len(frame) == 1024 for frame in frames))
        self.assertTrue(any(any(frame) for frame in frames))


class DeviceUrlTests(unittest.TestCase):
    """测试设备地址校验：只允许局域网地址。"""

    def test_private_device_url(self):
        """内网地址应被接受并去掉结尾斜杠。"""
        self.assertEqual(
            gateway.validate_device_url("http://192.168.1.50/"),
            "http://192.168.1.50",
        )

    def test_public_device_url_is_rejected(self):
        """公网地址应被拒绝，避免把控制接口暴露出去。"""
        with self.assertRaises(ValueError):
            gateway.validate_device_url("http://8.8.8.8")


class HttpsTests(unittest.TestCase):
    def test_https_is_optional(self):
        self.assertEqual(gateway.configure_https(object()), "http")

    def test_https_requires_both_certificate_and_key(self):
        with self.assertRaisesRegex(ValueError, "同时提供"):
            gateway.configure_https(object(), certfile="gateway.pem")

        with self.assertRaisesRegex(ValueError, "同时提供"):
            gateway.configure_https(object(), keyfile="gateway-key.pem")


if __name__ == "__main__":
    unittest.main()
