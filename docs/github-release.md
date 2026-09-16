# GitHub 发布与在线更新

公开仓库：https://github.com/xiaoruh4-oss/MotuNet

最新安装包：https://github.com/xiaoruh4-oss/MotuNet/releases/latest

## 同事安装和更新

从仓库 Releases 页面下载 `MotuNet-版本号-Setup-x64.exe` 并安装。公开安装包不需要 GitHub 账号。0.5.0 起，程序启动后后台检查一次更新，左下角也可点击“检查更新”。

发现新版后，用户可查看说明并选择下载，或选择稍后。确认下载时先停止弱网测试、恢复网络；文件下载完成后校验大小和 SHA256，再启动安装向导。安装器沿用应用标识和原安装目录，场景及皮肤数据保留。Windows 可能要求管理员确认。

自动检查失败只写入本地运行记录，不影响正常使用；手动检查失败会提示重试。当前测试运行期间，自动发现新版会推迟弹窗。取消下载或拒绝安装授权均可继续使用当前版本。

## 发布新版

1. 在 `netlab/__init__.py` 更新唯一版本号，并在 `CHANGELOG.md` 顶部编写对应版本说明。
2. 提交并推送代码，再推送同名标签，例如版本号 `0.5.1` 对应 `v0.5.1`。
3. GitHub Actions 在 Windows 执行自动测试、构建和程序启动检查。
4. 全部通过后创建 Release 草稿，上传安装包、SHA256 校验文件和 `latest.json`，最后将完整的 Release 发布。

失败的构建不会发布不完整的版本。GitHub Actions 使用仓库自带的 `GITHUB_TOKEN`，无需在客户端或仓库源码中保存个人令牌。

开发者本地也可以执行 `tools/build_release.ps1`，需要 Python 3.12、requirements.txt 中依赖，以及 Inno Setup 6。安装包和更新清单输出到 `deliverables`；构建目录、本机日志、环境和安装包不提交到 Git。

## 更新来源

`netlab/update_source.py` 保存固定的公开仓库标识。客户端读取该仓库 Releases 最新稳定版本中的 `latest.json`，只接受同一仓库、同一版本标签下的 Windows x64 安装包地址。下载只允许 HTTPS GitHub 发布服务及其资源域名，校验完成前不会运行文件。

版本按数字比较，不会把 `0.9.0` 误判为比 `0.10.0` 更新；相同或更旧版本不会提示升级。清单包含 `schema_version`、`platform`、`version`、`url`、`sha256`、`size`、`notes`。

软件只请求更新清单和安装包，不上传测试流量、场景配置或日志。更新依赖本地网络能访问 GitHub；不具备联网更新功能的 0.4.x 需要手动升级一次。
