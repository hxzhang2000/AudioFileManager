"""AudioFileManager 程序主入口（见开发方案 §11 项目结构、§12 开发计划）。

职责：
1. 创建 QApplication 实例，设置应用名称与版本号；
2. 初始化全局配置（config.settings.get_config）与日志（utils.logger）；
3. 支持命令行参数 ``--config`` 指定配置文件路径；
4. 应用深色主题 QSS 样式表、启用高 DPI 支持；
5. 创建 MainWindow 并显示；
6. 优雅退出：保存配置、清理资源。

运行：python main.py [--config 路径]
"""

import sys
import argparse
from pathlib import Path

# 高 DPI 缩放策略必须在创建 QApplication 之前设置
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMainWindow, QLabel, QVBoxLayout, QWidget

from config import settings
from utils.logger import setup_logger
from version import APP_NAME, APP_VERSION

# 应用元信息（单一来源：version.py）


# ----------------------------------------------------------------------
# 主窗口导入
# ----------------------------------------------------------------------
# 实际主窗口在 ui/main_window.py（开发方案 Phase 8 实现）。
# 若该模块尚未实现，使用下方占位窗口保证入口可运行；
# 待 ui.main_window 实现后会自动改用真实主窗口。
try:
    from ui.main_window import MainWindow  # type: ignore
except ImportError:  # 主窗口尚未实现时的占位实现
    class MainWindow(QMainWindow):
        """占位主窗口（实际主窗口将在 Phase 8 实现）。"""

        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle(APP_NAME)
            self.resize(960, 640)
            central = QWidget()
            layout = QVBoxLayout(central)
            layout.addWidget(QLabel(f"{APP_NAME} v{APP_VERSION}\n\n主窗口尚未实现（Phase 8）。"))
            layout.addStretch(1)
            self.setCentralWidget(central)

        def cleanup(self) -> None:
            """清理资源占位方法（实际实现中停止播放、释放 QMediaPlayer）。"""
            pass


def parse_args(argv: list[str]) -> argparse.Namespace:
    """解析命令行参数。

    可选参数：
        --config PATH  指定配置文件路径（默认为 config.settings.CONFIG_FILE）。
    """
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="AudioFileManager — 音频文件元数据整理工具",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="指定配置文件路径（默认使用 %%APPDATA%%/AudioFileManager/config.json）",
    )
    # 使用 parse_known_args，将未识别参数留给 Qt 处理
    args, _ = parser.parse_known_args(argv)
    return args


def main() -> int:
    """程序入口。"""
    # 1. 解析命令行参数
    args = parse_args(sys.argv[1:])

    # 2. 高 DPI 支持：在创建 QApplication 前设置缩放舍入策略（Qt6 默认已启用高 DPI）
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    # 3. 创建 QApplication 实例
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    # 避免在最后一个窗口关闭时退出（交由主窗口 closeEvent 控制），
    # 默认行为即可，此处显式声明意图
    app.setQuitOnLastWindowClosed(True)

    # 4. 初始化日志
    logger = setup_logger(APP_NAME)
    logger.info("=" * 50)
    logger.info(f"{APP_NAME} v{APP_VERSION} 启动")

    # 5. 初始化全局配置（若指定 --config 则覆盖默认配置文件路径）
    if args.config:
        config_path = Path(args.config)
        settings.CONFIG_FILE = config_path
        logger.info(f"使用指定配置文件: {config_path}")
    try:
        config = settings.get_config()
        logger.info(f"配置加载完成（版本 {config.get('version', '未知')}）")
    except Exception as e:
        logger.warning(f"配置加载失败，使用默认配置: {e}")
        config = {}

    # 应用日志文件记录开关（运行时启停文件日志，默认启用）
    try:
        from utils.logger import set_file_logging_enabled
        file_logging = config.get("logging", {}).get("enabled", True)
        set_file_logging_enabled(file_logging)
        logger.info(f"文件日志记录{'已启用' if file_logging else '已禁用'}")
    except Exception as e:
        logger.warning(f"应用日志开关失败: {e}")

    # 7. 创建并显示主窗口
    main_window = MainWindow()
    main_window.show()
    logger.info("主窗口已显示")

    # 8. 注册优雅退出：保存配置、清理资源
    def on_about_to_quit() -> None:
        logger.info("程序准备退出，开始保存配置与清理资源...")
        # 清理主窗口资源（停止播放、释放 QMediaPlayer 等）
        try:
            if hasattr(main_window, "cleanup"):
                main_window.cleanup()
        except Exception as e:
            logger.warning(f"清理主窗口资源时出错: {e}")
        # 保存配置（原子写入，见 config.settings.save_config）
        try:
            settings.save_config(settings.get_config())
            logger.info("配置已保存")
        except Exception as e:
            logger.warning(f"保存配置失败: {e}")
        logger.info(f"{APP_NAME} 已退出")

    app.aboutToQuit.connect(on_about_to_quit)

    # 9. 进入事件循环
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
