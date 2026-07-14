# AudioFileManager

> 音频文件管理工具：自动识别元数据、补全封面与歌词、整理目录结构。

基于 PyQt6 的桌面应用，面向本地音乐曲库，提供从「解析 → 搜索 → 补全封面/歌词 → 编码统一 → 写入标签 → 目录整理」的一站式批量处理能力。

---

[main-page](docs/main-page.png)

## 功能特性

- **多格式标签读写**：支持 MP3 / FLAC / M4A / OGG / WMA / APE 的标签读取与写入。
- **多 Provider 搜索架构**：iTunes、MusicBrainz、LRCLIB、Meting（网易云/腾讯/酷狗/虾米/百度）多音源并发匹配元数据与歌词。
- **批量处理工作流**：解析文件名与已有标签 → 在线搜索补全 → 补全封面/歌词 → 编码统一 → 写入标签 → 目录整理。
- **试听弹窗**：基于 `QMediaPlayer` 的音频播放与 LRC 歌词同步编辑。
- **深色主题 Qt 界面**，支持拖放添加文件、列表列宽手动调整、勾选列与序号列。
- **细粒度总开关**：可在「设置」中分别启用/禁用网络搜索、文件整理、文件名处理，灵活控制批处理范围。
- **运行时日志开关**：「设置 → 日志」可即时启停文件日志，无需重启。
- **目录整理迁移伴随文件**：复制/移动音频时一并迁移同目录的 `.lrc` 歌词与封面图，并按重命名前的原名定位伴随文件。

---

## 环境要求

- Python 3.8 或更高版本（PyQt6 6.6 要求）
- Windows（主要支持平台；`start.bat` 为 Windows 启动脚本）

## 依赖

见 `requirements.txt`：

```
PyQt6>=6.6.0
mutagen>=1.47.0
httpx>=0.27.0
Pillow>=10.0.0
chardet>=5.0.0
pytest>=7.0.0
```

## 安装

```bash
pip install -r requirements.txt
```

## 运行

```bash
python main.py
```

Windows 用户也可直接双击 `start.bat` 启动。

可选命令行参数：

```bash
python main.py --config <配置文件路径>
```

不指定 `--config` 时使用默认配置（见下文「配置」）。

---

## 配置

- **默认配置文件**：`%APPDATA%/AudioFileManager/config.json`（首次运行时基于出厂默认生成）
- **出厂默认**：`config/default_config.json`
- **运行时调整**：通过「设置」对话框修改日志、搜索、整理、文件名、试听等选项，退出时自动保存。

常用开关：

| 配置项 | 说明 | 默认 |
| --- | --- | --- |
| `logging.enabled` | 文件日志记录 | `true` |
| `search.enabled` | 是否进行网络搜索 | `true` |
| `organize.enabled` | 是否进行文件整理 | `true` |
| `filename.enabled` | 是否按模板重命名文件 | `true` |

---

## 项目结构

```
AudioFileManager/
├── main.py              # 程序入口（创建 QApplication、初始化配置与日志、显示主窗口）
├── version.py           # 版本号与元信息（单一来源）
├── start.bat            # Windows 启动脚本
├── requirements.txt     # 依赖清单
├── ui/                  # Qt 界面：主窗口、文件列表、详情面板、设置对话框、进度条、深色主题 QSS
├── audition/            # 试听弹窗与 LRC 歌词同步编辑
├── parser/              # 文件名解析与元数据读取（filename_parser / metadata_reader / artist_db）
├── processor/           # 批处理编排：扫描、重命名、整理、冲突解决、状态管理
├── search/              # 在线元数据 Provider（iTunes / MusicBrainz / LRCLIB / Meting）+ 限流
├── services/            # 标签读写（tag_writer 及按格式拆分的 tag_writers/）、编码统一、手动搜索
├── cache/               # 封面与歌词缓存（cover_cache / lrc_cache）
├── config/              # 配置加载与出厂默认（settings / default_config.json）
├── utils/               # 通用工具（logger / helpers / retry_on_locked）
├── tests/               # 单元测试
├── docs/                # 开发文档与设计说明
└── ui_design/           # 界面设计稿
```

---

## 测试

```bash
pytest
```

---

## 版本

当前版本：**v1.0.22**（详见 `CHANGELOG.md`）。

## 许可证

本项目尚未指定许可证。
