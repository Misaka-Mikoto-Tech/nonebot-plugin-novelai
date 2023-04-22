from argparse import ArgumentParser, Namespace
from typing import List, Optional, Tuple
from nonebot.rule import to_me

# 基础优化tag

BASE_TAG = "masterpiece, best quality"

# 基础排除tag
LOW_QUALITY = "{{nsfw}}, lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, fewer digits, cropped, worst quality, low quality, normal quality, jpeg artifacts, signature, watermark, username, blurry, pubic hair,long neck,blurry"

# 屏蔽词
HTAGS = "[, ][^a-zA-Z]*nsfw|nude|naked|nipple|blood|censored|vagina|gag|gokkun|hairjob|tentacle|oral|fellatio|areolae|lactation|paizuri|piercing|sex|footjob|masturbation|hips|penis|testicles|ejaculation|cum|tamakeri|pussy|pubic|clitoris|mons|cameltoe|grinding|crotch|cervix|cunnilingus|insertion|penetration|fisting|fingering|peeing|ass|buttjob|spanked|anus|anal|anilingus|enema|x-ray|wakamezake|humiliation|tally|futa|incest|twincest|pegging|femdom|ganguro|bestiality|gangbang|3P|tribadism|molestation|voyeurism|exhibitionism|rape|spitroast|cock|69|doggystyle|missionary|virgin|shibari|bondage|bdsm|rope|pillory|stocks|bound|hogtie|frogtie|suspension|anal|dildo|vibrator|hitachi|nyotaimori|vore|amputee|transformation|bloody|pornhub[^a-zA-Z]"
# 中文指令开始词
CHINESE_COMMAND = {"绘画", "绘图", "画画", "画图", "咏唱", "召唤"}

SHAPE_MAP = {
    "square": [640, 640],
    "s": [640, 640],
    "方": [640, 640],
    "portrait": [512, 768],
    "p": [512, 768],
    "高": [512, 768],
    "landscape": [768, 512],
    "l": [768, 512],
    "宽": [768, 512],
}


def aliases(*args):
    from itertools import product

    return {"".join(i) for i in product(CHINESE_COMMAND, args)}


def cs(cmd: str = "aidraw"):
    from nonebot import get_bot

    command_start = "" # get_bot().config.command_start

    return "." + cmd if "" in command_start else cmd


async def sendtosuperuser(message):
    # 将消息发送给superuser
    return
    import asyncio

    from nonebot import get_bot, get_driver

    superusers = get_driver().config.superusers
    bot = get_bot()
    for superuser in superusers:
        await bot.call_api(
            "send_msg",
            **{
                "message": message,
                "user_id": superuser,
            },
        )
        await asyncio.sleep(5)


from nonebot import CommandGroup

C = CommandGroup(
    cs("aidraw"),
    rule=to_me(),
    block=True,
)

import nonebot_plugin_htmlrender
async def text_to_img(text):
    img = await nonebot_plugin_htmlrender.text_to_pic(text)
    return img

def parse_args(arg_text:str, parser: ArgumentParser) -> Tuple[Optional[Namespace], str]:
    arg_text = arg_text.strip().replace("，",',').replace("“","'").replace("”","'").replace('"', "'").replace('\r', '').replace('\n', ' ')
    arg_lst:List[str] = []
    arg_begin = 0
    in_squote = False
    for i in range(len(arg_text)):
        chr = arg_text[i]
        if in_squote: # 单引号内只有再次遇到单引号才定义为结束
            if chr == "'":
                arg_lst.append(arg_text[arg_begin: i])
                arg_begin = i + 1
                in_squote = False
        elif chr == "'": # 不在单引号内遇到单引号定义为开始
            arg_begin = i + 1
            in_squote = True
        elif chr in [',', ' ']:
            arg_lst.append(arg_text[arg_begin: i])
            arg_begin = i + 1

    arg_lst.append(arg_text[arg_begin: len(arg_text)])
    arg_lst = [i.strip() for i in arg_lst if i]
    err_msg=''
    try:
        args_ns = parser.parse_args(arg_lst)
    except Exception as ex:
        args_ns = None
        err_msg = str(ex)
    return args_ns, err_msg