// SysPersona-OS · 本文件：SysPersona 安全规范 —— 可行性验证用密码学 oracle。
// SysPersona 安全规范 —— 可行性验证用密码学 oracle。
// 目的：用 OpenSSL（Node 24）作为「独立于 Python 参考代码」的实现，
// 验证容器格式、KDF 参数与失败语义。它不是产品代码。
//
// 协议：stdin 一个 JSON 对象，stdout 一个 JSON 对象。
// 用法：node oracle.js < request.json

"use strict";

const crypto = require("crypto");
const fs = require("fs");

function b64(s) {
  return Buffer.from(s, "base64");
}
function enc(buf) {
  return Buffer.from(buf).toString("base64");
}

function argon2id(req) {
  return new Promise((resolve, reject) => {
    const opts = {
      message: b64(req.pass_b64),
      nonce: b64(req.salt_b64),
      parallelism: req.parallelism | 0,
      tagLength: req.tag_length | 0,
      memory: req.memory | 0,
      passes: req.passes | 0,
    };
    if (req.secret_b64) opts.secret = b64(req.secret_b64);
    if (req.ad_b64) opts.associatedData = b64(req.ad_b64);
    crypto.argon2("argon2id", opts, (err, key) => {
      if (err) reject(err);
      else resolve({ key_b64: enc(key) });
    });
  });
}

const HANDLERS = {
  ping: async () => ({ ok: true, openssl: process.versions.openssl }),

  argon2id,

  scrypt: async (req) => ({
    key_b64: enc(
      crypto.scryptSync(b64(req.pass_b64), b64(req.salt_b64), req.key_len | 0, {
        N: req.N | 0,
        r: req.r | 0,
        p: req.p | 0,
        maxmem: req.maxmem || 1024 * 1024 * 1024,
      })
    ),
  }),

  hkdf: async (req) => ({
    key_b64: enc(
      Buffer.from(
        crypto.hkdfSync(
          "sha256",
          b64(req.ikm_b64),
          b64(req.salt_b64),
          req.info_b64 ? b64(req.info_b64) : Buffer.from(req.info || "", "utf8"),
          req.length | 0
        )
      )
    ),
  }),

  hmac: async (req) => ({
    mac_b64: enc(crypto.createHmac("sha256", b64(req.key_b64)).update(b64(req.message_b64)).digest()),
  }),

  gcm_encrypt: async (req) => {
    const cipher = crypto.createCipheriv("aes-256-gcm", b64(req.key_b64), b64(req.nonce_b64), {
      authTagLength: 16,
    });
    if (req.aad_b64) cipher.setAAD(b64(req.aad_b64));
    const ct = Buffer.concat([cipher.update(b64(req.plaintext_b64)), cipher.final()]);
    return { ct_b64: enc(Buffer.concat([ct, cipher.getAuthTag()])) };
  },

  gcm_decrypt: async (req) => {
    const blob = b64(req.ct_b64);
    const tag = blob.subarray(blob.length - 16);
    const ct = blob.subarray(0, blob.length - 16);
    const decipher = crypto.createDecipheriv("aes-256-gcm", b64(req.key_b64), b64(req.nonce_b64), {
      authTagLength: 16,
    });
    if (req.aad_b64) decipher.setAAD(b64(req.aad_b64));
    decipher.setAuthTag(tag);
    try {
      const pt = Buffer.concat([decipher.update(ct), decipher.final()]);
      return { plaintext_b64: enc(pt) };
    } catch (e) {
      return { error: "E_INTEGRITY" };
    }
  },

  ed25519_keygen: async () => {
    const { publicKey, privateKey } = crypto.generateKeyPairSync("ed25519");
    return {
      public_b64: enc(publicKey.export({ type: "spki", format: "der" })),
      private_b64: enc(privateKey.export({ type: "pkcs8", format: "der" })),
    };
  },

  ed25519_sign: async (req) => ({
    sig_b64: enc(crypto.sign(null, b64(req.message_b64), crypto.createPrivateKey({
      key: b64(req.private_b64),
      format: "der",
      type: "pkcs8",
    }))),
  }),

  ed25519_verify: async (req) => {
    const ok = crypto.verify(
      null,
      b64(req.message_b64),
      crypto.createPublicKey({ key: b64(req.public_b64), format: "der", type: "spki" }),
      b64(req.sig_b64)
    );
    return { ok };
  },

  // 原生 AEAD 吞吐：回答「这个格式在现代硬件上贵不贵」
  bench_gcm: async (req) => {
    const size = req.size | 0;
    const rounds = req.rounds | 0;
    const key = crypto.randomBytes(32);
    const buf = crypto.randomBytes(size);
    const nonce = crypto.randomBytes(12);
    const start = process.hrtime.bigint();
    for (let i = 0; i < rounds; i++) {
      const c = crypto.createCipheriv("aes-256-gcm", key, nonce, { authTagLength: 16 });
      c.setAAD(Buffer.from("syspersona"));
      c.update(buf);
      c.final();
    }
    const elapsed = Number(process.hrtime.bigint() - start) / 1e6;
    return { ms_per_call: elapsed / rounds, mb_per_s: (size * rounds) / (elapsed / 1000) / 1e6 };
  },

  // 批量执行，减少进程启动开销（仅测试用）
  batch: async (req) => {
    const out = [];
    for (const item of req.ops || []) {
      const fn = HANDLERS[item.op];
      if (!fn) {
        out.push({ error: "E_UNSUPPORTED" });
        continue;
      }
      try {
        out.push(await fn(item));
      } catch (e) {
        out.push({ error: "E_CRYPTO", detail: String(e.message || e) });
      }
    }
    return { results: out };
  },
};

async function main() {
  const raw = fs.readFileSync(0, "utf8");
  let req;
  try {
    req = JSON.parse(raw);
  } catch (e) {
    process.stdout.write(JSON.stringify({ error: "E_BAD_REQUEST" }));
    return;
  }
  const fn = HANDLERS[req.op];
  if (!fn) {
    process.stdout.write(JSON.stringify({ error: "E_UNSUPPORTED" }));
    return;
  }
  try {
    const res = await fn(req);
    process.stdout.write(JSON.stringify(res));
  } catch (e) {
    process.stdout.write(JSON.stringify({ error: "E_CRYPTO", detail: String(e.message || e) }));
  }
}

main();
