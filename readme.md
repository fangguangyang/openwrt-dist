# OpenWrt dist

为 amd64（`x86/64`）及 Widora MangoPi M28C（`rockchip/armv8`，RK3528）生成自定义软件源。OpenClash、PassWall 两组使用与固件版本和架构匹配的官方预编译包，其余软件使用 [ImmortalWrt 最新稳定版](https://downloads.immortalwrt.org/) 的官方 SDK 编译。自动识别 SDK/ImageBuilder 文件名及 opkg/apk 格式，并验证下载归档的 SHA256。

## 运行方式

在 `Settings → Secrets and variables → Actions` 中配置 Secret `TOKEN`（本仓库 Contents 读写权限）、下文对应包管理器的固定签名 Secret，以及 Variable `OPENWRT_VERSION`。

版本支持 `latest`（默认）、完整版本号（如 `25.12.2`）、版本系列（`25.12` / `openwrt-25.12`）和 `SNAPSHOT`。Actions → **Packages** 可手动覆盖版本；push 到 master、每月 1 日也会运行。

软件源流水线独立运行。`fangguangyang/OpenWrt` 固件流水线直接读取本仓库已发布的同版本、同 target Release，校验匹配后用 ImageBuilder 打包，不再为每次固件构建重复执行本工作流。首次发布新版本的软件源时先运行本仓库，完成后再运行固件流水线。

本工作流仍保留 `workflow_call`，供需要显式构建软件源的其他调用方使用：接收 SDK/ImageBuilder 元数据，输出 `{设备: Release软件源URL}`。这种显式调用会执行软件源任务；可复用调用中的 Variables 来自调用方仓库，独立运行时来自本仓库。

## 固定签名密钥（首次配置一次）

同一版本/target 始终使用同一个 tag。为使已有设备无需重新导入公钥就能更新软件源，签名私钥也必须保持不变。默认最新版本使用 APK，只需生成一次：

```bash
python3 scripts/signing-key.py generate apk
```

将生成的 `.build/signing/apk-signing-key.pem` 的完整内容配置为 **`APK_SIGNING_KEY` Secret**。在 `openwrt-dist` 和调用它的 `OpenWrt` 仓库中填写**同一个值**。私钥文件在忽略的 `.build/` 目录中，不提交到 Git；保留这份密钥以便后续构建复用。

公钥默认生成在仓库根目录 `apk-public-key.pem`，可以正常提交到 Git。私钥仍保存在 `.build/signing/`；公钥输出位置可用 `--public-key` 指定。

旧版 opkg 使用另一种原生签名格式，需要时执行：

```bash
python3 scripts/signing-key.py generate opkg --usign /path/to/sdk/staging_dir/host/bin/usign
```

将 `.build/signing/opkg-signing-key.json` 的完整内容配置为 **`OPKG_SIGNING_KEY` Secret**。其中包含 usign 私钥及匹配的公钥。仅构建 APK 版本时不用配置此项；仅构建 opkg 版本时不用配置 APK 项。

工作流在编译前检查对应 Secret。私钥仅在发布任务中写入 SDK 目录，不放进 SDK artifact 或编译缓存。发布时会拒绝用不同公钥覆盖已公开的软件源，避免现有设备突然验签失败。

## 并发编译

每个架构的 SDK 和源码 feeds 只准备一次，4 个组使用 SDK 编译，另外 2 个组下载官方预编译包：

| 软件组 | 主要软件 | 方式 |
| --- | --- | --- |
| `openclash` | OpenClash | 官方预编译包 |
| `passwall` | PassWall、Xray、V2Ray Plugin、Simple Obfs、Shadowsocks Rust | 官方预编译包 |
| `mosdns` | MosDNS 核心及 LuCI | SDK 编译 |
| `openlist` | OpenList2 核心及 LuCI | SDK 编译 |
| `storage` | Dockerman、Diskman | SDK 编译 |
| `platform` | AdGuardHome、Argon、Modemband、SMS Tool；M28C 额外添加 Design、iStore、QuickStart、FM350 USB 拨号及 3ginfo-lite | SDK 编译 |

OpenClash、PassWall 两组通过经过 SHA256 校验的官方 ImageBuilder 获取包管理器、软件源地址和公钥，原生 apk/opkg 验证官方索引及包内容。这两组不下载 SDK artifact、不恢复编译工具链、不调用 make，只下载 `package-groups.json` 中列出的软件包；普通运行依赖继续由设备或 ImageBuilder 的同版本官方源提供。合并后沿用现有签名和 Release 地址。预编译包的文件名、SHA256 和来源写入 `build-configs.tar.gz` 内的 `*.prebuilt.json`，版本以官方源为准。

所选发行版、版本、架构缺少现成包时直接失败，不回退自编译或混用其他版本。默认 ImmortalWrt 提供这两组软件；选择上游 OpenWrt 前需确认其官方源是否包含它们。

默认两种架构最多 **12 个任务并发**，其中编译任务使用 `make -j4`，预编译下载任务不运行 make。可选 Variables：

- `BUILD_MAX_PARALLEL`：最大同时构建的任务数，默认 `12`。实际受 GitHub 账号并发配额限制。
- `BUILD_JOBS`：单个编译任务的 make 并行度，默认 `4`，不影响预编译下载任务。
- `OPENWRT_DISTRO`：默认 `immortalwrt`。
- `OPENWRT_DEVICES`：默认 `amd64,m28c`。官方 OpenWrt 没有 M28C profile 时必须选 `amd64`。

缓存包括 SDK 下载、源码下载、编译后的 host 工具链及 Go build/module 缓存。缓存按 SDK 校验和和软件组隔离，源码提交变化会产生新 key。首次无缓存仍需编译；总耗时取决于最慢的软件组和 runner 排队，发布前必须等所有组成功。

下载和编译从开始就使用 `V=s BUILD_LOG=1`，显示 Rust/LLVM 等长任务的内部进度；每组结束后上传 `build-logs-<设备>-<软件组>` artifact，包含分包日志和 SDK 配置，失败时也保留。

## 软件清单和依赖

- `package-groups.json` 定义任务划分，`packages.txt` 是全部基本软件清单；两者必须一致。
- `packages-m28c.txt` 是 M28C 平台附加软件。
- M28C 的 FM350 USB 拨号使用 [modemfeed](https://github.com/koshev-msk/modemfeed) 当前的 `xmm-modem` / `luci-proto-xmm`（旧教程中的 `fm350-modem` / `luci-proto-fm350`），信号状态界面使用 [4IceG 的 `luci-app-3ginfo-lite`](https://github.com/4IceG/luci-app-3ginfo-lite)。明确指定包来源，仅构建这些包及其依赖；仍在现有 `platform` 组内编译，并随该架构的 Release 软件源发布。
- `feeds.conf.custom` 定义第三方来源，`feed-overrides.txt` 明确每个覆盖包的归属；其余依赖使用 SDK 配套 feeds。
- `single-packages.json` 定义 Makefile 位于仓库根目录的单包（Argon、Design），直接安装到 `package/custom`，避免 SDK 将根目录误识别为 feed。
- Go 配方来自当前官方 `openwrt/packages`，支持新版本 OpenList/代理核心；不把整个稳定 feeds 替换为 master。所有 feeds 和 Go 提交记录在 `sources.json`。
- MosDNS 核心、LuCI、geo2txt 来自同一个 sbwml feed，避免官方核心与另一来源 UI 的启动脚本冲突。AdGuardHome 使用 ImmortalWrt 配套 feed 的原生包。
- 选择上游 OpenWrt 构建 amd64 时，从对应版本系列的 ImmortalWrt LuCI feed 补充 AdGuardHome、Modemband、SMS Tool 界面；核心和编译工具链仍使用 OpenWrt SDK。
- Design 主题的日期版本后缀会规范为 apk 接受的格式；该兼容修改摘要也写入源码锁定信息。
- SDK 中解除 curl 选项的外层菜单依赖，修复冻结 libc 配置与 LDAP 选项形成的 Kconfig 循环；保留包依赖及原有默认功能。
- SDK 保留 `llvm.download-ci-llvm=false` 的兼容修改，供仍需 Rust 的自编译软件使用，摘要计入 `sources.json`。OpenClash、PassWall 两组使用预编译包，不再进入 Rust/LLVM 或 Ruby 编译步骤。
- 最新 ImmortalWrt feeds 已移除独立 `luci-app-shadowsocks-libev`；旧 `luci-app-shadowsocks` / `openwrt-dist-luci` 的 Shadowsocks 功能统一改由 PassWall 管理，并提供官方预编译的 Shadowsocks Rust 客户端/服务端。不再运行手工安装的 po2lmo、跳过 `PKG_HASH` 或复制一整份重名 small 源。

编译组只选择自己的软件及依赖；预编译组只下载清单指定的软件。合并时发布 `package-groups.json` 和设备附加清单中的指定软件，以及来自自定义 feed 的依赖；未被明确指定的 `base` / `luci` / `packages` / `routing` / `telephony` 公共依赖由同版本官方源提供，不重复发布各组生成的 `luci-base` 等基础包。MosDNS、OpenList、iStore 等自定义 feed 的核心和配套依赖仍保留。

自定义包同名异内容、缺少指定软件、不同源提交或缺组仍会让构建失败，不使用 `IGNORE_ERRORS=1`，也不随意选择某组的包覆盖冲突。所有输入检查通过后才复制并签名发布。`merge-report.json` 记录保留、排除、重复和冲突的包；发布失败时也单独上传 `merge-report-<设备>` artifact。此合并规则可直接处理已完成的各组 artifact，修复合并逻辑后只需重跑失败的发布任务，无需重新编译各组。

## 软件源产物（GitHub Releases）

编译产物作为 **Release 附件** 发布，不再将软件包提交到 Git 分支。tag 只包含发行版、版本、target，例如：

```text
packages-immortalwrt-25.12.2-x86-64
packages-immortalwrt-25.12.2-rockchip-armv8
```

每份 Release 逐个上传软件包，包含完整软件源所需的文件：

| 格式 | 索引 | 软件包 | 公钥 |
| --- | --- | --- | --- |
| apk | `packages.adb` | 单独的 `.apk` 附件 | `feed-key.pem` |
| opkg | `Packages`、`Packages.gz`、`Packages.sig` | 单独的 `.ipk` 附件 | `feed-key.pub` |

另有 `build.json`、`sources.json`、`merge-report.json`、`sha256sums` 和 `build-configs.tar.gz`。SDK 和各组中间产物仍作为 Actions artifact 临时传递。首次创建草稿 Release，公开前核对附件名称、大小和 GitHub SHA256；草稿附件可能使用 `untagged-...` 临时 URL，公开后再严格校验正式 tag 的下载地址。后续同版本/target 构建直接更新已有 Release。先上传新软件包，最后替换索引，所有内容和正式下载地址验证成功后才清理过期软件包。构建 ID、执行轮次和源码提交保留在 `build.json`，不进入 tag。

APK 使用签名索引中的 `${name}-${hash}.apk` 下载规则；opkg 文件名同样带内容摘要，并在规范化后重新索引。包内部版本和内容保留，避免 GitHub 调整附件名后出现下载 404；新旧软件包可在替换索引前共存。设备和 LuCI 仍按正常包名安装、显示版本。

每次发布使用相同 Secret 中的原生签名密钥，只发布公钥。私钥不上传到 Release 或 artifact，也不会关闭签名检查。`TOKEN` 继续用于 Release 发布，签名 Secret 仅用于签署索引。固定 tag 要求 Release 可编辑；不会修改其他版本或 target 的 Release。

附件替换不是原子更新，发布期间获取索引可能需要稍后重试。失败时可重跑发布任务；索引验证完成前不会清理上一批软件包。

## 配置第三方软件源

**可以直接使用 Release 作为软件源。** 必须选对发行版、版本和 target，并使用该 Release 的索引、公钥及单独的软件包附件；只提供打包下载的压缩包不能作为在线软件源。

工作流 Summary 会输出每个设备的 Release 页面和软件源基础 URL。把下面的 `<tag>` 替换为所选 Release 的完整 tag：

```text
# apk: /etc/apk/repositories.d/customfeeds.list
https://github.com/fangguangyang/openwrt-dist/releases/download/<tag>/packages.adb

# opkg: /etc/opkg/customfeeds.conf
src/gz custom https://github.com/fangguangyang/openwrt-dist/releases/download/<tag>
```

公钥地址分别为同一基础 URL 下的 `/feed-key.pem`、`/feed-key.pub`。apk 将公钥放入 `/etc/apk/keys/`，opkg 使用 `opkg-key add` 导入。

完成配置后正常运行：

```sh
# apk 系统
apk update
apk add luci-app-openclash

# opkg 系统
opkg update
opkg install luci-app-openclash
```

GitHub Release 附件会跳转到下载 CDN；原生 apk/opkg 的 HTTP 下载会跟随跳转。运行中的设备访问的是公开附件，无需配置 GitHub TOKEN。

同一 OpenWrt 版本/target 重建后，源 URL 和公钥保持不变，运行 `apk update` / `opkg update` 即可刷新软件列表。切换到其他 OpenWrt 版本时应使用对应的新 tag；旧的分支/raw URL 不会自动跳转到 Release。新版固件流水线自动写入所选版本的 Release 地址及公钥，不同版本的 Release 分别保留。

固件构建已统一放在 `fangguangyang/OpenWrt`，本仓库不再重复构建另一套镜像。

## 检查

```bash
python3 -m unittest discover -s tests -v
python3 scripts/release.py resolve --version latest --output release.json
```

原生 APK 下载、依赖解析和临时目录安装测试（SDK 或 ImageBuilder 中应有 host apk）：

```bash
TEST_APK_SDK=/path/to/imagebuilder python3 -m unittest discover -s tests -p test_feed_transport.py -v
```

该测试使用本机临时 HTTP 服务模拟 Release 到 CDN 的跳转，验证签名索引、含特殊字符的包版本及依赖安装；无需创建真实 Release。

`scripts/release.py` 和对应测试与固件仓库保持一致。修改版本解析逻辑时应同步两份。
