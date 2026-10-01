"""门禁模拟适配器：用软件模拟闸机硬件的扫码-核验-放行流程。

真实部署时替换 scan() 内部实现为硬件 SDK 调用即可，上层（Web API、
演示脚本、测试）不需要改动。适配器自己持有独立数据库连接，因此可以
安全地在线程中并发使用（用于验证"同一码两个入口同时扫"的拦截）。
"""
from . import db, services


class SimulatedGateAdapter:
    """模拟一台闸机：固定闸机名与方向（入区闸机/离区闸机/双向）。"""

    def __init__(self, gate_name, direction="auto", db_path=None):
        self.gate_name = gate_name
        self.direction = direction  # in / out / auto
        self.db_path = db_path

    def _conn(self):
        return db.connect(self.db_path)

    def scan(self, code, now=None):
        """模拟一次扫码：读取二维码内容 -> 调用核验服务 -> 返回闸机动作。

        返回 dict：allowed 是否开闸、direction 实际方向、deny_reason 拒绝原因、
        以及门岗大屏要展示的字段（代号/病区/遮罩证件等）。
        """
        conn = self._conn()
        try:
            result = services.verify_scan(
                conn, self.gate_name, code, direction=self.direction, now=now)
            result["open_gate"] = bool(result.get("allowed"))
            return result
        finally:
            conn.close()


# 预置两台入口闸机 + 一台出口闸机，演示"同一码两个入口同时扫"
def default_gates(db_path=None):
    return {
        "东门（入区）": SimulatedGateAdapter("东门", "in", db_path),
        "西门（入区）": SimulatedGateAdapter("西门", "in", db_path),
        "南门（离区）": SimulatedGateAdapter("南门", "out", db_path),
        "大厅（双向）": SimulatedGateAdapter("大厅", "auto", db_path),
    }
