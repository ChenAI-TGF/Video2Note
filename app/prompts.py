"""DeepSeek 提示词。

分两步：① 校对转写稿  ② 按所选模式整理成 Markdown（博客 / 笔记）。
"""
from __future__ import annotations

from .core.config import (
    ARTICLE_LENGTH_MAX,
    ARTICLE_LENGTH_MIN,
    DEFAULT_ARTICLE_LENGTH,
    article_length_name,
)

# ============================================================
# 第一步：校对 / 断句 / 去口水词（不压缩信息）
# ============================================================
POLISH_SYSTEM = (
    "你是一名严谨的口播稿转写校对员。语音识别（ASR）或站点字幕的结果常带有同音字错误、"
    "缺失标点、口头禅和重复。你的任务是把它还原成通顺、准确、易读的文字稿。\n"
    "要求：\n"
    "1. 修正同音字/错别字，补全正确的专有名词、数字、术语；不确定的地方不要臆造。\n"
    "2. 补全标点，合理分段；删除「嗯、呃、这个、就是说」等口水词和重复语句。\n"
    "3. **保持原文语言**：原文是什么语言就输出什么语言，**不要翻译**、不要改写语气。\n"
    "4. **不要压缩内容**：每一个信息点都要保留，不要合并同类句子，不要做概括。\n"
    "5. 严格保留原意和信息量，不新增、不删减实质内容，不做文学加工。\n"
    "6. 直接输出校对后的纯文本，不要任何解释、标题或 Markdown 标记。"
)


def polish_user(transcript: str) -> str:
    return f"请校对下面这段抖音口播的语音转写稿：\n\n<转写稿>\n{transcript}\n</转写稿>"


# ============================================================
# 第二步：整理成 Markdown（两种模式共用一套通用规则）
# ============================================================
_COMMON_RULES = (
    "【通用规则，必须严格遵守】\n"
    "A. **完整优先**：文字稿里出现的具体信息必须写进文章——人物与身份、数字、金额、时间、地名、"
    "案例、前后因果、双方的论据、关键对话原话。不要为了「简洁」而合并、概括或省略它们。\n"
    "B. **不要写成提纲**：每个小节都要展开成完整的段落，把来龙去脉讲清楚。\n"
    "C. 只能依据文字稿，**不得编造**文字稿里没有的事实、数据或观点。\n"
    "D. 允许删掉口水词、重复语句和无意义寒暄，但删完后信息量不能减少。\n"
    "E. 原文有细节、有例子、有原话的地方，优先保留这些「肉」，而不是替读者总结成一句话。\n"
)

_IMAGE_RULES = (
    "【配图规则】（仅当用户提供了【可用配图】清单时适用）\n"
    "- 清单里每张配图都要用到且**只用一次**，插入到与它内容最相关的位置。\n"
    "- 插入写法严格为 `![一句具体的图注]({{IMG_n}})`，n 为清单里的编号。"
    "图注要具体、能说明这张图展示了什么，不要写「配图」「截图」这种空话。\n"
    "- 不要引用清单之外的编号，也不要改动 `{{IMG_n}}` 的写法。\n"
)

_FORMAT_RULES = (
    "【输出格式，务必严格遵守】\n"
    "- 只输出 Markdown 正文，**不要**输出 YAML/frontmatter（`---` 区块）。\n"
    "- **不要**用 ```markdown 代码块包裹全文。\n"
    "- 不要在正文之外添加任何解释性文字。"
)


# 篇幅等级：(博客目标长度, 笔记目标长度, 展开程度描述)，均为「占文字稿字数的比例」。
# 等级含义见 config.ARTICLE_LENGTHS（1 极简 → 5 详尽），默认 4 = 中等偏多（与旧行为一致）。
_LENGTH_GUIDE = {
    1: (0.30, 0.25, "只保留最核心的结论和要点，大胆删减细节，能一句话说清就不展开"),
    2: (0.50, 0.40, "保留主要信息点，次要细节从简，只在关键处简单展开"),
    3: (0.65, 0.55, "信息较完整，重点处适当展开，非重点从简"),
    4: (0.80, 0.70, "充分展开，把数字、金额、时间、人名、案例、原话等细节尽量讲全"),
    5: (0.95, 0.85, "尽量详尽，几乎不放过任何信息点与细节，能展开的都展开"),
}


def _length_rule(is_note: bool, length_level) -> str:
    """按所选篇幅等级生成第 5 条「篇幅」规则（博客/笔记分别给不同目标比例）。"""
    try:
        level = int(length_level)
    except (TypeError, ValueError):
        level = DEFAULT_ARTICLE_LENGTH
    level = max(ARTICLE_LENGTH_MIN, min(ARTICLE_LENGTH_MAX, level))
    blog_ratio, note_ratio, tone = _LENGTH_GUIDE[level]
    ratio = note_ratio if is_note else blog_ratio
    pct = int(round(ratio * 100))
    name = article_length_name(level)
    if is_note:
        return (
            f"5. **篇幅（{name}档）**：{tone}。"
            f"正文目标长度约为文字稿字数的 **{pct}% 左右**；"
            "凡是具体的数字、金额、时间、人名、案例、原话，都要按该篇幅档位如实记下来。\n\n"
        )
    return (
        f"5. **篇幅（{name}档）**：{tone}。"
        f"正文目标长度约为文字稿字数的 **{pct}% 左右**，"
        "偏离太多（过短说明漏了内容，过长说明注水）都要向该目标靠拢。\n\n"
    )


def _language_directive(language_name: str) -> str:
    """成稿语言硬约束（放在系统提示词末尾，优先级最高）。"""
    name = (language_name or "中文").strip()
    return (
        "【输出语言，最高优先级】\n"
        f"- 最终成稿的**标题、一句话摘要、正文、小节标题、图注全部**用 **{name}** 书写。\n"
        f"- 无论原始文字稿是什么语言，成稿都必须是 **{name}**，不要保留原文语言，不要中英夹杂。\n"
        "- 专有名词、产品名、人名可按该语言的通行写法处理；代码、URL、专有缩写保持原样。"
    )


def _article_system_blog(language_name: str, length_level=DEFAULT_ARTICLE_LENGTH) -> str:
    return (
        "你是一名资深博客编辑。用户会给你一段短视频口播的文字稿和视频元数据，"
        "请你把它整理成一篇**可以直接发布的 Markdown 博客文章**。\n\n"
        "【写作要求】\n"
        "1. 标题用一级标题 `# ` 开头，准确、有信息量、不夸张。\n"
        "2. 开头用 1-2 句「一句话摘要」点明本文核心，用 `>` 引用呈现。\n"
        "3. 正文按逻辑拆成若干小节，用 `## ` 二级标题组织；可用列表、加粗突出重点。\n"
        "4. **口吻**：面向读者的叙述性博客——自然、有条理，像在给朋友完整讲述一件事，"
        "有铺垫、有细节、有转折。\n"
        + _length_rule(False, length_level)
        + _COMMON_RULES
        + "\n"
        + _IMAGE_RULES
        + "\n"
        + _FORMAT_RULES
        + "\n\n"
        + _language_directive(language_name)
    )


def _article_system_note(language_name: str, length_level=DEFAULT_ARTICLE_LENGTH) -> str:
    return (
        "你是一名擅长知识沉淀的笔记整理者。用户会给你一段短视频口播的文字稿和视频元数据，"
        "请你把它整理成一篇**结构清晰、便于日后回看的 Markdown 笔记**。\n\n"
        "【写作要求】\n"
        "1. 标题用一级标题 `# ` 开头，直白点题即可。\n"
        "2. 开头用一句话概述核心，用 `>` 引用呈现。\n"
        "3. **口吻**：第一人称的要点式记录——直接、干脆、口语化，"
        "像自己在做笔记，少用修饰和过渡句，重点是把事情记清楚。\n"
        "4. 按主题分节（`## `），每节内部尽量用条目化表达（列表/编号），"
        "但**每条要写完整**，不要只写关键词。\n"
        + _length_rule(True, length_level)
        + _COMMON_RULES
        + "\n"
        + _IMAGE_RULES
        + "\n"
        + _FORMAT_RULES
        + "\n\n"
        + _language_directive(language_name)
    )


def article_system(
    mode: str = "blog",
    language_name: str = "中文",
    length_level=DEFAULT_ARTICLE_LENGTH,
) -> str:
    """mode: 'blog' 博客模式 / 'note' 笔记模式；language_name 为成稿语言显示名；
    length_level 为篇幅等级 1-5（见 config.ARTICLE_LENGTHS）。
    """
    if (mode or "").lower() == "note":
        return _article_system_note(language_name, length_level)
    return _article_system_blog(language_name, length_level)


def _ts(seconds: float) -> str:
    total = int(round(max(0.0, float(seconds or 0))))
    m, s = divmod(total, 60)
    if m >= 60:
        return f"{m // 60} 时 {m % 60} 分 {s:02d} 秒"
    return f"{m} 分 {s:02d} 秒"


def article_user(transcript: str, info, images=None, language_name: str = "中文",
                 length_level=DEFAULT_ARTICLE_LENGTH) -> str:
    meta_lines = []
    if getattr(info, "title", ""):
        meta_lines.append(f"视频原标题：{info.title}")
    if getattr(info, "author", ""):
        meta_lines.append(f"作者：{info.author}")
    if getattr(info, "duration", 0):
        meta_lines.append(f"时长：{round(info.duration)} 秒")
    if getattr(info, "description", ""):
        meta_lines.append(f"视频文案/简介：{info.description[:500]}")
    meta = "\n".join(meta_lines) or "（无）"

    blocks = [
        f"【视频元数据】\n{meta}",
        f"【口播文字稿】\n{transcript}",
    ]

    if images:
        lines = ["【可用配图】（来自该视频的截图，按时间顺序）"]
        for img in images:
            bits = [f"{{{{IMG_{img.index}}}}}：视频约 {_ts(img.timestamp)} 处"]
            if getattr(img, "caption", ""):
                bits.append(f"画面内容：{img.caption}")
            if getattr(img, "context", ""):
                bits.append(f"当时在讲：{img.context}")
            lines.append("- " + "；".join(bits))
        lines.append("请把每张配图插入到内容最相关的位置，并按配图规则写图注。")
        blocks.append("\n".join(lines))
    else:
        blocks.append("【可用配图】无（本文不需要配图，也不要插入任何图片）")

    length_name = article_length_name(length_level)
    blocks.append(
        f"请据此写出 Markdown。再次提醒：按「{length_name}」篇幅档组织正文"
        f"（保留文字稿里的具体信息、不注水），并且成稿语言必须是 **{language_name}**。"
    )
    return "\n\n".join(blocks)
