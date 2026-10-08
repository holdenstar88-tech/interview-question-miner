"""Reject HR chatter and vague interview summaries, not technical topics."""
import re


def excluded_position(position: str) -> bool:
    """Classify the interview role, never keywords in individual questions."""
    return bool(re.search(
        r'测试|测开|质量保障|质量保证|\bQA\b|\bSDET\b|\bQE\b|\btest(?:ing)?\b|'
        r'产品经理|产品运营|运维|技术支持|售前|售后|数据标注', position, re.IGNORECASE))


def nontechnical_reason(text: str) -> str | None:
    value = re.sub(r'\s+', '', text).strip('？?。.!！')
    if re.search(r'薪资|薪酬|到岗|入职时间|什么时候.*实习|提前实习|为什么实习去了|哪段实习印象|自我介绍|反问环节', value):
        return '非专业知识问题'
    if re.search(r'(简单|大概|随便).*(问|聊).*(项目|情况)', value):
        return '未给出具体技术问题'
    if re.fullmatch(r'.*项目(的)?(相关情况|背景是什么|有哪些技术难点|有什么亮点或难点|是做什么的，应用场景是什么)', value):
        return '泛泛项目介绍'
    if re.fullmatch(r'(讲解|介绍|介绍一下|讲一下).*项目', value) or re.fullmatch(r'选择一个需求或项目深入展开讲一下', value):
        return '泛泛项目介绍'
    if re.fullmatch(r'(做过的项目中，哪个功能最有挑战|项目有什么难点、亮点|遇到的痛点是什么|你是如何解决的.*|请举一个印象较深的场景)', value):
        return '未给出具体技术问题'
    if (re.fullmatch(r'.*(是否涉及前端工作|有使用.{1,30}吗|是否使用过.{1,30}|对.{1,30}有了解吗|有没有实际使用或测试过.{1,30}|了解哪些前端框架)', value)
            and not re.search(r'原理|如何|怎么|为什么|区别|设计|实现|排查', value)):
        return '仅询问经历或熟悉程度'
    return None


def filter_question(question: dict) -> dict | None:
    """A concrete follow-up may salvage an otherwise generic lead-in."""
    follows = [f for f in question.get('follow_ups', []) if not nontechnical_reason(f)]
    if nontechnical_reason(question['question']):
        if not follows:
            return None
        return dict(question, question=follows[0], follow_ups=follows[1:])
    return dict(question, follow_ups=follows)
