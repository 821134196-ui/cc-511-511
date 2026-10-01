"""证件遮罩与模拟门禁适配器测试。"""
from app.gate_adapter import SimulatedGateAdapter
from app.security import (generate_badge_image, generate_pass_code, hash_code,
                          mask_id, mask_name, mask_phone)
from tests.helpers import WardTestBase
import os


class TestMasking(WardTestBase):
    def test_mask_id(self):
        self.assertEqual(mask_id("110105199203172246"), "110105********2246")
        self.assertEqual(mask_id("A1234"), "A***4")
        self.assertEqual(mask_id("AB"), "A*")
        self.assertEqual(mask_id("护照12345678", "护照"), "护********8")

    def test_mask_phone(self):
        self.assertEqual(mask_phone("13812345678"), "138****5678")

    def test_mask_name(self):
        self.assertEqual(mask_name("王"), "王")
        self.assertEqual(mask_name("李华"), "李*")
        self.assertEqual(mask_name("周晓燕"), "周*燕")

    def test_pass_code_only_hash_stored(self):
        plain, h, masked = generate_pass_code()
        self.assertTrue(plain.startswith("WXK-"))
        self.assertEqual(hash_code(plain), h)
        # 仅保留末段供陪护人核对，前两段必须遮挡
        self.assertIn("••••", masked)
        middle = plain.split("-")[2]
        self.assertNotIn(middle, masked)
        self.assertNotEqual(plain, masked)

    def test_badge_svg_written_and_masked(self):
        out = os.path.join(self.tmp.name, "badges")
        path = generate_badge_image("A-0217", "周晓燕", "女儿", "身份证",
                                    "110105199203172246", "呼吸内科", out_dir=out)
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            svg = f.read()
        self.assertIn("110105********2246", svg)
        self.assertNotIn("110105199203172246", svg)
        self.assertIn("虚构", svg)


class TestGateAdapter(WardTestBase):
    def test_signals_and_no_approve_capability(self):
        ad = SimulatedGateAdapter()
        ok = {"allowed": True, "message": "核验通过，允许入区"}
        sig = ad.present("HN-G1", ok)
        self.assertEqual(sig.lamp, "GREEN")
        self.assertEqual(sig.barrier, "OPEN")
        bad = {"allowed": False, "reason": "通行码已撤销：手机丢失"}
        sig2 = ad.present("HN-G1", bad)
        self.assertEqual(sig2.lamp, "RED")
        self.assertEqual(sig2.barrier, "CLOSED")
        self.assertIn("手机丢失", sig2.message)
        self.assertIn("无权审批", ad.cannot_approve())
