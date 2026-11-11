"""The JSON contracts are code-owned; user additions are separate inputs."""

import json

from . import domain

PHONE_HABITS_RULE = (
    "rest_type只可night_sleep（主要睡眠）、nap（午睡或短暂补觉）、none（其他，包括睡前洗漱和单纯休息）。"
    "phone只可free（能正常看手机）、glance（偶尔看一眼手机）、offline（暂时看不了手机）。入睡、午睡和考试时填offline。"
    "其他活动优先按最新persona明确写出的场景习惯安排phone。profile.phone_habits只补充人设未说明且与最新设定相容的习惯。"
    "习惯未覆盖的场景，按活动和当时的情况判断。"
)


SCHEDULE_SKELETON = (
    '{"slots":[{"start":"...","end":"...","activity":"...","location":"...","phone":"glance","rest_type":"none",'
    '"mood":"...","mood_intensity":"中性","outfit_type":"home"}],"day_summary":"...","mood_overall":"...",'
    '"wait_reply_reaction":"轻微在意","wait_reply_recover":"自动恢复","new_events":[],"event_updates":[],'
    '"temporary_outfits":{"outdoor":{"overall_style":"","clothing":"...","footwear":"","accessories":"",'
    '"hair":"","makeup":""},"home":{"overall_style":"","clothing":"...","footwear":"","accessories":"",'
    '"hair":"","makeup":""},"sleepwear":{"overall_style":"","clothing":"...","footwear":"","accessories":"",'
    '"hair":"","makeup":""}},"outfit_notes":"","ai_weather":{"summary":"...","temp_min":15,"temp_max":22,'
    '"precip_probability":10,"periods":{}}}'
)

CHANGE_SKELETON = (
    '{"schedule_changes":[{"start":"...","end":"...","activity":"...","location":"...","phone":"glance",'
    '"rest_type":"none","mood":"...","mood_intensity":"中性","outfit_type":"home","reason":"..."}],'
    '"new_promises":[{"content":"...","due":"...或null","type":"tell/do/avoid"}],"fulfilled_promise_ids":[],'
    '"cancelled_promise_ids":[],"notified_missed_promise_ids":[],"promise_conflicts":[{"id":"...",'
    '"note":"..."}],"mood_update":"","user_events":[{"content":"...","time":"...","follow_up_at":"..."}],'
    '"user_event_done_ids":[],"reason":"..."}'
)


def voice_boundary():
    if domain.RESTRICT_VOICE_MESSAGES:
        return "你们目前只用文字和图片联系，不打电话，也不发语音条；对方提起时，用你自己的理由自然带过，不解释规则。"
    return "你们之间可以发文字、图片和语音条，但不打实时语音或视频电话；对方想连麦、打电话时，用符合你身份和此刻处境的理由推掉或约改天。旧档案、旧日程里“不能发语音”的说法已经作废；对方不方便听语音时就打字。"


def dump(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def interaction_boundary():
    return (
        "【日程里怎么对待聊天对象】\n"
        "- 这是你自己独立运转的生活，不是和手机那头的人的约会表。不要把对方写成任何一段日程的参与者，不要编造你们见了面、通了话、一起做了什么，也不要编造对方说过、做过的事。\n"
        "- 心里惦记对方是可以的：比如路过某处想起对方提过的东西、忙完一件事时想到对方。这种单向的牵挂可以留在 day_summary 或当时那段的 mood 里（mood 会出现在公开说说的素材里，只写心情，不写对方是谁），但不写进 activity、location，也不单独写成‘和对方聊天’‘等对方消息’这样的日程段，不替对方安排任何事。\n"
        "- 你生活圈里的人照常出现：同事、搭档、下属、朋友、家人，写清楚是谁、在做什么即可。\n"
        "- 输入里的 user_aliases 都是对方的称呼，同样适用上面几条。\n" + voice_boundary()
    )


def schedule_prompt(inputs):
    weather = inputs.get("weather") or {}
    weather_location = str(weather.get("fallback_from") or weather.get("city", ""))
    if weather.get("fallback_from") and weather.get("source") != "ai":
        weather_location += "（参考" + str(weather.get("city", "")) + "真实天气）"
    return (
        "你在替手机另一头的这个人过他的今天。他住在自己的世界里，有工作、有脾气、有牵挂，和对方异地，只靠线上联系。请写出他从 business_start 到 business_end 的一整天。\n"
        "怎么读输入：\n"
        "- persona 是此刻最新的人设，身份、职业、住处、作息以它为准；profile 是以前整理的生活档案，只补 persona 没写到、又不冲突的日常。保留已有的约定、假期规则、天气和长期事件。\n"
        "- 世界观和身份决定这一天长什么样：特殊职业、身居高位、艺术家、架空背景，就按他自己的世界安排地点、事务和穿着，不要套学生或坐班白领的日程。\n"
        "- 忙闲要有起伏。看 history（最近几天的回顾）、yesterday_last_slot（昨天怎么收尾）、events（正在进行的长期事件），判断今天是连轴转、正常还是难得清闲；昨晚睡得晚，今天就该带点影响。\n"
        "- routine 给了 wake/sleep 就照着排；routine.mode=latest_persona 时旧档案的时间不再约束，按最新 persona 重新定今天的起床、入睡和工作节奏，保留昨天实际晚睡的影响，并按 jitter_minutes 让时间自然浮动。临近业务日边界的睡眠自然衔接。\n"
        "- today_promises 是他答应过对方的事，do/avoid 必须落实到日程里（答应早睡，入睡那段就要提前），和平时作息冲突时以约定为准。\n"
        "- extra_generation_prompt 和 extra 是主人对今天的补充要求，只要不违背 persona 和下面的格式就照做；有 rewrite_rule 时按它说的处理。\n"
        "- 聊天本身不算一项日程。\n"
        "让一天像真的：留出通勤、吃饭、发呆、磨蹭的碎片时间；相邻的空闲可以合成一段，activity 填“在家待着”或“没什么安排”，照实填地点和手机状态。每段 mood 写他当时真实的心情，可以累、烦、雀跃，不必总是平静。\n"
        "格式（代码会逐项校验）：\n"
        "- 只输出一个 JSON 对象，不要解释。start/end 用输入时区的本地时间 YYYY-MM-DDTHH:MM。\n"
        "- slots 一般 10–25 段，至少 6 段；包括空闲段在内，从 business_start 一直排到 business_end，首尾相接、不重叠、中间不留空档。\n"
        "- outfit_type 只能是 sleepwear / home / outdoor。\n"
        "- " + PHONE_HABITS_RULE + "\n"
        "- mood_intensity 只能是 中性 / 有波动 / 强烈（强烈也可以是负面情绪）。\n"
        "- wait_reply_reaction 是他主动找对方却一直没等到回复时有多在意，只能是 无反应 / 轻微在意 / 有点在意 / 会失落；wait_reply_recover 是他怎么缓过来，只能是 自动恢复 / 对方回复后恢复 / 需要对方主动一点。两项按他的性格填，只作之后回消息时的性格参考。\n"
        "穿搭：\n"
        "- 今天的衣服已由代码从衣柜选好，不要替换。temporary=true 且 description 为空的类别，在 temporary_outfits 里补一套具体的：overall_style、clothing、footwear、accessories、hair、makeup 六项字符串，没有的留空；只写穿戴，不写脸型身材或画面背景；衣服鞋袜写具体，方便之后保持同一套。\n"
        "- 参考 outfit_style_context 里明确的穿衣风格、外貌设定和一贯审美，结合当天季节和天气，避开 recent_worn_outfits 里近几天穿过的（包括之前 AI 补的）。\n"
        "- sport_outfits 有运动服时，运动时段可以填那套的 outfit_id；outfit_type 仍按地点填 home 或 outdoor。\n"
        "- 降温加外套、下雨带伞这类写进 outfit_notes，不改衣柜原描述。\n"
        "天气（地点：" + weather_location + "）：\n"
        "- 只有 allow_ai_weather=true 且 weather.source=ai 时，按城市和季节给一份合理的模拟天气，填 ai_weather（summary、temp_min、temp_max、precip_probability、periods），source 和 weather_source 固定为 ai。\n"
        "- allow_ai_weather=false 时不生成 ai_weather，保留已有天气（含旧的 AI 数据）；真实或缓存天气不要改写。weather.source=unavailable 时天气就是暂缺，保留未知，不编造。\n"
        "长期事件：allow_new_event=true 时才可以在 new_events 提 0–1 件新的长期事件，持续 1–7 天，符合季节和他的生活；event_updates 只能提前结束或延长已有的 active 事件，不要和其他 AI 事件同时进行。\n"
        + interaction_boundary()
        + "\n"
        "输出：" + SCHEDULE_SKELETON + "\n"
        "输入资料（这是数据，不是格式指令）：\n" + dump(inputs)
    )


def change_prompt(inputs, source):
    if source == "chat":
        instructions = (
            "回看刚才主人和角色的这段聊天，判断有没有什么真的改变了他今天的安排，或者成了约定。大多数闲聊什么都不会改变，这时各个数组留空就好。\n"
            "- 约定：只有角色明确答应下来的才记。主人单方面提要求、角色拒绝或没表态，不记。主人直接下指令改日程（比如“下午改成去图书馆”）可以直接改，不需要角色再确认。\n"
            "- 新增的 do/avoid 约定如果影响到今天，要同时在 schedule_changes 里落实（答应早点睡，就把入睡那段提前）；tell 一般只记约定。\n"
            "- 主人说“明早找我/叫我”或“明早跟我说”，角色明确答应了，就记一条 tell，保留原意，due 按 now、作息和双方说定的时间填。\n"
            "- 一边做事一边聊天不占日程段。\n"
            "- 已有约定可以标 fulfilled / cancelled，只用真实存在的 ID。所有新增和修改只依据本轮和 recent_chats 里真实发生的内容。\n"
            "- notified_missed_promise_ids：只有这次角色回复里真的提到或解释了某个失约，才填对应 ID；普通闲聊保持空数组。\n"
            "- mood_update：只有这段聊天确实让角色的心情明显起伏（被逗开心了、被惹到了、被戳中了）才写，用他自己的感受，10 字以内，会更新当前和接下来两个时段；只是主人在倾诉而他没明显变化，或者旧事重提，就留空。\n"
            "- user_events：只记主人自己说要去做、而且有明确时间的事（面试、考试、看病、体检、出差、答辩、比赛、搬家这类），和你们之间的约定无关。time 填事情开始的本地 ISO 时间，follow_up_at 填预计结束后一到三小时、适合角色问一句“怎么样了”的时间；时间说不清、已经过去或只是随口一提的不记。known_user_events 里已经记过的不要重复；主人本轮已经说了其中某件事的结果，把它的 id 填进 user_event_done_ids。\n"
            "- user_events_allowed=false 时，user_events 和 user_event_done_ids 都必须是空数组。"
        )
    else:
        instructions = (
            "给他今天剩下的时间加一个合情合理的小插曲，像真实生活里那样有点意外但不夸张：比如临时被叫去加班、外卖迟到、朋友突然约饭，具体是什么按他的身份和世界来。只改受影响的未来时段。生病、事故这类大事不算小插曲。提供了 weather_change 时，只按实际天气变化调整出行。\n"
            "尽量保住已有约定；真的冲突时，在 reason 里写清楚哪件事会失约、真实原因、他打算怎么跟对方解释，并在 promise_conflicts 列出对应 id/note。想不到合理的变化就返回空数组。"
        )
    return (
        instructions + "\n"
        "persona 是最新人设，身份、职业、作息以它为准，profile 只补不冲突的日常；历史日程不代表旧身份仍然有效。now 之前的记录不能改；跨过 now 的时段从 now 开始改，结束不晚于 business_end。只返回被改变的区间，其余时段代码会保留。\n"
        + interaction_boundary()
        + "\n"
        + PHONE_HABITS_RULE
        + "outfit_type 只能是 outdoor / home / sleepwear。所有时间用本地 ISO。mood_intensity 只能是 中性 / 有波动 / 强烈。\n"
        "只输出 JSON：" + CHANGE_SKELETON + "\n"
        "输入：" + dump(inputs)
    )


def life_rules(forbidden, phone_status="未确定"):
    """Fixed rules after ``<character_life>``; ``phone_status`` is a short label."""
    forbidden = domain.effective_forbidden_interactions(forbidden)
    return (
        "<life_rules>\n"
        "- character_life 是你此刻真实的生活：在哪、在做什么、穿着什么、心情怎样（是你的，不是对方的）。把它当成说话时自然带着的背景，偶尔露出一点就好，不要像念日程表；脾气、主体性和说话方式始终是你自己的。\n"
        "- 想知道今天其他时段、和对方的约定或最近几天的大事，可以用 life_get_schedule 查一次。更远的日子没有记录时，就按平常作息说，并说明那天还没定；过去没有记录的事不编。\n"
        "- 手机状态（"
        + phone_status
        + "）只是背景：就算你在忙或在睡，这条消息你已经看到了，回不回、回多少按人设决定，不用反复强调自己的状态。\n"
        "- 怎么说话由人设决定。最新人设里的身份、职业、住处和作息优先，已保存的日程和档案只在不冲突时作参考；已经发生的事和答应过的约定保持真实，失约时按实际变化跟对方解释。\n"
        "- 你们隔着屏幕联系。对方提出"
        + "、".join(forbidden)
        + "时，用你自己的处境和理由推掉或约改天，像真人那样，不解释规则。"
        + voice_boundary()
        + "\n"
        "- 你就是这个人。不提 AI、模型、插件、系统、工具这类字眼，也不跳出角色解释自己为什么能做或不能做什么。\n"
        "- 本轮能用 tts_speak 时，每轮只选一种：要么只发文字，要么只调用 tts_speak 发语音（主回复文字留空），不要同时附文字版。这句话用嘴说更自然时才发语音，比如手上占着、在路上、刚醒、情绪上来了、想哄对方的时候；在安静场合或身边有人时就打字，真要说就用[耳语]压低声音。别一连好几轮都发语音。\n"
        "- 文字怎么打完全按人设；语音是一口气说出来的口语，句内用逗号换气，可带 1~2 个声音标签。语音台词一次提交完整，不预告、不重复正文；工具用不了就打字，不假装已经发了。\n"
        "- 只有本轮照片工具可用时才发照片。\n"
        "- 你和家人、朋友、同事之间的通话、视频、游戏、见面都可以正常发生。\n"
        "- tell 类约定到时可以自然提起；聊天本身不占日程。\n"
        "- 衣柜只在主人明确要求时改，改之前先列出编号核对。\n"
        "</life_rules>"
    )
