| 检查 | 结果 | 说明 |
|---|---|---|
| R1 封装→解封，逐字节一致 | ✅ | 2958 B 容器，5 个文件，db sha256 5e992c4999b2… |
| R2a 错口令 → E_UNLOCK | ✅ | E_UNLOCK 保险库无法打开 |
| R2b 篡改密钥包裹 → E_UNLOCK | ✅ | E_UNLOCK 保险库无法打开 |
| R3a 篡改载荷末端 → E_INTEGRITY | ✅ | E_INTEGRITY E_INTEGRITY |
| R3b 改头部元数据 → E_INTEGRITY | ✅ | E_INTEGRITY 头部与签名不一致 |
| R3c 截断容器 → E_FORMAT | ✅ | E_FORMAT 容器被截断或尾部有多余字节 |
| R3d 尾部追加字节 → E_FORMAT | ✅ | E_FORMAT 容器被截断或尾部有多余字节 |
| R4 未知 KDF → E_UNSUPPORTED | ✅ | E_UNSUPPORTED 不支持的 KDF：argon2i_ |
| R5a 内核签名被改 → E_SIGNATURE | ✅ | E_SIGNATURE 内核签名无效 |
| R5b 内核变化未批准 → E_POLICY | ✅ | E_POLICY 内核自上次封装后已变化；需显式批准 |
| R5c 显式批准后可重建签名 | ✅ | 新内核 sha256 b940d2904a3a… |
| R6a 助记词生成与回读 | ✅ | 24 词：color deal private … thing ozone |
| R6b 仅凭助记词解封 | ✅ | db sha256 5e992c4999b2… |
| R6c 没有口令包裹时，口令不可用 | ✅ | E_UNLOCK 保险库无法打开 |
| R6d 删除口令包裹后，助记词仍可解 | ✅ | 恢复路径独立于口令 |
| R6e 抄错一个词 → 校验位报错 | ✅ | E_MNEMONIC 助记词校验位不匹配（可能抄错了一个词） |
| R7 离线备份副本 → memory.db 哈希一致 | ✅ | sha256 5e992c4999b282a3… |
| R8a 本机绑定正确 → 可解 | ✅ | 设备描述 c79de8e8a3d0… |
| R8b 换一台设备 → E_UNLOCK（同一句报错） | ✅ | E_UNLOCK 保险库无法打开 |
| R9a 复制模板 → 新 UUID | ✅ | 9f2b1c3d… → 7b18c683…  记忆=empty |
| R9b 模板被改 → E_SIGNATURE | ✅ | E_SIGNATURE 模板签名无效 |
| R10a 旧口令失效 | ✅ | E_UNLOCK 保险库无法打开 |
| R10b 新口令可解，载荷密文未变 | ✅ | 容器 2958→2958 B，载荷 2186 B 原样保留 |
| R11a 明文包默认拒绝导入 | ✅ | E_POLICY 明文包默认拒绝导入；确需迁移请显式允许 |
| R11b 显式允许后升级为密文，内容一致 | ✅ | 已封装 2522 B（scrypt 档） |
| R12 8 次封装 nonce 全不重复 | ✅ | 16 个 nonce / 16 个唯一 |
| R13 容器内无明文痕迹 | ✅ | 未出现 NAS / 阿岚 / 口令 / 表名 |
| R14 错口令/错设备不泄露差异 | ✅ | 统一为 ('E_UNLOCK', '保险库无法打开') |
| C1 浮点数被拒绝 | ✅ | E_FORMAT 规范化 JSON 不允许浮点数 |
| C2 非 NFC 字符串被拒绝 | ✅ | E_FORMAT 字符串必须是 NFC |
| C3 键序不影响规范字节 | ✅ | {"a":2,"b":1} |
| R15 Node 独立实现可读 Python 容器 | ✅ | 独立实现读出 5 个文件，内核 c91adfa2c2fa… |
| K1 生成 KAT 向量 | ✅ | AES-GCM 空消息 tag=530f8afbc74536b9…，BIP39 24 词 |
| K2 HKDF 与 RFC 5869 用例一致 | ✅ | 3cb25f25faacd57a90434f64d0362f2a… |
| K3 HMAC 与公开向量一致 | ✅ | f7bc83f430538424… |
| M0 进程外 provider 单次调用开销 | ✅ | 82 ms/次（进程内 provider 无此开销，M2 数字需减去它） |
| M1 scrypt-low 解锁耗时 | ✅ | 157 ms（scrypt {'N': 32768, 'r': 8, 'p': 1}） |
| M1 argon2id-low 解锁耗时 | ✅ | 111 ms（argon2id {'memory': 19456, 'passes': 2, 'parallelism': 1}） |
| M1 argon2id-mid 解锁耗时 | ✅ | 220 ms（argon2id {'memory': 65536, 'passes': 3, 'parallelism': 1}） |
| M2 48.0 KiB 数据库：封装/解封 | ✅ | 封装 743 ms，解封 767 ms，头部 732 B，载荷 10 KiB |
| M2 168.0 KiB 数据库：封装/解封 | ✅ | 封装 752 ms，解封 727 ms，头部 732 B，载荷 33 KiB |
| M2 656.0 KiB 数据库：封装/解封 | ✅ | 封装 781 ms，解封 743 ms，头部 732 B，载荷 126 KiB |
| M2b 原生 AES-256-GCM 1 MiB | ✅ | 0.5 ms/次，1975 MB/s |
| M2b 原生 AES-256-GCM 8 MiB | ✅ | 3.7 ms/次，2272 MB/s |
| M3 WAL 会在旁路文件留下明文 | ✅ | 提交后 wal 中含明文=True；checkpoint(TRUNCATE)+close 后残留文件=['wal.db'] |
| M4 删除行后 VACUUM 才真正清除明文 | ✅ | DELETE 后残留=True，VACUUM 后残留=False |
