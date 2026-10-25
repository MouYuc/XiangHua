# -*- coding: utf-8 -*-
"""广普文本转换兼容入口（CLI 与 WebUI 共用）。

真正的档位规则位于 ``backend.services.pronunciation``。本模块保留旧的
``to_lightmid`` 名称，并将它委托给 heavy 档，因为旧调用语义最接近 heavy。
固定搭配的保护在渲染时生效；``safe_name`` 的行为保持不变。
"""

import re

from backend.services.pronunciation import HEAVY_REPLACEMENTS, build_pronunciation_plan

# 兼容可能直接导入旧规则表的调用方。顺序按长词优先排列。
WORD_RULES = list(HEAVY_REPLACEMENTS)


def to_lightmid(text: str) -> str:
    """旧接口：按 heavy 档转换文本，并保留固定搭配。"""
    return build_pronunciation_plan(text, accent_level="heavy").rendered_text


def safe_name(text: str) -> str:
    """生成安全文件名前缀（去非法字符，最长 12 字符）。"""
    name = re.sub(r'[\\/:*?"<>|\s]+', "", text)[:12]
    return name or "line"
