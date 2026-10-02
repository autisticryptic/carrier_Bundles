# 三种数据库与直接筛选删除规则

当前策略：`simadmin-simulated-standard-pruning-v1`。

## 保持原格式

所有输出仍为 **SQLite schema v7、8 张表、`carrier-bundles-ims-v1` JSON 契约**。
不增加生成标记、继承格式或运行时解码器。曾考虑的 v2 存储方案已取消。

| 版本 | 文件名 | 处理 |
|---|---|---|
| 完整版 | 原始 `*.sqlite3` | 与输入完整库逐字节一致，包括已有图标 |
| 无图标版 | `*-no-icons.sqlite3` | 仅去掉图标及资产引用，全部配置保留 |
| 直接筛选精简版 | `*-minimal-no-icons.sqlite3` | 去图标，并删除经已验证标准模型覆盖的 LTE IMS / VoWiFi 接入配置 |

四来源 IPSW、IPCC、Pixel、小米保持独立；每种版本都有四份，共 12 份，不混合固件字段。

## 什么会被删除

LTE 和 VoWiFi **分别判断**：

- LTE 可覆盖、VoWiFi 不可覆盖：只删除 `access.lte` 及对应接入专属 SIP 配置。
- VoWiFi 可覆盖、LTE 不可覆盖：只删除 VoWiFi 部分。
- 所有实际接入配置都可覆盖，且没有其他接入专属策略：删除整条 `carrier_profiles` 记录；
  外键级联清理其匹配规则、来源关联和字段证据。
- 存在未覆盖接入时保留记录、共享 IMS 字段和其他配置；只把已删除接入的 readiness 状态
  按原有 v1 规则重算，不能继续标记为可用 catalog 配置。
- 删除已移除接入的字段证据。父级证据值只有在与原配置子树完全一致时才裁成保留部分；
  不可解释的原始证据仍保留。完整原始证据始终可在完整版中找到。

删除后的接入通过 SimAdmin **现有**来源缺失/不可用回退路径使用标准派生配置。
没有新格式重建这些已删除配置，也没有改生产地址族策略。已有显式 catalog 选择可能显示
该接入不可用，运行时按既有回退继续；其他不具备派生能力的 v7 消费者应选完整或无图标版。

### 判定不是“看见标准域名就删”

规则在 `simulation_pruning/prune.py`，要求来源状态为 ready、公共且无歧义的 PLMN、
标准 IMS 域/realm/身份模板、已测试的 AKA、通用 `ims` APN 和支持的发现方式。
VoWiFi 另查 ePDG、EAP-AKA、身份及 IKE 参数。

特殊域名、APN 认证、单地址族要求、显式加密/隐私策略、非默认 SIP 头、媒体/开通/
额外服务策略以及未知字段都会使该接入保留。**保留不代表派生必然注册失败**，而是现有模型
尚不能证明删掉该配置不会破坏它绑定的其他需求。当前不删除 NR 配置：没有实施真正的
NR/5GS 注册、NAS、5G-AKA 或 PDU session 模拟。

## 模拟覆盖与证据边界

独立脚本目录：[simulations/ims_registration](../simulations/ims_registration/README.md)。
它在临时副本中复用真实 SimAdmin 派生工厂、LTE/VoWiFi 请求构造与候选转换、共享 REGISTER
事务引擎及 Digest-AKA；只替换字节通道和 SIM 返回材料，不访问设备、D-Bus 或真实网络。
对端验证身份、URI、事务、租期和独立计算的 Digest，不能收到 REGISTER 就返回成功。

当前 19 场景符合预期：12 个模拟注册成功、7 个预期拒绝。涵盖 421/494 累加兜底、407、
AKAv1/AKAv2、423 认证前后协商、UDP 原报文重传、无关/过期消息、认证次数上限、普通 403、
错误证明、私有域名以及明确的省略/SMS-only 保护。

这是**有条件的标准核心网模型**：假设已开通、承载/P-CSCF 可达且 SIM 返回有效材料。
不证明某家真实运营商、每一张 SIM、IKE/IPsec 隧道或全部 5G 流程已经通过。
`simulation_pruning/verified-evidence.json` 记录源文件和测试程序摘要、正例及反例。
构建校验报告完整性；提供 `--simadmin-source` 时还检查本机代码是否与证据一致。

## 运行与构建

在本仓库中重新运行模拟，不改动提供的 SimAdmin 源目录：

```bash
python3 simulations/ims_registration/run.py \
  --consumer-source ../SimAdmin \
  --target-dir /path/to/cargo-cache \
  --report data/variants/new-simulation/report.json
```

需要 Linux、Rust/Cargo 以及已经缓存的依赖；使用 `--locked --offline` 构建。输出路径必须是新的。
修改测试程序或扩大删除规则后，应重新测试并更新证据，不能只把 `passed` 改成 true。

构建四来源、三版本：

```bash
python3 tools/build_variants.py \
  ../SimAdmin/carrier-bundles-iphone16promax-27.0.sqlite3 \
  ../SimAdmin/carrier-bundles-ios-ipcc.sqlite3 \
  ../SimAdmin/carrier-bundles-pixel-mustang.sqlite3 \
  ../SimAdmin/carrier-bundles-xiaomi15ultra-xuanyuan-baseband.sqlite3 \
  --simulation-report simulation_pruning/verified-evidence.json \
  --simadmin-source ../SimAdmin \
  --output-dir data/variants/new-catalog-set
```

`--simadmin-source` 可省略，以使用报告固定的已测试源码快照；常规 Actions 不重新运行 Rust
模拟，但会检查证据与随库测试程序的摘要。主 catalog-set 和独立 Pixel 构建都传入已检查证据。
没有传 `--simulation-report` 的旧调用仍保留历史“九类可选默认字段省略”行为，以兼容旧脚本；
那不是本次直接筛选模式。

输入文件不会被修改，活动 WAL/SHM/journal 会被拒绝，已有输出目录不会覆盖。输出包含：

- `catalog-variants.json`：输入/输出 SHA256、各表计数、策略和模拟证据身份。
- `*.pruning.json`：逐记录删除接入、整行删除标记、原配置指纹和保留原因计数。
- `simulation-evidence.json`：实际使用的模拟报告。
- `SHA256SUMS`：数据库和所有报告的摘要。

```bash
cd data/variants/new-catalog-set
sha256sum -c SHA256SUMS
```

## 当前快照的删除结果

基于工作区已有的 2026-09-24 提取快照，未重新下载固件：

| 来源 | 原 Profile | 删除 LTE | 删除 VoWiFi | 整行删除 |
|---|---:|---:|---:|---:|
| iPhone 16 Pro Max / iOS 27.0 | 1962 | 0 | 0 | 0 |
| Apple IPCC | 1846 | 1 | 0 | 1 |
| Pixel mustang | 1444 | 183 | 4 | 0 |
| Xiaomi xuanyuan | 721 | 430 | 0 | 1 |
| 合计 | 5973 | 614 | 4 | 2 |

不是将 618 份接入配置都算成整条记录。很多记录还要保留 NR 或另一个未覆盖接入，因此仍占
一个 Profile ID。iOS 保留较多的主要原因是媒体、开通、移动性或专属策略未被通用模型覆盖，
不是已证明它们无法派生注册。

本地实际消费者验证了：618 项移除接入通过现有来源绑定解析落到派生配置；11326 项其他
接入投影不变，NR 配置不变。测试用合成 IMSI 与明确的 home PLMN，不读取真实 SIM。
完整/无图标版、外键、原格式、原文件不变和负例另有 Python 回归。

在具有配套测试的 SimAdmin 工作区，可显式复核实库（普通 CI 不把未运行的实库测试当作通过）：

```bash
SIMADMIN_CATALOG_PRUNING_DIR=/absolute/path/to/new-catalog-set \
  cargo test --manifest-path backend/Cargo.toml --locked \
    generated_pruned_catalogs_preserve_other_accesses_and_resolve_derived -- --ignored --nocapture
```
