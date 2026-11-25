# Motu Net 弱网测试模板

> 历史设计说明：Windows 0.7.6 暂时隐藏“弱网用例”入口。本文保留此前模板流程与 JSON 格式，供维护和兼容已有数据参考，不代表当前发行版可见操作。日常测试请使用“场景测试”。

弱网模板描述“在什么网络条件下，对系统做什么操作，以及什么现象算通过”。它不保存账号、业务数据或包内容，也不绑定某个项目的 IP 和端口。目标地址、端口、协议和方向在 Motu Net 的“详细设置”中配置，测试对象名称在“弱网用例”页填写。

## 使用流程

1. 在“详细设置”中保存一个系统目标场景，填写目标 IP/端口、协议和方向，并先用“正常网络对照”确认链路。
2. 在“弱网用例”页选择“通用系统弱网回归（15项）”，填写系统或功能名称。
3. 点击“开始测试”，按当前测试项的操作执行；工具会自动启动网络条件、记录匹配流量，判定结束后恢复网络再进入下一项。
4. 在每项中填写实际现象并选择“通过”或“不通过”。重连界面、提示文案、数据一致性、幂等和恢复后可用性必须由测试人员确认。
5. 需要项目专属步骤时，先导出当前模板，修改 `steps`、`expected` 和必要的 `group`，再用“导入模板”保存为团队模板。

## JSON 结构

```json
{
  "schema_version": 1,
  "template": {
    "id": "login-system-v1",
    "name": "登录系统弱网回归",
    "description": "登录、验证码和重连检查",
    "source": "QA 用例库",
    "cases": [
      {
        "id": "LOGIN-01",
        "group": "连接保持",
        "title": "登录中途短时断网",
        "estimated_minutes": 5,
        "network": {
          "blackout": true,
          "start_delay_s": 3,
          "blackout_duration_s": 5
        },
        "steps": "输入账号并点击登录；等待断网后观察提示；网络恢复后再次查询登录状态。",
        "expected": "出现明确的断网/重连状态；登录结果只生效一次；恢复后可继续使用。",
        "human_verdict": true,
        "enabled": true
      }
    ]
  }
}
```

`network` 只允许使用 `delay_ms`、`jitter_ms`、`loss_pct`、`bandwidth_kbps`、`duplicate_pct`、`reorder_pct`、`reorder_ms`、`duration_s`、`blackout`、`start_delay_s`、`blackout_duration_s` 和 `blackout_loop`。未填写的网络字段会回到正常值，再叠加当前目标的 IP、端口、协议、方向和高级过滤条件。

`estimated_minutes` 是单条用例的执行预算，包含操作、观察、恢复确认和结果记录，取值为 1 到 30 的整数。标准模板 9 项预计 50 分钟，通用系统模板 15 项预计 60 分钟。计划页按所有已选用例累加，超过 60 分钟时不能开始，需减少业务场景、降低级别或改用自定义条件。该预算用于安排人工测试，不会替代测试人员对业务结果的判定。

`human_verdict` 目前保留为模板元数据，Motu Net 不会用包计数代替业务判定。`enabled: false` 可暂时跳过一条测试项；模板不能全部禁用。模板保存在 `%LOCALAPPDATA%\\MotuNet\\test-templates`，升级安装不会覆盖。
