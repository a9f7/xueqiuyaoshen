#!/usr/bin/env python3
"""持仓数据加密 / 解密（纯标准库，无任何第三方依赖）。

算法（前后端必须保持一致，前端见 index.html 的 decryptHoldings）：
  1. salt = os.urandom(16)
  2. key  = PBKDF2-HMAC-SHA256(password, salt, rounds=200000, dklen=32)
  3. 密钥流 keystream = 拼接 SHA256(key || uint64_be(counter))，取前 len(plaintext) 字节
  4. ciphertext = plaintext XOR keystream
  5. 输出 { v, algo, rounds, salt(b16), ct(b64) }

安全说明：
  - 这是「防路人 / 防搜索引擎」级保密，不是银行级。密文与解密逻辑都公开，
    懂技术的人仍可能破解。目的是避免持仓被随手看到 / 被索引。
  - 若密码错误，解密得到乱码，JSON.parse 会失败 -> 前端据此提示「密码错误」。
"""
import os
import json
import base64
import hashlib

ROUNDS = 200000
DKLEN = 32


def _derive_key(password: str, salt: bytes, rounds: int = ROUNDS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds, dklen=DKLEN)


def _keystream(key: bytes, length: int) -> bytes:
    out = bytearray()
    c = 0
    while len(out) < length:
        out += hashlib.sha256(key + c.to_bytes(8, "big")).digest()
        c += 1
    return bytes(out[:length])


def encrypt_obj(obj, password: str) -> dict:
    """把任意 JSON-serializable 对象加密为可序列化的密文 blob。"""
    salt = os.urandom(16)
    key = _derive_key(password, salt)
    pt = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    ks = _keystream(key, len(pt))
    ct = bytes(p ^ k for p, k in zip(pt, ks))
    return {
        "v": 1,
        "algo": "pbkdf2-sha256-xor-keystream",
        "rounds": ROUNDS,
        "salt": base64.b16encode(salt).decode("ascii"),
        "ct": base64.b64encode(ct).decode("ascii"),
    }


def decrypt_obj(blob: dict, password: str):
    """解密 blob 为原对象；密码错误会抛 JSONDecodeError / UnicodeDecodeError。"""
    salt = base64.b16decode(blob["salt"])
    rounds = int(blob.get("rounds", ROUNDS))
    key = _derive_key(password, salt, rounds)
    ct = base64.b64decode(blob["ct"])
    ks = _keystream(key, len(ct))
    pt = bytes(c ^ k for c, k in zip(ct, ks))
    return json.loads(pt.decode("utf-8"))


if __name__ == "__main__":
    # 自检：加密再解密应还原；错误密码应失败
    sample = {"uid": 123, "items": [{"symbol": "SH600519", "profit": 0.1234}]}
    blob = encrypt_obj(sample, "test-pwd")
    assert decrypt_obj(blob, "test-pwd") == sample, "roundtrip failed"
    try:
        decrypt_obj(blob, "wrong")
        print("ERROR: wrong password should fail")
    except Exception:
        print("OK: wrong password rejected")
    print("self-check passed; blob keys:", list(blob.keys()))
