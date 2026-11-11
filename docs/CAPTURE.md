# 网页会话抓取：虚拟列表的三个坑

> **SysPersona-OS** · 本文件定义：会话抓取与导出：来源、落盘位置、去重与失败处理
> 规范全文以仓库 `main` 为准；改了要同步 README / CHANGELOG / 台账。

> 2026-11-09 实测。适用对象是 DeepSeek 网页会话（`chat.deepseek.com`）这类**虚拟列表**界面。
> 参考实现：[`tools/cdp_capture/capture.js`](../tools/cdp_capture/capture.js)。

## 一、现象

同一份会话，肉眼看到 1098 条，采集器长期报 **402 条**，而且反复报"正文未变"。
换一次刷新、换一个窗口，条数还会掉到 40 条、151 条。

不是"没有新内容"，是**采集器只看到了尾巴**。

## 二、三个根因（都能复现）

### 1. 页面不可见时，虚拟列表**不重绘**

后台标签页 `document.hidden === true`，`requestAnimationFrame` 被节流，
滚动后列表不会重新挂载节点——于是无论滚到哪儿，`querySelectorAll` 拿到的还是同一批。

修法：`Emulation.setFocusEmulationEnabled` + `Page.setWebLifecycleState('active')`，
让页面以为自己在前台。实测加上这两行之后，同样的滚动会立刻重绘。

### 2. 刷新后视图锚在底部，`scrollTop` 赋值会被弹回

刷新后页面停在最新消息处，直接 `el.scrollTop = x` 往往被"粘底"逻辑覆盖，
甚至被重算后的高度弹回原位。

修法：用**真实滚轮事件**（`Input.dispatchMouseEvent` 的 `mouseWheel`），
一次滚一小段，让页面按自己的逻辑移动。实测滚轮位移精确生效（deltaY=-20000 → 位置减 20000）。

### 3. 列表总高是**估**的，滚动中一直在变

实测同一条会话的 `scrollHeight` 从 998,837 变成 1,129,722——未挂载的行用估算高度，
渲染后逐条修正。所以**不能用绝对坐标去重**（同一行的 top 会偏移），
只能按"相邻窗口的重叠行"做序列对齐：拿新窗口的尾部去匹配已收集列表的头部，
匹配上就只取差集。

## 三、用法

```powershell
# 全量：自底向上扫到顶
node tools\cdp_capture\capture.js --out sessions\2026-11-09\网页对话\xxx.md

# 增量：撞到"已归档的最后一条"就停，只输出锚点之后的消息
node tools\cdp_capture\capture.js --out delta.md --anchor-file anchor.txt
```

`anchor.txt` 里放归档末条正文的前 60 字（去掉所有空白即可）。

前提：一个带 `--remote-debugging-port=9222` 且**已登录**的浏览器窗口。

## 四、证据

| 日期 | 动作 | 条数 |
|---|---|---|
| 2026-11-09 07:21 | 旧采集器（单次 `querySelectorAll`） | 402（且被当成总数） |
| 2026-11-09 08:12 | 滚轮 + 重叠对齐，全量重抓 | **1020** |
| 2026-11-09 09:26 | 增量续抓（锚点模式） | **1098**（+78） |

## 五、没解决的部分（写清楚，别当已完成）

- **登录态**：调试浏览器的 profile 偶发丢登录，需要重新扫码。根因未定位，
  怀疑是 Edge 对同一 `--user-data-dir` 的复用/锁竞争；**换窗口要重登**这件事还没有自动化解法。
- **内部 API**：网页内部接口带 PoW + token 校验，用页面自带的 `userToken` 实测仍返回
  `{"code":40003,"msg":"INVALID_TOKEN"}`，不能作为稳定通道。
- **页面改版**：选择器（`.ds-virtual-list`、`.ds-message`、`.ds-markdown`、`.d29f3d7d`）随前端改版失效，
  脚本里没有做兜底探测。
