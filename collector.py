#!/usr/bin/env python3
"""V6.4 开放获取采集器的稳定入口。

核心实现已迁移到 chemistry_collector 包，保留旧脚本的命令及导入接口。"""
from chemistry_collector import *  # noqa: F401,F403
from chemistry_collector import feasibility as _feasibility
from chemistry_collector.cli import main
from chemistry_collector.discovery import journal_article_iterator


# 兼容旧版测试及外部调用方使用的迭代器动态替换入口。
# 保留旧版测试注入迭代器的方式，并确保临时替换在结束后恢复。
def collect_papers(client, args, root):
    original = _feasibility.journal_article_iterator
    _feasibility.journal_article_iterator = journal_article_iterator
    try:
        return _feasibility.collect_papers(client, args, root)
    finally:
        _feasibility.journal_article_iterator = original


if __name__ == "__main__":
    main()
