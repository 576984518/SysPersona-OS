// 独立读取器：只用 Node 内置能力，按《SECURITY_SPEC.md》解一个由 Python 写出的容器。
// 目的不是做产品，而是证明「格式不依赖某种语言/某个库」。
// 用法：node reader.js '{"container":"...","passphrase":"..."}'

"use strict";

const crypto = require("crypto");
const fs = require("fs");
const zlib = require("zlib");

const MAGIC = Buffer.from("SPVAULT1", "ascii");
const FORMAT = "syspersona-vault/1";

function b64d(s) {
  return Buffer.from(s, "base64url");
}

// 规范 §5.1：键排序、无空白、UTF-8、整数
function canon(value) {
  if (value === null) return "null";
  if (typeof value === "number") {
    if (!Number.isInteger(value)) throw new Error("float not allowed");
    return String(value);
  }
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map(canon).join(",") + "]";
  return (
    "{" +
    Object.keys(value)
      .sort()
      .map((k) => JSON.stringify(k) + ":" + canon(value[k]))
      .join(",") +
    "}"
  );
}

function parse(buf) {
  if (buf.length < 20 || !buf.subarray(0, 8).equals(MAGIC)) throw new Error("E_FORMAT magic");
  if (buf.readUInt16BE(8) !== 1) throw new Error("E_UNSUPPORTED version");
  const headerLen = buf.readUInt16BE(10);
  const headerBytes = buf.subarray(12, 12 + headerLen);
  const payloadLen = buf.readUInt32BE(12 + headerLen);
  const payload = buf.subarray(16 + headerLen);
  if (payload.length !== payloadLen) throw new Error("E_FORMAT length");
  const header = JSON.parse(headerBytes.toString("utf8"));
  if (header.format !== FORMAT) throw new Error("E_FORMAT format");
  return { header, payload };
}

function deriveKek(header, passphrase) {
  const salt = b64d(header.kdf.salt);
  const pass = Buffer.from(passphrase, "utf8");
  if (header.kdf.id === "scrypt") {
    const p = header.kdf.params;
    return crypto.scryptSync(pass, salt, 32, { N: p.N, r: p.r, p: p.p, maxmem: 1 << 30 });
  }
  if (header.kdf.id === "argon2id") {
    const p = header.kdf.params;
    return new Promise((resolve, reject) => {
      crypto.argon2(
        "argon2id",
        { message: pass, nonce: salt, parallelism: p.parallelism, tagLength: 32, memory: p.memory, passes: p.passes },
        (err, key) => (err ? reject(err) : resolve(key))
      );
    });
  }
  throw new Error("E_UNSUPPORTED kdf");
}

function aeadOpen(key, nonce, blob, aad) {
  const tag = blob.subarray(blob.length - 16);
  const ct = blob.subarray(0, blob.length - 16);
  const d = crypto.createDecipheriv("aes-256-gcm", key, nonce, { authTagLength: 16 });
  d.setAAD(aad);
  d.setAuthTag(tag);
  return Buffer.concat([d.update(ct), d.final()]);
}

function wrapAad(id) {
  const pre = Buffer.concat([MAGIC, Buffer.from([0, 1])]);
  return Buffer.concat([pre, Buffer.from("|wrap|" + id, "ascii")]);
}

function payloadAad(header) {
  const pre = Buffer.concat([MAGIC, Buffer.from([0, 1])]);
  const text = "|payload|" + header.container_id + "|" + header.content.salt + "|" + header.persona.id;
  return Buffer.concat([pre, Buffer.from(text, "ascii")]);
}

function unzip(buf) {
  let eocd = -1;
  for (let i = buf.length - 22; i >= 0; i--) {
    if (buf.readUInt32LE(i) === 0x06054b50) {
      eocd = i;
      break;
    }
  }
  if (eocd < 0) throw new Error("E_INTEGRITY zip eocd");
  const count = buf.readUInt16LE(eocd + 10);
  let off = buf.readUInt32LE(eocd + 16);
  const files = {};
  for (let i = 0; i < count; i++) {
    if (buf.readUInt32LE(off) !== 0x02014b50) throw new Error("E_INTEGRITY zip cd");
    const method = buf.readUInt16LE(off + 10);
    const compSize = buf.readUInt32LE(off + 20);
    const nameLen = buf.readUInt16LE(off + 28);
    const extraLen = buf.readUInt16LE(off + 30);
    const commentLen = buf.readUInt16LE(off + 32);
    const localOff = buf.readUInt32LE(off + 42);
    const name = buf.subarray(off + 46, off + 46 + nameLen).toString("utf8");
    const lName = buf.readUInt16LE(localOff + 26);
    const lExtra = buf.readUInt16LE(localOff + 28);
    const start = localOff + 30 + lName + lExtra;
    const comp = buf.subarray(start, start + compSize);
    files[name] = method === 0 ? Buffer.from(comp) : zlib.inflateRawSync(comp);
    off += 46 + nameLen + extraLen + commentLen;
  }
  return files;
}

async function main() {
  const req = JSON.parse(fs.readFileSync(0, "utf8"));
  const raw = fs.readFileSync(req.container);
  const { header, payload } = parse(raw);
  const kek = await deriveKek(header, req.passphrase);

  const wrap = (header.wraps || []).find((w) => w.id === "passphrase");
  if (!wrap) throw new Error("E_UNLOCK no wrap");
  const ks = aeadOpen(kek, b64d(wrap.nonce), b64d(wrap.ct), wrapAad("passphrase"));

  const contentSalt = b64d(header.content.salt);
  const hdrBody = { ...header };
  delete hdrBody.mac;
  const hdrKey = Buffer.from(
    crypto.hkdfSync("sha256", ks, contentSalt, Buffer.from("syspersona/v1/header"), 32)
  );
  const mac = crypto.createHmac("sha256", hdrKey).update(Buffer.from(canon(hdrBody), "utf8")).digest();
  if (!crypto.timingSafeEqual(mac, b64d(header.mac))) throw new Error("E_INTEGRITY header");

  const vk = Buffer.from(
    crypto.hkdfSync("sha256", ks, contentSalt, Buffer.from("syspersona/v1/content"), 32)
  );
  const plain = aeadOpen(vk, b64d(header.content.nonce), payload, payloadAad(header));
  const files = unzip(plain);
  const manifest = JSON.parse(files["manifest.json"].toString("utf8"));

  for (const entry of manifest.files) {
    const blob = files[entry.path];
    const sha = blob ? crypto.createHash("sha256").update(blob).digest("hex") : null;
    if (sha !== entry.sha256) throw new Error("E_INTEGRITY file " + entry.path);
  }

  const kernel = JSON.parse((files["persona/kernel.json"] || files["persona.json"]).toString("utf8"));
  const subset = {};
  for (const k of ["schema", "id", "version", "kernel"]) if (k in kernel) subset[k] = kernel[k];
  const body = Buffer.from(canon(subset), "utf8");
  if (crypto.createHash("sha256").update(body).digest("hex") !== manifest.kernel.sha256) {
    throw new Error("E_SIGNATURE kernel hash");
  }
  const kernelKey = Buffer.from(
    crypto.hkdfSync("sha256", ks, contentSalt, Buffer.from("syspersona/v1/kernel"), 32)
  );
  const sig = crypto.createHmac("sha256", kernelKey).update(body).digest();
  if (!crypto.timingSafeEqual(sig, b64d(manifest.kernel.sig.value))) throw new Error("E_SIGNATURE kernel");

  process.stdout.write(
    JSON.stringify({
      ok: true,
      persona_id: manifest.persona_id,
      kernel_sha256: manifest.kernel.sha256,
      files: manifest.files.map((f) => ({ path: f.path, size: f.size, sha256: f.sha256 })),
    })
  );
}

main().catch((err) => {
  process.stdout.write(JSON.stringify({ ok: false, error: String(err.message || err) }));
  process.exitCode = 1;
});
