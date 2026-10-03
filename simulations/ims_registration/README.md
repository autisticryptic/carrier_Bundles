# IMS / VoWiFi 离线注册模拟

本目录是可独立携带的测试脚本与 Rust 模拟对端，供数据库精简规则使用。数据库格式不变。

## 执行

```bash
python3 simulations/ims_registration/run.py \
  --consumer-source ../SimAdmin \
  --target-dir /path/to/cargo-cache \
  --report data/variants/simulation-run/report.json
```

需要 Linux、Python 3、Rust/Cargo 和已缓存的 SimAdmin 依赖。默认 Cargo 位于 PATH 或
`~/.cargo/bin/cargo`，可用 `--cargo` 指定。脚本以 `--locked --offline` 构建，不下载依赖。

- 复制 backend 和 VERSION 到临时目录，不修改指定的 SimAdmin checkout。
- 在临时副本添加 `cfg(test)` 接口，直接调用实际派生工厂、两接入候选及请求构造函数。
- 只运行 `offline_derivation_registration_matrix`，不启动正式程序或设备测试。
- 使用内存 SIP 通道、合成 SIM 返回值；不使用真实 SIM、D-Bus、网络命名空间或 XFRM。
- 输出路径必须不存在；失败保留日志，不生成“通过”报告。程序同时核对源文件在测试期间未变。

报告包含实际源码及测试程序摘要、21个场景结果；不输出 nonce、AKA 密钥、完整认证头或真实
用户身份。SQLite 构建器检查这些证据；若传入 `--simadmin-source`，还会与当前代码逐项核对。

## 实际验证的场景

13个正例：LTE首包必须具备完整sec-agree声明、LTE/Wi-Fi AKA、Wi-Fi 421和494累加兼容回退、407、AKAv2-MD5、
AKAv2-SHA-256、认证前后 423、无关/过期 SIP 帧、UDP 字节相同重传、明确省略与 SMS-only。

8个预期失败反例：显式disabled不能被required要求覆盖、关闭必要回退、认证次数耗尽、普通403、
特殊运营商域、错误Digest、非法nonce、未授权的普通MD5。**预期失败算测试通过，不算注册成功。**

对端在回复 200 前核对身份、URI、realm、nonce count、租期及独立计算的 Digest；不是
收到请求次数够多就宣布注册。候选转换和 REGISTER 事务执行来自真实 SimAdmin 代码，
但 SIM 认证结果与传输保护边界是测试替身。

## 不能推导的结论

- 不证明真实运营商已开通、白名单/entitlement 满足、P-CSCF/ePDG 可达。
- 不执行完整 IKE/EAP/IPsec 隧道与无线承载；安全传输元数据不是实际加密报文。
- NR 只检查已有命名规则，不执行 NAS、5G-AKA、PDU session 或 VoNR 注册。
- 一个通用正例不能证明所有运营商配置可删除。筛选器还必须排除特殊或未建模要求。

当前冻结证据在 `simulation_pruning/verified-evidence.json`；扩大覆盖后需重新运行，并更新
测试程序摘要和筛选规则。具体直接删除规则及实库计数见
[数据库变体说明](../../docs/CATALOG_VARIANTS.md)。
