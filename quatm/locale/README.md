# quatm/locale — 语言包与翻译约定

`zh.json` 是**唯一**的语言包：key = 源码里的英文原文（`tr('...')` 的字面量），
value = 中文译文。系统语言非中文时直接用源码英文；缺 key 会自动回退到原文。

i18n 实现在 `quatm/utils/i18n.py`（纯 stdlib），`quatm/GUI/infra/i18n.py` 只是
re-export；`locale_dir()` 对新旧位置都兼容。

## 术语表（务必统一，改译文前先对照）

| 英文 | 统一译法 | 备注 |
|---|---|---|
| Browser | 浏览器 | 实验浏览器主窗口 |
| Looper / Browser Loop | 循环器 | 面板与消息标签 `[循环器]` |
| Experiment | 实验 | |
| Experiment Queue | 实验队列 | |
| Prep station / PrepStation | 准备站 | |
| Properties | 属性库 | 分布式属性树（不是“属性记录服务”） |
| Property Logger | 属性服务 | 常驻落盘服务 |
| Property Editor | 属性编辑器 | |
| Data Manager / datamgr | 数据管理 | 标签 `[数据管理]`；模块名 `datamgr` 保留 |
| h5 Storage / H5 | 数据存储 | 标签统一 `[数据存储]` |
| Stream Monitor | 数据流监控 | |
| Timeline | 时间线 | |
| CameraHub / CamServer | 相机服务 | 同一功能必须同一说法 |
| BlackFly / iXon | BlackFly 相机 / iXon 相机 | 品牌保留英文 |
| ADwin / Rigol | ADwin / Rigol | 产品名保留 |
| ProcessManager | 进程管理 | |
| Analysis Manager | 分析管理 | |
| LivePlot | 实时曲线 | |
| Scroll Window | 滚动窗 | |
| Sequencer | 序列编辑器 | |
| Notifications | 通知 | |
| Image Divider | 图像归一 | |
| Ion State | 离子状态 | |
| gauge file | 校准文件 | |
| run | run | 技术术语保留（例：从 x/y 加载了 3 个 run） |
| task | 任务 | |
| watchdog | 看门狗 | |
| fail-closed | fail-closed | 安全术语保留（可加括号说明） |

## 消息标签（`[xxx]`）

源码里的 `[CameraHub]`、`[Browser Loop]` 这类前缀是**消息分类标签**：源码保留英文
（便于和代码/日志对照），但**译文里统一译成中文**（`[相机服务]`、`[循环器]`）。
新增消息请沿用上表的标签译法，不要另起新词。

## 该译 / 该保留（判据）

**要译**：组件、模块、类、概念的**俗称名**——它们是给人看的，不是路径。

| 原文 | 译法 |
|---|---|
| `datamgr` | 数据管理（模块） |
| `DataManager` | 数据管理器 |
| `DataSummary` / `SingleDataSummary` | 数据汇总 / 单次数据汇总 |
| `looplist` | 循环列表 |
| `Live` | “实时” |
| `Store` | “存储” |
| `Simulate` | “模拟” |
| `Caller` | “调用” |
| `shelved` | “已暂存” |
| `h5storage` | h5 存储窗口 |
| `Browser` / `Looper` | 浏览器 / 循环器 |

**保留原文**：一切能和代码或文件一一对应的东西——见下一节。

判断口诀：**能拿去搜索引擎/编辑器里定位的，保留；只是叫法，译。**

## 必须保留原文的内容

- **代码标识符/函数名**：`start_realtime()`、`loadFromFile()`、`_evaluate_and_send()`、
  `properties.json` 里的键名
- **路径/文件/环境变量**：`configuration/gauge_files/`、`device_db.py`、
  `QUATM_PROJECT_ROOT`、`/Devices/allow_uncalibrated_dac`、`core.pyx`
- **快捷键**：`Ctrl+P`、`Alt+←/→`、`F5`、`Shift+Delete`、`PgUp/PgDn`、`Backspace`
- **品牌与型号**：`Quatm`、`BlackFly`、`iXon`、`ADwin`、`Rigol DG1022Z`、`InfluxDB`、
  `Mattermost`、`pyqtgraph`、`Spinnaker`
- **单位与格式**：占位符 `%s`/`%d`/`%.0f`/`%r`/`{}`、`±10 V`、`h5`、`HDF5`、`TTL`、
  `DAC`、`ms`、`kHz`
- **示例占位文本**：`Bla`/`Blubb`（Qt 示例代码）

## 标点与文风

- 一律用**全角**中文标点（`：`、`，`、`。`、`（）`、`——`、`……`），
  **不要**半角冒号加空格（`xxx: ` ✗ → `xxx：` ✓）。
- 一句话说清"发生了什么 + 该怎么办"；错误消息尽量给出可操作的下一步。
- 用 `**…**` 强调关键否定/警告，例如“属性树**没有**写入磁盘”。
- 不要在译文里重复标签对应的英文（`[浏览器] Browser …` ✗ → `[浏览器] …` ✓）。
- 用词精简：能用两个字的不用四个字（“属性服务”而不是“属性记录服务”）。

## key 编写规则（新增/修改界面文案必读）

`zh.json` 的 key 就是源码里的**英文原文**（`tr('...')` 的字面量），所以 key 的写法
直接决定译文能不能长期对上：

1. **短**：一个 key 只装一个标题、一句话或一个步骤。超过 200 字符的 key 会在
   `tools/i18n/check.py` 报告末尾打印**告警清单（只告警、不失败）**；改到该处时按
   “标题 / 步骤 / 段落”拆成多个独立 `tr()`。
2. **稳定**：不要在 key 里塞变量值（时间、路径、机器码），用 `%s` / `{name}`
   占位；变量值变化不应产生新 key。
3. **不拼整句**：不要把多句话先拼成一个字符串再整体 `tr()`；每一段独立 `tr()`，
   拼接只用于换行、`<br>` 等**语言无关**的连接符。
4. **改文案必须同步 `zh.json`**：英文原文改一个标点就是新 key，旧条目会变成
   “语言包里有、代码里已不用的条目”；确认无引用后删掉旧条目。
5. **HTML/占位符原样保留**：`<b>`、`<br>`、`%s`、`{name}` 在译文里必须继续出现，
   位置以中文语序为准但一个都不能丢。

当前状态（P2-6 治理基线）：

* 已拆掉最长的 3 个 key（827 / 773 / 492 字符）：`Start-up sequence`、
  `Core concepts` 与 Stream Monitor 的“关于”文本，改为按步骤/条目/快捷键逐条
  `tr()`，拼接结果与拆分前的文本逐字符相同；
* 仍有 **27 条** key 超过 200 字符（最长 472 字符，`Where files live` 等），清单由
  `python tools/i18n/check.py` 末尾的“告警（不失败）”段落实时输出（本文件不逐个
  罗列，避免与源码漂移），后续按上表逐步拆分；**新增文案不要让 key 超过 200 字符**。
* 中文 key 必须为 0（`tests/test_i18n_structure.py` 会断言）。

被静态检查识别的写法：`tr('...')` / `_tr('...')`、`QCoreApplication.translate('context',
'source')`（取第 2 个参数 source 进语言包；context 是 Qt 消歧键，不翻译）、
`QT_TRANSLATE_NOOP` / `QT_TRANSLATE_NOOP3`。经辅助函数或循环变量进 `tr()` 的写法
由动态检查覆盖，统一由 `check.py --strict` 执行（见下节）。

## 校验工具

```bash
python tools/i18n/check.py           # 覆盖率 + 长 key 告警（期望“缺 0”）
python tools/i18n/check.py --strict  # CI 唯一入口：覆盖率 + 动态 key 检查（含中文 key 断言）
python tools/i18n/check_dynamic.py   # 单独看动态 key（经辅助函数/变量进 tr()）的覆盖情况
python tools/i18n/convert.py         # 批量把新增界面文字包上 tr() 并补语言包条目
```

`check.py --strict` 会直接调用 `check_dynamic.py` 的严格检查（进度输出接在覆盖率
报告后面），因此 CI 不需要再加一步；`check_dynamic.py --strict` 仍可单独运行排查。

## 第三方库（pyqtgraph）的界面文字

pyqtgraph 的右键菜单、轴控制面板、绘图选项里的文字是**库内的**，走它自己的
`QCoreApplication.translate()`（例：`View All`、`Mouse Mode`、`Transforms`、
`Downsample`、`Invert Axis`），Qt 官方翻译和 `zh.json` 都盖不到。处理方式在
`quatm/GUI/infra/pyqtgraph_i18n.py`：

* `PyQtGraphTranslator` —— 自定义 `QTranslator`，把 pyqtgraph 的查询接到 `zh.json`
  （**原文做 key**），由 `setup_app_theme()` 调 `install()` 装一次。这是主手段，
  必须在**创建绘图控件之前**装好。
* `localize_plot_widget()` / `localize_histogram_lut()` —— 兜底：对**已经建好**的菜单
  改文本（菜单弹出时会重建，所以另外挂了 `aboutToShow`）。控件建在
  `setup_app_theme()` 之前时靠它。

要新增一条：把 pyqtgraph 里的原文加进 `_PG_KEYS`（走 `translate()` 的）或
`_PG_LITERAL_KEYS`（直接写死的字面量，如 `widgets/ColorMapMenu.py`），再补 `zh.json`
译文。**原文必须逐字符抄一致**，写错一个字母就永远不命中。

必须显式处理的几处（库内写死 / 不走 translate()）：

| 位置 | 内容 | 处理 |
|---|---|---|
| `ParameterTree.__init__` | 表头 `Parameter` / `Value` | `localize_parameter_tree(tree)` |
| `ColorMapMenu.py` | `preset gradient` / `local` / `Linear` … | `localize_menu()`（`_PG_LITERAL_KEYS`） |
| `GradientEditorItem` | 色标菜单末尾的 `RGB` / `HSV` | 只改了翻译器输出，保留英文术语 |

**最关键的一条**：`QTranslator.translate()` 未命中时**必须返回 `None`**。实测
（PySide6 6.11）返回 `''` 会被 Qt 当成“译文就是空串”——pyqtgraph 里未列入白名单的
文字全部变成**空白**（出现过：色标菜单最后两项空白、轴面板的 `0`/`%` 消失），而且
Qt 自己的语言包也被压掉（`QPlatformTheme` 的 `Save` 变回英文）；返回原文同样会压掉
Qt 自己的翻译。

保留英文的：**色标名**（`magma`、`viridis`、`cet_*`）与**库名**（`matplotlib`）——
它们要写进配置、要能在代码里检索（见上一节的口诀）。
