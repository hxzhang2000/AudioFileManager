"""格式专用的标签写入/读取实现（§7.2）。

每个模块对应一种音频格式，提供 ``_write_`` / ``_cover_`` / ``_lyrics_`` /
``_read_`` / ``_read_cover_`` / ``_read_lyrics_`` 六个函数接口。

由 ``services.tag_writer`` 导入并通过路由表分发。
"""
