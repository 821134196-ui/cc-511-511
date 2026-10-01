"""生成陪护身份证明的遮罩样图（SVG），保存在本地 data/id_images/。

样图只含遮罩后的姓名与证件号，完整号码不落图片文件。
"""
import os

from .db import ID_IMAGE_DIR

_TEMPLATE = """<svg xmlns="http://www.w3.org/2000/svg" width="420" height="260" viewBox="0 0 420 260">
  <rect width="420" height="260" rx="14" fill="#ffffff" stroke="#cfd8da" stroke-width="2"/>
  <rect width="420" height="52" rx="14" fill="#0e8f8f"/>
  <rect y="38" width="420" height="14" fill="#0e8f8f"/>
  <text x="24" y="35" font-size="20" fill="#ffffff" font-family="sans-serif" font-weight="bold">陪护身份证明 · 虚构样图</text>
  <text x="330" y="35" font-size="13" fill="#d7efef" font-family="sans-serif">仅供系统演示</text>
  <text x="24" y="92" font-size="14" fill="#8a979c" font-family="sans-serif">陪护代号</text>
  <text x="24" y="118" font-size="24" fill="#1f2a2e" font-family="sans-serif" font-weight="bold">{code_name}</text>
  <text x="24" y="152" font-size="14" fill="#8a979c" font-family="sans-serif">姓名（遮罩）</text>
  <text x="24" y="176" font-size="20" fill="#1f2a2e" font-family="sans-serif">{name_masked}</text>
  <text x="220" y="152" font-size="14" fill="#8a979c" font-family="sans-serif">证件号码（遮罩）</text>
  <text x="220" y="176" font-size="16" fill="#1f2a2e" font-family="monospace">{id_masked}</text>
  <text x="24" y="212" font-size="14" fill="#8a979c" font-family="sans-serif">与患者关系</text>
  <text x="24" y="234" font-size="18" fill="#1f2a2e" font-family="sans-serif">{relationship}</text>
  <rect x="300" y="196" width="96" height="44" rx="6" fill="#eef6f6" stroke="#0e8f8f"/>
  <text x="312" y="224" font-size="15" fill="#0b6e6e" font-family="sans-serif">已核验备案</text>
</svg>
"""


def write_id_card(code_name, name_masked, id_masked, relationship, directory=None):
    """写入一张遮罩样图，返回相对项目根目录的路径。"""
    d = directory or ID_IMAGE_DIR
    os.makedirs(d, exist_ok=True)
    filename = f"{code_name}.svg"
    path = os.path.join(d, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_TEMPLATE.format(code_name=code_name, name_masked=name_masked,
                                 id_masked=id_masked, relationship=relationship))
    return os.path.relpath(path, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
