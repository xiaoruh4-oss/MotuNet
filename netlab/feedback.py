"""Describe actual packet evidence separately from configured impairment."""


def traffic_feedback(config, stats):
    received = int(stats.get("received", 0))
    if not received:
        return "尚未捕获目标流量；请操作被测程序，检查 IP / 端口、协议和方向。"
    sent = int(stats.get("sent", 0))
    if config.get("blackout"):
        return f"捕获 {received:,} 包 · 断网丢弃 {int(stats.get('blackout_dropped', 0)):,} 包"
    loss = float(config.get("loss_pct", 0))
    actual = int(stats.get("loss_dropped", 0))
    if loss:
        ratio = 100 * actual / received
        result = f"设定丢包 {loss:g}% · 捕获 {received:,} 包 · 随机丢弃 {actual:,} 包（{ratio:.1f}%）"
        if not actual:
            result += "；暂未发生随机丢包"
    else:
        result = f"捕获 {received:,} 包 · 转发 {sent:,} 包 · 随机丢包未开启"
    overflow = int(stats.get("overflow_dropped", 0))
    if overflow:
        result += f" · 队列满额外丢弃 {overflow:,} 包"
    return result
