# 蒸馏 (Distill)

> 课堂的录音、照片、手写关键词是"粗原料"——每晚流水线像蒸馏一样提取精华,产出结构化笔记草稿;人工校对 10 分钟,把 80 分草稿提纯到 95 分定稿。

**蒸馏 (Distill)** 是一套自托管的课堂笔记自动化系统:**课上 5 分钟采集 + 课后 10 分钟校对 = 一篇结构化、可检索、可复习的笔记**。录音、板书照片、手写关键词在宿舍服务器的流水线上自动完成转写、识别与整合,全部数据以纯 Markdown 存在自己手里。

- 完整设计方案:[docs/方案.md](./docs/方案.md)
- 每个"为什么不用 X"的决策理由:[docs/决策记录.md](./docs/决策记录.md)
- Obsidian 端配置:[docs/Obsidian配置清单.md](./docs/Obsidian配置清单.md)

## 一分钟了解

```
课上采集(你) → 课后传输(前台动作) → 夜间处理(服务器 cron 23:00) → 早晨校对(你,10 分钟) → 随时使用(Obsidian / 网页)
```

| 环节 | 谁做 | 说明 |
|---|---|---|
| 采集(课上) | 学生 | 纸笔记录关键词 + 手机录音 + 手机拍板书/PPT + 下课时给纸笔记拍一张(会话锚点) |
| 传输(课后) | 学生(前台动作) | 手机图库 → 分享 → 上传控制台(顺手选课程);安卓端零后台服务 |
| 处理(夜里) | 服务器 | `pipeline.py` 于 cron 23:00 触发:FunASR 转写录音 → VL 识别照片 → LLM 按素材整合草稿 |
| 校对(早上) | 学生 | 10 分钟:确认课程、合并/拆分分组、补易错点与答题规范 → 一键归档 |
| 使用(随时) | 学生 | Obsidian 或网页控制台阅读、搜索、复习 |

## 核心特性

- **一节课 = 一篇笔记**:补充素材(录音/照片/手写笔记)按拍摄时间自动并入已有会话,归档撞名自动合并,任何合并都经过人工校对确认([ADR-017](./docs/决策记录.md#adr-017-一节课--一篇笔记补充素材自动合并))
- **本地 STT 主力**:funasr(paraformer-zh + fsmn-vad + ct-punc)中文识别优、支持热词、22× 实时;faster-whisper 作英文课与备选引擎,未知课程自动检测语言路由([ADR-016](./docs/决策记录.md#adr-016-stt-升级funasrparaformer-zh为主力引擎)/[ADR-025](./docs/决策记录.md#adr-025-stt-英文支持按课程路由--语言自动检测))
- **热词自动补充(零 LLM)**:流水线每次启动用 jieba 从已归档笔记按课程提取高频术语写入每课热词文件,转写越用越准([ADR-026](./docs/决策记录.md#adr-026-热词自动补充零-llm从笔记库按课程提取))
- **VL 结构化照片**:板书/PPT/手写笔记 → 完整抄录 + LaTeX 公式 + 表格/mermaid 草稿,不是 OCR 式要点([ADR-007](./docs/决策记录.md#adr-007-照片用-vl-api-而非-paddleocr))
- **LLM 整合**:内容决定结构的完整复习笔记,记录知识本身,严禁"总结腔";素材缺失一律标"(待确认)",不编造([ADR-020](./docs/决策记录.md#adr-020-笔记生成内容决定结构的完整复习笔记禁总结腔))
- **网页控制台(PWA)**:上传 / 运行状态 / 校对一键归档 / 笔记浏览 / 全文搜索 / 网页编辑器(CodeMirror 6 + KaTeX 分屏实时预览),手机可安装为 App([ADR-019](./docs/决策记录.md#adr-019-笔记管理--可安装-apppwa)/[ADR-022](./docs/决策记录.md#adr-022-网页端笔记编辑器在线笔记--服务器网页不依赖云同步)/[ADR-024](./docs/决策记录.md#adr-024-笔记页深度重构设计系统--codemirror-编辑器--滚动修复))
- **笔记库管理**:重命名、移动课程、删除(入废纸篓可恢复)、导出 zip(md + 附件)
- **安卓零后台**:MIUI/HyperOS 会绞杀后台同步 App——所有传输都是前台动作,杀不掉([ADR-004](./docs/决策记录.md#adr-004-不用-syncthing--foldersync-等安卓后台同步))
- **数据自由**:笔记 = 文件夹里的 .md 文件,不锁任何编辑器;Obsidian 打开即用,换工具零成本([ADR-002](./docs/决策记录.md#adr-002-纯-markdown-文件夹裸文件)/[ADR-003](./docs/决策记录.md#adr-003-笔记应用选-obsidian定位为纯编辑器))

## 目录结构

```
distill/
├── README.md              本文件
├── config.example.yaml    配置模板(复制为 config.yaml 后填写)
├── requirements.txt       Python 依赖
├── pipeline.py            每晚批处理流水线:分组 + STT + VL + LLM 整合
├── upload.py              上传 + 控制台 API 服务(Flask 单文件)
├── webdav.py              WebDAV 文件中枢(wsgidav,纯 Python)
├── start.sh / stop.sh     启停脚本(上传 + WebDAV + 可选公网通道)
├── docs/
│   ├── 方案.md            完整设计方案(设计思想、数据模型、组件、成本、里程碑)
│   ├── 决策记录.md         全部 ADR(ADR-001 ~ ADR-027)
│   └── Obsidian配置清单.md  Obsidian 手机/PC 端配置
└── data/                  个人笔记数据(不进入本仓库,.gitignore 排除)
    ├── inbox/             文件传输中枢(WebDAV 挂载点)
    │   ├── <课程名>/      上传落点(按课程分目录,上传页选择)
    │   └── 废纸篓/        非笔记内容(可选)
    ├── 笔记/              笔记库(Obsidian 打开这个文件夹)
    │   ├── 未分类/        流水线草稿初落点(<日期>_<n>/ 子目录)
    │   └── <课程名>/      已归档笔记(每篇一个目录,含 附件/)
    ├── hotwords/          每课热词文件(流水线自动生成)
    ├── status.json        运行状态(供控制台读取)
    └── pipeline.lock      进程锁(防止与 cron 重叠运行)
```

## 快速开始

### 依赖

- **Python 3.12+**(STT 与 VL/LLM 依赖较重,建议独立 venv)
- **一台 24h 在线的服务器**:部署参考为无头服务器(Ubuntu 24.04,16 核 / 14G 内存,无 GPU)。内存紧张时请按 [ADR-016](./docs/决策记录.md#adr-016-stt-升级funasrparaformer-zh为主力引擎) 权衡 STT 引擎;STT 全程本地跑,不需要 GPU 也能用
- **一个 OpenAI 兼容的模型网关**:提供视觉模型(VL)与文本模型(LLM)即可,一学期花费 <10 元(见[成本说明](#成本说明一学期))
- 手机、平板、PC 任选其一用于采集与阅读(本系统按"手机采集 + PC/平板阅读"设计)

### 安装

```bash
git clone <本仓库地址> && cd distill
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp config.example.yaml config.yaml
# 编辑 config.yaml:
#   - api.base_url / api.api_key / 模型名(OpenAI 兼容网关)
#   - webdav.username / webdav.password(文件中枢访问凭据)
#   - courses 课程列表
```

### 启动

```bash
./start.sh                       # 启动上传控制台(8000)+ WebDAV(8082)
./stop.sh                        # 停止全部服务
.venv/bin/python pipeline.py --dry-run   # 只预览分组结果
.venv/bin/python pipeline.py             # 手动跑一次流水线
```

### 定时任务(每晚 23:00 批处理)

```bash
crontab -e   # 追加一行:
0 23 * * * cd ~/distill && ./.venv/bin/python pipeline.py >> logs/pipeline.log 2>&1
```

### 使用

1. **上传**:手机/PC 浏览器打开 `http://<服务器IP>:8000`(控制台,可安装为 PWA);或任意 WebDAV 客户端挂载 `http://<服务器IP>:8082` 直接拖文件进 `inbox/<课程>/`
2. **校对**:早上打开控制台 → 草稿列表 → 确认课程、合并/拆分分组、补答题规范 → 一键归档
3. **阅读/复习**:Obsidian 打开服务器上的 `data/笔记/`(配置见 [docs/Obsidian配置清单.md](./docs/Obsidian配置清单.md)),或直接用网页控制台的笔记浏览与全文搜索

## 成本说明(一学期)

| 项目 | 单价 | 估算 |
|---|---|---|
| VL(照片 ~500 张) | 通义 qwen-vl-plus 几厘/张;智谱 GLM-4V-Flash 有免费档 | **2-5 元** |
| 文本 LLM | 几厘/千 token | ~1-2 元 |
| STT | 本地 FunASR,免费 | 0 元 |
| 服务器资源 | 已有设备 | 0 元 |
| **合计** | | **<10 元/学期** |

> 数字来自实际部署估算。照片在流水线里会先压缩(按分辨率计费),实测单张照片压缩后约 110 图片 token;STT 全程本地,不产生 API 费用。

## 技术栈

| 层 | 选型 |
|---|---|
| 语言 | Python 3.12 |
| 上传 / 控制台 API | Flask(单文件 `upload.py`) |
| 文件中枢 | WebDAV(wsgidav + cheroot,纯 Python,零容器依赖) |
| 语音转写 | funasr(paraformer-zh + fsmn-vad + ct-punc)主力 / faster-whisper 英文与备选 |
| 视觉理解 | OpenAI 兼容 VL API(qwen-vl-plus / GLM-4V-Flash 等,按网关实测选型) |
| 文本整合 | OpenAI 兼容文本模型(按网关实测选型,见 [ADR-027](./docs/决策记录.md#adr-027-文本模型切换kimi-k25最终)) |
| 热词提取 | jieba(零 LLM 调用) |
| 前端 | React + MUI(PWA 控制台,构建产物由 `upload.py` 提供) |
| 网页编辑器 | CodeMirror 6 + KaTeX + GFM 分屏预览 |
| 调度 | cron |
| 笔记阅读 | Obsidian(纯编辑器定位,关闭双链/图谱)/ 网页控制台 |

## 许可证

[MIT](./LICENSE) © 2026 HydroGest
