#!/usr/bin/env python3
"""从 Mozilla 的 certdata.txt 生成 PEM 根证书包。

    make-ca-bundle.py <certdata.txt> <输出>

── 为什么自己写 ────────────────────────────────────────────────────────────

「从 Mozilla 的列表现场生成」这件事需要三个能力：解析 certdata.txt、判断
哪些证书受信任、把 DER 转成 PEM。它们都不复杂（base64 是标准库），但规则
里有几个地方容易做错，所以这个脚本尽量贴着 Debian 的 certdata2pem.py 来
——那是这份格式的权威处理方式之一。

── certdata.txt 长什么样 ──────────────────────────────────────────────────

一份 1.4 MB 的文本，由若干「对象」组成，对象之间用空行分隔：

    CKA_CLASS CK_OBJECT_CLASS CKO_CERTIFICATE
    CKA_LABEL UTF8 "GlobalSign Root CA"
    CKA_VALUE MULTILINE_OCTAL
    \\060\\202\\003\\165\\060\\202\\002\\135...
    END

    CKA_CLASS CK_OBJECT_CLASS CKO_NSS_TRUST
    CKA_LABEL UTF8 "GlobalSign Root CA"
    CKA_TRUST_SERVER_AUTH CK_TRUST CKT_NSS_TRUSTED_DELEGATOR

★ 证书和它的信任标记是【两个独立的对象】，靠 CKA_LABEL 关联。所以要两遍：
  第一遍建信任表，第二遍输出。

★ CKA_VALUE 是 DER，用**八进制转义**写着（\\060 是 '0' 的八进制）。要按字节解。

── 判断规则（照 Debian 的 certdata2pem.py）────────────────────────────────

    只看 CKA_TRUST_SERVER_AUTH
      == CKT_NSS_TRUSTED_DELEGATOR  → 收
      == CKT_NSS_NOT_TRUSTED        → 不收，而且【要报出来】
      == 别的（或者没有这个字段）    → 不收

★ 为什么只看 SERVER_AUTH：我们生成的这份是给 TLS 客户端用的（apk 装包、
  curl 取东西）。邮件签名、代码签名那些信任是别的用途，混进来只会让
  「这个证书为什么在这儿」变得说不清。

★ CKT_NSS_NOT_TRUSTED 要报出来，是因为它意味着「Mozilla 明确不信任它」，
  和「这张证书没被标记为受信任」不是一回事。前者值得人看一眼。
"""

import base64
import re
import sys

# 一个对象里的字段：KEY 后面跟值。MULTILINE_OCTAL 的值跨多行，到 END 为止。
FIELD = re.compile(r"^(CKA_[A-Z_]+)\s+(.*)$")

# 八进制转义：\060 是三位八进制
OCTAL = re.compile(r"\\([0-7]{3})")


def parse(text: str) -> list[dict]:
    """把 certdata.txt 解析成对象列表。

    每个对象是一个 dict：字段名 → 值。
    MULTILINE_OCTAL 的字段解成 bytes，其余是 str。
    """
    objects: list[dict] = []
    current: dict = {}
    key = None          # 正在读的 MULTILINE_OCTAL 字段名
    chunks: list[str] = []

    for raw in text.splitlines():
        line = raw.rstrip("\n")

        if key is not None:
            # 正在读多行值
            if line == "END":
                current[key] = _octal_to_bytes("".join(chunks))
                key, chunks = None, []
            else:
                chunks.append(line)
            continue

        if not line.strip():
            # 空行 = 对象结束
            if current:
                objects.append(current)
                current = {}
            continue

        if line.startswith("#"):
            continue

        m = FIELD.match(line)
        if not m:
            continue

        name, value = m.group(1), m.group(2)
        if value == "MULTILINE_OCTAL":
            key, chunks = name, []
        else:
            current[name] = _normalize(value)

    if current:
        objects.append(current)

    return objects


def _normalize(value: str) -> str:
    """把字段值规整成「我们真正要比对的那个词」。

    ★ certdata.txt 的值带【类型前缀】，这一点很容易看漏：

        CKA_CLASS              CK_OBJECT_CLASS  CKO_CERTIFICATE
        CKA_TRUST_SERVER_AUTH  CK_TRUST         CKT_NSS_TRUSTED_DELEGATOR

    第一版我直接拿整串跟 "CKO_CERTIFICATE" 比，结果一张证书都没解析出来
    （报「证书 0 张」），因为实际值是 "CK_OBJECT_CLASS CKO_CERTIFICATE"。

    所以：带 UTF8 的取引号里的内容，其余取【最后一个词】。
    """
    if value.startswith("UTF8 "):
        value = value[5:].strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            return value[1:-1]
    parts = value.split()
    return parts[-1] if parts else value


def _octal_to_bytes(s: str) -> bytes:
    """解八进制转义。行与行之间没有分隔符，直接接起来。"""
    out = bytearray()
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 3 < len(s) and s[i + 1 : i + 4].isdigit():
            out.append(int(s[i + 1 : i + 4], 8))
            i += 4
        else:
            # 理论上不会走到这儿，但真出现了就按原字节收，不静默丢
            out.extend(s[i].encode("utf-8", "surrogateescape"))
            i += 1
    return bytes(out)


def trusted_labels(objects: list[dict]) -> tuple[set[str], list[str]]:
    """返回（受信任的 label 集合，明确不受信任的 label 列表）。"""
    trust: set[str] = set()
    untrusted: list[str] = []

    for obj in objects:
        if obj.get("CKA_CLASS") != "CKO_NSS_TRUST":
            continue
        label = obj.get("CKA_LABEL")
        if label is None:
            continue

        server_auth = obj.get("CKA_TRUST_SERVER_AUTH")
        if server_auth == "CKT_NSS_TRUSTED_DELEGATOR":
            trust.add(label)
        elif server_auth == "CKT_NSS_NOT_TRUSTED":
            untrusted.append(label)

    return trust, untrusted


def to_pem(der: bytes) -> str:
    """DER → PEM。每 64 字符一行，是 RFC 7468 的规定。"""
    b64 = base64.b64encode(der).decode("ascii")
    lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
    return (
        "-----BEGIN CERTIFICATE-----\n"
        + "\n".join(lines)
        + "\n-----END CERTIFICATE-----\n"
    )


def fingerprint(der: bytes) -> str:
    """SHA-256 指纹，给人读的十六进制。"""
    import hashlib

    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i : i + 2] for i in range(0, len(h), 2))


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2

    src, dest = argv[1], argv[2]
    with open(src, encoding="utf-8", errors="replace") as fh:
        objects = parse(fh.read())

    trust, untrusted = trusted_labels(objects)

    certs = [o for o in objects if o.get("CKA_CLASS") == "CKO_CERTIFICATE"]
    print(f"  证书 {len(certs)} 张 ✗ 受信任的标记 {len(trust)} 个")

    if untrusted:
        print()
        print("  ★ 以下证书被 Mozilla 【明确标记为不受信任】:")
        for label in untrusted:
            print(f"      {label}")

    # ★ 去重按【DER 内容】。同一张证书可能有多个 label（交叉签名之类），
    #   按 label 去重会让它在包里出现两次。
    seen: set[bytes] = set()
    chosen: list[tuple[str, bytes]] = []
    for obj in certs:
        label = obj.get("CKA_LABEL")
        der = obj.get("CKA_VALUE")
        if not isinstance(der, bytes) or not der:
            continue
        if label not in trust:
            continue
        if der in seen:
            continue
        seen.add(der)
        chosen.append((label, der))

    # ★ 排序。certdata.txt 的顺序是 Mozilla 的维护顺序，会随版本变动；按
    #   label 排能让同样的输入产出同样的字节——不然证书集没变、包却变了。
    chosen.sort(key=lambda item: item[0])

    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(
            "# 由 YojigenOS 的 ca-certificates 配方从 Mozilla 的 certdata.txt 生成。\n"
            "# 来源: security/nss/lib/ckfw/builtins/certdata.txt\n"
            "# 只收 CKA_TRUST_SERVER_AUTH = CKT_NSS_TRUSTED_DELEGATOR 的证书。\n"
            "#\n"
            "# 不要手工编辑这个文件——它是构建产物。\n"
        )
        for label, der in chosen:
            fh.write(f"\n# {label}\n")
            fh.write(to_pem(der))

    total = sum(len(der) for _, der in chosen)
    print(f"  输出 {len(chosen)} 张 ✗ {total} 字节 DER")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
