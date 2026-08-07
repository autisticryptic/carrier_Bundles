# Pixel 官方固件提取器

这个目录把 Google Pixel Factory Image 中的公共静态 IMS/VoWiFi 配置映射到根目录的 [`schema.sql`](../../schema.sql)，并调用统一图标打包器生成只读 SQLite。

## 一键构建

系统需要 Python 3、Git 和 7-Zip (`7z`/`7zz`)。脚本会自行创建根目录 `.venv` 并安装固定版本的 protobuf；固件、解包缓存和数据库写入 `data/`，不会提交仓库。

先阅读 [Google Factory Images](https://developers.google.com/android/images) 的条款，再显式确认：

```bash
python3 android/pixel/build_catalog.py \
  --device redfin \
  --build-id UP1A.231105.001.B2 \
  --accept-google-terms
```

流程会自动完成：

```text
Google 官方元数据 -> 下载/SHA-256 校验 -> Factory ZIP/partition 解包
  -> CarrierSettings protobuf 解析 -> MCFG 制品清单 -> schema v6
  -> NekokoLPA2 图标匹配/下载 -> 完整性检查 -> 只读封存/原子发布
```

`--build-id latest` 可跟随指定设备的最新官方 build。为了可重复发布，正式构建应固定 build id。已下载的 ZIP 和镜像解包结果会复用。

在线构建会从 Google 页面读取机型名称。输出文件名、`catalog_release.notes`、`source_snapshots.device_family` 和 `source_snapshots.device_model` 会同时保留营销名称与设备代号，例如 `Pixel 5` / `redfin`。离线构建使用 `--device-name "Pixel 5"`；以后构建 Pixel 9 或更高型号时使用对应代号和名称即可，不会与 Pixel 5 catalog 混淆。

完全离线时需要显式提供固件元数据：

```bash
python3 android/pixel/build_catalog.py \
  --offline \
  --device redfin \
  --device-name "Pixel 5" \
  --build-id UP1A.231105.001.B2 \
  --os-version 14.0.0 \
  --factory-zip data/raw/pixel/redfin-up1a.231105.001.b2-factory-4e5a2679.zip \
  --factory-sha256 4e5a26793d8400f13b72cbd17aeb284e040b3236436f0c6f3119ccc77d4495ad \
  --skip-icon-sync
```

离线但希望继续打包图标时，去掉 `--skip-icon-sync`；固定已有 NekokoLPA2 checkout 时加 `--no-icon-repo-update`。

## GitHub Actions

可以在仓库的 **Actions -> Build Pixel catalog -> Run workflow** 手动构建。表单中的 `device` 是设备代号，`device_name` 是写入来源元数据的营销名称，`build_id` 应尽量填写固定的官方构建号。运行前必须阅读 Google Factory Images 条款并勾选确认；未确认时 workflow 会在下载前退出。

成功运行后下载 `pixel-carrier-bundles-<run-id>` artifact，其中包含：

- `carrier-bundles-pixel.sqlite3`：已通过完整性检查并封存为只读的 catalog。
- `catalog-summary.json`：release、生成器版本和主要表记录数。

线上构建不缓存或上传 Factory ZIP、`product.img`、`vendor.img` 和 MCFG 原文件。artifact 默认保留 14 天；它是构建结果，不会自动创建 GitHub Release。

## 当前提取范围

- 从 `product.img/etc/CarrierSettings` 读取 `carrier_list.pb`、`others.pb` 和设备专用 protobuf。
- 将 IMS APN、LTE/NR/IWLAN bearer、IP family、漫游协议、公共静态 APN 认证、MTU、ePDG、XCAP、entitlement、服务能力、SIP transport/安全要求、REGISTER expiry 和 User-Agent 模板写入规范表或原始证据表。
- 按 PLMN 和 3GPP TS 23.003 生成 IMS home domain、realm 和 ISIM 缺失时的 IMPI/IMPU fallback 模板；这些字段明确标记为 `standard_derived`，confidence 为 60。
- 从 Qualcomm Pixel 的 `vendor.img` 找出 `mcfg_sw.mbn`，记录路径、大小、SHA-256、build 和 baseband 版本。
- 对冲突的 IMS APN 候选不静默选值；规范 APN 字段保持 `NULL`，候选仍保存在 `raw_config_values`。

首个验证样本是 Pixel 5 (`redfin`) Android 14 `UP1A.231105.001.B2`，baseband `g7250-00264-230619-B-10346159`。

## 已知边界

当前 MCFG 只完成制品发现和 inventory，尚未语义解码 MBN 内部的 IMS/ePDG XML。`CarrierSettings` 是 Android framework/设备配置，不等同于完整 modem provisioning。因此这个 catalog 已适合做静态配置发现、profile 选择和客户端默认值输入，但不能单独保证完成用户态 IMS 注册。

运行时客户端仍需从合法的 SIM/ISIM 与网络会话获得实际 IMSI、IMPI/IMPU、AKA 凭据、动态 P-CSCF、分配地址、IPsec SPI/端口和 entitlement token；这些用户或会话数据不会进入本数据库。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试 fixture 仅使用合成的公共 protobuf 值，不包含真实 SIM 或用户身份。
