import importlib.util
import queue
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "gateway.py"
SPEC = importlib.util.spec_from_file_location("gateway", MODULE_PATH)
gateway = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gateway)


class ModelDecisionTests(unittest.TestCase):
    def test_set_light_decision(self):
        decision = gateway.parse_model_decision(
            '{"intent":"set_light","colors":["red","green"],"brightness":30,"reply":"好的。"}'
        )
        self.assertEqual(decision["intent"], "set_light")
        self.assertEqual(decision["colors"], ["red", "green"])
        self.assertEqual(decision["brightness"], 30)

    def test_non_action_fields_are_ignored(self):
        decision = gateway.parse_model_decision(
            '{"intent":"chat","colors":["white"],"brightness":45,"reply":"你好。"}'
        )
        self.assertEqual(decision["intent"], "chat")
        self.assertEqual(decision["colors"], [])
        self.assertEqual(decision["brightness"], 100)

    def test_invalid_light_settings_are_rejected(self):
        invalid = (
            '{"intent":"set_light","colors":["purple"],"brightness":30,"reply":"好的。"}',
            '{"intent":"set_light","colors":["red"],"brightness":101,"reply":"好的。"}',
            '{"intent":"set_light","colors":["off","red"],"brightness":50,"reply":"好的。"}',
        )
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(ValueError):
                gateway.parse_model_decision(content)


class LightCommandTests(unittest.TestCase):
    def test_explicit_color_and_mixed_color_commands(self):
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
        self.assertEqual(
            gateway.explicit_light_settings_from_text("把灯亮度调到30%"),
            {"colors": [], "brightness": 30},
        )

    def test_off_negation_and_questions(self):
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
        with self.assertRaises(ValueError):
            gateway.explicit_light_settings_from_text("红灯亮度调到120%")

    def test_rgb_mixing_and_brightness_scaling(self):
        self.assertEqual(
            gateway.compose_light_rgb(["red", "green"], 100),
            ((255, 255, 0), (255, 255, 0)),
        )
        self.assertEqual(
            gateway.compose_light_rgb(["red", "green"], 30),
            ((77, 77, 0), (255, 255, 0)),
        )

    def test_brightness_only_preserves_current_or_remembered_color(self):
        self.assertEqual(
            gateway.compose_light_rgb([], 50, current_rgb=(80, 0, 0)),
            ((128, 0, 0), (255, 0, 0)),
        )
        self.assertEqual(
            gateway.compose_light_rgb([], 40, current_rgb=(0, 0, 0), remembered_rgb=(0, 255, 0)),
            ((0, 102, 0), (0, 255, 0)),
        )


@unittest.skipIf(gateway.Image is None, "Pillow is not installed")
class OledRenderingTests(unittest.TestCase):
    def test_user_message_precedes_assistant_reply(self):
        messages = queue.Queue(maxsize=4)
        gateway.queue_oled_message(messages, "http://192.168.1.50", "YOU", "user text")
        gateway.queue_oled_message(messages, "http://192.168.1.50", "AI", "assistant reply")

        self.assertEqual(messages.get_nowait()[1:], ("YOU", "user text"))
        self.assertEqual(messages.get_nowait()[1:], ("AI", "assistant reply"))

    def test_frame_uses_ssd1306_page_byte_order(self):
        image = gateway.Image.new("1", (128, 64), 0)
        image.putpixel((3, 2), 1)
        image.putpixel((4, 15), 1)

        frame = gateway._image_to_ssd1306_frame(image)

        self.assertEqual(len(frame), 1024)
        self.assertEqual(frame[3], 1 << 2)
        self.assertEqual(frame[128 + 4], 1 << 7)

    def test_long_text_is_paginated_once(self):
        frames = gateway.render_display_frames("中文内容" * 12, "YOU")

        self.assertGreater(len(frames), 1)
        self.assertTrue(all(len(frame) == 1024 for frame in frames))
        self.assertTrue(any(any(frame) for frame in frames))


class DeviceUrlTests(unittest.TestCase):
    def test_private_device_url(self):
        self.assertEqual(
            gateway.validate_device_url("http://192.168.1.50/"),
            "http://192.168.1.50",
        )

    def test_public_device_url_is_rejected(self):
        with self.assertRaises(ValueError):
            gateway.validate_device_url("http://8.8.8.8")


if __name__ == "__main__":
    unittest.main()
