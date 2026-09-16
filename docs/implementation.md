# 实现说明

## 架构

`main.py` 负责 Qt 应用入口；`netlab/config.py` 定义场景默认值、预设、字段校验和 WinDivert filter 编译；`netlab/engine.py` 通过 `ctypes.WinDLL` 调用 WinDivert 2.x 的 Open/Recv/Send/Close API，在接收线程与发送线程之间使用带时间戳的优先队列调度数据包。GUI 通过快照读取统计，避免直接操作传输线程。

## 数据与调度

配置在启动前被规范化并生成 filter。引擎接收匹配数据包后依次执行 blackout/丢包、延迟与抖动、乱序偏移、限速排队、重复，再按到期时间发送并更新统计。WinDivert 同一句柄发送的数据包不会再次被本句柄捕获；filter 额外排除其他来源的 `impostor` 数据包，避免干扰。

## 驱动与权限

发行包将 `vendor/windivert/WinDivert.dll` 和 `WinDivert64.sys` 放入同级目录。WinDivert 句柄仅在用户点击开始且配置通过校验后打开；系统 UAC 负责提权。程序清单保持 `asInvoker`，不在启动阶段自动提升或修改网络。

## 错误与恢复

驱动打开、接收、发送异常会记录错误、停止线程、关闭句柄并清空队列。定时器到期走同一停止路径。日志仅写入本机文件，供调试和复盘。

## 构建

`MotuNet.spec` 使用 PyInstaller onedir + windowed，入口 `main.py`，名称 `MotuNet.exe`，并收集图标、场景、`vendor`、`docs`、README 和第三方声明。`tools/build_release.ps1` 生成 `staging/MotuNet` 后调用 Inno Setup 构建安装包，输出到 `deliverables`。

## 延迟断网

`start_delay_s` 为点击开始后的等待秒数；等待阶段不打开网络拦截句柄。等待结束后打开句柄并开始断网，`blackout_duration_s` 为实际断网秒数，到期自动停止。等待或断网中均可取消；普通弱网测试忽略这两个字段。导入未包含新字段的旧断网配置时沿用原 `duration_s`，包括 0 秒手动停止。

`blackout_loop` 默认为 `false`，为 `true` 时要求启用 `blackout`，且等待和断网时长均至少 1 秒。每轮断网结束后关闭拦截句柄、终止该轮收包线程，再进入下一轮正常联网等待，直到停止或出现错误。快照 `cycle` 表示当前轮次（从 1 开始），`elapsed` 为整次运行时长；阶段倒计时每轮重置。循环标记参与运行参数比较，保存和导入沿用现有配置结构，旧场景自动补充默认值。
