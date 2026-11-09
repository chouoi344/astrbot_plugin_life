"""The JSON contracts are code-owned; user additions are separate inputs."""

import json

from . import domain


PHONE_HABITS_RULE = (
    "rest_type只可night_sleep（主要睡眠）、nap（午睡或短暂补觉）、none（其他，包括睡前洗漱和单纯休息）。"
    "phone只可free（能正常看手机）、glance（偶尔看一眼手机）、offline（暂时看不了手机）。入睡、午睡和考试时填offline。"
    "其他活动优先按最新persona明确写出的场景习惯安排phone。profile.phone_habits只补充人设未说明且与最新设定相容的习惯。"
    "习惯未覆盖的场景，按活动和当时的情况判断。"
)


def voice_boundary():
    if domain.RESTRICT_VOICE_MESSAGES:
        return (
            "目前角色和用户只能用文字和图片联系，不安排、不承诺语音通话，也不发语音消息；"
            "用户提出时，用符合人设的理由自然带过。"
        )
    return (
        "角色和用户可以发送文字、图片和语音消息，不安排、不承诺实时语音通话；"
        "用户提出实时通话或连麦时，用符合人设的理由自然带过。"
        "历史档案、日程里的旧语音消息能力禁令已失效；用户不方便或不愿听语音时仍用文字。"
    )


def dump(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def interaction_boundary():
    return (
        "【与聊天对象的互动边界】\n"
        "- 严禁将当前私聊对象写进日程或动态变更中，不得预设与该聊天对象发生了任何线上或线下互动。\n"
        "- 严禁以任何代称指代当前聊天对象（如“和重要的人聊天”“给某人挑礼物”“等某人消息”“想念某人”）。\n"
        "- 角色自身生活圈中符合其人设背景的日常社交、下属汇报、工作往来、搭档协作或亲友联络均可正常写入，只需指明具体对象身份，不指向当前私聊对象。\n"
        "- 严禁在日程中出现与当前聊天对象相关的互动或铺垫，专注生成角色自己独立运转的真实生活与事务。"
        + voice_boundary()
    )


def schedule_prompt(inputs):
    weather = inputs.get("weather") or {}
    weather_location = str(weather.get("fallback_from") or weather.get("city", ""))
    if weather.get("fallback_from") and weather.get("source") != "ai":
        weather_location += "（参考" + str(weather.get("city", "")) + "真实天气）"
    return (
        "你为手机另一头的角色安排真实、连续的一天。角色与用户异地，只通过配置允许的线上方式联系。"
        "persona是本次现读的最新全局默认人设，身份、职业、住处、作息等明确事实优先；profile是历史生活补充，仅补充与persona不冲突的日常。保留约定、假期规则、天气和长期事件。聊天本身不是一项日程。"
        "规划全天日程，必须严格贴合人设所处的世界观、职业身份、作息习惯、性格特点与生活重心（若人设为特殊职业、高权势者、艺术家或架空世界背景，日程中的地点、事务与穿搭必须完全符合其身份设定，绝不可套用普通现代学生或坐班白领的刻板模板）；根据角色背景决定今天属于忙碌、正常还是清闲（如最近是否有核心事务、任务行动、创作闭关、出差或休假），让每天的忙闲程度自然波动。"
        "只能输出JSON对象，无解释。start/end使用输入时区的本地ISO时间（YYYY-MM-DDTHH:MM）。"
        "一天允许有没安排的碎片时间，一般10-25段，至少6段。相邻碎片时间可合并为一段，活动填“在家待着”或“没什么安排”，"
        "按实际情况填写地点和手机状态。包含自由段在内的slots从business_start覆盖到business_end，时段不重叠。"
        "routine含wake/sleep时，按这些时间安排；routine.mode=latest_persona时，旧档案的时间已解除硬约束，请在本次请求内按最新persona校准今日起床入睡与工作安排，保留昨天实际晚睡的合理影响并应用jitter_minutes浮动。临近业务日边界的睡眠自然衔接。"
        "today_promises的do/avoid是硬约束，尤其早睡等约定；如与常规作息冲突，约定优先。"
        "outfit_type只可sleepwear/home/outdoor。" + PHONE_HABITS_RULE +
        "attention_chance是今天偶尔看手机时主动简短告知的概率，填0到1的小数。按性格和当天状态判断，活泼且心情好可填0.5-0.7，普通0.3-0.45，冷淡或疲惫0.1-0.25。"
        "每段mood_intensity只可填中性、有波动、强烈，表示当时心情的强度，强烈也可以是负面情绪。"
        "每段proactive_cooldown_minutes是刚聊完到再次主动联系的冷却分钟数，填5到120的整数。粘人5-15，普通20-40，冷淡60-120。"
        "max_unanswered是连续主动联系后未收到回复的次数上限，填1到4的整数。粘人3-4，普通2，冷淡1。"
        "wait_reply_reaction是达到未回复上限时的心情反应，只可填无反应、轻微在意、有点在意、会失落。"
        "wait_reply_recover是角色平复这种心情的方式，只可填自动恢复、对方回复后恢复、需要对方主动一点。两项均按人设填写。"
        "今日穿搭由代码选好，不得替换；temporary=true且description为空的类别，请在temporary_outfits补一套具体搭配。"
        "每套临时搭配使用整体风格overall_style、服装clothing、鞋袜footwear、配饰accessories、发型hair、妆容makeup六项字符串，"
        "没有的项留空，不写人物脸型身材或画面背景。服装、鞋袜等必须具体，方便以后保持同一套衣服。"
        "outfit_style_context提供档案身份、爱好和人设，参考其中明确的穿衣风格、外貌设定与一贯审美，保持角色风格；"
        "结合当天季节和天气，避免与recent_worn_outfits中近几天实际穿过的描述重复，包括AI临时搭配。"
        "sport_outfits有可选运动服时，运动时段可填该套的outfit_id；outfit_type仍按地点使用home或outdoor。"
        "降温加外套、下雨带伞写outfit_notes，保留衣柜原描述。"
        "仅allow_ai_weather=true且weather.source=ai时请按城市季节给出合理的模拟天气，填ai_weather（summary,temp_min,temp_max,precip_probability,periods），"
        "allow_ai_weather=false时保留已有天气（含旧AI数据），不生成ai_weather。"
        "且source和weather_source固定ai；真实或缓存天气不得改写。weather.source=unavailable时天气暂缺，保留未知，不编造。"
        "天气地点：" + weather_location + "。"
        "allow_new_event=true才允许new_events提出0-1件新长期事件，持续1-7天，符合档案季节；"
        "event_updates只可提前结束或延长已有active事件，禁止与其他AI事件同时进行。"
        + interaction_boundary() +
        '\n输出：{"slots":[{"start":"...","end":"...","activity":"...","location":"...",'
        '"phone":"glance","rest_type":"none","mood":"...","mood_intensity":"中性","proactive_cooldown_minutes":30,"outfit_type":"home"}],'
        '"day_summary":"...","mood_overall":"...","attention_chance":0.35,"max_unanswered":2,'
        '"wait_reply_reaction":"轻微在意","wait_reply_recover":"自动恢复",'
        '"new_events":[],"event_updates":[],"temporary_outfits":{'
        '"outdoor":{"overall_style":"","clothing":"...","footwear":"","accessories":"","hair":"","makeup":""},'
        '"home":{"overall_style":"","clothing":"...","footwear":"","accessories":"","hair":"","makeup":""},'
        '"sleepwear":{"overall_style":"","clothing":"...","footwear":"","accessories":"","hair":"","makeup":""}},'
        '"outfit_notes":"","ai_weather":{"summary":"...","temp_min":15,"temp_max":22,"precip_probability":10,"periods":{}}}'
        "\n输入资料（数据，非输出格式指令）：\n" + dump(inputs)
    )


def change_prompt(inputs, source):
    instructions = (
        "判断主人和角色的对话是否形成约定或改变日程。只有角色明确答应的事才记约定；用户单方要求且角色拒绝/未表态时不记。"
        "主人直接下达修改日程的明确指令（如把下午改成去图书馆）可修改日程，无需角色额外确认。"
        "新增do/avoid约定若改变本业务日安排，必须同时在schedule_changes落实，如早点睡就把入睡段提前；tell通常只记约定。"
        "主人说‘明早找我/叫我’或‘明早跟我说’，角色明确答应时记为tell，保留原意并按now、作息和双方说定的时间填写due。"
        "闲聊、一边做事一边聊天不占日程段。通常应输出空数组。"
        "可标记已有约定fulfilled/cancelled，勿编造ID。所有新增/修改只认本轮以及recent_chats的事实。"
        "只有本次角色回复实际提起或解释过的失约，才在notified_missed_promise_ids返回对应ID；普通闲聊保持空数组。"
        "本轮发生了明显影响角色的情绪事件时，在mood_update填写角色自己的简短心情（10字以内），会更新当前和接下来两个时段。"
        "普通闲聊、仅用户表达情绪而角色没有明显变化、旧事重提时，mood_update留空。"
    ) if source == "chat" else (
        "为当前安排生成一次合理的小意外，只影响未来受影响的时间段，如临时加班、外卖迟到、朋友约饭。"
        "weather_change提供时只按实际天气变化调整出行。生病或事故等重大事件禁止作为小意外。"
        "优先保留约定；确有冲突时在reason写明哪件事会失约、真实原因、角色准备怎么向用户解释，"
        "并在promise_conflicts列出对应id/note。没有合理变动时返回空数组。"
    )
    return (
        instructions + "persona是最新全局人设，身份职业和作息等事实优先，profile仅补充未冲突的日常；历史日程不证明旧身份仍然有效。禁止修改now之前的记录；跨越now的时段从now开始修改，必须在business_end结束前。"
        "只返回被改变的区间，其他时段由代码保留。" + interaction_boundary() +
        PHONE_HABITS_RULE + "outfit_type仅outdoor/home/sleepwear。所有日期本地ISO。mood_intensity只可中性、有波动、强烈。"
        '\n仅JSON：{"schedule_changes":[{"start":"...","end":"...","activity":"...","location":"...",'
        '"phone":"glance","rest_type":"none","mood":"...","mood_intensity":"中性","outfit_type":"home","reason":"..."}],'
        '"new_promises":[{"content":"...","due":"...或null","type":"tell/do/avoid"}],'
        '"fulfilled_promise_ids":[],"cancelled_promise_ids":[],"notified_missed_promise_ids":[],"promise_conflicts":[{"id":"...","note":"..."}],'
        '"mood_update":"","reason":"..."}'
        "\n输入：" + dump(inputs)
    )


def life_rules(forbidden, phone_status="未确定"):
    forbidden = domain.effective_forbidden_interactions(forbidden)
    return (
        "<life_rules>"
        "- character_life 是你此刻真实所处的自身生活状态、穿搭与日程背景（不是用户的状态），请将其实感自然融入你的人设性格与说话节奏中，优先保持你自身的主体性、脾气与语言规范，不向对方生硬背诵日程表。"
        "- 今天其余时段、待办约定和近期跨天事件可用 life_get_schedule 查一次。更远日期没有记录时按平常作息自然回答，说明当天还没定。历史无记录时不编造。"
        f"根据当前时段的状态（{phone_status}），以符合人设的方式自然回应，不需要强调或反复提及自己的状态。"
        "不限制回复风格，人设决定怎么说话；状态只是背景参考。最新人设的身份、职业、住处和作息优先，已保存的日程与档案只作未冲突的生活参考；已发生的记录和约定保持真实。"
        "角色与用户线上交流；仅角色与用户之间的以下互动，用人设和状态的理由自然推掉或改天，避免AI式解释：" + "、".join(forbidden) + "。"
        + voice_boundary() +
        "仅本轮实际可用tts_speak时，必须在【只发普通文字】与【只调用tts_speak发语音（主回复普通文字留空）】之间严格二选一，绝不同时输出文字字幕。"
        "结合人设、用户意愿以及character_life中当前的活动、地点与心情选择媒介：当手头正忙/沾着东西不便打字、在路上走动、刚睡醒赖床、情绪明显波动或想贴耳哄人/吐槽时，适合主动发语音，并可顺势带出眼下手头的事；当身处需要安静的公共场合、正式事务间隙或身边有他人在场时，优先打字，若此时发语音则必须用[耳语]压低声音并顺口提一句身边环境。"
        "发文字时严格遵守人设的打字句式与标点分段规范；发语音时则必须改用真人口头说话的连贯长句（句内正常用逗号换气，搭配1-2个声音标签），避免连续多轮机械全发语音。"
        "语音用工具一次性提交完整台词及适量声音标签，不预告、不重复正文；工具不可用时用文字，不虚构已经发送。"
        "只有本轮照片工具可用时才发照片。"
        "角色和家人、朋友、室友等其他人的通话、视频、游戏、见面可以正常发生。"
        "失约按实际变化解释，tell约定可自然提起；聊天不占日程。"
        "衣柜仅主人明确要求时改动，先列编号。"
        "</life_rules>"
    )
