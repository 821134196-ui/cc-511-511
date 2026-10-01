"""模拟门禁适配器。

真实部署中适配器只负责读码、亮灯、驱动闸机；**没有审批人员的能力**。
本模拟实现把每次核验结果记录为信号灯/闸机动作，供演示与测试断言。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class GateSignal:
    gate_code: str
    lamp: str            # GREEN / RED
    barrier: str         # OPEN / CLOSED
    message: str
    result: dict[str, Any]


class SimulatedGateAdapter:
    """内存信号日志 + 可替换的核验回调（由 core 注入）。"""

    def __init__(self) -> None:
        self.signals: list[GateSignal] = []

    def present(self, gate_code: str, result: dict[str, Any]) -> GateSignal:
        allowed = bool(result.get("allowed"))
        sig = GateSignal(
            gate_code=gate_code,
            lamp="GREEN" if allowed else "RED",
            barrier="OPEN" if allowed else "CLOSED",
            message=("准入：" + result["message"]) if allowed else ("拒绝：" + result["reason"]),
            result=result,
        )
        self.signals.append(sig)
        return sig

    def cannot_approve(self) -> str:
        """门岗设备不提供任何审批接口——如被调用直接拒绝。"""
        return "门岗仅有核验权限，无权审批或发放通行码。"

    def last_signal(self) -> GateSignal | None:
        return self.signals[-1] if self.signals else None
