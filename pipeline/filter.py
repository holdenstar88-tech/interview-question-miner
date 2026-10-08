"""粗过滤:进入 LLM 前的关键词过滤(省钱第一道闸)。"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 匹配时忽略分隔符差异:如 "hr面" vs "hr 面"
_SPACE_RE = re.compile(r"[\s\-_/]+")


def _contains(haystack: str, word: str) -> bool:
    """去空白后做大小写不敏感的子串匹配。"""
    h = _SPACE_RE.sub("", haystack).lower()
    w = _SPACE_RE.sub("", word).lower()
    return w in h


def passes_coarse_filter(
    title: str, content: str, interview_words: list[str], position_words: list[str]
) -> tuple[bool, str]:
    """标题 + 完整公开正文必须同时命中面试词与岗位词。

    返回 (是否通过, 原因说明),原因写入日志便于调词表。
    """
    haystack = f"{title}\n{content}"
    hit_interview = [w for w in interview_words if _contains(haystack, w)]
    hit_position = [w for w in position_words if _contains(haystack, w)]
    if not hit_interview:
        return False, "未命中面试词"
    if not hit_position:
        return False, "未命中岗位词"
    return True, f"命中:面试词={hit_interview[:3]} 岗位词={hit_position[:3]}"
