# 查 CI 历史：`workflow_id` 会被静默忽略

**2026-10-20 实测的一个坑。** 它值得记，因为它的表现是"查询成功、结果看起来正常"。

## 坑

```bash
# ✗ 这个 workflow_id 参数【不生效】—— 它返回的是【所有】workflow 的 runs
gh api "repos/…/actions/runs?workflow_id=release.yml"
gh api "repos/…/actions/runs?workflow_id=381729112"    # 数字也一样不生效
```

实测：用 `workflow_id=release.yml` 查，返回的却是 bootstrap 的那一轮
（`39393102905`，`name=bootstrap`、`workflow_id=382067993`）。

★★ **而且它不报错** —— 200、有数据、格式对。所以"用文件名当 `workflow_id`"
这件事，看起来完全成功。

## 那次它骗了我什么

我当时在问「release.yml 的发布那步成功过吗」，而实际拿到的是**所有 workflow
的 run 列表**，然后我数出「27 轮成功」——那是所有 workflow 加起来的成功数，
和 release 无关。**好在那个数字被后来的交叉检查推翻了。**

## 正确做法

列出之后按 `path` 或 `name` 过滤：

```bash
# 一次列 60 轮，然后在本地过滤 —— 简单、可靠
gh api "repos/…/actions/runs?per_page=60" \
  | jq '[.workflow_runs[] | select(.path | endswith("release.yml"))]'

# 或者先拿数字 id（这一步是可靠的），再用它 —— 但见上面的实测，仍要核对
gh api "repos/…/actions/workflows" --jq '.workflows[] | "\(.id) \(.path)"'
```

★ **而每一条结论都要能自证**：拿到一个 run 之后，用
`gh api .../runs/<id> --jq '.name, .path'` 核对它真的是你以为的那个 workflow。
一行命令，而它能挡住上面那一整类错误。

## 而这件事的形状

和这个项目反复在修的那一类完全同源：

    一个不生效的参数 → 一个成功返回 → 一个看起来正常的结论

**"没生效"和"生效了但结果为空"长得一样；而"成功返回"不等于"问对了问题"。**
