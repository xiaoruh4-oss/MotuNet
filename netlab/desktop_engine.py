"""Dispatch packet impairment and physical adapter outages without mixing them."""
from .engine import Engine


class DesktopEngine:
    def __init__(self, dll_dir, *, packet=None, adapter=None):
        self.packet = packet or Engine(dll_dir)
        self.adapter = adapter
        self.active = self.packet
        if packet is None and adapter is None:
            from .adapter_engine import AdapterEngine
            self.adapter = AdapterEngine()
            if self.adapter.adopt_pending():
                self.active = self.adapter

    def validate_filter(self, expression):
        return self.packet.validate_filter(expression)

    def start(self, config):
        if self.active.snapshot().get("running"):
            raise RuntimeError("请先停止当前测试并完成恢复")
        if config.get("blackout") and config.get("blackout_mode") == "adapter":
            if self.adapter is None:
                from .adapter_engine import AdapterEngine
                self.adapter = AdapterEngine()
            self.active = self.adapter
        else:
            self.active = self.packet
        self.active.start(config)

    def stop(self):
        return self.active.stop()

    def snapshot(self):
        return self.active.snapshot()
