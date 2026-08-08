# Carrier Bundles

[![CI](https://github.com/autisticryptic/carrier_Bundles/actions/workflows/ci.yml/badge.svg)](https://github.com/autisticryptic/carrier_Bundles/actions/workflows/ci.yml)

从 iOS、Android 和 modem 固件中提取 IMS、VoLTE、VoNR、VoWiFi 的**公共静态运营商配置**，生成供外部项目只读查询的 SQLite catalog。

这个仓库不是用户态 IMS 客户端，也不保存 SIM、用户、线路或注册日志。数据库只随固件/基带来源更新而重新构建。

## 结构

```text
carrier_Bundles/
├── schema.sql             # 唯一、平台无关的 SQLite schema
├── ios/                   # 独立的 iOS IPSW / Carrier Bundle 提取项目
├── android/               # 独立的 Android / vendor modem 提取项目
├── icons/                 # 图标打包子项目、fallback 和 NekokoLPA2 checkout
├── docs/                  # 数据设计与适配器契约
├── tools/                 # 两个平台共用的 catalog 初始化和封存工具
├── tests/                 # schema、只读发布和资产测试
└── data/                  # 构建时自动创建，本地 SQLite 不提交
```

iOS/Android 可以使用不同解析器，但最终必须映射到根目录 [`schema.sql`](./schema.sql)，不能各自维护数据库表。

## 数据内容

- catalog release、固件/基带来源、构建号、解析器版本和哈希。
- 运营商、别名、PLMN、MCC/MNC、GID/SPN 和公开匹配前缀。
- LTE/EPC、NR/5GC、ePDG、N3IWF 接入配置。
- IMS domain/realm、IMPI/IMPU 格式模板、P-CSCF 发现、SIP REGISTER 和安全协商要求。
- LTE、5G、VoWiFi 各自的 SIP Header/Contact/Security-Client 覆盖，以及 IKE IDi/IDr 模板。
- VoLTE、VoNR、VoWiFi、SMS over IMS、entitlement 与紧急服务能力。
- 运营商 badge/logo 的数据库 BLOB、远端来源、哈希、许可和 attribution。
- 经过语义确认的厂商静态键及字段级证据；未确认的键只留在构建诊断 sidecar。

不保存具体 IMSI、ICCID、MSISDN、IMEI、IMPI/IMPU、Ki、OP/OPc、AKA 响应、会话密钥、线路绑定或注册结果。

## 不可变发布

每次更新创建一个新数据库，不在旧 release 上做迁移或用户编辑：

```bash
python3 tools/init_db.py data/carrier-bundles-2026.08.07.sqlite3 \
  --release-id 2026.08.07 \
  --generator-version development

# 运行 iOS/Android 导入器并完成校验后；seal 会先更新并打包图标
python3 tools/seal_db.py data/carrier-bundles-2026.08.07.sqlite3
```

消费者必须以只读方式打开：

```text
file:carrier-bundles-2026.08.07.sqlite3?mode=ro&immutable=1
```

推荐从 `v_profile_catalog` 读取运营商、匹配规则和完整 `config_json`；需要显示图标时按 `asset_id` 查询 `v_visual_asset_catalog`。

schema v7 的详细设计见 [`docs/数据库设计.md`](./docs/数据库设计.md)，平台输出规则见 [`docs/适配器契约.md`](./docs/适配器契约.md)，身份/Header 占位符见 [`docs/模板变量.md`](./docs/模板变量.md)。根目录 `schema.sql` 和 `config.schema.json` 是当前唯一查询/JSON 契约。

提取器的实现顺序、上游项目选择和每阶段交付边界见 [`docs/提取器路线图.md`](./docs/提取器路线图.md)。当前确定的主线是 Pixel 官方固件 -> iOS 官方 IPSW/Carrier Bundle -> Samsung AP/CSC/IMSService -> 独立 modem 适配器；三方 ROM 只作辅助证据。

## Pixel 自动构建

Pixel 提取器已经可以自动解析 Google 官方 Factory Image、生成 catalog、打包图标并封存：

```bash
python3 android/pixel/build_catalog.py \
  --device mustang \
  --build-id CP2A.260805.005 \
  --accept-google-terms
```

系统依赖是 Python 3、Git 和 7-Zip；Python protobuf 环境由脚本自动建立。完整参数、离线构建和当前字段边界见 [`android/pixel/README.md`](./android/pixel/README.md)。

## iOS 自动构建

iOS 提取器默认通过 ipsw.me 发现最新的 iPhone 16 Pro Max (`iPhone17,2`) 官方 Apple CDN IPSW，也支持固定版本和多个 Pro Max 代际：

```bash
python3 ios/build_catalog.py --product-type iPhone17,2 --version latest
```

下载和 APFS 提取已完成后，可以直接复用导出的 bundle 目录，不再下载约 8 GiB 的 RootFS 成员：

```bash
python3 ios/build_catalog.py \
  --bundle-root data/tmp/ios/iphone16pro-23g71/carrier-extract \
  --baseband data/tmp/ios/iphone16pro-23g71/outer/Mav24-2.70.01.Release.bbfw
```

完整参数、系统依赖和证据边界见 [`ios/README.md`](./ios/README.md)。

## GitHub Actions 在线构建

仓库包含两条线上流程：

- `CI`：每次 push/PR 自动在 Python 3.11 和 3.13 上执行语法检查及全部单元测试。
- `Build catalog set`：从 GitHub Actions 一次构建 Pixel 和多个 iPhone Pro Max catalog；每份 SQLite 保持独立，并同时上传 summary 与 `SHA256SUMS`。

Pixel 在线构建必须先阅读 [Google Factory Images 条款](https://developers.google.com/android/images)，并在手动表单中确认接受。Factory ZIP、解包镜像和缓存只存在于临时 runner，不会进入 artifact；artifact 只包含最终 SQLite 和 `catalog-summary.json`。建议正式发布固定 `build_id`，不要使用 `latest`。

iOS 全量提取需要下载大体积 AEA、解密 APFS 并使用 FUSE；`Build catalog set` 默认使用带 `ios-extractor` 标签的 self-hosted runner，避免在普通 runner 上因磁盘/FUSE 不足而产生半成品。常规 CI 仍只运行解析器和 fixture 测试。

## Release 发布

GitHub Release 只接收已经由 `tools/seal_db.py` 封存、再由 `tools/verify_catalog.py` 验证的数据库。发布工作流使用临时 `release-staging/**` 分支传递制品，通过仓库内 `release-assets/manifest.json` 固定 tag、目标 commit、release 标题和说明；目标 commit 必须与远端 `main` 完全相同。Release 附件包括原始 `.sqlite3`、`catalog-summary.json` 和 `SHA256SUMS`。

不同固件始终发布独立 catalog。Pixel 5 (`redfin`) 只作为归档，新的 Pixel 与各代 iPhone Pro Max 不会互相合并或补齐字段。

## iOS 验证基准

首个 iOS 提取样本固定为 iPhone 16 Pro (`iPhone17,1`) 的 Apple 官方 IPSW：iOS 26.6、build `23G71`、baseband `Mav24-2.70.01`。选择该设备是为了覆盖 LTE、5G NSA/SA、VoWiFi 以及可能存在的 VoNR 配置维度；iPhone 8 不作为本项目的 iOS 基准。

机型支持某项无线能力不代表 Carrier Bundle 一定公开该能力的完整注册参数。提取器只写入固件中能够验证语义的静态值，未发现的 VoNR、NR/5GC 或 SIP 字段保持缺失。IPSW、设备、build 和 baseband 出现在构建摘要，不进入运行时匹配表。

## 图标

图标匹配参考 [NekokoLPA2](https://github.com/iebb/NekokoLPA2)。它本身不附带运营商 logo，而是从 `operator-icons.pages.dev` 解析并下载图标。独立的 `icons` 子项目把该动作固定在构建期：`icons/package_icons.py` 以 shallow + sparse 方式更新本地 `icons/vendor/NekokoLPA2`，只检出图标来源代码和许可文件；然后读取提取器已经写入的 PLMN/GID/SPN/profile，只下载实际需要的 PNG，并以原始 BLOB 写入 `visual_assets.asset_data`。

`tools/seal_db.py` 默认在封存前调用图标打包器。需要离线测试时可以显式传入 `--skip-icon-sync`；需要固定当前 NekokoLPA2 commit 时使用 `--no-icon-repo-update`。图标服务没有声明可验证的统一再分发许可，因此数据库中相应资产会记录 `license_spdx='NOASSERTION'` 和完整来源，使用方仍需自行评估发布权利。

读取图标示例：

```sql
SELECT a.media_type, a.asset_data
FROM v_profile_catalog AS c
JOIN v_visual_asset_catalog AS a ON a.asset_id = c.asset_id
WHERE c.plmn = '310260';
```

## 当前状态

- SQLite schema v7 已可初始化、查询、嵌入图标和只读封存；8 张物理表通过 `carrier_profiles.config_json` 覆盖 IMS、LTE、NR、VoWiFi、IKE、SIP、媒体、ViLTE、XCAP、entitlement 和 emergency 配置。
- Pixel Factory Image 提取器已实现下载校验、sparse/ext4 解包、CarrierSettings 归一化、MCFG inventory 和一键构建。
- NekokoLPA2 图标同步/打包器与首批 fallback badge 已建立。
- Pixel 5 `redfin` Android 14 catalog 仅作为历史归档；新的默认目标是 Google 官方 Pixel 10 Pro XL `mustang` 全球 Factory Image，区域 `.A1` 行不会被 `latest` 误选。
- 设备、系统、build 和 baseband 只出现在构建摘要、日志和文件名，不进入运营商 profile 或运行时匹配表。
- Qualcomm MCFG 内部语义、Pixel 6+ Tensor modem 和 Samsung 提取器仍待实现。
- iOS I1/I2 已完成：现有 iPhone 16 Pro 解析结果可复现；构建器现在可以发现并独立生成 iPhone 16 Pro Max、15 Pro Max 等多个代际 catalog。
- iOS 构建已验证远端 ZIP64 单成员下载、AEA 解密、APFS 导出、D93 override/MVNO 合并、字段证据和只读封存；`.bbfw` 仍只做版本与哈希 inventory。
