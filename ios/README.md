# iOS Carrier Bundle 提取器

本目录从 Apple 官方 IPSW 的 Carrier/Country Bundle 提取公共静态 IMS、LTE/NR、VoWiFi、IKE、SIP、媒体和运营商能力，输出根目录 schema v7。iOS 与 Android 独立构建，不互补 profile。

## 数据源与工具

- [blacktop/ipsw](https://github.com/blacktop/ipsw) 用于远程 ZIP 枚举、AEA 解密和文件提取。
- ipsw.me 只用于发现 Product Type 对应的 Apple CDN URL；数据库证据仍来自 Apple IPSW/Carrier Bundle。
- Apple 的 `https://itunes.com/version` 索引提供独立 IPCC 更新；本项目参考 [mrlnc/ipcc-downloader](https://github.com/mrlnc/ipcc-downloader) 的公开协议说明，使用 `ios/download_ipcc.py` 自行解析 Apple plist 并从 Apple CDN 获取 `.ipcc`。参考仓库已归档且未声明许可证，因此未复制其代码。
- Apple 没有公开的完整 IMS/VoWiFi 配置 API。`.bbfw` 目前只作为固件制品，不猜测其中未确认的 IMS 字段。

首个验证样本是 iPhone 16 Pro (`iPhone17,1`) iOS 26.6 `23G71`。默认线上目标改为 iPhone 16 Pro Max (`iPhone17,2`) 的最新 signed IPSW；还可以独立构建 iPhone 15 Pro Max (`iPhone16,2`) 等代际。

## 构建

```bash
# 当前 Pro Max
python3 ios/build_catalog.py --product-type iPhone17,2 --version latest

# 上一代 Pro Max；输出仍是另一份独立 SQLite
python3 ios/build_catalog.py --product-type iPhone16,2 --version latest
```

正式 release 应把 `--version` 固定为具体 iOS 版本或 build。需要 Python 3、Git、Go、Rust/Cargo、FUSE 3 和足够容纳 AEA/APFS 的空间。

已经导出 bundle 时可跳过下载和 APFS：

```bash
python3 ios/build_catalog.py \
  --bundle-root data/tmp/ios/iphone16pro-23g71/carrier-extract \
  --baseband data/tmp/ios/iphone16pro-23g71/outer/Mav24-2.70.01.Release.bbfw
```

不带 `--product-type` 的离线命令沿用上述 iPhone 16 Pro 固定元数据。其他离线制品应显式给出 `--product-type`、`--version` 和必要时的 `--device-class`。

## IPCC 获取

只查询 Apple 索引，不下载：

```bash
python3 ios/download_ipcc.py --product iphone --bundle Maxis
```

下载、校验 Apple Digest、额外计算 SHA-256，并安全解压到现有解析器可识别的目录：

```bash
python3 ios/download_ipcc.py \
  --product iphone \
  --bundle Maxis \
  --download \
  --extract
```

默认只选择每个 bundle 的最新条目。`--all-versions` 保留历史版本；重复使用 `--bundle` 可以选择多个运营商或 MCCMNC 关键词。为避免误下载数千个文件，不带 `--bundle` 下载时必须显式加 `--download-all`。

默认输出：

- IPCC：`data/raw/ipcc/`；
- 规范化 bundle：`data/tmp/ipcc/bundles/System/Library/Carrier Bundles/iPhone/`；
- 来源、Apple Digest、SHA-256 和原始索引路径：`data/tmp/ipcc/manifest.json`。

IPCC 是 IPSW 之外的独立 Apple 发布来源，可能比某个固定 IPSW 新，也可能只覆盖少数运营商。不能假设 `itunes.com` 中存在全部系统 Carrier Bundle，更不能用 IPCC 字段补齐另一份 IPSW catalog；后续导入时应从单组 IPCC 制品独立生成数据库并保留来源证据。

### 构建独立 IPCC catalog

以下命令下载 Apple 索引中每个 iPhone bundle 的最新 IPCC，并生成一份不与 IPSW catalog 混合的只读 SQLite：

```bash
python3 ios/build_ipcc_catalog.py \
  --workers 12 \
  --output data/carrier-bundles-ios-ipcc.sqlite3
```

调试单个运营商时可重复使用 `--bundle`：

```bash
python3 ios/build_ipcc_catalog.py \
  --bundle Maxis \
  --skip-icon-sync \
  --output data/carrier-bundles-ios-ipcc-maxis.sqlite3
```

每个 profile 的来源证据会指向具体 Apple IPCC URL、IPCC SHA-256、Apple Digest 和 bundle 版本。个别历史 IPCC 下载失败时默认记录到 manifest 并继续构建其他成功 bundle；`--strict-downloads` 可改为任何失败都终止构建。

## v7 映射

| iOS 信息 | v7 位置 |
|---|---|
| Bundle 来源、哈希、解析器和字段证据 | `source_artifacts`、`profile_sources`、`field_evidence` |
| 运营商、PLMN、GID、SPN 和前缀 | `carriers`、`profile_match_rules` |
| APN/DNN、P-CSCF、IMS identity、IKE、SIP、codec、能力、entitlement、E911 | `carrier_profiles.config_json` |
| 图标 | `visual_assets` |

设备、Product Type、OS/build 和 baseband 只存在于输出文件名、日志与构建 summary，不写入数据库。未发现字段保持缺失；禁止输出实际 IMSI、ICCID、IMEI、IMPI/IMPU、AKA 材料、会话密钥和动态网络数据。

## Actions

全量 IPSW 需要大磁盘。仓库的 `Build catalog set` workflow 使用 macOS runner 和原生 `hdiutil` 处理 APFS；Linux 本地构建仍使用只读 APFS FUSE。IPCC catalog 使用独立 Ubuntu job，不需要 APFS/FUSE。Pixel、IPSW 和 IPCC 任一 job 失败时，其他成功数据库仍会进入 artifact/Release，且 `BUILD_STATUS.md` 会记录各来源结果。
