# OpenWrt dist

为 amd64（`x86/64`）及 Widora MangoPi M28C（`rockchip/armv8`，RK3528）编译自定义软件源。默认使用 [ImmortalWrt 最新稳定版](https://downloads.immortalwrt.org/) 的官方 SDK，自动识别架构、SDK 文件名及 opkg/apk 格式，并验证下载文件的 SHA256。

## 运行方式

在 `Settings → Secrets and variables → Actions` 中配置现有的 Secret `TOKEN`（本仓库 Contents 读写权限）和 Variable `OPENWRT_VERSION`。

版本支持 `latest`（默认）、完整版本号（如 `25.12.2`）、版本系列（`25.12` / `openwrt-25.12`）和 `SNAPSHOT`。Actions → **Packages** 可手动覆盖版本；push 到 master、每月 1 日也会运行。

本工作流还支持 `workflow_call`，由 `fangguangyang/OpenWrt` 固件流水线调用。它接收已经解析好的 SDK/ImageBuilder 元数据，输出 `{设备: 软件源commit}`，保证软件和镜像版本一致。**先推送本仓库的工作流，再启用固件仓库的调用。** 可复用调用中 Variables 来自调用方仓库；独立运行时来自本仓库。

## 并发编译

每个架构的 SDK 和源码 feeds 只准备一次，作为 artifact 分发给 6 个独立编译任务：

| 软件组 | 主要软件 |
| --- | --- |
| `openclash` | OpenClash |
| `passwall` | PassWall、Xray、V2Ray Plugin、Simple Obfs、Shadowsocks Rust |
| `mosdns` | MosDNS 核心及 LuCI |
| `openlist` | OpenList2 核心及 LuCI |
| `storage` | Dockerman、Diskman |
| `platform` | AdGuardHome、Argon、Modemband、SMS Tool；M28C 额外添加 Design、iStore、QuickStart |

默认两种架构最多 **12 个任务并发**，每个任务 `make -j4`。可选 Variables：

- `BUILD_MAX_PARALLEL`：最大同时构建的任务数，默认 `12`。实际受 GitHub 账号并发配额限制。
- `BUILD_JOBS`：单任务 make 并行度，默认 `4`，应与 runner CPU/内存匹配。
- `OPENWRT_DISTRO`：默认 `immortalwrt`。
- `OPENWRT_DEVICES`：默认 `amd64,m28c`。官方 OpenWrt 没有 M28C profile 时必须选 `amd64`。

缓存包括 SDK 下载、源码下载、编译后的 host 工具链及 Go build/module 缓存。缓存按 SDK 校验和和软件组隔离，源码提交变化会产生新 key。首次无缓存仍需编译；总耗时取决于最慢的软件组和 runner 排队，发布前必须等所有组成功。

## 软件清单和依赖

- `package-groups.json` 定义任务划分，`packages.txt` 是全部基本软件清单；两者必须一致。
- `packages-m28c.txt` 是 M28C 平台附加软件。
- `feeds.conf.custom` 定义第三方来源，`feed-overrides.txt` 明确每个覆盖包的归属；其余依赖使用 SDK 配套 feeds。
- `single-packages.json` 定义 Makefile 位于仓库根目录的单包（Argon、Design），直接安装到 `package/custom`，避免 SDK 将根目录误识别为 feed。
- Go 配方来自当前官方 `openwrt/packages`，支持新版本 OpenList/代理核心；不把整个稳定 feeds 替换为 master。所有 feeds 和 Go 提交记录在 `sources.json`。
- MosDNS 核心、LuCI、geo2txt 来自同一个 sbwml feed，避免官方核心与另一来源 UI 的启动脚本冲突。AdGuardHome 使用 ImmortalWrt 配套 feed 的原生包。
- 选择上游 OpenWrt 构建 amd64 时，从对应版本系列的 ImmortalWrt LuCI feed 补充 AdGuardHome、Modemband、SMS Tool 界面；核心和编译工具链仍使用 OpenWrt SDK。
- Design 主题的日期版本后缀会规范为 apk 接受的格式；该兼容修改摘要也写入源码锁定信息。
- SDK 中解除 curl 选项的外层菜单依赖，修复冻结 libc 配置与 LDAP 选项形成的 Kconfig 循环；保留包依赖及原有默认功能。
- 最新 ImmortalWrt feeds 已移除独立 `luci-app-shadowsocks-libev`；旧 `luci-app-shadowsocks` / `openwrt-dist-luci` 的 Shadowsocks 功能统一改由 PassWall 管理，并构建持续维护的 Shadowsocks Rust 客户端/服务端。不再运行手工安装的 po2lmo、跳过 `PKG_HASH` 或复制一整份重名 small 源。

各组只选择自己的软件及依赖。缺包、编译错误、不同源提交、缺组或同名异内容的软件包都会让构建失败，不使用 `IGNORE_ERRORS=1`。收集所有 feed 输出后统一生成并签名索引，避免原流程仅上传 `base` 而丢失软件。

## 软件源产物

发布分支为：

```text
packages/immortalwrt/<版本>/x86/64
packages/immortalwrt/<版本>/rockchip/armv8
```

每个分支包含软件包、索引、公钥、`build.json`、`sources.json` 和各组的 SDK 配置。发布保留父提交，不 force push；历史固件引用的源提交仍可访问。失败的组不会发布一个不完整软件源。各架构发布成功后，工作流输出其不可变 Git commit。

每次发布生成独立的原生签名密钥，只发布公钥：apk 为 `feed-key.pem`，opkg 为 `feed-key.pub`。私钥不上传到分支或 artifact。固件固定源的 commit 和公钥，无需额外签名 Secret，也不会关闭签名检查。

以具体 commit 配置软件源（把 `<commit>` 替换为 Actions 输出的值）：

```text
# apk: /etc/apk/repositories.d/customfeeds.list
https://raw.githubusercontent.com/fangguangyang/openwrt-dist/<commit>/packages.adb

# opkg: /etc/opkg/customfeeds.conf
src/gz custom https://raw.githubusercontent.com/fangguangyang/openwrt-dist/<commit>
```

apk 将同提交的 `feed-key.pem` 放入 `/etc/apk/keys/`；opkg 使用 `opkg-key add` 导入同提交的 `feed-key.pub`。新版固件流水线自动完成这些配置。

自编译源是不可变构建快照。切换到新快照时必须一起更新源 commit 和公钥，并保持发行版、版本和 target 一致；不能直接使用会变化的分支地址配合旧公钥。原来未区分版本的 `packages/x86_64` 等旧分支不再更新。

固件构建已统一放在 `fangguangyang/OpenWrt`，本仓库不再重复构建另一套镜像。

## 检查

```bash
python3 -m unittest discover -s tests -v
python3 scripts/release.py resolve --version latest --output release.json
```

`scripts/release.py` 和对应测试与固件仓库保持一致。修改版本解析逻辑时应同步两份。
