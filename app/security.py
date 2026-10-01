"""证件遮罩、通行码生成与虚构证件样图。

- 界面展示一律走 mask_*；完整号码只存 SQLite，不落日志/不回显明文码。
- generate_badge_image 输出本地 SVG 样图（虚构数据，仅演示遮罩效果）。
"""
from __future__ import annotations

import hashlib
import os
import secrets
import string

BADGE_DIR = os.environ.get(
    "WARD_BADGE_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "badges")
)


def mask_id(id_number: str, id_type: str = "身份证") -> str:
    """身份证 110101********1234 风格；短证件保留首末各 1 位。"""
    s = (id_number or "").strip()
    if id_type == "身份证" and len(s) >= 10:
        return s[:6] + "*" * (len(s) - 10) + s[-4:]
    if len(s) <= 1:
        return "*" * len(s)
    if len(s) == 2:
        return s[0] + "*"
    return s[0] + "*" * (len(s) - 2) + s[-1]


def mask_phone(phone: str) -> str:
    s = (phone or "").strip()
    if len(s) >= 7:
        return s[:3] + "****" + s[-4:]
    return "*" * len(s)


def mask_name(name: str) -> str:
    """大字页默认遮挡姓名：王*华 / 李*。"""
    if not name:
        return ""
    if len(name) == 1:
        return name
    if len(name) == 2:
        return name[0] + "*"
    return name[0] + "*" * (len(name) - 2) + name[-1]


def hash_code(code: str) -> str:
    return hashlib.sha256(("ward-pass:" + code).encode("utf-8")).hexdigest()


_CODE_ALPHABET = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"


def generate_pass_code() -> tuple[str, str, str]:
    """返回 (明文码[仅此刻返回给发码人], 哈希, 遮罩串)。"""
    parts = ["".join(secrets.choice(_CODE_ALPHABET) for _ in range(4)) for _ in range(3)]
    code = "WXK-" + "-".join(parts)            # WXK = 温溪康（虚构医院前缀）
    mask = "WXK-••••-" + parts[2]
    return code, hash_code(code), mask


def generate_badge_image(patient_alias: str, companion: str, relationship: str,
                         id_type: str, id_number: str, ward_name: str,
                         out_dir: str | None = None) -> str:
    """生成虚构证件遮罩样图 SVG，返回文件路径。"""
    out_dir = out_dir or BADGE_DIR
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"badge_{patient_alias}.svg")
    mid = mask_id(id_number, id_type)
    color = "#0E7C86"  # 医院青
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="520" height="320" viewBox="0 0 520 320" font-family="sans-serif">
  <rect width="520" height="320" fill="#F4F6F7" stroke="#C9D1D3" stroke-width="2" rx="10"/>
  <rect width="520" height="56" fill="{color}" rx="10"/>
  <rect y="36" width="520" height="20" fill="{color}"/>
  <text x="24" y="36" fill="#ffffff" font-size="20" font-weight="bold">温溪康和医院 · 陪护身份样例（虚构）</text>
  <circle cx="72" cy="132" r="38" fill="#DDEBED" stroke="{color}" stroke-width="2"/>
  <text x="72" y="140" fill="{color}" font-size="26" text-anchor="middle">照</text>
  <text x="136" y="108" fill="#1F2D2E" font-size="18">患者代号：<tspan font-weight="bold">{patient_alias}</tspan></text>
  <text x="136" y="138" fill="#1F2D2E" font-size="18">陪护人：{companion}（{relationship}）</text>
  <text x="136" y="168" fill="#1F2D2E" font-size="18">{id_type}：</text>
  <text x="226" y="168" fill="#B03A2E" font-size="18" font-weight="bold" letter-spacing="2">{mid}</text>
  <text x="24" y="226" fill="#5D6B6D" font-size="15">证件信息已遮罩，仅护士长审批时可在授权终端查看完整号码。</text>
  <text x="24" y="256" fill="#5D6B6D" font-size="15">病区：{ward_name} · 本样图为系统生成的虚构演示图，不构成真实证件。</text>
  <rect x="372" y="232" width="120" height="60" fill="#ffffff" stroke="#C9D1D3" rx="6"/>
  <text x="432" y="268" fill="#5D6B6D" font-size="13" text-anchor="middle">SAMPLE</text>
</svg>
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
    return path
