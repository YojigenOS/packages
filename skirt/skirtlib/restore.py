"""从 var/pkgs/<arch>/*.apk 把 payload 还原回 var/output/<arch>/。

    skirt restore --arch x86_64

── 为什么需要它 ────────────────────────────────────────────────────────────

建一个包要几十分钟，所以有几条路可以不重建就拿回它的产物：

    seed_run      从上一轮 CI 的 artifact 播种
    跳过          已经建过的（在 var/pkgs 里有 .apk）

**但它们都只拿回了 .apk，没有拿回 payload。**

于是：一个包【在仓库里】、`skirt build` 却报「依赖 X 还没有构建」——
因为 skirt 合并依赖时找的是 var/output/<arch>/<名字>/payload/，而那里是空的。

实测撞上的：

    ── 建 linux ──
    [解析] 依赖链: flex → bc → openssl → bzip2 → elfutils → linux
    skirt: 错误 依赖 openssl 还没有构建
           （找不到 var/output/x86_64/openssl-3.5.9-1/payload/）

openssl 明明在仓库里、就在 var/pkgs 里躺着——缺的只是那一步还原。

── 做法 ────────────────────────────────────────────────────────────────────

.apk 是 ADB 格式（apk v3 起），**不是 gzip tar**，`tar -tzf` 解不出来
（实测：0 条目）。只有 apk 自己解得开：

    apk extract --allow-untrusted <file.apk>

所以这里起一个容器跑它。容器里跑有两个原因：宿主（CI 是 Ubuntu）没有 apk；
而且不能污染宿主。

── 它是从 shell 搬过来的（2026-10-15）────────────────────────────────────

原来是 `scripts/restore-payloads.sh`（142 行 shell，逻辑一半在容器里的 heredoc）。
搬它的理由和 elf-deps 那次一样：**shell 里的判定测不到**。现在：

    · 名字 → 目录名、跳过判断、.buildkey 的落点 —— 全在 Python 里（可测）
    · 容器里只剩"解包"这一件事：读一张清单，逐个 `apk extract`，把结果报回来

★ 而容器里的清单和那行汇总都由 Python 生成/读取，容器不再自己做判断——
  上一版的两个坑（buildkey 的名字、buildkey 的位置）恰恰都在容器的 shell 里。

── 三次踩出来的东西（都留着）──────────────────────────────────────────────

① **`.buildkey` 的源头名字带 `.apk`**：`skirt pack` 写的是
   `<名字>-<版本>-r<release>.apk.buildkey` —— `.apk` 在【中间】。这里第一版
   写成了 `<basename 去掉 .apk>.buildkey`，于是两边差三个字符，找不到，而症状
   是"跳过判断说盘上没有 key"——**离成因很远**。

② **拷贝在"payload 已经在了就跳过"之前**：那些包恰恰是最该拿到 key 的一批
   （它们在 var/output 里已经有目录了）。

③ **容器以 root 跑**（apk 要 root），写出来的文件也归 root；而 skirt 以宿主
   用户跑，于是删不掉上一轮的产物：

       skirt: 错误 清理失败: var/output/x86_64/filesystem-1.0-2
              [Errno 13] Permission denied: '.../payload'

   所以跑完把属主改回宿主。用宿主自己的 uid/gid，不能写死——CI 上是
   runner(1001)，本机是别人。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .common import OUTPUT_DIR, PKGS_DIR, die

#: build_key 的旁路文件后缀。带 `.apk` 的完整文件名 + 它（见 ① 上面的解释）。
# ★ 这个名字【只有一份出处】——在 `pack.py` 里（2026-10-15 收的）。
#
#   这里原来自己写了一份 `.buildkey`，并自己拼 `<apk 名>.buildkey`。两份常量
#   漂开的表现正是 `pack.py` 注释里记着的那次事故：名字差几个字符 → 找不到 →
#   说"盘上没有 key" → 每次都全量重建。**而它是静默的。**
from .pack import BUILDKEY_SUFFIX, sidecar_name  # noqa: F401

#: 从 `<名字>-<版本>-r<发布号>.apk` 的 stem 里剥出名字。
#: 名字自己可能带连字符（glibc-dev），所以从右往左剥。
_NAME_RE = re.compile(r"-[^-]+-r[0-9]+$")

#: 兜底：名字到目录名，`-r<发布号>` → `-<发布号>`（目录名里没有那个 r）。
_RELEASE_RE = re.compile(r"-r([0-9]+)$")

#: 容器里解包的脚本。生成而不是写死：清单挂在 /work，由 Python 写。
#:
#: ★ 不用 `set -e`：一个包解不开要记下来继续解下一个（上一版就是这样）。
#:   `set -e` 会在第一个失败处退出，那样剩下的包一个都不会被还原。
#:
#: ★ `apk extract ... </dev/null` 那个重定向不是多余的：`while read` 的循环体里
#:   只要有谁读了 stdin，**剩下的清单就被它吃掉了**，循环提前结束，而症状是
#:   "后面那批包一个都没还原"。清单是喂给 read 的，别的命令都不该碰 stdin。
_SCRIPT = """\
# 由 skirt restore 生成。这里只做解包，判断都在 skirt 那边。
while IFS='\t' read -r rel apk; do
    [ -n "$rel" ] || continue
    dest="/out/$rel/payload"
    if ! mkdir -p "$dest"; then
        printf '@@restore\\t%s\\tfail\\n' "$rel"
        continue
    fi
    if (cd "$dest" && apk extract --allow-untrusted "/pkgs/$apk" </dev/null >/dev/null 2>&1); then
        printf '@@restore\\t%s\\tok\\n' "$rel"
    else
        rmdir "$dest" 2>/dev/null || true
        printf '@@restore\\t%s\\tfail\\n' "$rel"
        echo "    ✗ 解不开 $apk"
    fi
done < /work/list

# 属主还给宿主，否则 skirt 删不掉自己上一轮的产物（见模块顶上 ③）。
if [ -n "${HOST_UID:-}" ]; then
    chown -R "$HOST_UID:${HOST_GID:-$HOST_UID}" /out 2>/dev/null || true
fi
"""

#: 容器回报结果的行前缀
_MARK = "@@restore\t"


@dataclass
class Job:
    """一个 .apk 的还原计划。"""

    apk: Path            #: var/pkgs/<arch>/<...>.apk
    rel: str             #: 产物目录名 <名字>-<版本>-<发布号>
    buildkey: Path       #: 源 .buildkey（可能不存在）
    skip: bool           #: payload 已经在了 → 不解它
    guessed: bool = False  #: rel 是从文件名推的（配方表里没有这个名字）

    def payload(self, output_dir: Path) -> Path:
        return output_dir / self.rel / "payload"

    def dest(self, output_dir: Path) -> Path:
        """产物目录（payload 的父目录，.buildkey 也放这儿）。"""
        return output_dir / self.rel


# ------------------------------------------------------------------ 名字 → 目录

def recipe_versions() -> dict[str, str]:
    """「配方名 → <版本>-<发布号>」——目录名就是这么拼出来的。

    ★ 为什么问 skirt 自己，不从文件名剥。

    文件名是 `openssl-3.5.9-r1.apk`，而目录是 `openssl-3.5.9-1`——那个 `r`
    只出现在文件名里。用正则去掉它不是不行，但那是【复制一份 skirt 的命名
    规则】，两边迟早会漂。

    ★ **子包不在这张表里**，这不是漏了 —— 见 plan()：子包走文件名那条路，
      而那条路对子包是【更准】的一条。
    """
    from .spec import all_recipe_names, load_spec

    return {spec["package"]["name"]: _ver_rel(spec["package"])
            for spec in (load_spec(n) for n in all_recipe_names())}


def subpackage_names() -> set[str]:
    """所有子包名。它们【不进】版本表（见 plan()），但"认识"它们有用：
    一个是"名字不在任何配方里"那个警告要区分"子包"和"真的不认识"。"""
    from .spec import all_recipe_names, load_spec, subpackages

    out: set[str] = set()
    for name in all_recipe_names():
        for sub in subpackages(load_spec(name)):
            out.add(sub["name"])
    return out


def _ver_rel(pkg: dict) -> str:
    return f"{pkg['version']}-{pkg['release']}"


def plan(pkgs_dir: Path, output_dir: Path,
         versions: dict[str, str] | None = None,
         subpackages: set[str] | None = None) -> list[Job]:
    """给 pkgs_dir 里的每个 .apk 算一份还原计划。**不碰文件系统上的产物。**

    目录名有两条路：

      配方名（表里有）   → `<名字>-<表里的版本-发布号>`
      其余（子包、陌生名字）→ 从文件名推：`-<版本>-r<发布号>` → `-<版本>-<发布号>`

    ★ 而【子包为什么故意走第二条】，2026-10-15 对着真数据核过：

        var/pkgs 里同时躺着 libssl-3.5.9-r1.apk 和 libssl-3.5.9-r2.apk，
        而配方现在是 3.5.9-2。老脚本（也是这么写的）给 r1 推出的目录是
        libssl-3.5.9-1 —— 也就是**那个包自己那次构建的目录**，对。

        反过来，如果子包也走表，两个都会落到 libssl-3.5.9-2，于是"先到的
        赢"：r2 的目录里装进 r1 的 payload。**看起来正常，内容是旧的。**

      所以：子包从文件名推不是"退而求其次"，它是唯一能让每个版本各归各位的
      那条路（.apk 的文件名和产物目录名本来就是一回事 —— `skirt pack` 从
      meta.json 同时拼出这两个）。
    """
    if versions is None:
        versions = recipe_versions()
    if subpackages is None:
        subpackages = subpackage_names()

    旧版本: list[str] = []
    jobs: list[Job] = []
    for apk in sorted(pkgs_dir.glob("*.apk")):
        base = apk.name[: -len(".apk")]
        name = _NAME_RE.sub("", base)
        ver = versions.get(name)
        if ver is not None:
            # ★★★ 先问一句：这个 .apk 是不是【当前配方】的那个？
            #
            #   var/pkgs 里会同时躺着同一个包的 r1 / r2 / r3（跨轮的缓存就是
            #   这样）。而 `rel` 是从【配方】问来的，所以三个都会指向【配方当前
            #   那个版本】的目录 —— 后面按 rel 去重时只解第一个，而文件名排序
            #   下第一个是【版本最小】的那个。
            #
            #   2026-10-17 实测的后果：配方写 release=3，而还原解的是 r1 的
            #   payload，于是打包、发布出去的【都是 r1】。**升了 release 而
            #   不生效**，而每一处都报"成功"。
            #
            #   ★ 判据：文件名去掉 `-r` 之后该【正好等于】配方给的 `<版本>-<发布号>`。
            #     不等就是旧 release —— 跳过它，并且【说出来】（静默跳过等于没有）。
            stem_rel = _RELEASE_RE.sub(r"-\1", base[len(name) :].lstrip("-"))
            if stem_rel != ver:
                旧版本.append(f"{apk.name}（配方要 {ver}）")
                continue
            rel = f"{name}-{ver}"
            guessed = False
        else:
            rel = _RELEASE_RE.sub(r"-\1", base)
            # 子包走这条路是设计好的；其它名字走这条路是"猜"——要说出来。
            guessed = name not in subpackages
        jobs.append(Job(apk=apk, rel=rel,
                        buildkey=pkgs_dir / sidecar_name(apk.name),
                        skip=(output_dir / rel / "payload").is_dir(),
                        guessed=guessed))
    plan.旧版本 = 旧版本          # ★ 给调用方一句：跳过了哪些旧 release
    return jobs


# ------------------------------------------------------------------ 搬旁路文件

def _by_rel(jobs: list[Job]) -> dict[str, list[str]]:
    """目录名 → 哪些 .apk 指向它（按计划顺序）。多于一个就是撞了。"""
    out: dict[str, list[str]] = {}
    for job in jobs:
        out.setdefault(job.rel, []).append(job.apk.name)
    return out


def copy_buildkeys(jobs: list[Job], output_dir: Path) -> int:
    """把 `.buildkey` 拷进产物目录。返回拷了几个。

    ★ 对【所有】job 都做，包括那些 payload 已经在了的（跳过的那批）——
      这正是 ② 说的那个坑：跳过的那批最需要 key。
    """
    n = 0
    for job in jobs:
        if not job.buildkey.is_file():
            continue
        dest = job.dest(output_dir)
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(job.buildkey, dest / BUILDKEY_SUFFIX)
        n += 1
    return n


# ------------------------------------------------------------------ 解包

def extract_with_apk(jobs: list[Job], pkgs_dir: Path, output_dir: Path,
                     image: str = "alpine:latest") -> dict[str, str]:
    """在容器里 `apk extract`。返回 {目录名: "ok"|"fail"}。

    ★ 容器里【不装 apk-tools】。上一版有一行 `apk add --no-cache apk-tools`，
      而对 alpine:latest 实测是多余的：它自带的 apk 就是 3.0.8，`apk extract`
      直接能用（解 76 个包的那次实测）。去掉它的好处不只是快——**这一步不再
      需要网络**，而原来每次还原都要连 Alpine 的仓库。
    """
    with tempfile.TemporaryDirectory(prefix="skirt-restore-") as td:
        work = Path(td)
        (work / "list").write_text(
            "".join(f"{j.rel}\t{j.apk.name}\n" for j in jobs), encoding="utf-8")
        (work / "extract.sh").write_text(_SCRIPT, encoding="utf-8")

        cmd = ["docker", "run", "--rm",
               "-v", f"{pkgs_dir}:/pkgs:ro",
               "-v", f"{output_dir}:/out",
               "-v", f"{work}:/work:ro",
               "-e", f"HOST_UID={os.getuid()}",
               "-e", f"HOST_GID={os.getgid()}",
               image, "sh", "/work/extract.sh"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            die("没有 docker —— 解 .apk 只能靠容器里的 apk extract（宿主上没有 apk）")

        done: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            if line.startswith(_MARK):
                _, rel, verdict = line.split("\t")
                done[rel] = verdict
            else:
                print(f"  {line}")
        if proc.returncode != 0:
            print(proc.stderr.strip(), file=sys.stderr)
            die(f"容器里的还原失败了（退出码 {proc.returncode}）"
                f"——上面那段就是原因")
        return done


# ------------------------------------------------------------------ 总入口

def restore(arch: str, *, pkgs_dir: Path | None = None,
            output_dir: Path | None = None,
            versions: dict[str, str] | None = None,
            extractor=extract_with_apk) -> tuple[int, int, int]:
    """还原一个架构的 payload。返回 (还原了几个, 跳过几个, 失败几个)。

    ★ 汇总是 Python 打的，不是容器打的：跳过数只有在分别知道"哪些本来就在"
      和"哪些真的解出来了"之后才算得出来。

    ★ `extractor` 可以换掉 —— 测试用它跑完整条路（除了容器那一步）。
    """
    pkgs_dir = PKGS_DIR / arch if pkgs_dir is None else pkgs_dir
    output_dir = OUTPUT_DIR / arch if output_dir is None else output_dir

    if not pkgs_dir.is_dir():
        print(f"  没有 {pkgs_dir}，没什么可还原的")
        return (0, 0, 0)

    jobs = plan(pkgs_dir, output_dir, versions)
    if not jobs:
        print(f"  {pkgs_dir} 里没有 .apk")
        return (0, 0, 0)

    output_dir.mkdir(parents=True, exist_ok=True)

    # ── 一个目录名可能被两个 .apk 指向 ────────────────────────────────────
    #
    # 目录名是 `<名字>-<版本>-<发布号>`，而版本号是从【配方】问来的。所以
    # var/pkgs 里同时躺着同一个包的 r1 和 r2 时（缓存在跨运行时就是这样），
    # 两个都会指向【配方当前那个版本】的目录。
    #
    # 老脚本是在循环里撞见的：第二个包看到"目录已经在了"，于是计进【已有】。
    # 这里按同样的顺序去重——**只有第一个真的去解**，后面的算已有。
    # （顺序是文件名的排序，所以先解的是版本小的那个。这是老行为，不是新
    #   决定：它意味着那个目录里装的是【老版本】的 payload。★ 下面会把它说出来。）
    seen: set[str] = set()
    todo: list[Job] = []
    skip = 0
    for job in jobs:
        if job.skip or job.rel in seen:
            skip += 1
            continue
        seen.add(job.rel)
        todo.append(job)

    shared = {rel: names for rel, names in _by_rel(jobs).items() if len(names) > 1}

    print(f"  要还原 {len(jobs)} 个包（{arch}）")

    # ★ 旁路文件在【跳过之前】搬（见模块顶上 ②）。
    n_keys = copy_buildkeys(jobs, output_dir)

    done = extractor(todo, pkgs_dir, output_dir) if todo else {}
    ok = sum(1 for j in todo if done.get(j.rel) == "ok")
    fail = [j for j in todo if done.get(j.rel) != "ok"]

    # ★★★ 2026-10-17：解出来的 payload 里到底有几个文件。
    #
    #   为什么需要这一句。那一轮全量自举挂在"跑测试"，而报错是：
    #
    #       meson: devel 但既没有可执行文件，也没有头文件
    #
    #   而 restore 那边报的是「还原 101 个，失败 0 个」—— **两句话对不上**，
    #   中间没有任何线索说明"解出来了"和"里面有东西"是两件事。
    #
    #   `apk extract` 的退出码 0 只说明"这个 .apk 我读得懂"，不说明"解出了
    #   文件"。而空的 payload 会让【下游】报错，报错的地方离成因很远 ——
    #   那正是这个项目一直在修的那一类。
    #
    #   ★ 判据：.apk 大于 100 KB 而 payload 少于 3 个文件。真的有很小的包
    #     （filesystem 就是），所以这是【警告】不是失败 —— 但它必须被说出来。
    可疑 = []
    for j in todo:
        if done.get(j.rel) != "ok":
            continue
        payload = output_dir / j.rel / "payload"
        n = sum(1 for f in payload.rglob("*") if f.is_file() or f.is_symlink())
        if j.apk.stat().st_size > 100 * 1024 and n < 3:
            可疑.append(f"{j.rel}（{j.apk.stat().st_size // 1024} KB 的包只解出 {n} 个文件）")

    # 拿不到 key 的那批要说出来：跳过判断以后见不到 .buildkey，会把它们当
    # "不知道上次是什么样"（= 每次都重建）。而这是【正常的一半】：很老的
    # 产物本来就没有旁路文件。
    no_key = [j for j in jobs if not j.buildkey.is_file()]
    guessed = [j for j in jobs if j.guessed]

    print(f"  还原 {ok} 个，已有 {skip} 个，失败 {len(fail)} 个"
          f"（顺带取回 {n_keys} 份 .buildkey）")
    # ★★★ 2026-10-17：这一条以前是"只说一句就完了"——它会静静地用【旧版本】
    #   的 payload。现在 plan() 已经把旧 release 挡在外面（只处理配方要的那个），
    #   所以 `shared` 里剩下的同名是【另一回事】：子包、或者两个配方碰巧同名。
    #   而它仍然该说出来。
    if 可疑:
        print(f"  ★★ 有 {len(可疑)} 个包解出来的文件少得可疑 —— 那会让下游以"
              f"一个看不出原因的方式失败：")
        for line in 可疑[:6]:
            print(f"      {line}")
        if len(可疑) > 6:
            print(f"      …还有 {len(可疑) - 6} 个")
    if shared:
        for rel, names in sorted(shared.items()):
            print(f"  ★ {len(names)} 个包指向同一个目录 {rel}：{'、'.join(names)}"
                  f" —— 只还原了排在前面的那个")
    skipped_old = getattr(plan, "旧版本", [])
    if skipped_old:
        print(f"  ★ 跳过 {len(skipped_old)} 个【旧 release】（不覆盖产物，"
              f"也不是这一轮要的）：")
        for line in skipped_old[:5]:
            print(f"      {line}")
        if len(skipped_old) > 5:
            print(f"      …还有 {len(skipped_old) - 5} 个")
    if no_key:
        print(f"  ★ {len(no_key)} 个包没有 .buildkey（build_key 出现之前建的）"
              f"——跳过判断会当它们未知")
    if guessed:
        print(f"  ★ {len(guessed)} 个包的名字不在配方表里，目录名是从文件名推的："
              f"{', '.join(j.rel for j in guessed[:3])}"
              + ("…" if len(guessed) > 3 else ""))

    return (ok, skip, len(fail))
