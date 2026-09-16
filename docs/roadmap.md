# 路线图

## v0.1（当前）

完成中文桌面界面、预设与本地 JSON、日志、统计，以及 delay/jitter/loss/blackout/rate limit/duplicate/reorder 和 filter 匹配。支持 WinDivert 2.2 x64，开始时提权。

## v0.2

增加 TCP RST 场景（需明确风险提示和回滚验证）、更细粒度的时间分布、实验报告导出和配置版本迁移。

## v0.3

评估按进程过滤、规则集合、远程测试代理和移动端/网关协同；这些能力需要额外驱动或代理设计，不能通过首版 UI 直接推导。

## 质量与运维

建立管理员与非管理员矩阵、IPv4/IPv6、TCP/UDP、localhost、长时间运行和异常断电测试；后续再评估签名驱动、集中审计和企业策略集成。
