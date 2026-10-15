"""把一个架构的所有 .apk 收成一个 apk 软件仓库。

    skirt repo --arch x86_64 [--sign-key ~/.yojigenos/keys/<密钥>.rsa]

产出：
    var/repo/<arch>/APKINDEX.tar.gz      ← 索引（唯一的"特殊"文件）
    var/repo/<arch>/*.apk                ← 所有已发布的包

── 一个 apk 仓库【不是一个服务】────────────────────────────────────────

它就是【一个目录 + 一个索引文件】。没有数据库、没有要跑的程序、没有 API。
所以任何能发静态文件的托管都行：nginx / Cloudflare R2 / S3 / 普通 web 目录。

    repo.os.yojigen.cn/
    ├── aarch64/
    │   ├── APKINDEX.tar.gz
    │   ├── hello-2.12.3-r1.apk
    │   └── …（每个包每个版本一份）
    └── x86_64/
        └── 同上

── 仓库 URL 和 pkgname-spec 的配合（踩过）──────────────────────────────

客户端那边的写法是【仓库根】，apk 自己会加架构那层：

    --repository https://repo.os.yojigen.cn
      → 它去拿 https://repo.os.yojigen.cn/aarch64/APKINDEX.tar.gz

所以 `--pkgname-spec` 要【带】架构前缀——它是相对于仓库根的：

    --pkgname-spec '<架构>/${name}-${version}.apk'   ← 架构跟着 --arch 走

两边都带就变成 aarch64/aarch64/… （实测 404），两边都不带也拿不到包。

★ 而它里面那两个 `${…}` 必须是【字面量】，由 mkndx 自己替换。上一版是在
  容器里跑 `sh -c`，所以要把它们夹在单引号里给那个 sh 看 —— 一层套一层，
  随时可能被谁展开一次（展开了的后果是**索引里存的下载路径全错**，而那要等到
  客户端装不上才现形）。现在整条命令走 argv（`docker run … apk mkndx …`），
  根本没有 shell 参与，那个坑从结构上没了。

── 为什么不用 sync --delete ─────────────────────────────────────────────

包仓库是【只增不减】的：manifest 钉住版本，半年后还要能按同样的版本重建。
所以上传用 `rclone copy`，不能用 `rclone sync`（那会删掉旧版本）。
（上传那一步在 scripts/deploy.sh，不在这个模块里。）

── 它是从 shell 搬过来的（2026-10-15）──────────────────────────────────

原来是 `scripts/repo.sh`（220 行）。搬的理由和其它两个一样：**判定要能测**。

★ 而这一搬顺带消掉了两个坑（都在原来的注释里记着）：

  ① 「检查【产物】而不是退出码」—— 原来那条 mkndx 是
     `docker run … | grep … | sed …`，管道的退出码是最后那个 sed 的，所以
     mkndx 说"14 errors, not creating index"时脚本照样打了个 ✓。
     **现在没有管道**：subprocess 直接拿 mkndx 的退出码，而且照旧再查一遍
     产物（退出码 0 不等于产出了东西，两条都要）。

  ② 那对单引号（见上）—— 现在没有 shell，不需要给谁看引号。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .common import ROOT, die

#: 索引文件名。名字叫 .tar.gz，而里面是 ADB 格式（apk v3）——不是 gzip tar。
INDEX_NAME = "APKINDEX.tar.gz"

#: 签名记号（给 deploy.sh 看的）。它记的是"用哪个密钥签的"。
SIGNED_MARK = ".repo-signed"

#: mkndx 的一条已知警告：没有 datahash 的包。它不影响索引，不要刷屏。
_KNOWN_WARNING = re.compile(r"^\s*warning: support for packages without datahash")

#: 容器回报用的行前缀
_MARK_INDEX = "@@index"
_MARK_PKG = "@@pkg\t"


# ------------------------------------------------------------------ 收拢

def apk_files(directory: Path) -> list[Path]:
    """目录里的 .apk，按名字排序（= 原来那个 `*.apk` 展开的顺序）。"""
    return sorted(directory.glob("*.apk"))


def stage(pkgs: list[Path], repo_dir: Path) -> list[Path]:
    """把 .apk 复制进仓库目录。

    ★ 用 cp 而不是 mv —— var/pkgs 是"打包的产物"，var/repo 是"要发布的东西"，
      两者是不同的东西，repo 可以从 pkgs 重新生成。
    """
    out = []
    for apk in pkgs:
        dest = repo_dir / apk.name
        if apk.resolve() != dest.resolve():
            shutil.copyfile(apk, dest)
        out.append(dest)
    return out


# ------------------------------------------------------------------ 线上已有的包

#: 拉回线上包要用的三个环境变量（都在 CI 的 secret 里）
S3_VARS = ("S3_BASE_URL", "S3_BUCKET", "S3_AK")


def has_s3_credentials(env: dict | None = None) -> bool:
    """有没有 S3 凭据。没有就是本地开发——照旧跳过拉取。"""
    env = os.environ if env is None else env
    return all(env.get(k) for k in S3_VARS)


def sync_from_s3(repo_dir: Path, arch: str, env: dict | None = None) -> bool:
    """把【线上已有的包】拉回 repo_dir。返回是否真的拉过。

    ★ 为什么必须做。

    CI 每次都是干净的工作区，var/pkgs 里只有【这次打的包】。直接 mkndx 的话，
    生成的索引只列这次的包——传上去就会把之前发布的包从索引里抹掉。

    包文件还在线上，但 apk 只能看到索引里列的包，所以它们等于消失了。

    ★ 而且 apk 没有别的办法：mkndx 的 `--index` 只是"复用元数据缓存来加速"，
      实测它连已有索引里的条目都认不出来（建一个 2 项的索引，用 --index 读它，
      报的是 "0 packages"）。它也没有"往索引里追加"的子命令。

    所以只能把包文件凑齐——索引是从包文件生成的，就绕不开这一步。

    ★ 只拉 .apk，不拉索引：索引要重新生成（它得包含这次的包）。

    ★ 默认就做，不设开关。原来没有这一步，是因为"CI 是干净的"这件事被忽略了；
      做成可选的话，忘了加开关就又是一次事故。没有凭据（本地开发）时自动跳过。

    ★ `--user`：rclone 必须以【宿主的 uid/gid】跑。

      否则它拉下来的文件属主是 root，而调用方（CI 的 runner 用户）不是
      root——下一步的 `cp "$PKGS"/*.apk "$REPO/"` 要覆盖这些文件，会直接
      Permission denied：

          cp: cannot create regular file '.../filesystem-1.0-r1.apk': Permission denied

      ★ 这个坑只在【线上已经有同名包】时才会踩到：第一次发某个包时 var/repo
        里没有它，cp 是新建，root 属主无所谓；第二次发同一个包（或者线上有、
        本地也要有）才需要覆盖，那时才炸。实测就是：发 sed 那次过了，发 base
        这次挂在 filesystem/glibc/sed。
    """
    env = os.environ if env is None else env
    if not has_s3_credentials(env):
        print("  （没有 S3 凭据，跳过拉取线上——本地开发时正常）")
        return False

    print()
    print("  拉回线上已有的包…")
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{repo_dir}:/repo",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", "RCLONE_CONFIG_YOJIGEN_TYPE=s3",
        "-e", "RCLONE_CONFIG_YOJIGEN_PROVIDER=Other",
        "-e", f"RCLONE_CONFIG_YOJIGEN_ENDPOINT={env['S3_BASE_URL']}",
        "-e", f"RCLONE_CONFIG_YOJIGEN_ACCESS_KEY_ID={env['S3_AK']}",
        "-e", f"RCLONE_CONFIG_YOJIGEN_SECRET_ACCESS_KEY={env.get('S3_SK', '')}",
        "-e", "RCLONE_CONFIG_YOJIGEN_FORCE_PATH_STYLE=true",
        "-e", "RCLONE_CONFIG_YOJIGEN_REGION=us-east-1",
        "rclone/rclone", "copy", f"yojigen:{env['S3_BUCKET']}/{arch}", "/repo",
        "--include", "*.apk", "--no-traverse",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        die("没有 docker —— 拉回线上的包要靠容器里的 rclone")

    for line in proc.stdout.splitlines() + proc.stderr.splitlines():
        if line.strip() and "NOTICE:" not in line:
            print(f"    {line}")
    if proc.returncode != 0:
        # 原来这里是 `|| true`：拉不动也继续。继续是对的（本地开发、或者线上
        # 还没有东西），但**不能不说**——那正是"上一次传上去的包可能被抹掉"
        # 那个事故的形状。
        print(f"    ★ rclone 退出码 {proc.returncode} —— 线上那份可能没拉全，"
              f"建出来的索引就可能少包（照旧继续了）")
    return True


# ------------------------------------------------------------------ 索引

def public_key(sign_key: Path, dest_dir: Path) -> Path:
    """公钥是【现场从私钥推导】的，不存副本——一个源头，不会漂移。

    为什么需要它：mkndx 建索引时会【逐个验证】它索引的包，不信任就拒绝：

        ERROR: x.apk: UNTRUSTED signature
        ERROR: 1 errors, not creating index

    所以 `--keys-dir` 指的目录里必须有对应的公钥。
    """
    dest = dest_dir / (sign_key.name + ".pub")
    try:
        proc = subprocess.run(
            ["openssl", "rsa", "-in", str(sign_key), "-pubout", "-out", str(dest)],
            capture_output=True, text=True)
    except FileNotFoundError:
        die("没有 openssl —— 签名要现场从私钥推导公钥")
    if proc.returncode != 0 or not dest.is_file():
        die(f"从私钥导出公钥失败。是有效的 RSA 私钥吗？\n       {proc.stderr.strip()}")
    return dest


def make_index(repo_dir: Path, arch: str, *, desc: str,
               sign_key: Path | None = None, keys_dir: Path | None = None,
               image: str = "alpine:latest") -> Path:
    """在容器里 `apk mkndx` 建索引。返回索引文件。

    ★ 没有管道、没有 shell：

        · 没有 shell  → `${name}`/`${version}` 不需要给谁看引号（见模块顶上 ★②）
        · 没有管道    → mkndx 的退出码就是 docker 的退出码（见 ★①）

    ★ 而退出码之外还要查【产物】：退出码 0 不等于产出了东西。两样都查。
    """
    index = repo_dir / INDEX_NAME
    index.unlink(missing_ok=True)     # 原来的 `rm -f APKINDEX.tar.gz`

    names = [p.name for p in apk_files(repo_dir)]
    if not names:
        die(f"{repo_dir} 里没有 .apk，没法建索引")

    cmd = ["docker", "run", "--rm", "--workdir", "/repo",
           "-v", f"{repo_dir}:/repo"]
    if sign_key is not None and keys_dir is not None:
        cmd += ["-v", f"{sign_key}:/key.rsa:ro", "-v", f"{keys_dir}:/keys:ro"]
        sign_args = ["--sign-key", "/key.rsa", "--keys-dir", "/keys"]
    else:
        sign_args = ["--allow-untrusted"]
    cmd += [image, "apk", "mkndx", *sign_args,
            "--pkgname-spec", f"{arch}/${{name}}-${{version}}.apk",
            "--description", desc,
            "--output", INDEX_NAME, *names]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        die("没有 docker —— 建索引要靠容器里的 apk mkndx（宿主上没有 apk）")

    for line in (proc.stdout + proc.stderr).splitlines():
        if line.strip() and not _KNOWN_WARNING.search(line):
            print(f"  {line}")

    # ★ 两样都查：退出码，和产物。
    #
    #   上面那句不是多余的——原来的脚本就是"打出一个 ✓ 而索引根本没生成"过：
    #   管道里 grep/sed 把 mkndx 的退出码吃掉了（`14 errors, not creating index`
    #   之后照样打 ✓）。这里没有管道，所以退出码是真的；但"退出码是 0"仍然
    #   不等于"产出了东西"，所以再加一道产物的检查。
    if proc.returncode != 0:
        die(f"mkndx 退出码 {proc.returncode}（失败原因在上面那几行里）\n"
            f"       最常见的是包没签名——pack.sh 要带 --sign-key。")
    if not index.is_file() or index.stat().st_size == 0:
        die("索引没有生成。上面那些 ERROR 就是原因。\n"
            "       最常见的是包没签名——pack.sh 要带 --sign-key。")
    return index


def mark_signed(root: Path, arch: str, key_name: str) -> Path:
    """留个记号给 deploy.sh：这个架构的索引是用哪个密钥签的。

    ★ 为什么不让 deploy.sh 自己去看索引：索引是 ADB 格式，不是 gzip tar，
      `tar -tzf` 什么都解不出来；而里面的签名标记没有可 grep 的原始字节
      （`# sig v00 …` 是 apk adbdump 的【渲染】，不是文件里的字符串；要读它
      就只能起容器）。
      deploy.sh 原来查的是 "$SRC/yojigenos.rsa.pub"——那个文件从来不存在，
      于是它永远报「没有签名」，而实际是签了的。

    与其在部署那边重新推导一遍"签没签"，不如让【签名的那一方】记下来。
    放在 var/ 而不是 var/repo/：后者会被 rclone 整个传上去。
    """
    path = root / "var" / f"{SIGNED_MARK}-{arch}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key_name + "\n", encoding="utf-8")
    return path


def unmark_signed(root: Path, arch: str) -> None:
    (root / "var" / f"{SIGNED_MARK}-{arch}").unlink(missing_ok=True)


# ------------------------------------------------------------------ 读回来（查产物）

def parse_index_dump(text: str) -> list[tuple[str, str]]:
    """`apk adbdump APKINDEX.tar.gz` 的渲染 → [(名字, 版本), …]。

    那个渲染长这样（节选自真的索引）：

        packages: # 60 items
          - name: apk-tools
            version: 3.0.8-r1
            …

    ★ 只在 `packages:` 这一段里找，而且认死那两行的缩进。写错的后果是**这条
      检查会报错**（而不是静默通过）——因为下面找不到条目时抛 ValueError，
      调用方把它翻成"看不懂 apk 的输出"。宁可炸也不要"检查了个寂寞"。
    """
    entries: list[tuple[str, str]] = []
    in_packages = False
    name: str | None = None
    for line in text.splitlines():
        if not line.startswith((" ", "\t")):
            in_packages = line.startswith("packages:")
            continue
        if not in_packages:
            continue
        m = re.match(r"^  - name: (\S+)\s*$", line)
        if m:
            name = m.group(1)
            continue
        m = re.match(r"^    version: (\S+)\s*$", line)
        if m and name is not None:
            entries.append((name, m.group(1)))
            name = None
    if not entries:
        raise ValueError("这段 adbdump 输出里读不出 packages 条目（格式变了？）")
    return entries


def parse_package_dump(text: str) -> tuple[str, str] | None:
    """`apk adbdump <某个 .apk>` 的渲染 → (名字, 版本)，读不出就 None。

    包的那份长这样：

        info:
          name: hello
          version: 1.0-r0
          …
        paths: # 3 items
          - name: usr        ← ★ 注意：paths 里也有 name，缩进不同

    所以这里认死 `info:` 开头、两个空格缩进的那两行（paths 里的 name 要么带
    `- `、要么缩进更深）。
    """
    in_info = False
    name = version = None
    for line in text.splitlines():
        if not line.startswith((" ", "\t")):
            in_info = line.startswith("info:")
            continue
        if not in_info:
            continue
        m = re.match(r"^  name: (\S+)\s*$", line)
        if m:
            name = m.group(1)
            continue
        m = re.match(r"^  version: (\S+)\s*$", line)
        if m:
            version = m.group(1)
    if name is None or version is None:
        return None
    return (name, version)


def _dump(repo_dir: Path, image: str = "alpine:latest") -> str:
    """容器里把索引和每个包的 name/version 渲染出来。一次容器，一把读完。

    ★ 每个包只取前 20 行（info 那段在最前面），不把整份 paths 拖回来 ——
      gcc 那种包的 paths 有几十万行。

    ★ `< /dev/null` 不是多余的：`for` 循环里只要有谁读了 stdin，剩下的就没了。
    """
    script = (
        f"echo '{_MARK_INDEX}'\n"
        f"apk adbdump /repo/{INDEX_NAME} </dev/null 2>&1\n"
        "for f in /repo/*.apk; do\n"
        "    printf '@@pkg\\t%s\\n' \"$(basename \"$f\")\"\n"
        "    apk adbdump \"$f\" </dev/null 2>&1 | head -20\n"
        "done\n")
    with tempfile.TemporaryDirectory(prefix="skirt-repo-") as td:
        script_file = Path(td) / "dump.sh"
        script_file.write_text(script, encoding="utf-8")
        try:
            proc = subprocess.run(
                ["docker", "run", "--rm", "-v", f"{repo_dir}:/repo:ro",
                 "-v", f"{td}:/work:ro", image, "sh", "/work/dump.sh"],
                capture_output=True, text=True)
        except FileNotFoundError:
            die("没有 docker —— 读索引要靠容器里的 apk adbdump")
    if proc.returncode != 0:
        die(f"读索引失败（容器退出码 {proc.returncode}）：\n{proc.stdout}{proc.stderr}")
    return proc.stdout


def _split_report(text: str) -> tuple[str, dict[str, str]]:
    """把 `_dump()` 的回报拆成 (索引那段的文本, {包文件名: 它的那段文本})。"""
    index_text, _, rest = text.partition(_MARK_PKG)
    index_text = index_text.replace(_MARK_INDEX, "", 1)
    per_file: dict[str, str] = {}
    for chunk in rest.split(_MARK_PKG):
        if not chunk.strip():
            continue
        name, _, body = chunk.partition("\n")
        per_file[name.strip()] = body
    return index_text, per_file


def diff_index(entries: list[tuple[str, str]],
               identities: dict[str, tuple[str, str] | None]) -> list[str]:
    """索引条目 vs 仓库里的包 → 问题列表（纯函数，**不碰容器**）。

    ★ 查什么、为什么值得查：

       索引里列了、而仓库里没有那个包   → 客户端会 404
       仓库里有、而索引里没列           → 那个包等于没发布
                                          （"传上去把之前的包抹掉"就是这一类的反面）

      这是"检查产物"那条规矩的延长线：`-s APKINDEX.tar.gz` 只说明文件非空，
      说明不了它有没有把这次的包都收进去。

    ★ 比的是 (名字, 版本) 对，不是"个数"：索引能列出别的版本的同一个包，
      而"数目对了内容不对"正是这条要拦的东西。读不出身份的那些（ident 是 None）
      由调用方报，这里跳过。
    """
    problems: list[str] = []
    listed = set(entries)
    for fname, ident in sorted(identities.items()):
        if ident is None:
            continue
        if ident not in listed:
            problems.append(f"{fname}: 没进索引（索引里没有 {ident[0]} {ident[1]}）")
    for name, version in entries:
        if (name, version) not in set(identities.values()):
            problems.append(f"索引里列了 {name} {version}，而仓库里没有这个包")
    return problems


def check_index(repo_dir: Path, image: str = "alpine:latest"
                ) -> tuple[list[tuple[str, str]], dict[str, tuple[str, str] | None],
                           list[str]]:
    """把索引和仓库里的包【对一遍】。返回 (索引条目, 每个包的身份, 问题列表)。"""
    text = _dump(repo_dir, image)
    index_text, per_file = _split_report(text)

    problems: list[str] = []

    try:
        entries = parse_index_dump(index_text)
    except ValueError as exc:
        die(f"看不懂索引的渲染：{exc}\n"
            f"       （apk 换了 adbdump 的格式？下面是它给的前几行）\n"
            + "\n".join(f"       {l}" for l in index_text.splitlines()[:6]))

    identities: dict[str, tuple[str, str] | None] = {}
    for fname, body in sorted(per_file.items()):
        identities[fname] = parse_package_dump(body)
        if identities[fname] is None:
            problems.append(f"{fname}: 读不出它的 name/version"
                            f"（apk adbdump 的输出变了？）")

    problems += diff_index(entries, identities)
    return entries, identities, problems


# ------------------------------------------------------------------ 总入口

def build_index(*, arch: str, pkgs_dir: Path, repo_dir: Path, desc: str,
                sign_key: Path | None = None, sync: bool = True,
                image: str = "alpine:latest", root: Path = ROOT) -> Path:
    """收拢 → （可选）拉线上 → 建索引 → 查一遍。返回索引文件。

    ★ 不再问"要不要拉线上"：原来那一步做了就默认做，理由是忘了加开关就是
      一次事故（见 sync_from_s3）。没凭据时它自己跳过。
    """
    repo_dir.mkdir(parents=True, exist_ok=True)
    if sync:
        sync_from_s3(repo_dir, arch)

    pkgs = apk_files(pkgs_dir)
    stage(pkgs, repo_dir)
    all_here = apk_files(repo_dir)
    if sync:
        print(f"  建索引时会包含 {len(all_here)} 个包（线上的 + 这次的）")

    if sign_key is not None:
        with tempfile.TemporaryDirectory(prefix="skirt-repo-keys-") as kd:
            keys_dir = Path(kd)
            public_key(sign_key, keys_dir)
            print(f"  签名: {sign_key.name}  （公钥现场从它推导）")
            index = make_index(repo_dir, arch, desc=desc,
                               sign_key=sign_key, keys_dir=keys_dir, image=image)
        mark_signed(root, arch, sign_key.name)
    else:
        print("  ★ 不签名（本地开发用；装的时候要 --allow-untrusted）")
        print("     签名：scripts/keys.sh gen 然后 "
              "./skirt/skirt repo --sign-key ~/.yojigenos/keys/<密钥>.rsa")
        index = make_index(repo_dir, arch, desc=desc, image=image)
        unmark_signed(root, arch)

    entries, identities, problems = check_index(repo_dir, image)
    print()
    print(f"  索引里有 {len(entries)} 条 · 仓库里有 {len(identities)} 个包")
    if problems:
        for p in problems:
            print(f"    ✗ {p}")
        die(f"索引和仓库对不上（{len(problems)} 处）——上面那些就是")
    return index
